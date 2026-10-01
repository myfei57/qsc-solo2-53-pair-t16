"""推焦顺序的阶段表：导焦栅对位 → 锁定 → 推焦 → 退杆 → 解锁。

五个阶段全部为幂等收敛型（``resume``）：对位重算偏差；已锁定的导焦栅不再
抬；推焦只向目标杆位收敛、绝不先退杆；退杆只向零位收敛；解锁重复下发仍是
解锁。"推焦杆停在半程"正是 ``extend`` 阶段半途，续做就是继续向前推到位。
"""

from __future__ import annotations

from ..guide.machine import GuideCar
from ..pusher.machine import PusherMachine
from ..recovery.phase import POLICY_RESUME, Phase, PhaseContext, PhaseResult

MAX_STEPS = 2400


def _align(ctx: PhaseContext) -> PhaseResult:
    guide: GuideCar = ctx.machines["guide"]
    alignment = guide.align(
        ctx.oven, ctx.params["guide_target_mm"], ctx.params["guide_measured_mm"]
    )
    ctx.step(
        {
            "component": "guide",
            "action": "align_apron",
            "detail": {**alignment.as_dict(), "tolerance_mm": guide.align_tolerance()},
        },
        {"guide": guide.snapshot().as_dict()},
    )
    if not alignment.ok:
        return PhaseResult(violation="guide_not_aligned")
    return PhaseResult()


def _lock(ctx: PhaseContext) -> PhaseResult:
    guide: GuideCar = ctx.machines["guide"]
    if not guide.is_locked():
        angle = guide.lock_apron()
    else:
        angle = guide.snapshot().apron_deg
    ctx.step(
        {
            "component": "guide",
            "action": "lock_apron",
            "detail": {"apron_deg": angle, "locked": guide.is_locked()},
        },
        {"guide": guide.snapshot().as_dict()},
    )
    return PhaseResult()


def _extend(ctx: PhaseContext) -> PhaseResult:
    pusher: PusherMachine = ctx.machines["pusher"]
    target = ctx.params["stroke_target_mm"]
    pusher.set_speed(ctx.params["speed_mm_s"])
    # 幂等：已在前进或已到位都不重发指令；ram 停在半程时 drive_to 目标仍
    # 大于当前杆位，续做就是继续向前推，绝不为"重新开始"而后退。
    if pusher.state == "idle" and abs(pusher.ram_mm - target) >= 1e-9:
        pusher.drive_to(target)
    for _ in range(MAX_STEPS):
        if pusher.at_target():
            break
        pusher.advance(ctx.dt)
        ctx.tick({"pusher": pusher.snapshot().as_dict()})
    ctx.step(
        {
            "component": "pusher",
            "action": "extend",
            "detail": {"ram_mm": round(pusher.ram_mm, 2), "target_mm": target},
        },
        {"pusher": pusher.snapshot().as_dict()},
    )
    if not pusher.at_target():
        return PhaseResult(violation="stroke_incomplete")
    return PhaseResult()


def _retract(ctx: PhaseContext) -> PhaseResult:
    pusher: PusherMachine = ctx.machines["pusher"]
    # 幂等：已在退杆中（半途续做）不重发；杆已回零（阶段实际已完成）不动作；
    # 只有停在前伸位置时才下达退杆令。
    if pusher.state != "retracting" and pusher.ram_mm > 1e-9:
        pusher.retract()
    for _ in range(MAX_STEPS):
        if pusher.at_target():
            break
        pusher.advance(ctx.dt)
        ctx.tick({"pusher": pusher.snapshot().as_dict()})
    ctx.step(
        {"component": "pusher", "action": "retract", "detail": {"ram_mm": round(pusher.ram_mm, 2)}},
        {"pusher": pusher.snapshot().as_dict()},
    )
    return PhaseResult()


def _unlock(ctx: PhaseContext) -> PhaseResult:
    guide: GuideCar = ctx.machines["guide"]
    if guide.is_locked():
        guide.unlock_apron()
    ctx.step(
        {
            "component": "guide",
            "action": "unlock_apron",
            "detail": {"apron_deg": guide.snapshot().apron_deg, "locked": guide.is_locked()},
        },
        {"guide": guide.snapshot().as_dict()},
    )
    return PhaseResult()


PHASES: tuple[Phase, ...] = (
    Phase("align", POLICY_RESUME, _align, ("align_apron",)),
    Phase("lock", POLICY_RESUME, _lock, ("lock_apron",)),
    Phase("extend", POLICY_RESUME, _extend, ("extend",)),
    Phase("retract", POLICY_RESUME, _retract, ("retract",)),
    Phase("unlock", POLICY_RESUME, _unlock, ("unlock_apron",)),
)


def push_oven(
    pusher: PusherMachine,
    guide: GuideCar,
    oven: str,
    guide_target_mm: float,
    guide_measured_mm: float,
    stroke_target_mm: float,
    speed_mm_s: float,
    dt: float = 1.0,
) -> dict:
    """执行一炉推焦（无持久化的本地校验入口），返回带步骤与违约项的报告。"""

    from .local import run_local

    return run_local(
        "push",
        oven,
        dt,
        {
            "guide_target_mm": guide_target_mm,
            "guide_measured_mm": guide_measured_mm,
            "stroke_target_mm": stroke_target_mm,
            "speed_mm_s": speed_mm_s,
        },
        {"pusher": pusher, "guide": guide},
    )
