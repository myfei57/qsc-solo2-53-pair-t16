"""续做决定：按阶段策略判定"接着做 / 退回去 / 等人工确认"。

判定规则定死如下，任何调用方拿到的都是同一个纯函数的结果：

* 粒度固定为阶段（``granularity = "phase"``），不接受按步骤选择；
* 审计里已有操作结束事件 → ``already_complete``，进度文档只是残留；
* 双源证据不符、或参数迁移后未重新核对通过 → ``blocked``，禁止自动续做；
* 否则取第一个未完成阶段，按其固定策略：``resume`` 续做、``rollback``
  先回退再重做、``manual`` 必须操作工确认（确认后按 ``resume`` 执行）。

决定自带确定性标识（``decision_id``），同一份决定对象既写进进度文档的
``last_decision``，也原样进审计流水，事后两边逐字段可核对一致。
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .fingerprint import fingerprint_config, verify_params
from .phase import (
    ACTION_BLOCKED,
    ACTION_COMPLETE,
    ACTION_MANUAL,
    ACTION_RESUME,
    ACTION_ROLLBACK,
    GRANULARITY,
    Phase,
)
from .progress import OperationProgress

DECISION_EVENT = "recovery_decided"
REVERIFY_EVENT = "recovery_reverified"
ACK_EVENT = "recovery_ack"


def reverify(config, progress: OperationProgress, now: float) -> dict:
    """参数迁移后的重新核对：指纹重算 + 入参按新极限重核。"""

    current_fp = fingerprint_config(config)
    problems = verify_params(config, progress.operation, progress.params)
    return {
        "ts": round(now, 3),
        "passed": not problems,
        "problems": problems,
        "fingerprint_before": progress.config_fingerprint,
        "fingerprint_after": current_fp,
    }


def decide(
    progress: OperationProgress,
    table: tuple[Phase, ...],
    evidence: dict,
    config_match: bool,
    now: float,
) -> dict:
    """按阶段表、双源核对与配置指纹比对结果生成续做决定。"""

    keys = tuple(phase.key for phase in table)
    current_key = progress.current_phase(keys)
    current = next((phase for phase in table if phase.key == current_key), None)

    reasons: list[str] = []
    if not evidence.get("started_seen"):
        reasons.append("start_event_missing")
    if evidence.get("missing_actions"):
        reasons.append("evidence_mismatch")

    migrated = progress.reverified is not None
    # 未迁移：指纹必须与启动时一致；已迁移：必须留有通过的重新核对记录。
    config_ok = (not migrated and config_match) or (migrated and bool(progress.reverified.get("passed")))
    if not config_ok:
        reasons.append("config_migrated")

    decision: dict[str, Any] = {
        "op_id": progress.op_id,
        "operation": progress.operation,
        "oven": progress.oven,
        "ts": round(now, 3),
        "granularity": GRANULARITY,
        "phase": current_key,
        "policy": current.policy if current is not None else None,
        "resume_from": current_key,
        "reasons": reasons,
        "config_match": config_ok,
        "migration_reverified": migrated,
        "evidence": {
            "ok": evidence.get("ok", False),
            "started_seen": evidence.get("started_seen", False),
            "complete_seen": evidence.get("complete_seen", False),
            "audit_records": evidence.get("audit_records", 0),
            "missing_actions": list(evidence.get("missing_actions", [])),
        },
        "phases": {key: progress.phase_status(key) for key in keys},
        "acks": list(progress.acks),
    }

    if evidence.get("complete_seen"):
        action = ACTION_COMPLETE
    elif reasons:
        action = ACTION_BLOCKED
    elif current is None:
        # 阶段全部完成但结束事件未落（恰好在收尾瞬间掉电）：只补收尾，不重放。
        action = ACTION_RESUME
        decision["resume_from"] = None
    elif current.policy == "rollback":
        action = ACTION_ROLLBACK
    elif current.policy == "manual":
        action = ACTION_RESUME if progress.is_acked(current.key) else ACTION_MANUAL
    else:
        action = ACTION_RESUME

    decision["action"] = action
    decision["decision_id"] = decision_id(decision)
    return decision


def decision_id(decision: dict) -> str:
    """对决定的判定性字段取稳定哈希，作为双端核对的同一标识。"""

    basis = {
        key: decision[key]
        for key in (
            "op_id",
            "action",
            "phase",
            "policy",
            "resume_from",
            "granularity",
            "reasons",
            "config_match",
            "migration_reverified",
            "phases",
            "acks",
        )
        if key in decision
    }
    basis["missing"] = decision.get("evidence", {}).get("missing_actions", [])
    payload = json.dumps(copy.deepcopy(basis), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"dec-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"
