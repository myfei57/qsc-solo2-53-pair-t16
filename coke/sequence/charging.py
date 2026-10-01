"""装煤顺序的阶段表：煤斗对位 → 调压 → 桥管翻板 → 给煤 → 关翻板。

五个阶段全部为幂等收敛型（``resume``）：对位重算偏差、调压向目标带收敛、
开翻板前先按闩锁状态判断、给煤只补未给够的吨位、关翻板重复下发仍为关闭。
续做时已完成的阶段整个跳过，进行中的阶段从当前现场继续收敛，绝不重放。
"""

from __future__ import annotations

from ..charger.machine import ChargingCar
from ..gas.collector import CollectorMain
from ..gas.flap import BridgeFlap
from ..recovery.phase import POLICY_RESUME, Phase, PhaseContext, PhaseResult

MAX_STEPS = 600


def _flap_fragment(flap: BridgeFlap) -> dict:
    """翻板现场片段：角度、开度与闩锁计数一并入检查点。"""

    return {
        "angle_deg": flap.angle_deg,
        "open": flap.is_open(),
        "latch": {
            "engaged": flap.latch.is_engaged(),
            "cycles": flap.latch.cycles(),
            "resets": flap.latch.resets(),
        },
    }


def _align(ctx: PhaseContext) -> PhaseResult:
    charger: ChargingCar = ctx.machines["charger"]
    alignment = charger.align_hopper(
        ctx.oven, ctx.params["hopper_target_mm"], ctx.params["hopper_measured_mm"]
    )
    step = {
        "component": "charger",
        "action": "align_hopper",
        "detail": alignment.as_dict(),
    }
    ctx.step(step, {"charger": charger.snapshot().as_dict()})
    if not alignment.ok:
        return PhaseResult(violation="hopper_not_aligned")
    return PhaseResult()


def _regulate(ctx: PhaseContext) -> PhaseResult:
    collector: CollectorMain = ctx.machines["collector"]
    for _ in range(MAX_STEPS):
        if collector.settled():
            break
        collector.regulate(ctx.dt)
        ctx.tick({"collector": {"pressure_pa": round(collector.pressure_pa, 2)}})
    step = {
        "component": "gas",
        "action": "regulate_pressure",
        "detail": {
            "pressure_pa": round(collector.pressure_pa, 2),
            "target_pa": collector.limits.pressure_target_pa,
            "settled": collector.settled(),
        },
    }
    ctx.step(step, {"collector": {"pressure_pa": round(collector.pressure_pa, 2), "settled": True}})
    if not collector.settled():
        return PhaseResult(violation="pressure_not_settled")
    return PhaseResult()


def _open_flap(ctx: PhaseContext) -> PhaseResult:
    flap: BridgeFlap = ctx.machines["flap"]
    # 幂等关键：闩锁已置位或翻板已开都算本阶段已到位，只有未开时才开。
    if not flap.is_open():
        flap.reset_latch()
        angle = flap.open()
    else:
        angle = flap.angle_deg
    ctx.step({"component": "gas", "action": "open_flap", "detail": {"angle_deg": angle}},
             {"flap": _flap_fragment(flap)})
    return PhaseResult()


def _feed(ctx: PhaseContext) -> PhaseResult:
    charger: ChargingCar = ctx.machines["charger"]
    for _ in range(MAX_STEPS):
        if charger.feed_complete():
            break
        charger.feed(ctx.dt)
        ctx.tick({"charger": charger.snapshot().as_dict()})
    ctx.step(
        {
            "component": "charger",
            "action": "feed",
            "detail": {"fed_t": round(charger.snapshot().fed_t, 3), "aligned": charger.is_aligned()},
        },
        {"charger": charger.snapshot().as_dict()},
    )
    if not charger.feed_complete():
        return PhaseResult(violation="feed_incomplete")
    return PhaseResult(extra={"fed_t": charger.snapshot().fed_t})


def _close_flap(ctx: PhaseContext) -> PhaseResult:
    flap: BridgeFlap = ctx.machines["flap"]
    if flap.is_open():
        flap.close()
    ctx.step(
        {"component": "gas", "action": "close_flap", "detail": {"angle_deg": flap.angle_deg}},
        {"flap": _flap_fragment(flap)},
    )
    return PhaseResult()


PHASES: tuple[Phase, ...] = (
    Phase("align", POLICY_RESUME, _align, ("align_hopper",)),
    Phase("regulate", POLICY_RESUME, _regulate, ("regulate_pressure",)),
    Phase("open_flap", POLICY_RESUME, _open_flap, ("open_flap",)),
    Phase("feed", POLICY_RESUME, _feed, ("feed",)),
    Phase("close_flap", POLICY_RESUME, _close_flap, ("close_flap",)),
)


def charge_oven(
    charger: ChargingCar,
    collector: CollectorMain,
    flap: BridgeFlap,
    oven: str,
    hopper_target_mm: float,
    hopper_measured_mm: float,
    dt: float = 1.0,
) -> dict:
    """执行一炉装煤（无持久化的本地校验入口），返回带步骤与违约项的报告。"""

    from .local import run_local

    return run_local(
        "charge",
        oven,
        dt,
        {"hopper_target_mm": hopper_target_mm, "hopper_measured_mm": hopper_measured_mm},
        {"charger": charger, "collector": collector, "flap": flap},
    )
