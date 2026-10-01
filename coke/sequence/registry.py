"""操作阶段表注册表。

五种操作各有一张写死顺序的阶段表；续做粒度在注册表层就锁死为阶段，任何
调用方都只能按 ``PHASE_TABLES`` 的顺序与策略恢复，不存在按步骤挑选续做的
入口。
"""

from __future__ import annotations

from . import charging, cleaning, pushing, quenching, reversal
from ..recovery.phase import Phase

PHASE_TABLES: dict[str, tuple[Phase, ...]] = {
    "charge": charging.PHASES,
    "push": pushing.PHASES,
    "quench": quenching.PHASES,
    "reverse": reversal.PHASES,
    "clean": cleaning.PHASES,
}


def phase_keys(operation: str) -> tuple[str, ...]:
    """返回某操作的阶段键顺序。"""

    return tuple(phase.key for phase in PHASE_TABLES[operation])


def phase_table(operation: str) -> tuple[Phase, ...]:
    """返回某操作的阶段表，未知操作抛 ``KeyError``。"""

    return PHASE_TABLES[operation]
