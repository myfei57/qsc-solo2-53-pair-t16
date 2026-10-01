"""推焦顺序：导焦栅对位 → 锁定导焦栅 → 推焦 → 退杆。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..guide.machine import GuideCar
from ..pusher.machine import PusherMachine

if TYPE_CHECKING:
    from ..recovery.progress import ProgressTracker

MAX_STEPS = 2400


def push_oven(
    pusher: PusherMachine,
    guide: GuideCar,
    oven: str,
    guide_target_mm: float,
    guide_measured_mm: float,
    stroke_target_mm: float,
    speed_mm_s: float,
    dt: float = 1.0,
    tracker: "ProgressTracker | None" = None,
) -> dict:
    """执行一炉推焦，返回带步骤与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []

    guide.reset()
    if tracker is not None:
        tracker.enter("align", state={"guide": {"aligned": False, "locked": False, "measured_mm": guide_measured_mm}, "pusher": {"ram_mm": 0.0, "target_mm": 0.0, "state": "idle"}})
    alignment = guide.align(oven, guide_target_mm, guide_measured_mm)
    steps.append(
        {
            "component": "guide",
            "action": "align_apron",
            "detail": {**alignment.as_dict(), "tolerance_mm": guide.align_tolerance()},
        }
    )
    if not alignment.ok:
        violations.append("guide_not_aligned")
        if tracker is not None:
            tracker.finish()
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

    if tracker is not None:
        tracker.enter("lock", state={"guide": {"aligned": True, "locked": False, "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": 0.0, "state": pusher.state}})
    angle = guide.lock_apron()
    steps.append(
        {
            "component": "guide",
            "action": "lock_apron",
            "detail": {"apron_deg": angle, "locked": guide.is_locked()},
        }
    )

    pusher.set_speed(speed_mm_s)
    pusher.drive_to(stroke_target_mm)
    if tracker is not None:
        tracker.enter("extend", state={"guide": {"aligned": True, "locked": True, "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": stroke_target_mm, "state": "extending"}})
    for _ in range(MAX_STEPS):
        if pusher.at_target():
            break
        pusher.advance(dt)
        if tracker is not None:
            tracker.checkpoint({"guide": {"aligned": True, "locked": True, "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": stroke_target_mm, "state": pusher.state}})
    steps.append(
        {
            "component": "pusher",
            "action": "extend",
            "detail": {"ram_mm": round(pusher.ram_mm, 2), "target_mm": stroke_target_mm},
        }
    )
    if not pusher.at_target():
        violations.append("stroke_incomplete")
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

    pusher.retract()
    if tracker is not None:
        tracker.enter("retract", state={"guide": {"aligned": True, "locked": True, "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": 0.0, "state": "retracting"}})
    for _ in range(MAX_STEPS):
        if pusher.at_target():
            break
        pusher.advance(dt)
        if tracker is not None:
            tracker.checkpoint({"guide": {"aligned": True, "locked": True, "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": 0.0, "state": pusher.state}})
    steps.append({"component": "pusher", "action": "retract", "detail": {"ram_mm": round(pusher.ram_mm, 2)}})

    if tracker is not None:
        tracker.enter("unlock", state={"guide": {"aligned": True, "locked": True, "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": 0.0, "state": pusher.state}})
    guide.unlock_apron()
    steps.append(
        {
            "component": "guide",
            "action": "unlock_apron",
            "detail": {"apron_deg": guide.snapshot().apron_deg, "locked": guide.is_locked()},
        }
    )
    return {"oven": oven, "ok": True, "steps": steps, "violations": violations}
