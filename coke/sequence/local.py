"""无持久化的本地阶段执行器。

保留旧的函数式入口（``charge_oven`` / ``push_oven`` / …）供单元校验与联锁
验证使用：不做检查点落盘、不涉及恢复，只是把阶段表从头跑到尾，产出与原先
同口径的报告。平台侧带持久化的执行走 :mod:`coke.recovery.runner`。
"""

from __future__ import annotations

from typing import Any

from ..recovery.phase import PhaseContext, PhaseResult
from .registry import phase_table


def run_local(
    operation: str,
    oven: str,
    dt: float,
    params: dict[str, Any],
    machines: dict[str, Any],
) -> dict:
    """把某操作的阶段表从头执行一遍，返回带步骤与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []
    extras: dict[str, Any] = {}

    def on_step(step: dict, fragment: dict) -> None:
        steps.append(step)

    ctx = PhaseContext(oven=oven, dt=dt, params=params, machines=machines, on_step=on_step)

    table = phase_table(operation)
    guard = next((phase.guard for phase in table if phase.guard is not None), None)
    if guard is not None:
        violation = guard(ctx)
        if violation:
            return {"oven": oven, "ok": False, "steps": steps, "violations": [violation]}

    for phase in table:
        result: PhaseResult = phase.run(ctx)
        extras.update(result.extra)
        if result.violation:
            violations.append(result.violation)
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, **extras}

    return {"oven": oven, "ok": True, "steps": steps, "violations": violations, **extras}
