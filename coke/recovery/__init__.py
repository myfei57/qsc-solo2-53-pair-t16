"""中断恢复：按阶段判定续做或退回。

口径定死：恢复判定只按*阶段*做，不按步骤做。每个编排划分成有序阶段，阶段
策略在 :data:`coke.recovery.stages.STAGE_POLICY` 里写死为 ``continue``（续做）
或 ``rollback``（退回）；阶段内部的给煤量、杆位、走行距离、喷水量只是"进度
量"检查点，用来让续做幂等，不参与续做／退回判定。
"""

from __future__ import annotations

from .manager import RecoveryManager
from .progress import Progress, ProgressStore, ProgressTracker, fingerprint_config
from .stages import (
    BLOCKED,
    CONTINUE,
    NONE,
    REVERIFY,
    ROLLBACK,
    OPERATION_STAGES,
    STAGE_POLICY,
    policy_for,
    stage_index,
)

__all__ = [
    "RecoveryManager",
    "Progress",
    "ProgressStore",
    "ProgressTracker",
    "fingerprint_config",
    "CONTINUE",
    "ROLLBACK",
    "REVERIFY",
    "BLOCKED",
    "NONE",
    "OPERATION_STAGES",
    "STAGE_POLICY",
    "policy_for",
    "stage_index",
]
