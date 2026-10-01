"""编排进度与重启恢复。

恢复粒度在 :mod:`coke.recovery.phase` 定死为阶段（``granularity = "phase"``）：
阶段表声明每个阶段的固定策略（续做 / 回退 / 人工确认），运行器
:class:`~coke.recovery.runner.RecoveryRunner` 负责进度落盘、双源核对、配置
迁移核对与续做执行。
"""

from __future__ import annotations

from .decision import decide, decision_id, reverify
from .evidence import cross_check
from .fingerprint import fingerprint_config, verify_params
from .phase import (
    GRANULARITY,
    ACTION_BLOCKED,
    ACTION_COMPLETE,
    ACTION_MANUAL,
    ACTION_RESUME,
    ACTION_ROLLBACK,
    POLICY_MANUAL,
    POLICY_RESUME,
    POLICY_ROLLBACK,
    CrashInjection,
    Phase,
    PhaseContext,
    PhaseResult,
    RecoveryBlocked,
)
from .progress import PROGRESS_DOC, OperationProgress, PhaseState
from .runner import RecoveryRunner

__all__ = [
    "GRANULARITY",
    "ACTION_BLOCKED",
    "ACTION_COMPLETE",
    "ACTION_MANUAL",
    "ACTION_RESUME",
    "ACTION_ROLLBACK",
    "POLICY_MANUAL",
    "POLICY_RESUME",
    "POLICY_ROLLBACK",
    "CrashInjection",
    "Phase",
    "PhaseContext",
    "PhaseResult",
    "RecoveryBlocked",
    "PROGRESS_DOC",
    "OperationProgress",
    "PhaseState",
    "RecoveryRunner",
    "cross_check",
    "decide",
    "decision_id",
    "reverify",
    "fingerprint_config",
    "verify_params",
]
