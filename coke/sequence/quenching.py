"""熄焦顺序的阶段表：接焦 → 走行 → 喷淋。

三个阶段全部为幂等收敛型（``resume``）：走行从当前已走距离继续向熄焦塔
收敛，喷淋阀已开则只补未喷够的时长。喷淋阶段包含"开阀"与"喷淋"两个审计
步骤——阶段是恢复单元，步骤只是阶段内的留痕。
"""

from __future__ import annotations

from ..quench.machine import QuenchCar
from ..recovery.phase import POLICY_RESUME, Phase, PhaseContext, PhaseResult

MAX_STEPS = 2400


def _fragment(quench: QuenchCar) -> dict:
    """熄焦现场片段：快照供展示，原始计时与水量供精确重建（快照值有舍入）。"""

    snap = quench.snapshot().as_dict()
    snap["spray_elapsed_s"] = round(quench.spray.elapsed_s(), 3)
    snap["spray_water_m3_raw"] = quench.spray.water_m3()
    return {"quench": snap}


def _receive(ctx: PhaseContext) -> PhaseResult:
    quench: QuenchCar = ctx.machines["quench"]
    quench.receive(ctx.oven)
    ctx.step(
        {"component": "quench", "action": "receive", "detail": {"oven": ctx.oven}},
        _fragment(quench),
    )
    return PhaseResult()


def _travel(ctx: PhaseContext) -> PhaseResult:
    quench: QuenchCar = ctx.machines["quench"]
    for _ in range(MAX_STEPS):
        if quench.at_tower():
            break
        quench.travel(ctx.dt)
        ctx.tick(_fragment(quench))
    ctx.step(
        {
            "component": "quench",
            "action": "travel",
            "detail": {"travelled_m": round(quench.snapshot().travelled_m, 2)},
        },
        _fragment(quench),
    )
    if not quench.at_tower():
        return PhaseResult(violation="travel_incomplete")
    return PhaseResult()


def _spray(ctx: PhaseContext) -> PhaseResult:
    quench: QuenchCar = ctx.machines["quench"]
    # 幂等：阀已开（半途喷淋）直接继续计时；已喷完则整个动作都是空操作。
    if not quench.spray.open and not quench.spray.complete():
        quench.start_spray()
        ctx.step(
            {
                "component": "quench",
                "action": "start_spray",
                "detail": {"duration_s": quench.spray.duration_s},
            },
            _fragment(quench),
        )
    for _ in range(MAX_STEPS):
        if quench.spray.complete():
            break
        quench.spray_step(ctx.dt)
        ctx.tick(_fragment(quench))
    ctx.step(
        {
            "component": "quench",
            "action": "spray",
            "detail": {
                "water_m3": round(quench.spray.water_m3(), 3),
                "complete": quench.spray.complete(),
            },
        },
        _fragment(quench),
    )
    if not quench.spray.complete():
        return PhaseResult(violation="spray_incomplete")
    return PhaseResult()


PHASES: tuple[Phase, ...] = (
    Phase("receive", POLICY_RESUME, _receive, ("receive",)),
    Phase("travel", POLICY_RESUME, _travel, ("travel",)),
    Phase("spray", POLICY_RESUME, _spray, ("start_spray", "spray")),
)


def quench_oven(quench: QuenchCar, oven: str, dt: float = 1.0) -> dict:
    """执行一炉熄焦（无持久化的本地校验入口），返回带步骤与违约项的报告。"""

    from .local import run_local

    return run_local("quench", oven, dt, {}, {"quench": quench})
