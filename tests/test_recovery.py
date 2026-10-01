"""编排进度与重启恢复。

恢复判定粒度固定为阶段。下列用例覆盖：半途续做幂等、回退重做、人工确认、
停机期间改参数（迁移）必须重新核对、进度与审计双源核对，以及决定在控制台
与审计间口径一致。
"""

from __future__ import annotations

import json

import pytest

from coke.app import build_platform
from coke.config import PlatformConfig, config_to_mapping, default_config
from coke.recovery import (
    ACTION_BLOCKED,
    ACTION_COMPLETE,
    ACTION_MANUAL,
    ACTION_RESUME,
    ACTION_ROLLBACK,
    GRANULARITY,
    PROGRESS_DOC,
    RecoveryBlocked,
)
from coke.recovery.progress import OperationProgress


def _restart(clock, storage, config=None):
    return build_platform(config or default_config(), clock=clock, storage_root=storage)


def test_granularity_is_phase_and_fixed():
    assert GRANULARITY == "phase"
    status_keys = ("granularity", "active", "progress", "decision")
    platform = build_platform(default_config(), storage_root="/tmp/_unused_recovery")
    assert all(key in platform.recovery_status() for key in status_keys)


def test_push_resume_from_mid_extend_is_idempotent(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _restart(clock, storage)
    first.start()
    drill = first.crash_drill(operation="push", phase="extend")
    assert drill["crashed"]["at"] == "extend/@tick"
    # 推焦杆停在半程：有杆位、未到目标、状态为前进中。
    mid_ram = first.pusher.ram_mm
    assert 0.0 < mid_ram < first.stroke.baseline_mm
    assert first.pusher.state == "extending"

    second = _restart(clock, storage)
    decision = second.recovery_status()["decision"]
    assert decision["granularity"] == "phase"
    assert decision["action"] == ACTION_RESUME
    assert decision["phase"] == "extend"
    assert decision["policy"] == "resume"
    # 已完成阶段在决定里都是 done，续做不会从对齐重来。
    assert [decision["phases"][key] for key in ("align", "lock")] == ["done", "done"]

    strokes_before = second.pusher.snapshot().strokes
    report = second.resume_operation()
    assert report["ok"] is True
    assert report["resumed"] is True
    # 幂等：对位、锁定步骤没有被重放（审计里各只出现一次）。
    counts: dict[str, int] = {}
    for record in second.audit.replay_from_journal():
        counts[record["action"]] = counts.get(record["action"], 0) + 1
    assert counts["align_apron"] == 1
    assert counts["lock_apron"] == 1
    assert counts["extend"] == 1
    assert second.pusher.ram_mm == 0.0
    assert second.pusher.snapshot().strokes == strokes_before + 1
    assert second.recovery_status()["active"] is False


def test_quench_resume_continues_travel_and_spray(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _restart(clock, storage)
    first.start()
    first.crash_drill(operation="quench", phase="travel")
    travelled = first.quench_car.snapshot().travelled_m
    assert 0.0 < travelled < first.quench_car.plan.distance_m

    second = _restart(clock, storage)
    decision = second.recovery_status()["decision"]
    assert decision["action"] == ACTION_RESUME
    assert decision["phase"] == "travel"
    report = second.resume_operation()
    assert report["ok"] is True
    counts = {}
    for record in second.audit.replay_from_journal():
        counts[record["action"]] = counts.get(record["action"], 0) + 1
    assert counts["receive"] == 1
    assert counts["travel"] == 1
    assert counts["spray"] == 1


def test_spray_resume_does_not_double_water(tmp_path, clock):
    """喷淋阀已开后掉电：续做只补剩余时长，不从头再喷一遍。"""

    storage = str(tmp_path / "state")
    first = _restart(clock, storage)
    first.start()
    first.crash_drill(operation="quench", phase="spray")
    # start_spray 事件先落审计、掉电紧随其后；喷淋时长可能为 0 或一个步长。
    second = _restart(clock, storage)
    assert second.recovery_status()["decision"]["phase"] == "spray"
    report = second.resume_operation()
    assert report["ok"] is True
    water = second.quench_car.snapshot().water_m3
    expected = (
        second.quench_car.spray.duration_s / 3600.0 * second.quench_car.spray.water_flow_m3_h
    )
    assert water == pytest.approx(expected, rel=1e-6)


def test_clean_rolls_back_and_rewrites_phase(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _restart(clock, storage)
    first.start()
    first.crash_drill(operation="clean", phase="clean")
    assert 0.0 < first.door.progress() < 1.0

    second = _restart(clock, storage)
    decision = second.recovery_status()["decision"]
    assert decision["action"] == ACTION_ROLLBACK
    assert decision["phase"] == "clean"
    assert decision["policy"] == "rollback"
    report = second.resume_operation()
    assert report["ok"] is True
    actions = [record["action"] for record in second.audit.replay_from_journal()]
    assert "rollback_clean" in actions
    # 回退之后清扫从头计满：闩锁最终压紧、炉门关闭。
    assert second.door.latch.is_engaged()
    assert second.door.door_open() is False


def test_reverse_requires_manual_ack(tmp_path, clock):
    storage = str(tmp_path / "state")
    config = default_config()
    first = _restart(clock, storage, config)
    first.start()
    clock.advance(config.heating.reversal_interval_s)
    drill = first.crash_drill(operation="reverse", phase="switch", action="close_waste_gas")
    assert drill["crashed"]["at"] == "switch/close_waste_gas"
    # 停在切换缝里：废气阀已关、煤气阀未开。
    assert first.valves.waste_open is False
    assert first.valves.gas_open is False

    second = _restart(clock, storage, config)
    decision = second.recovery_status()["decision"]
    assert decision["action"] == ACTION_MANUAL
    assert decision["phase"] == "switch"
    with pytest.raises(RecoveryBlocked):
        second.resume_operation()
    # 操作工核对阀位后确认。
    ack = second.acknowledge_recovery()
    assert ack["acked"] == "switch"
    assert second.recovery_status()["decision"]["action"] == ACTION_RESUME
    report = second.resume_operation()
    assert report["ok"] is True
    assert second.valves.gas_open is True


def test_config_change_during_downtime_blocks_until_reverified(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _restart(clock, storage)
    first.start()
    first.crash_drill(operation="push", phase="extend")

    changed = _mutated_config(max_stroke_mm=3300.0)
    second = _restart(clock, storage, changed)
    decision = second.recovery_status()["decision"]
    assert decision["action"] == ACTION_BLOCKED
    assert "config_migrated" in decision["reasons"]
    assert decision["migration_reverified"] is False
    with pytest.raises(RecoveryBlocked):
        second.resume_operation()

    # 新极限仍容得下原推焦目标（3200 < 3300），重新核对通过后续做。
    reverified = second.reverify_recovery()
    assert reverified["reverified"]["passed"] is True
    assert second.recovery_status()["decision"]["action"] == ACTION_RESUME
    report = second.resume_operation()
    assert report["ok"] is True


def test_config_change_that_invalidates_params_stays_blocked(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _restart(clock, storage)
    first.start()
    first.crash_drill(operation="push", phase="extend")

    # 最大行程被调小到推焦目标以下（基线同步下调，保证配置本身合法）：
    # 旧操作入参在新极限下不再合法。
    changed = _mutated_config(stroke_baseline_mm=2900.0, max_stroke_mm=3000.0)
    second = _restart(clock, storage, changed)
    with pytest.raises(RecoveryBlocked) as info:
        second.reverify_recovery()
    assert "stroke_target_exceeds_max" in str(info.value)
    assert second.recovery_status()["decision"]["action"] == ACTION_BLOCKED


def test_decision_is_consistent_between_console_and_audit(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _restart(clock, storage)
    first.start()
    first.crash_drill(operation="push", phase="extend")

    second = _restart(clock, storage)
    shown = second.recovery_status()["decision"]
    journal = second.audit.replay_from_journal()
    decided = [item for item in journal if item["action"] == "recovery_decided"]
    assert len(decided) == 1
    assert decided[0]["payload"]["decision_id"] == shown["decision_id"]
    assert decided[0]["payload"]["decision_action"] == shown["action"]
    # 决定同时落进度文档，三份来源同一 decision_id。
    raw = second.store.read(PROGRESS_DOC)
    progress = OperationProgress.from_dict(raw)
    assert progress is not None
    assert progress.last_decision["decision_id"] == shown["decision_id"]


def test_evidence_mismatch_blocks_resume(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _restart(clock, storage)
    first.start()
    first.crash_drill(operation="push", phase="extend")

    # 人为篡改进度：声称锁定阶段做过，但审计里查无凭据。
    raw = first.store.read(PROGRESS_DOC)
    raw["phases"]["lock"]["status"] = "done"
    raw["phases"]["lock"]["logged"] = ["not_a_real_action"]
    first.store.write(PROGRESS_DOC, raw)

    second = _restart(clock, storage)
    decision = second.recovery_status()["decision"]
    assert decision["action"] == ACTION_BLOCKED
    assert "evidence_mismatch" in decision["reasons"]
    with pytest.raises(RecoveryBlocked):
        second.resume_operation()


def test_already_complete_progress_is_cleared_on_restart(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _restart(clock, storage)
    first.start()
    report = first.push(oven="oven01")
    assert report["ok"] is True
    assert first.store.exists(PROGRESS_DOC) is False

    # 人为留下一份"阶段全完成"的残留进度，审计里已有结束事件。
    second = _restart(clock, storage)
    raw = {
        "op_id": report["op_id"],
        "operation": "push",
        "oven": "oven01",
        "params": {},
        "dt": 1.0,
        "config_fingerprint": second.recovery_status()["progress"],
        "started_ts": clock.now(),
        "updated_ts": clock.now(),
        "status": "running",
        "phases": {},
        "checkpoint": {},
    }
    second.store.write(PROGRESS_DOC, raw)
    third = _restart(clock, storage)
    decision = third.recovery_status()["decision"]
    assert decision is None
    assert third.store.exists(PROGRESS_DOC) is False


def test_abandon_recovery_clears_progress_with_audit(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _restart(clock, storage)
    first.start()
    first.crash_drill(operation="push", phase="extend")
    second = _restart(clock, storage)
    result = second.abandon_recovery()
    assert result["abandoned"] is True
    assert second.store.exists(PROGRESS_DOC) is False
    assert any(item["action"] == "recovery_abandoned" for item in second.audit.replay_from_journal())
    # 废弃后可以开始新操作。
    assert second.push(oven="oven01")["ok"] is True


def _mutated_config(**overrides) -> PlatformConfig:
    mapping = config_to_mapping(default_config())
    mapping["limits"]["pusher"].update(overrides)
    return PlatformConfig.from_mapping(mapping)
