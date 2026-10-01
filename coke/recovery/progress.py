"""编排进度文档。

进度是一份原子写入的 JSON 文档（``operation_progress``），与审计流水分开：
审计只追加、不可改写，是恢复核对的第三方凭据；进度文档记录"平台自己认为走到
哪了"，每完成一个离散步骤或循环心跳就原子覆盖一次。两者口径不一致即判定
证据不符，续做被阻塞，交人工核对——平台不允许用单一来源自证清白。

文档结构::

    {
      "op_id": "op-...", "operation": "push", "oven": "oven01",
      "started_ts": ..., "updated_ts": ..., "status": "running|failed",
      "params": {...操作入参...}, "dt": 1.0,
      "config_fingerprint": "sha256:...",
      "phases": {"align": {"status": "done", "logged": ["align_apron"]}, ...},
      "checkpoint": {"pusher": {...}, ...},
      "acks": ["switch"],                 # 操作工已确认续做的危险阶段
      "reverified": {...} | null,         # 参数迁移后的重新核对结果
      "last_decision": {...} | null       # 最近一次落审计的续做决定（同一份）
    }

判定粒度固定为阶段：``phases`` 里每个键就是阶段表的一条，恢复只看阶段状态，
不存在"步骤级续做"这个概念。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .phase import STATUS_DONE, STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING

PROGRESS_DOC = "operation_progress"
PHASE_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED)


@dataclass
class PhaseState:
    """单个阶段的落盘状态。

    ``logged`` 是本阶段已经发往审计流水的动作名（按发出顺序）。恢复核对时
    要求它是审计记录的子集：进度声称做过的，审计里必须真有凭据。
    """

    status: str = STATUS_PENDING
    logged: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "PhaseState":
        status = str(raw.get("status", STATUS_PENDING))
        if status not in PHASE_STATUSES:
            status = STATUS_PENDING
        logged_raw = raw.get("logged", [])
        logged = [str(item) for item in logged_raw] if isinstance(logged_raw, list) else []
        return cls(status=status, logged=logged)

    def as_dict(self) -> dict:
        return {"status": self.status, "logged": list(self.logged)}


@dataclass
class OperationProgress:
    """一个未收尾操作的编排进度。"""

    op_id: str
    operation: str
    oven: str
    params: dict[str, Any]
    dt: float
    config_fingerprint: str
    started_ts: float
    updated_ts: float
    status: str = STATUS_RUNNING
    phases: dict[str, PhaseState] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    violation: str | None = None
    acks: list[str] = field(default_factory=list)
    reverified: dict[str, Any] | None = None
    last_decision: dict[str, Any] | None = None

    def phase_status(self, key: str) -> str:
        """返回某阶段状态，未登记视为未开始。"""

        state = self.phases.get(key)
        return state.status if state is not None else STATUS_PENDING

    def ensure_phase(self, key: str) -> PhaseState:
        """取阶段状态，不存在则登记为未开始。"""

        state = self.phases.get(key)
        if state is None:
            state = PhaseState()
            self.phases[key] = state
        return state

    def set_phase(self, key: str, status: str) -> PhaseState:
        """覆盖某阶段状态。"""

        if status not in PHASE_STATUSES:
            raise ValueError(f"未知阶段状态: {status}")
        state = self.ensure_phase(key)
        state.status = status
        return state

    def log_action(self, phase_key: str, action: str) -> None:
        """登记一个已发往审计的阶段动作。"""

        state = self.ensure_phase(phase_key)
        if action not in state.logged:
            state.logged.append(action)

    def current_phase(self, order: tuple[str, ...]) -> str | None:
        """按阶段表顺序返回第一个未完成的阶段；全部完成返回 ``None``。"""

        for key in order:
            if self.phase_status(key) != STATUS_DONE:
                return key
        return None

    def is_finished(self, order: tuple[str, ...]) -> bool:
        """阶段表内是否所有阶段都已完成。"""

        return self.current_phase(order) is None

    def ack_phase(self, key: str) -> bool:
        """记录操作工对某危险阶段的续做确认，返回是否为新增确认。"""

        if key in self.acks:
            return False
        self.acks.append(key)
        return True

    def is_acked(self, key: str) -> bool:
        """该阶段是否已取得操作工确认。"""

        return key in self.acks

    def as_dict(self) -> dict:
        return {
            "op_id": self.op_id,
            "operation": self.operation,
            "oven": self.oven,
            "params": dict(self.params),
            "dt": self.dt,
            "config_fingerprint": self.config_fingerprint,
            "started_ts": round(self.started_ts, 3),
            "updated_ts": round(self.updated_ts, 3),
            "status": self.status,
            "phases": {key: state.as_dict() for key, state in self.phases.items()},
            "checkpoint": self.checkpoint,
            "violation": self.violation,
            "acks": list(self.acks),
            "reverified": dict(self.reverified) if self.reverified is not None else None,
            "last_decision": dict(self.last_decision) if self.last_decision is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "OperationProgress | None":
        """从落盘字段还原进度；结构不可识别时返回 ``None``，由调用方阻塞处理。"""

        if not isinstance(raw, dict):
            return None
        try:
            phases_raw = raw.get("phases", {})
            phases = (
                {str(key): PhaseState.from_dict(value) for key, value in phases_raw.items()}
                if isinstance(phases_raw, dict)
                else {}
            )
            checkpoint = raw.get("checkpoint", {})
            params = raw.get("params", {})
            acks = raw.get("acks", [])
            reverified = raw.get("reverified")
            last_decision = raw.get("last_decision")
            return cls(
                op_id=str(raw["op_id"]),
                operation=str(raw["operation"]),
                oven=str(raw.get("oven", "")),
                params=dict(params) if isinstance(params, dict) else {},
                dt=float(raw.get("dt", 1.0)),
                config_fingerprint=str(raw.get("config_fingerprint", "")),
                started_ts=float(raw.get("started_ts", 0.0)),
                updated_ts=float(raw.get("updated_ts", 0.0)),
                status=str(raw.get("status", STATUS_RUNNING)),
                phases=phases,
                checkpoint=dict(checkpoint) if isinstance(checkpoint, dict) else {},
                violation=raw.get("violation"),
                acks=[str(item) for item in acks] if isinstance(acks, list) else [],
                reverified=dict(reverified) if isinstance(reverified, dict) else None,
                last_decision=dict(last_decision) if isinstance(last_decision, dict) else None,
            )
        except (KeyError, TypeError, ValueError):
            return None
