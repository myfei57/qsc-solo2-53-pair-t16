"""炉门清扫顺序：松开闩锁 → 开炉门 → 清扫 → 复位闩锁。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..door.machine import DoorCleaner

if TYPE_CHECKING:
    from ..recovery.progress import ProgressTracker

MAX_STEPS = 600


def clean_door(
    door: DoorCleaner,
    oven: str,
    dt: float = 1.0,
    tracker: "ProgressTracker | None" = None,
) -> dict:
    """执行一炉炉门清扫，返回带步骤与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []

    if tracker is not None:
        tracker.enter("release", state={"door": {"engaged": True, "door_open": False, "elapsed_s": 0.0, "cleaned": False, "oven": oven}})
    door.latch.release()
    steps.append(
        {
            "component": "door",
            "action": "release_latch",
            "detail": {"engaged": door.latch.is_engaged(), "position_mm": door.latch.position_mm},
        }
    )

    if tracker is not None:
        tracker.enter("open", state={"door": {"engaged": False, "door_open": False, "elapsed_s": 0.0, "cleaned": False, "oven": oven}})
    door.open_door(oven)
    steps.append({"component": "door", "action": "open_door", "detail": {"oven": oven}})

    if tracker is not None:
        tracker.enter("clean", state={"door": {"engaged": False, "door_open": True, "elapsed_s": 0.0, "cleaned": False, "oven": oven}})
    for _ in range(MAX_STEPS):
        if door.progress() >= 1.0:
            break
        door.clean(dt)
        if tracker is not None:
            tracker.checkpoint({"door": {"engaged": False, "door_open": True, "elapsed_s": door.progress() * door.limits.clean_duration_s, "cleaned": False, "oven": oven}})
    steps.append({"component": "door", "action": "clean", "detail": {"progress": round(door.progress(), 3)}})
    if door.progress() < 1.0:
        violations.append("clean_incomplete")
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

    if tracker is not None:
        tracker.enter("mark", state={"door": {"engaged": False, "door_open": True, "elapsed_s": door.limits.clean_duration_s, "cleaned": False, "oven": oven}})
    cycles = door.mark_cleaned()
    steps.append({"component": "door", "action": "mark_cleaned", "detail": {"cycles": cycles}})

    if tracker is not None:
        tracker.enter("close", state={"door": {"engaged": False, "door_open": True, "elapsed_s": door.limits.clean_duration_s, "cleaned": True, "oven": oven}})
    door.close_door()
    steps.append(
        {
            "component": "door",
            "action": "close_door",
            "detail": {"latch_engaged": door.latch.is_engaged(), "cleaned": door.is_cleaned()},
        }
    )
    return {"oven": oven, "ok": True, "steps": steps, "violations": violations}
