"""恢复判定与核对（按阶段，定死）。

平台启动时调用 :meth:`RecoveryManager.boot_analyze`，它做四件可核对的事：

1. **哈希链核对**：落盘进度文档自带哈希链，被改动直接 ``blocked``，不恢复；
2. **流水核对**：把审计流水里同一 ``op_id`` 的阶段轨迹与落盘阶段逐条对照，
   对不上同样 ``blocked``（恢复过程与审计必须一致）；
3. **参数迁移核对**：开工配置指纹与当前配置指纹不一致，说明停机期间有人改
   过参数，结论置 ``reverify``，必须先重新核对才能续做；
4. **物理量核对 + 阶段判定**：按设备读回核对已完成阶段，再按
   :data:`coke.recovery.stages.STAGE_POLICY` 给出 ``continue`` / ``rollback``。

判定结论带稳定 ``decision_id``（对同一进度与配置重算结果相同），续做决定
与落审计共用这枚标识，控制台展示和审计事后比对必须对得上。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..clock import Clock
from ..config import PlatformConfig
from ..eventbus import Event, EventBus
from . import resume as runners
from .progress import Progress, ProgressStore, ProgressTracker, digest, fingerprint_config
from .stages import (
    BLOCKED,
    CONTINUE,
    NONE,
    REVERIFY,
    ROLLBACK,
    policy_for,
    rationale_for,
    stages_for,
)


@dataclass(frozen=True)
class DeviceBundle:
    """恢复执行器需要的全部现场设备（平台装配时注入，避免反向导入 app）。"""

    charger: Any
    collector: Any
    flap: Any
    pusher: Any
    guide: Any
    quench: Any
    valves: Any
    temperature: Any
    reversal: Any
    door: Any


@dataclass(frozen=True)
class RecoveryDecision:
    """一次恢复判定的可核对结论。"""

    outcome: str
    operation: str
    oven: str
    op_id: str
    interrupted_stage: str | None
    completed_stages: tuple[str, ...]
    policy: str
    rationale: str
    config_drift: bool
    checks: tuple[dict, ...]
    journal_match: bool
    progress_hash_valid: bool
    decision_id: str
    ts: float

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "granularity": "stage",
            "operation": self.operation,
            "oven": self.oven,
            "op_id": self.op_id,
            "interrupted_stage": self.interrupted_stage,
            "completed_stages": list(self.completed_stages),
            "policy": self.policy,
            "rationale": self.rationale,
            "config_drift": self.config_drift,
            "checks": list(self.checks),
            "journal_match": self.journal_match,
            "progress_hash_valid": self.progress_hash_valid,
            "decision_id": self.decision_id,
            "ts": round(self.ts, 3),
        }


def _check(name: str, ok: bool, detail: str, **extra: Any) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail, **extra}


class RecoveryManager:
    """持有中断进度、给出恢复判定并执行续做／退回。"""

    def __init__(
        self,
        config: PlatformConfig,
        clock: Clock,
        bus: EventBus,
        store: ProgressStore,
        devices: DeviceBundle,
        audit_replay: Callable[[], list[dict]],
    ) -> None:
        self._config = config
        self._clock = clock
        self._bus = bus
        self._store = store
        self._devices = devices
        self._audit_replay = audit_replay
        self.tracker = ProgressTracker(store, bus)
        self._lock = threading.RLock()
        self._decision: RecoveryDecision | None = None
        self._progress: Progress | None = None
        self._reverified = False

    # ------------------------------------------------------------------ 判定

    def _journal_stages(self, op_id: str) -> list[str]:
        """从落盘审计流水还原某操作进入过的阶段顺序。"""

        stages: list[str] = []
        for raw in self._audit_replay():
            payload = raw.get("payload", {})
            if raw.get("component") != "recovery" or raw.get("action") != "progress_stage":
                continue
            if payload.get("op_id") != op_id:
                continue
            stages.append(str(payload.get("stage", "")))
        return stages

    def _crosscheck_journal(self, progress: Progress) -> tuple[bool, dict]:
        logged = self._journal_stages(progress.op_id)
        entered = [record["stage"] for record in progress.stages]
        # 落盘阶段必须是流水轨迹的前缀；流水允许更多（落盘文档可能刚好少一条），
        # 但已落盘的阶段一条都不能缺、顺序也不能变。开工即崩（两边都无阶段）也算一致。
        match = (logged[: len(entered)] == entered) and (bool(entered) or not logged)
        check = _check(
            "journal_crosscheck",
            match,
            "落盘阶段与审计流水一致" if match else "落盘阶段与审计流水对不上",
            logged_stages=logged,
            stored_stages=entered,
        )
        return match, check

    def _verify_stages(self, progress: Progress) -> list[dict]:
        """按当前设备读回核对*已完成*阶段的物理闭环条件。"""

        d = self._devices
        op = progress.operation
        completed = [record["stage"] for record in progress.stages[:-1]]
        checks: list[dict] = []

        if op == "charge":
            ch = progress.latest_state().get("charger", {})
            if "align" in completed:
                tol = self._config.charger.align_tolerance_mm
                dev = abs(float(progress.params.get("hopper_measured_mm", 0.0)) - float(progress.params.get("hopper_target_mm", 0.0)))
                checks.append(_check("align_retained", d.charger.is_aligned() and dev <= tol, f"对位偏差 {dev:.1f}mm / 公差 {tol}mm", deviation_mm=round(dev, 2)))
            if "regulate" in completed:
                checks.append(_check("pressure_settled", d.collector.settled(), f"压力 {d.collector.pressure_pa:.1f}Pa 目标带 ±{self._config.collector.pressure_band_pa}Pa", pressure_pa=round(d.collector.pressure_pa, 2)))
            if "flap" in completed:
                # 翻板一旦开过，闩锁应已置位；收尾闭环则翻板已关。
                checks.append(_check("flap_latched", d.flap.latch.is_engaged(), f"翻板闩锁置位={d.flap.latch.is_engaged()}", flap_open=d.flap.is_open()))
            if "feed" in completed:
                target_t = self._config.charger.coal_target_t
                checks.append(_check("feed_quantity", d.charger.feed_complete(), f"已给 {d.charger.snapshot().fed_t:.2f}t / 目标 {target_t}t", fed_t=round(d.charger.snapshot().fed_t, 3), target_t=target_t))

        elif op == "push":
            if "align" in completed:
                tol = self._config.guide.align_tolerance_mm
                dev = abs(float(progress.params.get("guide_measured_mm", 0.0)) - float(progress.params.get("guide_target_mm", 0.0)))
                checks.append(_check("align_retained", d.guide.is_aligned() and dev <= tol, f"对位偏差 {dev:.1f}mm / 公差 {tol}mm", deviation_mm=round(dev, 2)))
            if "lock" in completed:
                checks.append(_check("apron_locked", d.guide.is_locked(), f"导焦栅锁定={d.guide.is_locked()}"))
            if "extend" in completed:
                target = float(progress.params.get("stroke_target_mm", self._config.pusher.stroke_baseline_mm))
                checks.append(_check("stroke_reached", d.pusher.ram_mm >= target - 1e-6, f"杆位 {d.pusher.ram_mm:.1f}mm / 目标 {target}mm", ram_mm=round(d.pusher.ram_mm, 2)))
            if "retract" in completed:
                checks.append(_check("ram_home", d.pusher.ram_mm <= 1e-6, f"杆位 {d.pusher.ram_mm:.1f}mm 已归零"))

        elif op == "quench":
            if "receive" in completed:
                checks.append(_check("coke_received", True, f"红焦已在熄焦车（{progress.oven}）"))
            if "travel" in completed:
                checks.append(_check("at_tower", d.quench.at_tower(), f"走行 {d.quench.snapshot().travelled_m:.1f}m / {self._config.quench.travel_distance_m}m"))
            if "spray" in completed:
                checks.append(_check("spray_quantity", d.quench.spray.complete(), f"已喷淋 {d.quench.spray.elapsed_s:.1f}s / {self._config.quench.spray_duration_s}s", elapsed_s=round(d.quench.spray.elapsed_s, 2)))

        elif op == "reverse":
            if "switch" in completed:
                single = d.valves.waste_open ^ d.valves.gas_open
                checks.append(_check("valve_exclusive", single, f"废气阀={d.valves.waste_open} 煤气阀={d.valves.gas_open}（必须互斥单一）"))
            if "confirm" in completed:
                checks.append(_check("temperature_confirmed", d.temperature.confirmed(), f"炉温 {d.temperature.value_c:.1f}°C / 目标带 ±{self._config.heating.temp_band_c}°C"))

        elif op == "clean":
            if "release" in completed:
                checks.append(_check("latch_released", not d.door.latch.is_engaged(), f"闩锁压紧={d.door.latch.is_engaged()}"))
            if "open" in completed:
                checks.append(_check("door_open", d.door.door_open(), f"炉门打开={d.door.door_open()}"))
            if "clean" in completed:
                checks.append(_check("clean_progress", d.door.progress() >= 1.0, f"清扫进度 {d.door.progress():.2f}"))
            if "mark" in completed:
                checks.append(_check("clean_marked", d.door.is_cleaned(), f"已标记清扫完成={d.door.is_cleaned()}"))

        return checks

    def _make_decision_id(self, progress: Progress, drift: bool, checks: tuple[dict, ...], journal_match: bool, hash_valid: bool) -> str:
        basis = {
            "op_id": progress.op_id,
            "operation": progress.operation,
            "interrupted_stage": progress.interrupted_stage,
            "stages": [r["stage"] for r in progress.stages],
            "current_config": fingerprint_config(self._config),
            "drift": drift,
            "checks": checks,
            "journal_match": journal_match,
            "hash_valid": hash_valid,
        }
        return digest(basis)

    def boot_analyze(self) -> RecoveryDecision:
        """启动时分析落盘进度，给出恢复结论；同时把设备恢复到记录的进度量。"""

        with self._lock:
            self._decision = None
            self._reverified = False
            if not self._store.exists():
                decision = RecoveryDecision(
                    outcome=NONE, operation="", oven="", op_id="", interrupted_stage=None,
                    completed_stages=(), policy="", rationale="无中断编排", config_drift=False,
                    checks=(), journal_match=True, progress_hash_valid=True,
                    decision_id=digest({"outcome": NONE}), ts=self._clock.now(),
                )
                self._decision = decision
                return decision

            try:
                progress = self._store.load()
            except ValueError:
                decision = RecoveryDecision(
                    outcome=BLOCKED, operation="", oven="", op_id="", interrupted_stage=None,
                    completed_stages=(), policy="", rationale="进度哈希链核对失败，禁止自动恢复，需人工确认",
                    config_drift=False, checks=(_check("progress_hash", False, "哈希链断裂"),),
                    journal_match=False, progress_hash_valid=False,
                    decision_id=digest({"outcome": BLOCKED, "reason": "hash"}), ts=self._clock.now(),
                )
                self._decision = decision
                self._publish_decision(decision)
                return decision

            assert progress is not None
            self._progress = progress
            self.tracker.bind(progress, mode="resume")

            journal_match, journal_check = self._crosscheck_journal(progress)
            drift = progress.config_fingerprint != fingerprint_config(self._config)

            # 先把内存设备恢复到记录的进度量，读回核对才有现场依据。
            self._rehydrate(progress)
            checks = tuple(self._verify_stages(progress))
            checks = (*checks, journal_check)
            physical_ok = all(item["ok"] for item in checks if item["check"] != "journal_crosscheck")

            # 开工即崩、一条阶段都没落：没有任何物理动作，按首阶段策略处理。
            stage = progress.interrupted_stage or stages_for(progress.operation)[0]
            policy = policy_for(progress.operation, stage)
            rationale = rationale_for(progress.operation, stage)
            if not progress.stages:
                rationale = f"开工后、首个阶段动作前即中断，无任何物理动作：{rationale}"
            completed = tuple(record["stage"] for record in progress.stages[:-1])
            hash_valid = True

            if not journal_match:
                outcome = BLOCKED
                rationale = "进度与审计流水不一致，禁止自动恢复，需人工核对"
            elif drift:
                outcome = REVERIFY
                rationale = f"停机期间参数被改动（迁移），续做前必须重新核对：{rationale}"
            elif not physical_ok:
                outcome = REVERIFY
                rationale = f"现场读回与已完成阶段不符，需重新核对后再{ '续做' if policy == CONTINUE else '退回'}：{rationale}"
            else:
                outcome = CONTINUE if policy == CONTINUE else ROLLBACK

            decision = RecoveryDecision(
                outcome=outcome,
                operation=progress.operation,
                oven=progress.oven,
                op_id=progress.op_id,
                interrupted_stage=stage or None,
                completed_stages=completed,
                policy=policy,
                rationale=rationale,
                config_drift=drift,
                checks=checks,
                journal_match=journal_match,
                progress_hash_valid=hash_valid,
                decision_id=self._make_decision_id(progress, drift, checks, journal_match, hash_valid),
                ts=self._clock.now(),
            )
            self._decision = decision
            self._publish_decision(decision)
            return decision

    def _publish_decision(self, decision: RecoveryDecision) -> None:
        self._bus.publish(
            Event.of(
                "recovery",
                "recovery",
                "recovery_decision",
                oven=decision.oven,
                op_id=decision.op_id,
                severity="alarm" if decision.outcome == BLOCKED else "warning",
                message=f"恢复判定：{decision.outcome}（{decision.operation}/{decision.interrupted_stage}）{decision.rationale}",
                decision_id=decision.decision_id,
                outcome=decision.outcome,
                granularity="stage",
                interrupted_stage=decision.interrupted_stage or "",
                policy=decision.policy,
                config_drift=decision.config_drift,
                journal_match=decision.journal_match,
            )
        )

    def _rehydrate(self, progress: Progress) -> None:
        """按进度量恢复设备内存态（不产生物理动作、不写动作计数）。"""

        d = self._devices
        if progress.operation == "charge":
            runners.restore_charge(d.charger, d.collector, d.flap, progress)
        elif progress.operation == "push":
            runners.restore_push(d.pusher, d.guide, progress)
        elif progress.operation == "quench":
            runners.restore_quench(d.quench, progress)
        elif progress.operation == "reverse":
            runners.restore_reverse(d.valves, d.temperature, progress)
        elif progress.operation == "clean":
            runners.restore_clean(d.door, progress)

    # ------------------------------------------------------------------ 核对门

    def reverify(self) -> RecoveryDecision:
        """停机改参或现场不符后，按当前参数与现场读回重新核对一遍。"""

        with self._lock:
            if self._progress is None or self._decision is None:
                raise ValueError("没有待核对的中断编排")
            if self._decision.outcome == BLOCKED:
                raise ValueError("进度已被判定为禁止自动恢复，不能重新核对")
            progress = self._progress
            checks = tuple(self._verify_stages(progress))
            journal_match, journal_check = self._crosscheck_journal(progress)
            checks = (*checks, journal_check)
            physical_ok = all(item["ok"] for item in checks if item["check"] != "journal_crosscheck")
            drift_now = progress.config_fingerprint != fingerprint_config(self._config)
            stage = progress.interrupted_stage or stages_for(progress.operation)[0]
            policy = policy_for(progress.operation, stage)
            rationale = rationale_for(progress.operation, stage)
            if not (journal_match and physical_ok):
                outcome = BLOCKED if not journal_match else REVERIFY
            else:
                self._reverified = True
                outcome = CONTINUE if policy == CONTINUE else ROLLBACK
                rationale = (
                    f"参数迁移已按新参数重新核对通过（原指纹 {progress.config_fingerprint[:8]} → "
                    f"新指纹 {fingerprint_config(self._config)[:8]}）：{rationale}"
                    if drift_now
                    else f"现场重新核对通过：{rationale}"
                )
            decision = RecoveryDecision(
                outcome=outcome,
                operation=progress.operation,
                oven=progress.oven,
                op_id=progress.op_id,
                interrupted_stage=stage or None,
                completed_stages=tuple(record["stage"] for record in progress.stages[:-1]),
                policy=policy,
                rationale=rationale,
                config_drift=drift_now,
                checks=checks,
                journal_match=journal_match,
                progress_hash_valid=True,
                decision_id=self._make_decision_id(progress, drift_now, checks, journal_match, True),
                ts=self._clock.now(),
            )
            self._decision = decision
            self._bus.publish(
                Event.of(
                    "recovery",
                    "recovery",
                    "recovery_reverified",
                    oven=progress.oven,
                    op_id=progress.op_id,
                    severity="notice" if outcome in (CONTINUE, ROLLBACK) else "warning",
                    message=rationale,
                    decision_id=decision.decision_id,
                    outcome=outcome,
                    config_drift=drift_now,
                )
            )
            return decision

    # ------------------------------------------------------------------ 执行

    def _require_ready(self, expected_outcome: str) -> Progress:
        if self._progress is None or self._decision is None:
            raise ValueError("没有待恢复的中断编排")
        if self._decision.outcome == BLOCKED:
            raise ValueError("恢复已被禁止（进度或审计核对失败），需人工介入")
        if self._decision.outcome == REVERIFY and not self._reverified:
            raise ValueError("停机期间参数已改动或现场未核对，必须先重新核对才能续做")
        if expected_outcome == ROLLBACK and self._decision.policy != ROLLBACK:
            raise ValueError(f"阶段 {self._decision.interrupted_stage} 策略为续做，不允许退回")
        if expected_outcome == CONTINUE and self._decision.policy != CONTINUE:
            raise ValueError(f"阶段 {self._decision.interrupted_stage} 策略为退回，不允许续做")
        return self._progress

    def resume(self, dt: float = 1.0) -> dict:
        """按阶段策略续做（幂等）；只有续做型阶段可调用。"""

        with self._lock:
            progress = self._require_ready(CONTINUE)
            decision_id = self._decision.decision_id
            d = self._devices
            self.tracker.bind(progress, mode="resume")
            if progress.operation == "charge":
                report = runners.resume_charge(d.charger, d.collector, d.flap, progress, float(progress.params.get("dt", dt)), self.tracker)
            elif progress.operation == "push":
                speed = float(progress.params.get("speed_mm_s", self._config.pusher.speed_default_mm_s))
                report = runners.resume_push(d.pusher, d.guide, progress, speed, float(progress.params.get("dt", dt)), self.tracker)
            elif progress.operation == "quench":
                report = runners.resume_quench(d.quench, progress, float(progress.params.get("dt", dt)), self.tracker)
            elif progress.operation == "reverse":
                report = runners.resume_reverse(d.reversal, d.valves, d.temperature, progress, self.tracker)
            else:
                report = runners.resume_clean(d.door, progress, float(progress.params.get("dt", dt)), self.tracker)
            self._finish_run(report, "resume")
            return {"op_id": progress.op_id, "decision_id": decision_id, **report}

    def rollback(self) -> dict:
        """按阶段策略退回（幂等）；只有前置的退回型阶段可调用。"""

        with self._lock:
            progress = self._require_ready(ROLLBACK)
            decision_id = self._decision.decision_id
            d = self._devices
            self.tracker.bind(progress, mode="rollback")
            if progress.operation == "charge":
                report = runners.rollback_charge(d.charger, d.collector, d.flap, progress, self.tracker)
            elif progress.operation == "push":
                report = runners.rollback_push(d.pusher, d.guide, progress, self.tracker)
            elif progress.operation == "quench":
                report = runners.rollback_quench(d.quench, progress, self.tracker)
            elif progress.operation == "clean":
                report = runners.rollback_clean(d.door, progress, self.tracker)
            else:
                raise ValueError(f"编排 {progress.operation} 没有退回型阶段")
            self._finish_run(report, "rollback")
            return {"op_id": progress.op_id, "decision_id": decision_id, **report}

    def _finish_run(self, report: dict, kind: str) -> None:
        assert self._decision is not None
        assert self._progress is not None
        if report.get("ok"):
            self.tracker.finish()
            self._bus.publish(
                Event.of(
                    "recovery",
                    "recovery",
                    f"recovery_{kind}d",
                    oven=self._progress.oven,
                    op_id=self._progress.op_id,
                    severity="notice",
                    message=f"中断编排已{'续做完成' if kind == 'resume' else '退回复位'}",
                    decision_id=self._decision.decision_id,
                )
            )
            self._progress = None
            self._reverified = False
            self._decision = None
        else:
            # 续做自身条件不满足：保留进度，重新分析，交操作工决定。
            self._bus.publish(
                Event.of(
                    "recovery",
                    "recovery",
                    f"recovery_{kind}_incomplete",
                    oven=self._progress.oven,
                    op_id=self._progress.op_id,
                    severity="warning",
                    message="恢复动作未满足条件，进度保留",
                    decision_id=self._decision.decision_id,
                    violations=list(report.get("violations", [])),
                )
            )
            self.boot_analyze()

    # ------------------------------------------------------------------ 展示

    def status(self) -> dict:
        """供控制台展示：进度 + 判定 + 阶段策略表（与审计同口径）。"""

        with self._lock:
            if self._decision is None or self._decision.outcome == NONE:
                return {"pending": False, "outcome": NONE, "granularity": "stage"}
            decision = self._decision.as_dict()
            policy_table: dict[str, dict[str, str]] = {}
            if self._progress is not None:
                for stage in stages_for(self._progress.operation):
                    policy_table[stage] = {
                        "policy": policy_for(self._progress.operation, stage),
                        "rationale": rationale_for(self._progress.operation, stage),
                    }
            return {
                "pending": True,
                "granularity": "stage",
                "reverified": self._reverified,
                "decision": decision,
                "stage_policy": policy_table,
                "stages_total": len(stages_for(self._progress.operation)) if self._progress else 0,
                "stages_entered": len(self._progress.stages) if self._progress else 0,
            }

    def guard_new_operation(self) -> None:
        """新编排开工前的联锁：有未闭环中断时必须先恢复，禁止叠加操作。"""

        with self._lock:
            if self._store.exists():
                from ..errors import InterlockError

                outcome = self._decision.outcome if self._decision else "unknown"
                raise InterlockError(
                    f"存在未恢复的中断编排（结论 {outcome}），必须先续做或退回，禁止叠加新操作",
                    component="recovery",
                )
