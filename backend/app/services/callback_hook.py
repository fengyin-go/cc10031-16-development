"""回调钩子服务 — 消费校验结果触发下游系统回调。

这是新建的跨模块消费服务（不存在于 spec 的独立模块），
是结果校验链路的末端消费者。

数据流:
    channel("rpa.validation_complete") ← result_verify
    → callback_hook 消费 → 模拟下游系统回调（Webhook, MQ, HTTP）
    → channel("rpa.callback_executed")
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from typing import Any

from app.store import store


class CallbackHookService:
    """回调钩子管理 — 消费校验结果触发下游通知。"""

    SERVICE_NAME = "callback_hook"

    def __init__(self) -> None:
        self._callbacks: list[dict[str, Any]] = []

    def consume_validation_and_callback(self) -> dict[str, Any]:
        """消费校验结果并触发下游回调。

        从 channel("rpa.validation_complete") 获取校验完成事件，
        根据校验结果决定回调策略（全量通知、仅异常、仅通过）。

        Returns:
            {"triggered": int, "successful": int, "details": [...]}
        """
        msgs = store.channel.consume(
            consumer=self.SERVICE_NAME,
            topic="rpa.validation_complete",
        )
        if not msgs:
            return {"triggered": 0, "successful": 0, "details": []}

        details = []
        triggered = 0
        successful = 0

        for msg in msgs:
            validation = msg.payload
            batch_ref = validation.get("batch_ref", "")
            passed = validation.get("passed", 0)
            failed = validation.get("failed", 0)

            # 决定回调策略
            if failed > 0:
                strategy = "alert_with_data"  # 有失败，通知异常
            else:
                strategy = "success_notify"  # 全量通过，发送成功通知

            if passed == 0 and failed == 0:
                strategy = "skip"  # 无数据，跳过

            if strategy == "skip":
                store.channel.confirm(msg.msg_id, self.SERVICE_NAME)
                continue

            # 模拟回调执行
            callback_id = str(uuid.uuid4())[:8]
            callback_result = self._execute_callback(
                callback_id, batch_ref, strategy, validation)

            triggered += 1
            if callback_result["success"]:
                successful += 1

            # 发布回调结果
            store.channel.publish(
                topic="rpa.callback_executed",
                source=self.SERVICE_NAME,
                payload={
                    "callback_id": callback_id,
                    "batch_ref": batch_ref,
                    "strategy": strategy,
                    "success": callback_result["success"],
                    "response_code": callback_result.get("response_code", 200),
                    "duration_ms": callback_result.get("duration_ms", 0),
                },
            )

            store.channel.confirm(msg.msg_id, self.SERVICE_NAME)
            details.append({
                "callback_id": callback_id,
                "batch_ref": batch_ref,
                "strategy": strategy,
                "success": callback_result["success"],
            })

        return {
            "triggered": triggered,
            "successful": successful,
            "failed": triggered - successful,
            "details": details,
        }

    def _execute_callback(
        self,
        callback_id: str,
        batch_ref: str,
        strategy: str,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        """模拟执行下游系统回调（HTTP Webhook / MQ 消息）。"""
        duration_ms = random.randint(20, 300)

        # 模拟各种回调场景
        endpoints = {
            "alert_with_data": "/api/webhooks/rpa/alert",
            "success_notify": "/api/webhooks/rpa/delivery",
        }
        endpoint = endpoints.get(strategy, "/api/webhooks/rpa/default")

        # 回调节奏控制：5% 失败率
        success = random.random() > 0.05

        response_code = 200 if success else random.choice([500, 503, 408])

        callback_record = {
            "callback_id": callback_id,
            "batch_ref": batch_ref,
            "strategy": strategy,
            "endpoint": endpoint,
            "passed": validation.get("passed", 0),
            "failed": validation.get("failed", 0),
            "success": success,
            "response_code": response_code,
            "duration_ms": duration_ms,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._callbacks.append(callback_record)

        return {
            "callback_id": callback_id,
            "success": success,
            "response_code": response_code,
            "duration_ms": duration_ms,
        }

    def record_delivery(self, receipt: dict[str, Any]) -> None:
        """登记模板版本交付回执（由 template_version 服务调用）。

        回执持久化两版模板标识，供下游按字段差异类别核对。
        """
        self._callbacks.append({
            "callback_id": receipt["receipt_id"],
            "batch_ref": receipt["batch_ref"],
            "strategy": f"template_delivery:{receipt['receipt_kind']}",
            "endpoint": "/api/webhooks/rpa/delivery",
            "success": True,
            "response_code": 200,
            "base_version_id": receipt["base_version_id"],
            "delivered_version_id": receipt["delivered_version_id"],
            "diff_id": receipt.get("diff_id"),
            "file_id": receipt.get("file_id"),
            "executed_at": receipt["delivered_at"],
        })

    def list_callbacks(self,
                        batch_ref: str | None = None) -> list[dict[str, Any]]:
        """列出回调记录。"""
        if batch_ref:
            return [c for c in self._callbacks if c["batch_ref"] == batch_ref]
        return list(self._callbacks)

    def get_callback_stats(self) -> dict[str, Any]:
        """获取回调统计。"""
        total = len(self._callbacks)
        successful = sum(1 for c in self._callbacks if c["success"])
        return {
            "total_callbacks": total,
            "successful": successful,
            "failed": total - successful,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        stats = self.get_callback_stats()
        return {
            "module": self.SERVICE_NAME,
            "entity": "回调钩子",
            "artifact": "下游回调记录",
            **stats,
            "callback_events": len(
                store.channel.snapshot(topic="rpa.callback_executed")),
        }


# 全局回调实例
callback_hook = CallbackHookService()