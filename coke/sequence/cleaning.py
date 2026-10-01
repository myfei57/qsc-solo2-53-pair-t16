"""炉门清扫顺序的阶段表：松闩锁 → 开炉门 → 清扫 → 关炉门压紧。

清扫是全平台唯一一个 ``rollback`` 阶段：清扫头必须全程连续清扫，半途掉电
时已扫时长不可信（没人能证明中间没有卡头），恢复时先把清扫计时退回阶段
入口（炉门保持打开、闩锁保持松开，这本身是安全态），再完整重扫一遍。
松闩锁、开炉门、关炉门压紧都是幂等收敛型。
"""

from __future__ import annotations

from ..door.machine import DoorCleaner
from ..recovery.phase import POLICY_RESUME, POLICY_ROLLBACK, Phase, PhaseContext, PhaseResult

MAX_STEPS = 600


def _fragment(door: DoorCleaner) -> dict:
    """炉门现场片段：快照之外补上清扫计时与闩锁循环数。"""

    snap = door.snapshot().as_dict()
    snap["elapsed_s"] = round(door.elapsed_s(), 3)
    snap["latch_cycles"] = door.latch.cycles()
    return {"door": snap}


def _release(ctx: PhaseContext) -> PhaseResult:
    door: DoorCleaner = ctx.machines["door"]
    if door.latch.is_engaged():
        door.latch.release()
    ctx.step(
        {
            "component": "door",
            "action": "release_latch",
            "detail": {"engaged": door.latch.is_engaged(), "position_mm": door.latch.position_mm},
        },
        _fragment(door),
    )
    return PhaseResult()


def _open(ctx: PhaseContext) -> PhaseResult:
    door: DoorCleaner = ctx.machines["door"]
    if not door.door_open():
        door.open_door(ctx.oven)
    ctx.step(
        {"component": "door", "action": "open_door", "detail": {"oven": ctx.oven}},
        _fragment(door),
    )
    return PhaseResult()


def _rollback_clean(ctx: PhaseContext) -> None:
    """清扫半途退回入口：炉门保持打开、闩锁保持松开，只把清扫计时清零。"""

    door: DoorCleaner = ctx.machines["door"]
    door.open_door(ctx.oven)
    ctx.step(
        {
            "component": "door",
            "action": "rollback_clean",
            "detail": {"oven": ctx.oven, "door_open": door.door_open(), "progress": round(door.progress(), 3)},
        },
        _fragment(door),
    )


def _clean(ctx: PhaseContext) -> PhaseResult:
    door: DoorCleaner = ctx.machines["door"]
    # 幂等：本操作已标记过完成（重做到此）就不再重复计数。
    if not door.is_cleaned():
        for _ in range(MAX_STEPS):
            if door.progress() >= 1.0:
                break
            door.clean(ctx.dt)
            ctx.tick(_fragment(door))
        ctx.step(
            {"component": "door", "action": "clean", "detail": {"progress": round(door.progress(), 3)}},
            _fragment(door),
        )
        if door.progress() < 1.0:
            return PhaseResult(violation="clean_incomplete")
        cycles = door.mark_cleaned()
        ctx.params["clean_cycles"] = cycles
    ctx.step(
        {"component": "door", "action": "mark_cleaned", "detail": {"cycles": door.snapshot().cycles}},
        _fragment(door),
    )
    return PhaseResult(extra={"cycles": door.snapshot().cycles})


def _close(ctx: PhaseContext) -> PhaseResult:
    door: DoorCleaner = ctx.machines["door"]
    if door.door_open():
        door.close_door()
    ctx.step(
        {
            "component": "door",
            "action": "close_door",
            "detail": {"latch_engaged": door.latch.is_engaged(), "cleaned": door.is_cleaned()},
        },
        _fragment(door),
    )
    return PhaseResult()


PHASES: tuple[Phase, ...] = (
    Phase("release", POLICY_RESUME, _release, ("release_latch",)),
    Phase("open", POLICY_RESUME, _open, ("open_door",)),
    Phase("clean", POLICY_ROLLBACK, _clean, ("clean", "mark_cleaned"), rollback=_rollback_clean),
    Phase("close", POLICY_RESUME, _close, ("close_door",)),
)


def clean_door(door: DoorCleaner, oven: str, dt: float = 1.0) -> dict:
    """执行一炉炉门清扫（无持久化的本地校验入口），返回带步骤与违约项的报告。"""

    from .local import run_local

    return run_local("clean", oven, dt, {}, {"door": door})
