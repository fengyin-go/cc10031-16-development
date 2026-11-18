"""模板版本比对服务 — 输出模板版本快照、字段级差异清单与处理项交付。

解决的核心问题：输出模板更新后只把格式名写回内存记录，下游无法区分
字段是新增、删除还是类型变化。本服务提供：

1. 字段映射与空值口径解析（parse_field_mapping / parse_null_policy）
2. TemplateVersionStore: 模板版本快照（不可变，含字段映射 + 空值口径）
3. diff_field_mappings: 互斥三类字段差异（新增/删除/类型变化）
4. TemplateDiffStore: 差异清单存储（按来源批次可查）
5. ProcessingItemStore: 处理项（串联批次→模板版本→输出文件→回调回执，
   受控只读，重复导入只认第一条记录）

数据流:
    batch_tracker 校验完成 → 记录批次启动时模板版本
    → 输出回调新到达交付模板 → 快照为新版本
    → 两版比对 → 字段级差异清单（带来源批次）
    → 读取已校验采集记录 → 生成带两版模板标识的格式化输出文件
    → 处理项交付 → 按字段差异类别选择交付回执，持久化两版模板标识
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.store import store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 字段映射与空值口径解析
# ---------------------------------------------------------------------------

def parse_field_mapping(raw: Any) -> dict[str, str]:
    """解析模板的字段映射配置为 {字段名: 类型} 结构。

    支持格式：
    - dict: {"名称": "text", "金额": "numeric"}
    - JSON 字符串: '{"名称": "text"}' 或 '[{"name": "名称", "type": "text"}]'
    - 分隔的 name:type 对: "名称:text,金额:numeric"（中英文冒号/逗号/分号均可）
    - 纯字段名列表: "名称,金额,日期"（类型默认 text）
    - 单个字段名（兜底，类型默认 text）
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, (list, tuple)):
        result: dict[str, str] = {}
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name") or item.get("field")
                if name:
                    result[str(name)] = str(item.get("type", "text"))
            elif item is not None and str(item).strip():
                result[str(item).strip()] = "text"
        return result

    text = str(raw).strip()
    if not text:
        return {}

    # JSON 字符串
    if text.startswith(("{", "[")):
        try:
            return parse_field_mapping(json.loads(text))
        except (ValueError, TypeError):
            pass

    # 分隔符拆分（兼容中英文逗号/分号）
    normalized = (
        text.replace("；", ",").replace(";", ",").replace("，", ",")
    )
    result = {}
    for part in (p.strip() for p in normalized.split(",") if p.strip()):
        if ":" in part:
            name, _, ftype = part.partition(":")
        elif "：" in part:
            name, _, ftype = part.partition("：")
        else:
            name, ftype = part, "text"
        result[name.strip()] = ftype.strip() or "text"
    return result


def parse_null_policy(raw: Any) -> str:
    """解析空值口径（空值表示），统一为字符串口径标识，缺省为 NA。"""
    if raw is None or str(raw).strip() == "":
        return "NA"
    return str(raw).strip()


# ---------------------------------------------------------------------------
# 字段级差异比对（互斥分类）
# ---------------------------------------------------------------------------

def diff_field_mappings(
    old_mapping: dict[str, str],
    new_mapping: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """比对两版字段映射，产出互斥的三类字段级差异。

    每个字段只能归入 新增/删除/类型变化 中的一个类别：
    - 仅存在于新版 → added（新增）
    - 仅存在于旧版 → removed（删除）
    - 两版都在但类型不同 → type_changed（类型变化）
    """
    added = [
        {"field": name, "type": new_mapping[name]}
        for name in new_mapping if name not in old_mapping
    ]
    removed = [
        {"field": name, "type": old_mapping[name]}
        for name in old_mapping if name not in new_mapping
    ]
    type_changed = [
        {
            "field": name,
            "old_type": old_mapping[name],
            "new_type": new_mapping[name],
        }
        for name in old_mapping
        if name in new_mapping and old_mapping[name] != new_mapping[name]
    ]
    return {"added": added, "removed": removed, "type_changed": type_changed}


def classify_diff(diff: dict[str, list[dict[str, Any]]]) -> str:
    """按字段差异类别给出交付回执类别。

    - 含删除或类型变化 → "breaking"（变更交付，下游需要调整）
    - 仅新增字段 → "additive"（增量交付，下游兼容）
    - 无任何差异 → "standard"（标准交付）
    """
    if diff["removed"] or diff["type_changed"]:
        return "breaking"
    if diff["added"]:
        return "additive"
    return "standard"


# ---------------------------------------------------------------------------
# 模板版本快照存储
# ---------------------------------------------------------------------------

@dataclass
class TemplateVersion:
    """模板版本快照：某一时刻模板的字段映射与空值口径，不可变。"""
    version_id: str
    template_id: int
    template_name: str
    version_no: int
    field_mapping: dict[str, str]
    null_policy: str
    output_type: str
    source: str  # batch_start（批次启动）/ callback_delivery（回调新到达）/ manual
    created_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "template_id": self.template_id,
            "template_name": self.template_name,
            "version_no": self.version_no,
            "field_mapping": dict(self.field_mapping),
            "null_policy": self.null_policy,
            "output_type": self.output_type,
            "source": self.source,
            "created_at": self.created_at,
        }


class TemplateVersionStore:
    """模板版本快照存储：模板每次更新/新到达都形成一个新的不可变版本。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._versions: list[TemplateVersion] = []

    def snapshot(self, template: dict[str, Any], source: str) -> TemplateVersion:
        """对模板当前状态生成版本快照。

        内容与该模板最新版本一致时复用既有版本（版本按内容去重，
        模板真正更新时才产生新版本）。
        """
        with self._lock:
            template_id = int(template.get("id", 0))
            field_mapping = parse_field_mapping(template.get("字段映射"))
            null_policy = parse_null_policy(template.get("空值表示"))
            output_type = str(template.get("输出类型", "csv"))

            latest = None
            for v in reversed(self._versions):
                if v.template_id == template_id:
                    latest = v
                    break
            if latest is not None and \
                    latest.field_mapping == field_mapping and \
                    latest.null_policy == null_policy and \
                    latest.output_type == output_type:
                return latest

            self._seq += 1
            version_no = 1 + sum(
                1 for v in self._versions if v.template_id == template_id)
            version = TemplateVersion(
                version_id=f"TV-{self._seq:04d}",
                template_id=template_id,
                template_name=str(template.get("模板名称", "")),
                version_no=version_no,
                field_mapping=field_mapping,
                null_policy=null_policy,
                output_type=output_type,
                source=source,
            )
            self._versions.append(version)
            return version

    def get(self, version_id: str) -> TemplateVersion | None:
        with self._lock:
            for v in self._versions:
                if v.version_id == version_id:
                    return v
            return None

    def latest_for(self, template_id: int) -> TemplateVersion | None:
        with self._lock:
            for v in reversed(self._versions):
                if v.template_id == template_id:
                    return v
            return None

    def list_all(self, template_id: int | None = None) -> list[TemplateVersion]:
        with self._lock:
            if template_id is None:
                return list(self._versions)
            return [v for v in self._versions if v.template_id == template_id]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._versions)


# ---------------------------------------------------------------------------
# 差异清单存储
# ---------------------------------------------------------------------------

@dataclass
class TemplateDiff:
    """两版模板的字段级差异清单，带来源批次，可逐项核对。"""
    diff_id: str
    batch_ref: str
    base_version_id: str
    current_version_id: str
    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    type_changed: list[dict[str, Any]]
    null_policy_changed: bool
    base_null_policy: str
    current_null_policy: str
    receipt_kind: str
    created_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "diff_id": self.diff_id,
            "batch_ref": self.batch_ref,
            "base_version_id": self.base_version_id,
            "current_version_id": self.current_version_id,
            "added": list(self.added),
            "removed": list(self.removed),
            "type_changed": list(self.type_changed),
            "null_policy_changed": self.null_policy_changed,
            "base_null_policy": self.base_null_policy,
            "current_null_policy": self.current_null_policy,
            "receipt_kind": self.receipt_kind,
            "created_at": self.created_at,
        }


class TemplateDiffStore:
    """差异清单存储：按来源批次可检索，支撑批次工作台的差异核对。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._diffs: list[TemplateDiff] = []

    def add(self, batch_ref: str, base: TemplateVersion,
            current: TemplateVersion) -> TemplateDiff:
        """对两版模板做字段级比对并落库差异清单。"""
        diff = diff_field_mappings(base.field_mapping, current.field_mapping)
        with self._lock:
            self._seq += 1
            entry = TemplateDiff(
                diff_id=f"DIFF-{self._seq:04d}",
                batch_ref=batch_ref,
                base_version_id=base.version_id,
                current_version_id=current.version_id,
                added=diff["added"],
                removed=diff["removed"],
                type_changed=diff["type_changed"],
                null_policy_changed=base.null_policy != current.null_policy,
                base_null_policy=base.null_policy,
                current_null_policy=current.null_policy,
                receipt_kind=classify_diff(diff),
            )
            self._diffs.append(entry)
            return entry

    def find_for_pair(self, batch_ref: str, base_version_id: str,
                      current_version_id: str) -> TemplateDiff | None:
        """查找指定批次下同一版本对的既有差异清单（避免重复比对）。"""
        with self._lock:
            for d in self._diffs:
                if d.batch_ref == batch_ref and \
                        d.base_version_id == base_version_id and \
                        d.current_version_id == current_version_id:
                    return d
            return None

    def get(self, diff_id: str) -> TemplateDiff | None:
        with self._lock:
            for d in self._diffs:
                if d.diff_id == diff_id:
                    return d
            return None

    def list_by_batch(self, batch_ref: str) -> list[TemplateDiff]:
        with self._lock:
            return [d for d in self._diffs if d.batch_ref == batch_ref]

    def list_all(self) -> list[TemplateDiff]:
        with self._lock:
            return list(self._diffs)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._diffs)


# ---------------------------------------------------------------------------
# 处理项存储（受控只读：只提供创建/状态流转/查询，不提供修改与删除）
# ---------------------------------------------------------------------------

@dataclass
class ProcessingItem:
    """处理项：串联 批次→校验→模板版本→输出文件→回调回执 的处理链路。

    - 重复导入只认第一条记录：同一 batch_ref 只存在一个处理项
    - 受控只读：外部只能查看，状态流转只能经由服务方法
    """
    item_id: str
    batch_ref: str
    template_id: int
    batch_start_version_id: str
    status: str = "待生成"  # 待生成 → 已生成 → 已交付
    delivered_version_id: str | None = None
    diff_id: str | None = None
    output_file: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    created_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "batch_ref": self.batch_ref,
            "template_id": self.template_id,
            "batch_start_version_id": self.batch_start_version_id,
            "delivered_version_id": self.delivered_version_id,
            "diff_id": self.diff_id,
            "status": self.status,
            "output_file": self.output_file,
            "receipt": self.receipt,
            "created_at": self.created_at,
        }


class ProcessingItemStore:
    """处理项存储：受控只读，无更新/删除接口，状态流转只经服务方法。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._items: list[ProcessingItem] = []

    def create(self, batch_ref: str, template_id: int,
               batch_start_version_id: str) -> tuple[ProcessingItem, bool]:
        """创建处理项；同一 batch_ref 重复导入只认第一条记录。

        Returns:
            (处理项, 是否新建)
        """
        with self._lock:
            for item in self._items:
                if item.batch_ref == batch_ref:
                    return item, False
            self._seq += 1
            item = ProcessingItem(
                item_id=f"PROC-{self._seq:04d}",
                batch_ref=batch_ref,
                template_id=template_id,
                batch_start_version_id=batch_start_version_id,
            )
            self._items.append(item)
            return item, True

    def attach_output(self, item_id: str, diff_id: str,
                      output_file: dict[str, Any],
                      delivered_version_id: str) -> ProcessingItem | None:
        """登记格式化输出文件与差异清单引用，状态流转为「已生成」。"""
        with self._lock:
            for item in self._items:
                if item.item_id == item_id:
                    item.diff_id = diff_id
                    item.output_file = output_file
                    item.delivered_version_id = delivered_version_id
                    item.status = "已生成"
                    return item
            return None

    def attach_receipt(self, item_id: str,
                       receipt: dict[str, Any]) -> ProcessingItem | None:
        """登记交付回执，状态流转为「已交付」。"""
        with self._lock:
            for item in self._items:
                if item.item_id == item_id:
                    item.receipt = receipt
                    item.status = "已交付"
                    return item
            return None

    def refresh_diff_ref(self, item_id: str, diff_id: str) -> ProcessingItem | None:
        """刷新处理项的差异清单引用（仅未交付前允许，交付后冻结）。"""
        with self._lock:
            for item in self._items:
                if item.item_id == item_id:
                    if item.status != "已交付":
                        item.diff_id = diff_id
                    return item
            return None

    def get(self, item_id: str) -> ProcessingItem | None:
        with self._lock:
            for item in self._items:
                if item.item_id == item_id:
                    return item
            return None

    def list_all(self, batch_ref: str | None = None) -> list[ProcessingItem]:
        with self._lock:
            if batch_ref is None:
                return list(self._items)
            return [i for i in self._items if i.batch_ref == batch_ref]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._items)


# ---------------------------------------------------------------------------
# 模板版本比对服务（统筹链路）
# ---------------------------------------------------------------------------

class TemplateVersionService:
    """模板版本比对：快照 → 差异 → 输出文件 → 处理项 → 交付回执。"""

    SERVICE_NAME = "template_version"
    STRATEGIES = ("batch_start", "current")

    def __init__(self) -> None:
        self.versions = TemplateVersionStore()
        self.diffs = TemplateDiffStore()
        self.items = ProcessingItemStore()
        self._file_seq = 0
        self._receipt_seq = 0
        self._lock = threading.Lock()

    # ---- 链路主入口 ----

    def run_version_compare_flow(
        self,
        batch_ref: str,
        template_id: int,
        strategy: str = "batch_start",
        incoming_template: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """模板版本比对完整链路。

        1. 读取已校验采集记录（来源批次）
        2. 批次启动时模板版本（处理项已有则复用，否则快照当前模板）
        3. 新到达输出回调交付模板 → 快照为新版本
        4. 两版字段级比对 → 差异清单（带两版差异和来源批次）
        5. 按策略生成带两版模板标识的格式化输出文件
           （batch_start=按批次启动时模板；current=按当前模板重新生成）
        6. 处理项落库（重复导入只认第一条）并联动更新待办

        冲突约束：批次校验完成后模板发生更新时，两种策略都只能交付
        一个带模板版本的文件 —— 处理项已生成输出文件后不再重复生成。
        """
        if strategy not in self.STRATEGIES:
            return {"ok": False,
                    "error": f"未知策略 {strategy}，可选：{list(self.STRATEGIES)}"}

        # 1. 读取已校验采集记录
        records = [
            e for e in store.collect_records.list_by_batch(batch_ref)
            if e.validated
        ]
        if not records:
            return {"ok": False,
                    "error": f"批次 {batch_ref} 无已校验采集记录，请先执行采集校验链路"}

        # 2/3. 模板版本：批次启动版本 + 新到达版本
        current_template = self._load_template(template_id, incoming_template)
        if current_template is None:
            return {"ok": False, "error": f"输出模板 {template_id} 不存在"}

        # 处理项落库：重复导入只认第一条记录
        existing = self.items.list_all(batch_ref)
        if existing:
            item = existing[0]
            created = False
            base_version = self.versions.get(item.batch_start_version_id)
            if base_version is None:
                return {"ok": False,
                        "error": f"处理项 {item.item_id} 的批次启动版本缺失"}
        else:
            # 批次启动时模板版本快照
            base_version = self.versions.snapshot(current_template, "batch_start")
            item, created = self.items.create(
                batch_ref=batch_ref, template_id=template_id,
                batch_start_version_id=base_version.version_id)
            self._sync_pending(batch_ref, pending=True)

        # 新到达输出回调交付模板 → 快照为新版本（内容未变则复用既有版本）
        current_version = self.versions.snapshot(
            current_template, "callback_delivery")

        # 字段级差异清单：批次启动版本 vs 新到达版本。
        # 即使输出文件已生成，模板更新后再次进入也要留下两版差异，
        # 供批次工作台核对回执所引用的字段版本。同一版本对不重复比对。
        diff = self.diffs.find_for_pair(
            item.batch_ref, base_version.version_id, current_version.version_id)
        if diff is None:
            diff = self.diffs.add(item.batch_ref, base_version, current_version)
        # 处理项未交付前，差异引用跟随最新比对结果
        self.items.refresh_diff_ref(item.item_id, diff.diff_id)

        # 已生成输出文件的处理项：冲突下只交付一个带模板版本的文件
        if item.output_file is not None:
            return {
                "ok": True,
                "reused": True,
                "message": "处理项已生成输出文件，冲突下只交付一个带模板版本的文件",
                "item": item.as_dict(),
                "diff": diff.as_dict(),
            }

        # 5. 生成带两版模板标识的格式化输出文件
        gen_version = base_version if strategy == "batch_start" else current_version
        output_file = self._build_output_file(
            item=item, gen_version=gen_version, records=records,
            strategy=strategy)

        # 6. 处理项落库 + 联动更新待办
        self.items.attach_output(
            item.item_id, diff.diff_id, output_file, gen_version.version_id)

        return {
            "ok": True,
            "reused": False,
            "created_item": created,
            "item": item.as_dict(),
            "diff": diff.as_dict(),
            "base_version": base_version.as_dict(),
            "current_version": current_version.as_dict(),
        }

    # ---- 交付（回调接口） ----

    def deliver(self, item_id: str) -> dict[str, Any]:
        """交付处理项：回调接口直接使用处理项已记录的格式化输出文件。

        按处理项已记录的版本、按字段差异类别选择交付回执，
        并持久化两版模板标识。重复交付幂等，只认第一次回执。
        """
        item = self.items.get(item_id)
        if item is None:
            return {"ok": False, "error": f"处理项 {item_id} 不存在"}
        if item.receipt is not None:
            return {"ok": True, "reused": True, "item": item.as_dict(),
                    "receipt": item.receipt}
        if item.output_file is None or item.diff_id is None:
            return {"ok": False,
                    "error": f"处理项 {item_id} 尚未生成输出文件，请先执行比对链路"}

        diff = self.diffs.get(item.diff_id)
        if diff is None:
            return {"ok": False, "error": f"差异清单 {item.diff_id} 缺失"}

        with self._lock:
            self._receipt_seq += 1
            receipt_id = f"RCPT-{self._receipt_seq:04d}"

        downstream_action = {
            "standard": "无需调整",
            "additive": "下游兼容，可选适配新增字段",
            "breaking": "需要下游调整：存在删除或类型变化字段",
        }[diff.receipt_kind]

        # 回执持久化两版模板标识
        receipt = {
            "receipt_id": receipt_id,
            "item_id": item.item_id,
            "batch_ref": item.batch_ref,
            "receipt_kind": diff.receipt_kind,
            "downstream_action": downstream_action,
            "base_version_id": item.batch_start_version_id,
            "delivered_version_id": item.delivered_version_id,
            "diff_id": diff.diff_id,
            "file_id": item.output_file["file_id"],
            "record_count": item.output_file["record_count"],
            "delivered_at": _now(),
        }
        self.items.attach_receipt(item_id, receipt)

        # 登记到回调钩子，作为下游交付记录
        from app.services.callback_hook import callback_hook
        callback_hook.record_delivery(receipt)

        # 联动更新待办：交付完成后批次与模板不再待办
        self._sync_pending(item.batch_ref, pending=False)

        return {"ok": True, "reused": False, "item": item.as_dict(),
                "receipt": receipt}

    # ---- 一致性核对 ----

    def consistency_check(self, item_id: str) -> dict[str, Any]:
        """核对处理项业务记录与结果明细是否对得上。

        检查处理项 ↔ 差异清单 ↔ 输出文件 ↔ 回执之间的引用一致性。
        """
        item = self.items.get(item_id)
        if item is None:
            return {"ok": False, "error": f"处理项 {item_id} 不存在"}
        problems: list[str] = []

        diff = self.diffs.get(item.diff_id) if item.diff_id else None
        if item.diff_id and diff is None:
            problems.append(f"差异清单 {item.diff_id} 不存在")
        if diff and diff.batch_ref != item.batch_ref:
            problems.append("差异清单来源批次与处理项不一致")
        if diff and diff.base_version_id != item.batch_start_version_id:
            problems.append("差异清单基准版本与处理项批次启动版本不一致")

        base_version = self.versions.get(item.batch_start_version_id)
        if base_version is None:
            problems.append(f"批次启动版本 {item.batch_start_version_id} 不存在")
        if item.delivered_version_id and \
                self.versions.get(item.delivered_version_id) is None:
            problems.append(f"交付版本 {item.delivered_version_id} 不存在")

        file_ok = True
        if item.output_file is not None:
            f = item.output_file
            file_ok = (
                f.get("batch_ref") == item.batch_ref
                and f.get("base_version_id") == item.batch_start_version_id
                and f.get("delivered_version_id") == item.delivered_version_id
            )
            if not file_ok:
                problems.append("输出文件的两版模板标识与处理项不一致")

        if item.receipt is not None:
            r = item.receipt
            if r.get("base_version_id") != item.batch_start_version_id or \
                    r.get("delivered_version_id") != item.delivered_version_id:
                problems.append("回执的两版模板标识与处理项不一致")
            if item.output_file and r.get("file_id") != item.output_file.get("file_id"):
                problems.append("回执引用的输出文件与处理项不一致")

        return {
            "ok": not problems,
            "item_id": item_id,
            "status": item.status,
            "problems": problems,
        }

    # ---- 查询（受控只读） ----

    def list_versions(self, template_id: int | None = None) -> list[dict[str, Any]]:
        return [v.as_dict() for v in self.versions.list_all(template_id)]

    def list_diffs(self, batch_ref: str | None = None) -> list[dict[str, Any]]:
        if batch_ref:
            return [d.as_dict() for d in self.diffs.list_by_batch(batch_ref)]
        return [d.as_dict() for d in self.diffs.list_all()]

    def list_items(self, batch_ref: str | None = None) -> list[dict[str, Any]]:
        return [i.as_dict() for i in self.items.list_all(batch_ref)]

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        item = self.items.get(item_id)
        return item.as_dict() if item else None

    # ---- 内部方法 ----

    def _load_template(
        self,
        template_id: int,
        incoming_template: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """读取新到达的输出模板：优先使用回调交付的模板内容，
        否则读取 output_format 模块当前记录。"""
        if incoming_template is not None:
            tpl = dict(incoming_template)
            tpl.setdefault("id", template_id)
            return tpl
        return store.find("output_format", template_id)

    def _build_output_file(
        self,
        item: ProcessingItem,
        gen_version: TemplateVersion,
        records: list[Any],
        strategy: str,
    ) -> dict[str, Any]:
        """生成格式化输出文件：与两版模板标识一起生成，带来源批次。

        按生成版本的字段映射逐字段取值，空值按模板空值口径填充。
        """
        with self._lock:
            self._file_seq += 1
            file_id = f"OUT-{self._file_seq:04d}"

        formatted = []
        for entry in records:
            row: dict[str, Any] = {}
            for fname in gen_version.field_mapping:
                value = entry.fields.get(fname)
                if value is None or str(value).strip() == "":
                    row[fname] = gen_version.null_policy
                else:
                    row[fname] = value
            formatted.append(row)

        return {
            "file_id": file_id,
            "batch_ref": item.batch_ref,
            "base_version_id": item.batch_start_version_id,
            "delivered_version_id": gen_version.version_id,
            "strategy": strategy,
            "template_id": item.template_id,
            "output_type": gen_version.output_type,
            "null_policy": gen_version.null_policy,
            "record_count": len(formatted),
            "records": formatted,
            "created_at": _now(),
        }

    def _sync_pending(self, batch_ref: str, pending: bool) -> None:
        """联动更新待办：批次追踪中对应批次的待办标志随处理项状态流转。"""
        for row in store.rows("batch_tracker"):
            if row.get("批次编号") == batch_ref:
                row["pending"] = pending

    def domain_snapshot(self) -> dict[str, Any]:
        items = self.items.list_all()
        return {
            "module": self.SERVICE_NAME,
            "entity": "模板版本比对",
            "artifact": "字段级差异清单",
            "versions": self.versions.count,
            "diffs": self.diffs.count,
            "processing_items": len(items),
            "delivered": sum(1 for i in items if i.status == "已交付"),
        }


# 全局模板版本比对实例
template_version_service = TemplateVersionService()
