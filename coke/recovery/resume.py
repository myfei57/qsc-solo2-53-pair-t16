"""幂等续做与退回执行器。

续做铁律：已经做完的阶段绝不重做。执行器先按落盘进度量恢复设备内存态，再对
每个*已完成*阶段做读回核对（核对通过只记一条 ``verify_*`` 审计步骤，不产生
物理动作），到达中断阶段后才从该阶段的实际进度量继续。给煤量、杆位、走行
距离、喷淋时长、清扫时长全部按进度量补齐，不会把已做过的物理量再做一遍。

退回只对 :data:`coke.recovery.stages.STAGE_POLICY` 里标成 ``rollback`` 的前置
阶段开放，由 :class:`coke.recovery.manager.RecoveryManager` 决定后调用。
"""

from __future__ import annotations

from ..charger.machine import ChargingCar
from ..door.machine import DoorCleaner
from ..gas.collector import CollectorMain
from ..gas.flap import BridgeFlap
from ..guide.machine import GuideCar
from ..heating.machine import HeatingReversal
from ..heating.temperature import OvenTemperature
from ..heating.valvetrain import ValveTrain
from ..pusher.machine import PusherMachine
from ..quench.machine import QuenchCar
from .progress import Progress
from .stages import stage_index


def _completed(progress: Progress) -> list[str]:
    """中断前已进入的阶段（最后一条是中断阶段本身，不算完成）。"""

    entered = [record["stage"] for record in progress.stages]
    return entered[:-1]


def _verify(steps: list[dict], component: str, action: str, detail: dict) -> None:
    steps.append({"component": component, "action": f"verify_{action}", "detail": {"resume_check": True, **detail}})


# --------------------------------------------------------------------------- 装煤


def restore_charge(charger: ChargingCar, collector: CollectorMain, flap: BridgeFlap, progress: Progress) -> None:
    state = progress.latest_state()
    ch = state.get("charger", {})
    gas = state.get("gas", {})
    charger.restore(
        aligned=bool(ch.get("aligned", False)),
        fed_t=float(ch.get("fed_t", 0.0)),
        hopper_mm=float(ch.get("hopper_mm", 0.0)),
    )
    collector.restore(float(gas.get("pressure_pa", 30.0)))
    flap.restore(
        open_=bool(gas.get("flap_open", False)),
        latch_cycles=int(gas["latch_cycles"]) if "latch_cycles" in gas else None,
    )


def resume_charge(
    charger: ChargingCar,
    collector: CollectorMain,
    flap: BridgeFlap,
    progress: Progress,
    dt: float,
    tracker,
) -> dict:
    """装煤幂等续做。"""

    restore_charge(charger, collector, flap, progress)
    oven = progress.oven
    params = progress.params
    steps: list[dict] = []
    violations: list[str] = []
    done = set(_completed(progress))
    interrupted = progress.interrupted_stage
    idx = stage_index("charge", interrupted)  # type: ignore[arg-type]

    if idx >= stage_index("charge", "align") and "align" in done:
        # 已完成对位：重新核对实测位置仍在公差内，不重复移动煤斗。
        ok = abs(float(params.get("hopper_measured_mm", 0.0)) - float(params.get("hopper_target_mm", 0.0))) <= charger.limits.align_tolerance_mm
        _verify(steps, "charger", "align_hopper", {"aligned": ok, "hopper_mm": charger.snapshot().hopper_measured_mm})
        if not ok:
            violations.append("hopper_not_aligned")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "resumed": True}

    if interrupted == "align":
        tracker.enter("align", state={"charger": {"aligned": False, "fed_t": 0.0, "hopper_mm": params.get("hopper_measured_mm", 0.0)}})
        alignment = charger.align_hopper(oven, float(params.get("hopper_target_mm", 0.0)), float(params.get("hopper_measured_mm", 0.0)))
        steps.append({"component": "charger", "action": "align_hopper", "detail": alignment.as_dict()})
        if not alignment.ok:
            violations.append("hopper_not_aligned")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "resumed": True}
        done.add("align")

    if "regulate" in done:
        _verify(steps, "gas", "regulate_pressure", {"pressure_pa": round(collector.pressure_pa, 2), "settled": collector.settled()})

    if interrupted == "regulate" or stage_index("charge", interrupted) <= stage_index("charge", "regulate"):
        flap.reset_latch()
        tracker.enter("regulate", state={"charger": {"aligned": charger.is_aligned(), "fed_t": charger.snapshot().fed_t, "hopper_mm": charger.snapshot().hopper_measured_mm}, "gas": {"pressure_pa": collector.pressure_pa, "flap_open": flap.is_open(), "latch_cycles": flap.latch.cycles()}})
        for _ in range(600):
            if collector.settled():
                break
            collector.regulate(dt)
        steps.append({"component": "gas", "action": "regulate_pressure", "detail": {"pressure_pa": round(collector.pressure_pa, 2), "target_pa": collector.limits.pressure_target_pa, "settled": collector.settled()}})
        if not collector.settled():
            violations.append("pressure_not_settled")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "resumed": True}
        done.add("regulate")

    if "flap" in done:
        _verify(steps, "gas", "open_flap", {"angle_deg": flap.angle_deg, "flap_open": flap.is_open()})

    if interrupted == "flap":
        tracker.enter("flap", state={"charger": {"aligned": charger.is_aligned(), "fed_t": charger.snapshot().fed_t, "hopper_mm": charger.snapshot().hopper_measured_mm}, "gas": {"pressure_pa": collector.pressure_pa, "flap_open": flap.is_open(), "latch_cycles": flap.latch.cycles()}})
        angle = flap.open()
        steps.append({"component": "gas", "action": "open_flap", "detail": {"angle_deg": angle}})
        done.add("flap")

    if "feed" in done:
        _verify(steps, "charger", "feed", {"fed_t": round(charger.snapshot().fed_t, 3), "complete": charger.feed_complete()})

    fed_t = charger.snapshot().fed_t
    if interrupted == "feed" or (stage_index("charge", interrupted) >= stage_index("charge", "feed") and not charger.feed_complete()):
        tracker.enter("feed", state={"charger": {"aligned": charger.is_aligned(), "fed_t": fed_t, "hopper_mm": charger.snapshot().hopper_measured_mm}, "gas": {"pressure_pa": collector.pressure_pa, "flap_open": flap.is_open(), "latch_cycles": flap.latch.cycles()}})
        for _ in range(600):
            if charger.feed_complete():
                break
            fed_t += charger.feed(dt)
            tracker.checkpoint({"charger": {"aligned": True, "fed_t": charger.snapshot().fed_t, "hopper_mm": charger.snapshot().hopper_measured_mm}, "gas": {"pressure_pa": collector.pressure_pa, "flap_open": flap.is_open(), "latch_cycles": flap.latch.cycles()}})
        steps.append({"component": "charger", "action": "feed", "detail": {"fed_t": round(charger.snapshot().fed_t, 3), "aligned": charger.is_aligned()}})
        if not charger.feed_complete():
            violations.append("feed_incomplete")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "resumed": True}
        done.add("feed")

    tracker.enter("close_flap", state={"charger": {"aligned": charger.is_aligned(), "fed_t": charger.snapshot().fed_t, "hopper_mm": charger.snapshot().hopper_measured_mm}, "gas": {"pressure_pa": collector.pressure_pa, "flap_open": flap.is_open(), "latch_cycles": flap.latch.cycles()}})
    flap.close()
    steps.append({"component": "gas", "action": "close_flap", "detail": {"angle_deg": flap.angle_deg}})
    return {"oven": oven, "ok": True, "steps": steps, "violations": violations, "fed_t": charger.snapshot().fed_t, "resumed": True}


def rollback_charge(
    charger: ChargingCar,
    collector: CollectorMain,
    flap: BridgeFlap,
    progress: Progress,
    tracker,
) -> dict:
    """装煤退回（只可能发生在 align 阶段）：确认翻板未开、复位对位。"""

    restore_charge(charger, collector, flap, progress)
    steps: list[dict] = []
    if not flap.is_open():
        flap.reset_latch()
    charger.restore(aligned=False, fed_t=0.0)
    steps.append({"component": "charger", "action": "rollback_alignment", "detail": {"aligned": False, "flap_open": flap.is_open()}})
    tracker.finish()
    return {"oven": progress.oven, "ok": True, "steps": steps, "violations": [], "rolled_back": True}


# --------------------------------------------------------------------------- 推焦


def restore_push(pusher: PusherMachine, guide: GuideCar, progress: Progress) -> None:
    state = progress.latest_state()
    g = state.get("guide", {})
    p = state.get("pusher", {})
    guide.restore(
        aligned=bool(g.get("aligned", False)),
        locked=bool(g.get("locked", False)),
        measured_mm=float(g.get("measured_mm", 0.0)),
    )
    pusher.restore(
        float(p.get("ram_mm", 0.0)),
        target_mm=float(p.get("target_mm", 0.0)),
        state=str(p.get("state", "idle")),
    )


def resume_push(
    pusher: PusherMachine,
    guide: GuideCar,
    progress: Progress,
    speed_mm_s: float,
    dt: float,
    tracker,
) -> dict:
    """推焦幂等续做。"""

    restore_push(pusher, guide, progress)
    oven = progress.oven
    params = progress.params
    target = float(params.get("stroke_target_mm", 3200.0))
    steps: list[dict] = []
    violations: list[str] = []
    done = set(_completed(progress))
    interrupted = progress.interrupted_stage
    idx = stage_index("push", interrupted)  # type: ignore[arg-type]

    if "align" in done:
        ok = abs(float(params.get("guide_measured_mm", 0.0)) - float(params.get("guide_target_mm", 0.0))) <= guide.align_tolerance()
        _verify(steps, "guide", "align_apron", {"aligned": ok, "measured_mm": guide.snapshot().measured_mm})
        if not ok:
            violations.append("guide_not_aligned")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "resumed": True}

    if interrupted == "align":
        guide.reset()
        tracker.enter("align", state={"guide": {"aligned": False, "locked": False, "measured_mm": params.get("guide_measured_mm", 0.0)}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": 0.0, "state": pusher.state}})
        alignment = guide.align(oven, float(params.get("guide_target_mm", 0.0)), float(params.get("guide_measured_mm", 0.0)))
        steps.append({"component": "guide", "action": "align_apron", "detail": {**alignment.as_dict(), "tolerance_mm": guide.align_tolerance()}})
        if not alignment.ok:
            violations.append("guide_not_aligned")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "resumed": True}
        done.add("align")

    if "lock" in done:
        _verify(steps, "guide", "lock_apron", {"locked": guide.is_locked(), "apron_deg": guide.snapshot().apron_deg})

    if interrupted == "lock":
        tracker.enter("lock", state={"guide": {"aligned": True, "locked": False, "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": 0.0, "state": pusher.state}})
        angle = guide.lock_apron()
        steps.append({"component": "guide", "action": "lock_apron", "detail": {"apron_deg": angle, "locked": guide.is_locked()}})
        done.add("lock")

    if "extend" in done:
        _verify(steps, "pusher", "extend", {"ram_mm": round(pusher.ram_mm, 2), "strokes": pusher.snapshot().strokes})

    if idx >= stage_index("push", "extend") and not (pusher.state == "idle" and abs(pusher.ram_mm - target) < 1e-9):
        pusher.set_speed(speed_mm_s)
        if pusher.state != "extending":
            pusher.drive_to(target)
        tracker.enter("extend", state={"guide": {"aligned": guide.is_aligned(), "locked": guide.is_locked(), "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": target, "state": pusher.state}})
        for _ in range(2400):
            if pusher.at_target():
                break
            pusher.advance(dt)
            tracker.checkpoint({"guide": {"aligned": guide.is_aligned(), "locked": guide.is_locked(), "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": target, "state": pusher.state}})
        steps.append({"component": "pusher", "action": "extend", "detail": {"ram_mm": round(pusher.ram_mm, 2), "target_mm": target}})
        if not pusher.at_target():
            violations.append("stroke_incomplete")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "resumed": True}
        done.add("extend")

    if "retract" in done:
        _verify(steps, "pusher", "retract", {"ram_mm": round(pusher.ram_mm, 2)})

    if pusher.ram_mm > 1e-9:
        pusher.retract()
        tracker.enter("retract", state={"guide": {"aligned": guide.is_aligned(), "locked": guide.is_locked(), "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": 0.0, "state": pusher.state}})
        for _ in range(2400):
            if pusher.at_target():
                break
            pusher.advance(dt)
            tracker.checkpoint({"guide": {"aligned": guide.is_aligned(), "locked": guide.is_locked(), "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": 0.0, "state": pusher.state}})
        steps.append({"component": "pusher", "action": "retract", "detail": {"ram_mm": round(pusher.ram_mm, 2)}})
        done.add("retract")

    if guide.is_locked():
        tracker.enter("unlock", state={"guide": {"aligned": guide.is_aligned(), "locked": True, "measured_mm": guide.snapshot().measured_mm}, "pusher": {"ram_mm": pusher.ram_mm, "target_mm": 0.0, "state": pusher.state}})
        guide.unlock_apron()
        steps.append({"component": "guide", "action": "unlock_apron", "detail": {"apron_deg": guide.snapshot().apron_deg, "locked": guide.is_locked()}})
    else:
        _verify(steps, "guide", "unlock_apron", {"locked": False})
    return {"oven": oven, "ok": True, "steps": steps, "violations": violations, "resumed": True}


def rollback_push(pusher: PusherMachine, guide: GuideCar, progress: Progress, tracker) -> dict:
    """推焦退回（只可能发生在 align 阶段）：导焦栅未锁，直接复位。"""

    restore_push(pusher, guide, progress)
    steps: list[dict] = []
    if guide.is_locked():
        # 防御：若现场其实已锁定，不能退回，必须转续做。
        return {"oven": progress.oven, "ok": False, "steps": [], "violations": ["apron_locked_cannot_rollback"], "rolled_back": False}
    guide.reset()
    steps.append({"component": "guide", "action": "rollback_alignment", "detail": {"aligned": False, "locked": False}})
    tracker.finish()
    return {"oven": progress.oven, "ok": True, "steps": steps, "violations": [], "rolled_back": True}


# --------------------------------------------------------------------------- 熄焦


def restore_quench(quench: QuenchCar, progress: Progress) -> None:
    q = progress.latest_state().get("quench", {})
    quench.restore(
        travelled_m=float(q.get("travelled_m", 0.0)),
        spray_elapsed_s=float(q.get("spray_elapsed_s", 0.0)),
        oven=str(q.get("oven", progress.oven)),
    )


def resume_quench(quench: QuenchCar, progress: Progress, dt: float, tracker) -> dict:
    """熄焦幂等续做：走到塔 → 按已喷时长续喷。"""

    restore_quench(quench, progress)
    oven = progress.oven
    steps: list[dict] = []
    violations: list[str] = []
    done = set(_completed(progress))
    interrupted = progress.interrupted_stage

    if "receive" in done:
        _verify(steps, "quench", "receive", {"oven": oven, "loaded": quench.snapshot().loaded})

    if "travel" in done:
        _verify(steps, "quench", "travel", {"travelled_m": round(quench.snapshot().travelled_m, 2), "at_tower": quench.at_tower()})

    if interrupted in ("receive", "travel") and not quench.at_tower():
        stage = "travel" if interrupted == "travel" else "receive"
        if stage == "receive":
            tracker.enter("receive", state={"quench": {"travelled_m": quench.snapshot().travelled_m, "spray_elapsed_s": quench.spray.elapsed_s, "oven": oven}})
            done.add("receive")
        tracker.enter("travel", state={"quench": {"travelled_m": quench.snapshot().travelled_m, "spray_elapsed_s": quench.spray.elapsed_s, "oven": oven}})
        for _ in range(2400):
            if quench.at_tower():
                break
            quench.travel(dt)
            tracker.checkpoint({"quench": {"travelled_m": quench.snapshot().travelled_m, "spray_elapsed_s": quench.spray.elapsed_s, "oven": oven}})
        steps.append({"component": "quench", "action": "travel", "detail": {"travelled_m": round(quench.snapshot().travelled_m, 2)}})
        if not quench.at_tower():
            violations.append("travel_incomplete")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "resumed": True}
        done.add("travel")

    if not quench.spray.complete():
        if not quench.spray.open:
            quench.start_spray()
            steps.append({"component": "quench", "action": "start_spray", "detail": {"duration_s": quench.spray.duration_s}})
        tracker.enter("spray", state={"quench": {"travelled_m": quench.snapshot().travelled_m, "spray_elapsed_s": quench.spray.elapsed_s, "oven": oven}})
        for _ in range(2400):
            if quench.spray.complete():
                break
            quench.spray_step(dt)
            tracker.checkpoint({"quench": {"travelled_m": quench.snapshot().travelled_m, "spray_elapsed_s": quench.spray.elapsed_s, "oven": oven}})
        steps.append({"component": "quench", "action": "spray", "detail": {"water_m3": round(quench.spray.water_m3(), 3), "complete": quench.spray.complete()}})
        if not quench.spray.complete():
            violations.append("spray_incomplete")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "resumed": True}
    else:
        _verify(steps, "quench", "spray", {"water_m3": round(quench.spray.water_m3(), 3), "complete": True})
    return {"oven": oven, "ok": True, "steps": steps, "violations": violations, "resumed": True}


def rollback_quench(quench: QuenchCar, progress: Progress, tracker) -> dict:
    """熄焦退回（只可能发生在 receive）：尚未走行，复位让车回焦侧重接。"""

    restore_quench(quench, progress)
    steps: list[dict] = []
    if quench.at_tower():
        return {"oven": progress.oven, "ok": False, "steps": [], "violations": ["already_at_tower_cannot_rollback"], "rolled_back": False}
    quench.reset()
    steps.append({"component": "quench", "action": "rollback_receive", "detail": {"loaded": False, "travelled_m": 0.0}})
    tracker.finish()
    return {"oven": progress.oven, "ok": True, "steps": steps, "violations": [], "rolled_back": True}


# --------------------------------------------------------------------------- 换向


def restore_reverse(valves: ValveTrain, temperature: OvenTemperature, progress: Progress) -> str:
    h = progress.latest_state().get("heating", {})
    direction = str(h.get("direction", "waste_to_gas" if h.get("gas_open") else "gas_to_waste"))
    valves.restore(waste_open=bool(h.get("waste_open", True)), gas_open=bool(h.get("gas_open", False)))
    temperature.restore(float(h.get("temperature_c", temperature.limits.temp_target_c)))
    return direction


def resume_reverse(
    reversal: HeatingReversal,
    valves: ValveTrain,
    temperature: OvenTemperature,
    progress: Progress,
    tracker,
) -> dict:
    """换向幂等续做：阀组落到单一稳定终态，再补炉温确认。"""

    direction = restore_reverse(valves, temperature, progress)
    oven = progress.oven
    target_temp = float(progress.params.get("temperature_c", temperature.limits.temp_target_c))
    steps: list[dict] = []
    violations: list[str] = []
    interrupted = progress.interrupted_stage or "switch"

    if interrupted == "switch":
        tracker.enter("switch", state={"heating": {"waste_open": valves.waste_open, "gas_open": valves.gas_open, "temperature_c": temperature.value_c}})
        if direction == "waste_to_gas":
            if valves.waste_open:
                valves.close_waste_gas()
                steps.append({"component": "heating", "action": "close_waste_gas", "detail": {"open": valves.waste_open}})
            if not valves.gas_open:
                valves.open_gas()
                steps.append({"component": "heating", "action": "open_gas", "detail": {"open": valves.gas_open}})
        else:
            if valves.gas_open:
                valves.close_gas()
                steps.append({"component": "heating", "action": "close_gas", "detail": {"open": valves.gas_open}})
            if not valves.waste_open:
                valves.open_waste_gas()
                steps.append({"component": "heating", "action": "open_waste_gas", "detail": {"open": valves.waste_open}})
    else:
        _verify(steps, "heating", "switch", {"waste_open": valves.waste_open, "gas_open": valves.gas_open, "direction": direction})

    tracker.enter("confirm", state={"heating": {"waste_open": valves.waste_open, "gas_open": valves.gas_open, "temperature_c": temperature.value_c, "direction": direction}})
    temperature.update(target_temp)
    confirmed = temperature.confirmed()
    steps.append({"component": "heating", "action": "confirm_temperature", "detail": {"temperature_c": round(temperature.value_c, 2), "confirmed": confirmed}})
    if confirmed:
        reversal.mark_reversed()
    else:
        violations.append("temp_not_confirmed")
    return {"oven": oven, "ok": confirmed, "steps": steps, "violations": violations, "direction": direction, "resumed": True}


# --------------------------------------------------------------------------- 清扫


def restore_clean(door: DoorCleaner, progress: Progress) -> None:
    d = progress.latest_state().get("door", {})
    engaged = bool(d.get("engaged", True))
    door.restore(
        door_open=bool(d.get("door_open", False)),
        elapsed_s=float(d.get("elapsed_s", 0.0)),
        cleaned=bool(d.get("cleaned", False)),
        oven=str(d.get("oven", progress.oven)),
    )
    door.latch.restore(engaged=engaged)


def resume_clean(door: DoorCleaner, progress: Progress, dt: float, tracker) -> dict:
    """炉门清扫幂等续做：按已累计时长续扫，再关门压紧。"""

    restore_clean(door, progress)
    oven = progress.oven
    steps: list[dict] = []
    violations: list[str] = []
    done = set(_completed(progress))
    interrupted = progress.interrupted_stage
    idx = stage_index("clean", interrupted)  # type: ignore[arg-type]

    if "release" in done:
        _verify(steps, "door", "release_latch", {"engaged": door.latch.is_engaged(), "position_mm": door.latch.position_mm})
    if "open" in done:
        _verify(steps, "door", "open_door", {"door_open": door.door_open()})
    if "clean" in done:
        _verify(steps, "door", "clean", {"progress": round(door.progress(), 3)})

    if interrupted == "release":
        tracker.enter("release", state={"door": {"engaged": True, "door_open": False, "elapsed_s": 0.0, "cleaned": False, "oven": oven}})
        door.latch.release()
        steps.append({"component": "door", "action": "release_latch", "detail": {"engaged": door.latch.is_engaged(), "position_mm": door.latch.position_mm}})
        tracker.enter("open", state={"door": {"engaged": False, "door_open": False, "elapsed_s": 0.0, "cleaned": False, "oven": oven}})
        door.open_door(oven)
        steps.append({"component": "door", "action": "open_door", "detail": {"oven": oven}})
        done.update({"release", "open"})

    if interrupted == "open":
        tracker.enter("open", state={"door": {"engaged": door.latch.is_engaged(), "door_open": False, "elapsed_s": 0.0, "cleaned": False, "oven": oven}})
        door.open_door(oven)
        steps.append({"component": "door", "action": "open_door", "detail": {"oven": oven}})
        done.add("open")

    if idx <= stage_index("clean", "clean") and door.door_open() and door.progress() < 1.0:
        tracker.enter("clean", state={"door": {"engaged": door.latch.is_engaged(), "door_open": True, "elapsed_s": door.progress() * door.limits.clean_duration_s, "cleaned": False, "oven": oven}})
        for _ in range(600):
            if door.progress() >= 1.0:
                break
            door.clean(dt)
            tracker.checkpoint({"door": {"engaged": False, "door_open": True, "elapsed_s": door.progress() * door.limits.clean_duration_s, "cleaned": False, "oven": oven}})
        steps.append({"component": "door", "action": "clean", "detail": {"progress": round(door.progress(), 3)}})
        if door.progress() < 1.0:
            violations.append("clean_incomplete")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations, "resumed": True}
        done.add("clean")

    if not door.is_cleaned():
        tracker.enter("mark", state={"door": {"engaged": False, "door_open": True, "elapsed_s": door.limits.clean_duration_s, "cleaned": False, "oven": oven}})
        cycles = door.mark_cleaned()
        steps.append({"component": "door", "action": "mark_cleaned", "detail": {"cycles": cycles}})
    else:
        _verify(steps, "door", "mark_cleaned", {"cleaned": True})

    tracker.enter("close", state={"door": {"engaged": False, "door_open": True, "elapsed_s": door.limits.clean_duration_s, "cleaned": True, "oven": oven}})
    door.close_door()
    steps.append({"component": "door", "action": "close_door", "detail": {"latch_engaged": door.latch.is_engaged(), "cleaned": door.is_cleaned()}})
    return {"oven": oven, "ok": True, "steps": steps, "violations": violations, "resumed": True}


def rollback_clean(door: DoorCleaner, progress: Progress, tracker) -> dict:
    """清扫退回（只可能发生在 release）：炉门未开，重新压紧闩锁。"""

    restore_clean(door, progress)
    steps: list[dict] = []
    if door.door_open():
        return {"oven": progress.oven, "ok": False, "steps": [], "violations": ["door_open_cannot_rollback"], "rolled_back": False}
    door.latch.set_engaged()
    steps.append({"component": "door", "action": "rollback_latch", "detail": {"engaged": True, "position_mm": door.latch.position_mm}})
    tracker.finish()
    return {"oven": progress.oven, "ok": True, "steps": steps, "violations": [], "rolled_back": True}
