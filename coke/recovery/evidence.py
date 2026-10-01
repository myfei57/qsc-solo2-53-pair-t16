"""恢复证据核对：进度文档与审计流水双源比对。

进度文档是平台自证，审计流水是只追加凭据。恢复前必须核对：

1. 操作开始事件（``<operation>_started``）与操作结束事件（``<operation>_complete``）
   各出现几次——开始必须存在，结束存在就意味着该操作其实已收尾，进度文档
   是残留，按"已完成"处理而不是续做；
2. 进度里每个已完成阶段登记的动作（``PhaseState.logged``）必须都能在该
   ``op_id`` 的审计流水里找到凭据。多出"自证做过、审计没有"的动作即证据
   不符，续做阻塞。
"""

from __future__ import annotations

from ..audit.record import AuditRecord
from .progress import OperationProgress
from .phase import STATUS_DONE


def collect_op_records(records: list[AuditRecord], op_id: str) -> list[AuditRecord]:
    """从回放记录里挑出属于某操作的全部流水（按时间升序）。"""

    return [
        record
        for record in records
        if str(record.payload.get("op_id", "")) == op_id
    ]


def audit_actions(op_records: list[AuditRecord]) -> list[str]:
    """返回该操作流水里出现过的全部动作名（按发生顺序，保留重复）。"""

    return [record.action for record in op_records]


def cross_check(progress: OperationProgress, records: list[AuditRecord]) -> dict:
    """双源核对，返回核对结果。

    ``complete_seen``：审计里已有本操作的结束事件；
    ``started_seen``：审计里有本操作的开始事件；
    ``missing_actions``：进度声称做过、但审计里查无凭据的动作；
    ``ok``：开始事件在、无缺失动作。结束事件的处理由调用方决定（按已完成
    收尾而不是续做）。
    """

    op_records = collect_op_records(records, progress.op_id)
    actions = audit_actions(op_records)
    action_set = set(actions)

    missing: list[str] = []
    for key, state in progress.phases.items():
        if state.status != STATUS_DONE:
            continue
        for action in state.logged:
            if action not in action_set:
                missing.append(f"{key}:{action}")

    return {
        "ok": bool(op_records) and not missing,
        "started_seen": f"{progress.operation}_started" in action_set,
        "complete_seen": f"{progress.operation}_complete" in action_set,
        "missing_actions": missing,
        "audit_records": len(op_records),
    }
