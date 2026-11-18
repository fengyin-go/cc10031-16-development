"""模板版本比对测试 — 覆盖字段级差异、输出文件、处理项与交付回执。

验收要点：
1. 字段映射与空值口径解析
2. 每个字段差异只能归入 新增/删除/类型变化 中的一个类别（互斥）
3. 输出文件与两版模板标识一起生成，带来源批次
4. 重复导入只认第一条记录
5. 冲突下两种策略都只能交付一个带模板版本的文件
6. 回调交付按字段差异类别选择回执，持久化两版模板标识
7. 业务记录和结果明细对得上（一致性核对）
8. 处理项受控只读，串联处理链路并联动更新待办
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.store import store
from app.services.template_version import (
    TemplateVersionService,
    classify_diff,
    diff_field_mappings,
    parse_field_mapping,
    parse_null_policy,
)


# ── 字段映射与空值口径解析 ──

class TestParseFieldMapping:
    def test_parse_dict(self) -> None:
        assert parse_field_mapping({"名称": "text", "金额": "numeric"}) == \
            {"名称": "text", "金额": "numeric"}

    def test_parse_json_object_string(self) -> None:
        raw = '{"名称": "text", "金额": "numeric"}'
        assert parse_field_mapping(raw) == {"名称": "text", "金额": "numeric"}

    def test_parse_json_array_string(self) -> None:
        raw = '[{"name": "名称", "type": "text"}, {"name": "日期", "type": "date"}]'
        assert parse_field_mapping(raw) == {"名称": "text", "日期": "date"}

    def test_parse_name_type_pairs(self) -> None:
        assert parse_field_mapping("名称:text,金额:numeric") == \
            {"名称": "text", "金额": "numeric"}

    def test_parse_chinese_separators(self) -> None:
        assert parse_field_mapping("名称：text，金额：numeric；日期：date") == \
            {"名称": "text", "金额": "numeric", "日期": "date"}

    def test_parse_plain_field_list_defaults_text(self) -> None:
        assert parse_field_mapping("名称,金额,日期") == \
            {"名称": "text", "金额": "text", "日期": "text"}

    def test_parse_fallback_single_field(self) -> None:
        assert parse_field_mapping("输出格式样例1") == {"输出格式样例1": "text"}

    def test_parse_empty(self) -> None:
        assert parse_field_mapping(None) == {}
        assert parse_field_mapping("") == {}

    def test_parse_null_policy(self) -> None:
        assert parse_null_policy("空字符串") == "空字符串"
        assert parse_null_policy("") == "NA"
        assert parse_null_policy(None) == "NA"


# ── 字段级差异互斥分类 ──

class TestDiffFieldMappings:
    def test_added_only(self) -> None:
        diff = diff_field_mappings({"a": "text"}, {"a": "text", "b": "numeric"})
        assert diff["added"] == [{"field": "b", "type": "numeric"}]
        assert diff["removed"] == []
        assert diff["type_changed"] == []

    def test_removed_only(self) -> None:
        diff = diff_field_mappings({"a": "text", "b": "numeric"}, {"a": "text"})
        assert diff["removed"] == [{"field": "b", "type": "numeric"}]
        assert diff["added"] == []
        assert diff["type_changed"] == []

    def test_type_changed_only(self) -> None:
        diff = diff_field_mappings({"a": "text"}, {"a": "numeric"})
        assert diff["type_changed"] == [
            {"field": "a", "old_type": "text", "new_type": "numeric"}]
        assert diff["added"] == []
        assert diff["removed"] == []

    def test_each_field_in_exactly_one_category(self) -> None:
        """每个字段差异只能归入新增、删除或类型变化中的一个类别。"""
        old = {"keep": "text", "chg": "text", "del": "date"}
        new = {"keep": "text", "chg": "numeric", "add": "text"}
        diff = diff_field_mappings(old, new)

        fields_by_category = (
            [f["field"] for f in diff["added"]]
            + [f["field"] for f in diff["removed"]]
            + [f["field"] for f in diff["type_changed"]]
        )
        # 无重复：任一字段只出现在一个类别
        assert len(fields_by_category) == len(set(fields_by_category))
        # 无遗漏且互斥
        assert set(fields_by_category) == {"add", "del", "chg"}
        assert "keep" not in fields_by_category

    def test_classify_diff(self) -> None:
        assert classify_diff(
            {"added": [], "removed": [], "type_changed": []}) == "standard"
        assert classify_diff(
            {"added": [{"field": "x"}], "removed": [],
             "type_changed": []}) == "additive"
        assert classify_diff(
            {"added": [{"field": "x"}], "removed": [{"field": "y"}],
             "type_changed": []}) == "breaking"
        assert classify_diff(
            {"added": [], "removed": [],
             "type_changed": [{"field": "z"}]}) == "breaking"


# ── 链路级测试：快照 → 差异 → 输出文件 → 处理项 → 交付 ──

@pytest.fixture()
def svc() -> TemplateVersionService:
    """每个用例独立的比对服务实例，避免串数据。"""
    return TemplateVersionService()


def _seed_validated_records(batch_ref: str) -> None:
    """构造已校验采集记录（模拟采集校验链路产出）。"""
    rec = store.collect_records.add(
        batch_ref=batch_ref, source="data_extract",
        fields={"名称": "产品A", "金额": "100.5", "日期": "2026-11-01"})
    store.collect_records.mark_validated(rec.entry_id, 1.0)
    rec2 = store.collect_records.add(
        batch_ref=batch_ref, source="data_extract",
        fields={"名称": "产品B", "金额": "", "日期": "2026-11-02"})
    store.collect_records.mark_validated(rec2.entry_id, 0.9)
    # 未校验记录不应被读取
    store.collect_records.add(
        batch_ref=batch_ref, source="data_extract",
        fields={"名称": "未校验", "金额": "1", "日期": "2026-11-03"})


OLD_TEMPLATE = {
    "id": 901, "模板名称": "订单输出模板",
    "输出类型": "csv",
    "字段映射": "名称:text,金额:numeric,日期:date",
    "空值表示": "NA",
}
NEW_TEMPLATE = {
    "id": 901, "模板名称": "订单输出模板",
    "输出类型": "csv",
    "字段映射": "名称:text,金额:text,备注:text",  # 金额改型、日期删除、备注新增
    "空值表示": "NULL",
}


class TestVersionCompareFlow:
    def test_two_version_diff_after_template_update(self, svc) -> None:
        """模板更新后再次进入：生成两版差异清单，文件仍只交付一个。"""
        batch = "BATC-T-003"
        _seed_validated_records(batch)

        # 批次启动时模板为旧版 → 生成处理项与输出文件
        r1 = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901,
            incoming_template=OLD_TEMPLATE, strategy="batch_start")
        assert r1["ok"] is True
        assert r1["diff"]["added"] == []
        assert r1["diff"]["removed"] == []
        assert r1["diff"]["type_changed"] == []
        file_id = r1["item"]["output_file"]["file_id"]

        # 模板更新（新到达回调交付模板）→ 再次进入批次工作台
        r2 = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901,
            incoming_template=NEW_TEMPLATE, strategy="current")
        assert r2["ok"] is True
        assert r2["reused"] is True  # 冲突下只交付一个带模板版本的文件
        assert r2["item"]["output_file"]["file_id"] == file_id

        # 但两版差异清单已生成，可逐项核对
        diff = r2["diff"]
        assert diff["batch_ref"] == batch
        assert diff["base_version_id"] == r1["item"]["batch_start_version_id"]
        assert diff["current_version_id"] != diff["base_version_id"]
        assert [f["field"] for f in diff["added"]] == ["备注"]
        assert [f["field"] for f in diff["removed"]] == ["日期"]
        assert [f["field"] for f in diff["type_changed"]] == ["金额"]
        assert diff["type_changed"][0]["old_type"] == "numeric"
        assert diff["type_changed"][0]["new_type"] == "text"
        # 空值口径变化
        assert diff["null_policy_changed"] is True
        assert diff["base_null_policy"] == "NA"
        assert diff["current_null_policy"] == "NULL"
        assert diff["receipt_kind"] == "breaking"

        # 按来源批次可打开差异清单
        batch_diffs = svc.list_diffs(batch)
        assert len(batch_diffs) == 2  # 启动时自比 + 更新后两版比对

    def test_output_file_generated_with_two_version_ids(self, svc) -> None:
        """输出文件与两版模板标识一起生成，带来源批次。"""
        batch = "BATC-T-004"
        _seed_validated_records(batch)
        result = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901,
            incoming_template=OLD_TEMPLATE, strategy="batch_start")
        assert result["ok"] is True
        out = result["item"]["output_file"]
        assert out["batch_ref"] == batch
        assert out["base_version_id"] == result["item"]["batch_start_version_id"]
        assert out["delivered_version_id"] is not None
        assert out["strategy"] == "batch_start"
        # 只读取已校验采集记录（2 条已校验，未校验的不计入）
        assert out["record_count"] == 2
        # 空值口径：金额为空 → 按模板口径 NA 填充
        assert out["records"][1]["金额"] == "NA"
        # 映射外字段不进入输出文件
        assert set(out["records"][0].keys()) == {"名称", "金额", "日期"}

    def test_current_strategy_regenerates_with_new_template(self, svc) -> None:
        """按当前模板重新生成：交付版本为新到达模板版本。"""
        batch = "BATC-T-004B"
        _seed_validated_records(batch)
        result = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901,
            incoming_template=NEW_TEMPLATE, strategy="current")
        assert result["ok"] is True
        out = result["item"]["output_file"]
        assert out["strategy"] == "current"
        assert out["null_policy"] == "NULL"
        assert set(out["records"][0].keys()) == {"名称", "金额", "备注"}

    def test_missing_validated_records_rejected(self, svc) -> None:
        result = svc.run_version_compare_flow(
            batch_ref="BATC-NONE", template_id=901,
            incoming_template=OLD_TEMPLATE)
        assert result["ok"] is False
        assert "已校验" in result["error"]

    def test_unknown_strategy_rejected(self, svc) -> None:
        batch = "BATC-T-005"
        _seed_validated_records(batch)
        result = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901,
            incoming_template=OLD_TEMPLATE, strategy="bogus")
        assert result["ok"] is False

    def test_duplicate_import_keeps_first(self, svc) -> None:
        """重复导入只认第一条记录：同一批次重复进入复用同一处理项。"""
        batch = "BATC-T-006"
        _seed_validated_records(batch)
        r1 = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901, incoming_template=OLD_TEMPLATE)
        r2 = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901, incoming_template=NEW_TEMPLATE)
        assert r1["item"]["item_id"] == r2["item"]["item_id"]
        assert r2["reused"] is True
        assert len(svc.items.list_all(batch)) == 1

    def test_conflict_delivers_single_versioned_file(self, svc) -> None:
        """批次校验完成后模板更新：两种策略都只能交付一个带模板版本的文件。"""
        batch = "BATC-T-007"
        _seed_validated_records(batch)
        # 按批次启动时模板生成
        r1 = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901,
            incoming_template=OLD_TEMPLATE, strategy="batch_start")
        assert r1["ok"] is True
        file_id = r1["item"]["output_file"]["file_id"]

        # 模板更新后按当前模板重新生成 → 已生成文件，只能复用
        r2 = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901,
            incoming_template=NEW_TEMPLATE, strategy="current")
        assert r2["ok"] is True
        assert r2["reused"] is True
        assert r2["item"]["output_file"]["file_id"] == file_id
        # 仍然只有一个输出文件、一个交付版本
        assert r2["item"]["output_file"]["delivered_version_id"] == \
            r1["item"]["output_file"]["delivered_version_id"]


class TestDeliver:
    def _prepare_item(self, svc, batch: str,
                      old=OLD_TEMPLATE, new=NEW_TEMPLATE) -> str:
        """准备处理项：批次启动（旧模板）→ 模板更新后再次进入（新模板）。"""
        _seed_validated_records(batch)
        r1 = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901, incoming_template=old)
        item_id = r1["item"]["item_id"]
        # 模板更新后再次进入批次工作台 → 生成两版差异清单
        svc.run_version_compare_flow(
            batch_ref=batch, template_id=901, incoming_template=new)
        return item_id

    def test_deliver_selects_receipt_by_diff_category(self, svc) -> None:
        """按字段差异类别选择交付回执，并持久化两版模板标识。"""
        item_id = self._prepare_item(svc, "BATC-D-001")
        result = svc.deliver(item_id)
        assert result["ok"] is True
        receipt = result["receipt"]
        # 含删除+改型 → breaking 回执
        assert receipt["receipt_kind"] == "breaking"
        assert "需要下游调整" in receipt["downstream_action"]
        # 回执持久化两版模板标识
        item = svc.get_item(item_id)
        assert receipt["base_version_id"] == item["batch_start_version_id"]
        assert receipt["delivered_version_id"] == item["delivered_version_id"]
        assert receipt["file_id"] == item["output_file"]["file_id"]
        assert item["status"] == "已交付"

    def test_deliver_additive_receipt(self, svc) -> None:
        """仅新增字段 → additive 回执，下游兼容。"""
        new_only_added = dict(OLD_TEMPLATE)
        new_only_added["字段映射"] = "名称:text,金额:numeric,日期:date,备注:text"
        item_id = self._prepare_item(svc, "BATC-D-002", new=new_only_added)
        result = svc.deliver(item_id)
        assert result["receipt"]["receipt_kind"] == "additive"

    def test_deliver_standard_receipt(self, svc) -> None:
        """无差异 → standard 回执。"""
        item_id = self._prepare_item(svc, "BATC-D-003", new=OLD_TEMPLATE)
        result = svc.deliver(item_id)
        assert result["receipt"]["receipt_kind"] == "standard"

    def test_deliver_idempotent(self, svc) -> None:
        """重复交付幂等：只认第一次回执。"""
        item_id = self._prepare_item(svc, "BATC-D-004")
        r1 = svc.deliver(item_id)
        r2 = svc.deliver(item_id)
        assert r2["reused"] is True
        assert r1["receipt"]["receipt_id"] == r2["receipt"]["receipt_id"]

    def test_deliver_without_output_file_rejected(self, svc) -> None:
        item, _ = svc.items.create(
            batch_ref="BATC-D-EMPTY", template_id=901,
            batch_start_version_id="TV-XXXX")
        result = svc.deliver(item.item_id)
        assert result["ok"] is False

    def test_deliver_updates_todo(self, svc) -> None:
        """串联处理链路并联动更新待办：交付后批次待办关闭。"""
        batch = "BATC-D-005"
        store.rows("batch_tracker").append({
            "id": 99001, "批次编号": batch, "触发流程": "测试流程",
            "采集时间": "2026-11-18", "目标条数": "10", "成功条数": "10",
            "遗漏条数": "0", "校验状态": "已完成",
            "status": "已完成", "pending": True, "abnormal": False,
        })
        item_id = self._prepare_item(svc, batch)
        row = next(r for r in store.rows("batch_tracker")
                   if r.get("批次编号") == batch)
        assert row["pending"] is True  # 处理中 → 待办
        svc.deliver(item_id)
        assert row["pending"] is False  # 交付后 → 待办关闭


class TestConsistency:
    def test_records_match_details(self, svc) -> None:
        """业务记录和结果明细对得上：处理项 ↔ 差异 ↔ 文件 ↔ 回执。"""
        batch = "BATC-C-001"
        _seed_validated_records(batch)
        r1 = svc.run_version_compare_flow(
            batch_ref=batch, template_id=901, incoming_template=OLD_TEMPLATE)
        item_id = r1["item"]["item_id"]
        svc.run_version_compare_flow(
            batch_ref=batch, template_id=901, incoming_template=NEW_TEMPLATE)
        svc.deliver(item_id)

        check = svc.consistency_check(item_id)
        assert check["ok"] is True
        assert check["problems"] == []
        assert check["status"] == "已交付"

    def test_processing_item_store_is_read_only_controlled(self, svc) -> None:
        """受控只读：处理项存储不暴露更新/删除接口。"""
        assert not hasattr(svc.items, "update")
        assert not hasattr(svc.items, "delete")
        assert not hasattr(svc.items, "remove")

    def test_domain_snapshot(self, svc) -> None:
        batch = "BATC-C-003"
        _seed_validated_records(batch)
        svc.run_version_compare_flow(
            batch_ref=batch, template_id=901, incoming_template=OLD_TEMPLATE)
        snap = svc.domain_snapshot()
        assert snap["module"] == "template_version"
        assert snap["versions"] >= 1
        assert snap["processing_items"] >= 1
