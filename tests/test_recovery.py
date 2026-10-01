"""重启后中断恢复：按阶段判定、幂等续做、退回、迁移重核与可核对性。

所有用例都真实经过"落盘 → 新建平台实例（模拟重启）→ 判定 → 执行"，
不依赖内存对象复用。判定粒度在测试里钉死为 ``stage``。
"""

from __future__ import annotations

import dataclasses

import pytest

from coke.config import default_config
from coke.console.api import Api
from coke.errors import InterlockError
from coke.recovery.progress import DOC_ID, ProgressStore, digest, fingerprint_config
from coke.recovery.stages import CONTINUE, ROLLBACK, STAGE_POLICY, stages_for
from coke.store.jsonfile import JsonStore


def _tracker(platform):
    return platform.recovery.tracker


def _crash_during_charge_feed(platform, fed_limit: float = 7.0):
    """制造"装煤开到翻板、给煤给了一半"的现场后模拟掉电。"""

    platform.start()
    t = _tracker(platform)
    t.start(
        "op-charge-feed", "charge", "oven01",
        {"hopper_target_mm": 0.0, "hopper_measured_mm": 0.0, "dt": 1.0},
        fingerprint_config(platform.config), platform.clock.now(),
    )
    t.enter("align", state={"charger": {"aligned": False, "fed_t": 0.0, "hopper_mm": 0.0}})
    platform.charger.align_hopper("oven01", 0.0, 0.0)
    platform.flap.reset_latch()
    t.enter("regulate", state={"charger": {"aligned": True, "fed_t": 0.0, "hopper_mm": 0.0},
                               "gas": {"pressure_pa": 90.0, "flap_open": False, "latch_cycles": 0}})
    for _ in range(600):
        if platform.collector.settled():
            break
        platform.collector.regulate(1.0)
    t.enter("flap", state={"charger": {"aligned": True, "fed_t": 0.0, "hopper_mm": 0.0},
                           "gas": {"pressure_pa": platform.collector.pressure_pa,
                                   "flap_open": False, "latch_cycles": 0}})
    platform.flap.open()
    t.enter("feed", state={"charger": {"aligned": True, "fed_t": 0.0, "hopper_mm": 0.0},
                           "gas": {"pressure_pa": platform.collector.pressure_pa,
                                   "flap_open": True, "latch_cycles": 1}})
    fed = 0.0
    while fed < fed_limit:
        fed += platform.charger.feed(1.0)
        t.checkpoint({"charger": {"aligned": True, "fed_t": platform.charger.snapshot().fed_t, "hopper_mm": 0.0},
                      "gas": {"pressure_pa": platform.collector.pressure_pa,
                              "flap_open": True, "latch_cycles": 1}})
    return platform.charger.snapshot().fed_t


def test_policy_granularity_is_fixed_to_stage():
    # 判定口径钉死：每个阶段只能是 continue / rollback，且覆盖全部阶段。
    for operation, stages in STAGE_POLICY.items():
        assert set(stages) == set(stages_for(operation))
        assert set(stages.values()) <= {CONTINUE, ROLLBACK}
    # 前置、可逆阶段才允许退回；一旦产生不可逆物理动作必须续做。
    assert STAGE_POLICY["charge"]["align"] == ROLLBACK
    assert STAGE_POLICY["charge"]["feed"] == CONTINUE
    assert STAGE_POLICY["push"]["extend"] == CONTINUE
    assert STAGE_POLICY["quench"]["travel"] == CONTINUE
    assert STAGE_POLICY["clean"]["open"] == CONTINUE


def test_clean_start_has_no_pending(platform):
    platform.start()
    status = platform.recover_status()
    assert status["pending"] is False
    assert status["granularity"] == "stage"


def test_mid_feed_crash_resumes_idempotently(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _new(storage, clock)
    already_fed = _crash_during_charge_feed(first)
    assert 0.0 < already_fed < first.config.charger.coal_target_t

    second = _new(storage, clock)
    decision = second.recover_status()["decision"]
    assert decision["granularity"] == "stage"
    assert decision["outcome"] == "continue"
    assert decision["interrupted_stage"] == "feed"
    assert decision["completed_stages"] == ["align", "regulate", "flap"]
    assert all(check["ok"] for check in decision["checks"])

    report = second.recover_resume(dt=1.0)
    assert report["ok"] is True
    # 只补到目标吨位，已经给过的部分不重复。
    assert abs(report["fed_t"] - second.config.charger.coal_target_t) < 1e-9
    actions = [step["action"] for step in report["steps"]]
    assert "align_hopper" not in actions
    assert "open_flap" not in actions
    assert all(action.startswith("verify_") for action in actions[:3])
    # 翻板闩锁只开过一次，续做没有重复开阀。
    assert second.flap.latch.cycles() == 1
    assert second.recover_status()["pending"] is False


def test_rollback_stage_rejects_resume_and_vice_versa(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _new(storage, clock)
    first.start()
    t = _tracker(first)
    t.start("op-align", "charge", "oven01",
            {"hopper_target_mm": 0.0, "hopper_measured_mm": 0.0, "dt": 1.0},
            fingerprint_config(first.config), clock.now())
    t.enter("align", state={"charger": {"aligned": False, "fed_t": 0.0, "hopper_mm": 0.0}})

    second = _new(storage, clock)
    decision = second.recover_status()["decision"]
    assert decision["outcome"] == "rollback"
    with pytest.raises(ValueError):
        second.recover_resume()
    report = second.recover_rollback()
    assert report["ok"] is True
    assert report["rolled_back"] is True
    assert abs(second.charger.snapshot().fed_t) < 1e-9
    assert not second.flap.is_open()
    assert second.recover_status()["pending"] is False


def test_push_mid_extend_completes_then_retracts_once(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _new(storage, clock)
    first.start()
    t = _tracker(first)
    t.start("op-push", "push", "oven01",
            {"guide_target_mm": 0.0, "guide_measured_mm": 0.0, "stroke_target_mm": 3200.0,
             "speed_mm_s": 24.0, "dt": 1.0},
            fingerprint_config(first.config), clock.now())
    t.enter("align", state={"guide": {"aligned": False, "locked": False, "measured_mm": 0.0},
                            "pusher": {"ram_mm": 0.0, "target_mm": 0.0, "state": "idle"}})
    first.guide.align("oven01", 0.0, 0.0)
    t.enter("lock", state={"guide": {"aligned": True, "locked": False, "measured_mm": 0.0},
                           "pusher": {"ram_mm": 0.0, "target_mm": 0.0, "state": "idle"}})
    first.guide.lock_apron()
    first.pusher.set_speed(24.0)
    first.pusher.drive_to(3200.0)
    t.enter("extend", state={"guide": {"aligned": True, "locked": True, "measured_mm": 0.0},
                             "pusher": {"ram_mm": 0.0, "target_mm": 3200.0, "state": "extending"}})
    for _ in range(40):
        first.pusher.advance(1.0)
        t.checkpoint({"guide": {"aligned": True, "locked": True, "measured_mm": 0.0},
                      "pusher": {"ram_mm": first.pusher.ram_mm, "target_mm": 3200.0,
                                 "state": first.pusher.state}})
    assert 0.0 < first.pusher.ram_mm < 3200.0

    second = _new(storage, clock)
    assert second.recover_status()["decision"]["outcome"] == "continue"
    assert second.guide.is_locked()
    report = second.recover_resume(dt=1.0)
    assert report["ok"] is True
    assert abs(second.pusher.ram_mm) < 1e-9
    assert not second.guide.is_locked()
    assert second.pusher.snapshot().strokes == 1
    actions = [step["action"] for step in report["steps"]]
    assert "align_apron" not in actions and "lock_apron" not in actions


def test_reverse_mid_switch_resumes_to_single_valve_state(tmp_path, clock):
    storage = str(tmp_path / "state")
    config = default_config()
    first = _new(storage, clock, config)
    first.start()
    clock.advance(config.heating.reversal_interval_s)
    t = _tracker(first)
    t.start("op-reverse", "reverse", "oven01", {"temperature_c": config.heating.temp_target_c},
            fingerprint_config(config), clock.now())
    t.enter("switch", state={"heating": {"waste_open": True, "gas_open": False,
                                         "temperature_c": config.heating.temp_target_c,
                                         "direction": "waste_to_gas"}})
    first.valves.close_waste_gas()
    # 两阀全关窗口必须有检查点，重启后才知道只补开煤气阀。
    t.checkpoint({"heating": {"waste_open": False, "gas_open": False,
                              "temperature_c": config.heating.temp_target_c,
                              "direction": "waste_to_gas"}})

    second = _new(storage, clock, config)
    report = second.recover_resume()
    assert report["ok"] is True
    assert not second.valves.waste_open and second.valves.gas_open
    actions = [step["action"] for step in report["steps"]]
    assert "close_waste_gas" not in actions
    assert "open_gas" in actions


def test_config_change_during_downtime_requires_reverify(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _new(storage, clock)
    _crash_during_charge_feed(first)

    drifted = dataclasses.replace(
        default_config(),
        charger=dataclasses.replace(default_config().charger, coal_target_t=30.0),
    )
    second = _new(storage, clock, drifted)
    decision = second.recover_status()["decision"]
    assert decision["config_drift"] is True
    assert decision["outcome"] == "reverify"
    with pytest.raises(ValueError):
        second.recover_resume()
    reverified = second.recover_reverify()
    assert reverified["outcome"] == "continue"
    report = second.recover_resume(dt=1.0)
    assert report["ok"] is True
    assert abs(report["fed_t"] - 30.0) < 1e-9


def test_tampered_progress_is_blocked(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _new(storage, clock)
    _crash_during_charge_feed(first)
    store = JsonStore(storage)
    raw = store.read(DOC_ID)
    raw["stages"][-1]["stage"] = "align"  # 直接改内容但不重算哈希
    store.write(DOC_ID, raw)

    second = _new(storage, clock)
    decision = second.recover_status()["decision"]
    assert decision["outcome"] == "blocked"
    assert decision["progress_hash_valid"] is False
    with pytest.raises(ValueError):
        second.recover_resume()
    with pytest.raises(ValueError):
        second.recover_rollback()
    with pytest.raises(ValueError):
        second.recover_reverify()


def test_journal_mismatch_is_blocked(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _new(storage, clock)
    _crash_during_charge_feed(first)
    store = JsonStore(storage)
    raw = store.read(DOC_ID)
    raw["stages"][-1]["stage"] = "align"
    record = raw["stages"][-1]
    record["content_hash"] = digest(
        {key: value for key, value in record.items() if key not in ("prev_hash", "content_hash")}
    )
    raw["content_hash"] = record["content_hash"]
    store.write(DOC_ID, raw)  # 哈希链自洽，但与审计流水阶段轨迹不符

    second = _new(storage, clock)
    decision = second.recover_status()["decision"]
    assert decision["progress_hash_valid"] is True
    assert decision["journal_match"] is False
    assert decision["outcome"] == "blocked"


def test_pending_recovery_blocks_new_operation(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _new(storage, clock)
    _crash_during_charge_feed(first)

    second = _new(storage, clock)
    with pytest.raises(InterlockError):
        second.charge(oven="oven02")


def test_decision_id_is_stable_and_matches_audit(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _new(storage, clock)
    _crash_during_charge_feed(first)
    id_a = _new(storage, clock).recover_status()["decision"]["decision_id"]
    second = _new(storage, clock)
    id_b = second.recover_status()["decision"]["decision_id"]
    assert id_a == id_b
    report = second.recover_resume(dt=1.0)
    assert report["decision_id"] == id_b
    entries = second.audit.entries(limit=100)
    decision_event = next(e for e in entries if e["action"] == "recovery_decision")
    resumed_event = next(e for e in entries if e["action"] == "recovery_resumed")
    assert decision_event["payload"]["decision_id"] == resumed_event["payload"]["decision_id"]


def test_console_exposes_recovery_status_and_actions(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = _new(storage, clock)
    _crash_during_charge_feed(first)

    second = _new(storage, clock)
    api = Api(second)
    code, payload = api.handle("GET", "/api/recovery", {}, {})
    assert code == 200
    assert payload["granularity"] == "stage"
    assert payload["decision"]["outcome"] == "continue"
    assert "feed" in payload["stage_policy"]
    code, resumed = api.handle("POST", "/api/recovery/resume", {}, {"dt": 1.0})
    assert code == 200 and resumed["ok"] is True
    code, status = api.handle("GET", "/api/status", {}, {})
    assert code == 200 and status["recovery"]["pending"] is False
    routes = {route["path"] for route in api.routes()}
    assert {"/api/recovery", "/api/recovery/resume",
            "/api/recovery/rollback", "/api/recovery/reverify"} <= routes


def test_normal_operation_leaves_no_progress(platform):
    platform.start()
    assert platform.charge(oven="oven01")["ok"] is True
    assert platform.recover_status()["pending"] is False
    assert platform.quench(oven="oven01")["ok"] is True
    assert platform.recover_status()["pending"] is False


def _new(storage: str, clock, config=None):
    from coke.app import build_platform

    return build_platform(config or default_config(), clock=clock, storage_root=storage)
