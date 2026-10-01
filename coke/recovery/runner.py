"""带持久化检查点的编排运行器：启动、续做、回退、人工确认、迁移复核。

运行器是阶段表（怎么动设备）、进度文档（自证走到哪）、审计流水（第三方
凭据）和配置指纹（参数有没有被动过）四者之间唯一的协调者：

* ``start``    新操作：写开始事件、落初始进度、从头跑阶段表；
* ``restore``  重启后：读进度 → 按检查点重建设备现场 → 双源核对 → 生成续做
               决定，决定原样落审计且写回进度，但不自动执行；
* ``resume``   按已有决定执行：``resume`` 从当前阶段幂等续做、``rollback``
               先退回阶段入口再整段重做、``manual`` 必须先 ``ack``；
* ``reverify`` 参数迁移后的重新核对，通过才解除 ``blocked``；
* ``ack``      操作工对危险阶段（阀组切换）的显式确认。

续做的幂等性由两层保证：阶段函数本身按现场状态决定是否下发动作（已到位不
重发），运行器在阶段级跳过所有已完成阶段——已经做过的步骤不会再做一遍。
"""

from __future__ import annotations

from typing import Any, Callable

from ..audit.log import AuditLog
from ..audit.record import AuditRecord
from ..clock import Clock
from ..config import PlatformConfig
from ..eventbus import Event
from ..store.jsonfile import JsonStore
from .decision import ACK_EVENT, DECISION_EVENT, REVERIFY_EVENT, decide, reverify
from .evidence import cross_check
from .fingerprint import fingerprint_config
from .phase import (
    ACTION_BLOCKED,
    ACTION_COMPLETE,
    ACTION_MANUAL,
    ACTION_RESUME,
    ACTION_ROLLBACK,
    CrashInjection,
    PhaseContext,
    PhaseResult,
    RecoveryBlocked,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from ..sequence.registry import phase_keys, phase_table
from .progress import PROGRESS_DOC, OperationProgress


class RecoveryRunner:
    """编排进度与恢复的协调者。"""

    def __init__(
        self,
        config: PlatformConfig,
        clock: Clock,
        store: JsonStore,
        audit: AuditLog,
        bus: Any,
        machines_of: Callable[[str], dict[str, Any]],
    ) -> None:
        self._config = config
        self._clock = clock
        self._store = store
        self._audit = audit
        self._bus = bus
        self._machines_of = machines_of
        # 演练用：(op_id, phase_key, action) -> 剩余触发次数。
        self._crashes: dict[tuple[str, str, str], int] = {}
        self._progress: OperationProgress | None = None
        self._last_decision: dict | None = None

    # ------------------------------------------------------------------ 读取

    def has_progress(self) -> bool:
        """是否存在未收尾操作（内存判定，供操作互斥与控制台展示使用）。"""

        return self._progress is not None

    def progress(self) -> OperationProgress | None:
        return self._progress

    def status(self) -> dict:
        """供控制台展示与审计核对的恢复状态。"""

        if self._progress is None:
            return {
                "granularity": "phase",
                "active": False,
                "progress": None,
                "decision": self._last_decision,
            }
        progress = self._progress
        keys = phase_keys(progress.operation)
        return {
            "granularity": "phase",
            "active": True,
            "progress": {
                "op_id": progress.op_id,
                "operation": progress.operation,
                "oven": progress.oven,
                "status": progress.status,
                "started_ts": progress.started_ts,
                "updated_ts": progress.updated_ts,
                "violation": progress.violation,
                "phases": {key: progress.phase_status(key) for key in keys},
                "current_phase": progress.current_phase(keys),
                "checkpoint": progress.checkpoint,
                "acks": list(progress.acks),
                "reverified": progress.reverified,
                "config_fingerprint": progress.config_fingerprint,
            },
            "decision": self._last_decision,
        }

    # ---------------------------------------------------------------- 新操作

    def start(
        self,
        op_id: str,
        operation: str,
        oven: str,
        params: dict[str, Any],
        dt: float,
        prepare: Callable[[], None] | None = None,
    ) -> dict:
        """启动一个新操作并从头执行；已有未收尾操作时拒绝。"""

        if self.has_progress() and self._progress.status != STATUS_FAILED:
            raise RecoveryBlocked(
                f"存在未收尾操作 {self._progress.op_id}（{self._progress.operation}），禁止开始新操作",
                decision=self._last_decision or {},
            )
        if self.has_progress() and self._progress.status == STATUS_FAILED:
            # 上一操作已联锁失败并留在 failed；新操作显式接管前先废弃它。
            old = self._progress
            self._publish(
                "audit", "recovery_superseded", severity="warning",
                message=f"失败操作 {old.op_id} 被新操作接管",
                op_id=old.op_id, oven=old.oven, operation=old.operation,
            )
            self._clear()
        if prepare is not None:
            prepare()

        now = self._clock.now()
        progress = OperationProgress(
            op_id=op_id,
            operation=operation,
            oven=oven,
            params=dict(params),
            dt=dt,
            config_fingerprint=fingerprint_config(self._config),
            started_ts=now,
            updated_ts=now,
        )
        self._progress = progress
        self._publish("audit", f"{operation}_started", severity="notice",
                      message=f"{operation} 开始", op_id=op_id, oven=oven)
        self._save(progress)

        return self._run(progress, start_index=0)

    # ------------------------------------------------------------------ 恢复

    def restore(self) -> dict | None:
        """重启恢复：重建现场、双源核对、生成并落审计续做决定（不执行动作）。

        无进度文档时返回 ``None``。审计里已有结束事件的残留进度会在核对后
        清掉，平台回到无半途状态。
        """

        raw = self._store.read(PROGRESS_DOC)
        if raw is None:
            return None
        progress = OperationProgress.from_dict(raw)
        if progress is None:
            raise RecoveryBlocked("编排进度文档无法识别，需人工核对后清除", decision={})
        self._progress = progress

        try:
            table = phase_table(progress.operation)
        except KeyError:
            raise RecoveryBlocked(f"未知操作类型 {progress.operation!r}，需人工核对", decision={})

        self._rebuild_machines(progress)
        records = self._replay()
        evidence = cross_check(progress, records)
        decision = decide(
            progress, table, evidence,
            config_match=progress.config_fingerprint == fingerprint_config(self._config),
            now=self._clock.now(),
        )
        progress.last_decision = decision
        self._last_decision = decision
        self._save(progress)
        self._publish(
            "audit", DECISION_EVENT, severity="notice",
            message=f"恢复决定：{decision['action']}",
            op_id=progress.op_id, oven=progress.oven,
            decision_id=decision["decision_id"], decision_action=decision["action"],
            operation=progress.operation, granularity=decision["granularity"],
            phase=str(decision.get("phase") or ""), policy=str(decision.get("policy") or ""),
        )

        if decision["action"] == ACTION_COMPLETE:
            # 审计已证明操作收尾，残留进度直接清除，不重复发结束事件。
            self._clear()
        elif progress.is_finished([phase.key for phase in table]):
            # 阶段全部完成、只差结束事件（收尾瞬间掉电）：恢复决定续做收尾。
            decision["note"] = "all_phases_done_finish_only"
        return decision

    def resume(self) -> dict:
        """按最近一次续做决定执行恢复。"""

        progress = self._require_progress()
        self._refresh_decision()
        decision = self._last_decision
        assert decision is not None
        action = decision["action"]

        if action == ACTION_COMPLETE:
            failed = self._progress is not None and self._progress.status == STATUS_FAILED
            violation = self._progress.violation if failed else None
            self._clear()
            return self._terminal_report(
                progress,
                ok=not failed,
                steps=[],
                violations=[violation] if violation else [],
                resumed=True,
            )
        if action in (ACTION_BLOCKED, ACTION_MANUAL):
            raise RecoveryBlocked(self._block_message(decision), decision=decision)
        if action == ACTION_ROLLBACK:
            return self._run_with_rollback(progress, decision)
        if action == ACTION_RESUME:
            return self._run_resume(progress, decision)
        raise RecoveryBlocked(f"未知续做动作 {action}", decision=decision)

    def ack(self, phase_key: str | None = None) -> dict:
        """操作工确认危险阶段可续做；确认落进度并写审计。"""

        progress = self._require_progress()
        self._refresh_decision()
        decision = self._last_decision
        assert decision is not None
        target = phase_key or decision.get("phase")
        table = phase_table(progress.operation)
        keys = [phase.key for phase in table]
        if target not in keys:
            raise RecoveryBlocked(f"阶段 {target!r} 不在 {progress.operation} 的阶段表内", decision=decision)
        added = progress.ack_phase(str(target))
        self._save(progress)
        self._publish(
            "audit", ACK_EVENT, severity="notice",
            message=f"操作工确认阶段 {target} 可续做",
            op_id=progress.op_id, oven=progress.oven, phase=str(target), first=added,
        )
        self._refresh_decision()
        return {"acked": target, "added": added, "decision": self._last_decision}

    def reverify(self) -> dict:
        """停机期间参数被改过后的迁移重新核对。"""

        progress = self._require_progress()
        result = reverify(self._config, progress, self._clock.now())
        progress.reverified = result
        self._save(progress)
        self._publish(
            "audit", REVERIFY_EVENT,
            severity="notice" if result["passed"] else "warning",
            message="参数迁移重新核对通过" if result["passed"] else "参数迁移重新核对未通过",
            op_id=progress.op_id, oven=progress.oven,
            passed=result["passed"], fingerprint_after=result["fingerprint_after"],
        )
        if not result["passed"]:
            self._refresh_decision()
            raise RecoveryBlocked(
                f"参数迁移后重新核对未通过，禁止续做：{','.join(result['problems'])}",
                decision=self._last_decision or {},
            )
        self._refresh_decision()
        return {"reverified": result, "decision": self._last_decision}

    def clear(self) -> bool:
        """清除未收尾进度（人工核对确认废弃该操作后使用），并写审计。"""

        progress = self._progress
        if progress is None:
            return False
        self._publish(
            "audit", "recovery_abandoned", severity="warning",
            message="人工核对后废弃未收尾操作",
            op_id=progress.op_id, oven=progress.oven, operation=progress.operation,
        )
        self._clear()
        return True

    def arm_crash(self, op_id: str, phase_key: str, action: str, times: int = 1) -> None:
        """演练用：在指定阶段步骤处模拟掉电 ``times`` 次。"""

        self._crashes[(op_id, phase_key, action)] = max(1, times)

    def arm_tick_crash(self, op_id: str, phase_key: str, times: int = 1) -> None:
        """演练用：在指定阶段的循环心跳处模拟掉电（动作名固定为 ``@tick``）。"""

        self._crashes[(op_id, phase_key, "@tick")] = max(1, times)

    # --------------------------------------------------------------- 内部执行

    def _run_resume(self, progress: OperationProgress, decision: dict) -> dict:
        table = phase_table(progress.operation)
        keys = [phase.key for phase in table]
        resume_from = decision.get("resume_from")
        if resume_from is None:
            # 所有阶段都完成，只差收尾事件。
            self._publish_resume(progress, decision, phase_key="", action="finish")
            return self._finish(progress, steps=[])
        start_index = keys.index(resume_from)
        self._publish_resume(progress, decision, phase_key=resume_from, action="resume")
        return self._run(progress, start_index=start_index, resumed=True)

    def _run_with_rollback(self, progress: OperationProgress, decision: dict) -> dict:
        table = phase_table(progress.operation)
        keys = [phase.key for phase in table]
        phase_key = str(decision["phase"])
        phase = table[keys.index(phase_key)]
        start_index = keys.index(phase_key)

        steps: list[dict] = []
        ctx = self._bind_context(progress, phase_key, steps)
        assert phase.rollback is not None
        phase.rollback(ctx)
        # 回退后该阶段从"进行中"重新开始，已登记动作清空（回退步骤本身另计）。
        state = progress.set_phase(phase_key, STATUS_RUNNING)
        state.logged = [item for item in state.logged if item == "rollback_clean"]
        progress.violation = None
        self._save(progress)
        self._publish_resume(progress, decision, phase_key=phase_key, action="rollback")

        report = self._run(progress, start_index=start_index, resumed=True, prior_steps=steps)
        return report

    def _run(
        self,
        progress: OperationProgress,
        start_index: int,
        resumed: bool = False,
        prior_steps: list[dict] | None = None,
    ) -> dict:
        table = phase_table(progress.operation)
        steps: list[dict] = list(prior_steps or [])
        machines = self._machines_of(progress.operation)

        # 操作前提交互前提只在全新启动时判一次；续做时前提属于"启动时已核对过
        # 的旧状态"（如换向间隔在原平台已用掉），不再重判。
        if not resumed and start_index == 0:
            for phase in table:
                if phase.guard is None:
                    continue
                guard_ctx = PhaseContext(progress.oven, progress.dt, progress.params, machines)
                violation = phase.guard(guard_ctx)
                if violation:
                    return self._fail(progress, steps, violation, resumed=resumed)
                break

        extras: dict[str, Any] = {}
        for index, phase in enumerate(table[start_index:], start=start_index):
            ctx = self._bind_context(progress, phase.key, steps)
            progress.set_phase(phase.key, STATUS_RUNNING)
            self._save(progress)
            result: PhaseResult = phase.run(ctx)
            extras.update(result.extra)
            if result.violation:
                return self._fail(progress, steps, result.violation, resumed=resumed)
            progress.set_phase(phase.key, STATUS_DONE)
            self._save(progress)

        return self._finish(progress, steps=steps, extras=extras, resumed=resumed)

    def _fail(
        self, progress: OperationProgress, steps: list[dict], violation: str, resumed: bool
    ) -> dict:
        """联锁失败：记告警、把进度留在 failed，不再自动续做。"""

        progress.status = STATUS_FAILED
        progress.violation = violation
        self._save(progress)
        self._publish(
            "audit", "interlock_rejected", severity="alarm",
            message=f"{progress.operation} 被拒绝：{violation}",
            op_id=progress.op_id, oven=progress.oven, violation=violation,
        )
        report = {
            "oven": progress.oven,
            "ok": False,
            "steps": steps,
            "violations": [violation],
            "resumed": resumed,
        }
        self._publish_complete(progress, ok=False, violation=violation)
        return report

    def _finish(
        self,
        progress: OperationProgress,
        steps: list[dict],
        extras: dict[str, Any] | None = None,
        resumed: bool = False,
    ) -> dict:
        report = {
            "oven": progress.oven,
            "ok": True,
            "steps": steps,
            "violations": [],
            "resumed": resumed,
            **(extras or {}),
        }
        self._publish_complete(progress, ok=True)
        self._clear()
        return report

    def _terminal_report(
        self, progress: OperationProgress, ok: bool, steps: list, violations: list, resumed: bool
    ) -> dict:
        return {
            "oven": progress.oven,
            "ok": ok,
            "steps": steps,
            "violations": violations,
            "resumed": resumed,
        }

    # ----------------------------------------------------------------- 绑定

    def _bind_context(self, progress: OperationProgress, phase_key: str, steps: list[dict]) -> PhaseContext:
        machines = self._machines_of(progress.operation)

        def on_step(step: dict, fragment: dict) -> None:
            steps.append(step)
            action = str(step.get("action", ""))
            detail = step.get("detail", {})
            flat = {
                key: value
                for key, value in detail.items()
                if not isinstance(value, (dict, list))
                and key not in ("oven", "op_id", "severity", "message")
            }
            self._publish(
                str(step.get("component", "audit")), action, severity="notice",
                oven=progress.oven, op_id=progress.op_id, **flat,
            )
            progress.log_action(phase_key, action)
            self._merge_checkpoint(progress, fragment)
            progress.updated_ts = self._clock.now()
            self._save(progress)
            # 审计事件与检查点都落盘后才模拟掉电，复现"步骤已持久化即停机"。
            self._maybe_crash(progress, phase_key, action)

        def on_tick(fragment: dict) -> None:
            self._maybe_crash(progress, phase_key, "@tick", fragment)
            self._merge_checkpoint(progress, fragment)
            progress.updated_ts = self._clock.now()
            self._save(progress)

        return PhaseContext(
            oven=progress.oven, dt=progress.dt, params=progress.params,
            machines=machines, on_step=on_step, on_tick=on_tick,
        )

    def _maybe_crash(
        self, progress: OperationProgress, phase_key: str, action: str, fragment: dict | None = None
    ) -> None:
        key = (progress.op_id, phase_key, action)
        remaining = self._crashes.get(key, 0)
        if remaining <= 0:
            return
        remaining -= 1
        if remaining:
            self._crashes[key] = remaining
        else:
            self._crashes.pop(key, None)
        # 抛异常前先把当前现场片段与检查点落盘，模拟"最后一个心跳已持久化后掉电"。
        if fragment:
            self._merge_checkpoint(progress, fragment)
        progress.updated_ts = self._clock.now()
        self._save(progress)
        raise CrashInjection(f"模拟掉电：{progress.op_id} {phase_key}/{action}")

    @staticmethod
    def _merge_checkpoint(progress: OperationProgress, fragment: dict) -> None:
        for key, value in fragment.items():
            if isinstance(value, dict):
                merged = dict(progress.checkpoint.get(key, {})) if isinstance(
                    progress.checkpoint.get(key), dict
                ) else {}
                merged.update(value)
                progress.checkpoint[key] = merged
            else:
                progress.checkpoint[key] = value

    # ------------------------------------------------------------- 设备重建

    def _rebuild_machines(self, progress: OperationProgress) -> None:
        """按检查点把本操作涉及的设备重建到半途现场。"""

        machines = self._machines_of(progress.operation)
        mapping = _RESTORE_MAP.get(progress.operation, {})
        for key, device_name in mapping.items():
            fragment = progress.checkpoint.get(key)
            if isinstance(fragment, dict):
                getattr(machines[device_name], "restore_state")(fragment)

    # ------------------------------------------------------------- 决定刷新

    def _refresh_decision(self) -> dict:
        progress = self._require_progress()
        table = phase_table(progress.operation)
        evidence = cross_check(progress, self._replay())
        decision = decide(
            progress, table, evidence,
            config_match=progress.config_fingerprint == fingerprint_config(self._config),
            now=self._clock.now(),
        )
        progress.last_decision = decision
        self._last_decision = decision
        self._save(progress)
        return decision

    # ------------------------------------------------------------- 审计与存储

    def _replay(self) -> list[AuditRecord]:
        try:
            return self._audit.to_records(self._audit.replay_from_journal(limit=100_000))
        except Exception:
            return []

    def _publish(self, topic: str, action: str, severity: str = "notice", **payload: Any) -> None:
        self._bus.publish(Event.of(topic, topic, action, severity=severity, **payload))

    def _publish_resume(self, progress: OperationProgress, decision: dict, phase_key: str, action: str) -> None:
        self._publish(
            "audit", "recovery_resume", severity="notice",
            message=f"按决定 {decision['decision_id']} {action}",
            op_id=progress.op_id, oven=progress.oven,
            decision_id=decision["decision_id"], decision_action=decision["action"],
            phase=phase_key, mode=action,
        )

    def _publish_complete(self, progress: OperationProgress, ok: bool, violation: str | None = None) -> None:
        payload: dict[str, Any] = {
            "op_id": progress.op_id,
            "operation": progress.operation,
            "oven": progress.oven,
            "ok": ok,
            "violations": [violation] if violation else [],
        }
        self._publish(
            "audit", f"{progress.operation}_complete",
            severity="notice" if ok else "warning",
            message=f"{progress.operation} 完成" if ok else f"{progress.operation} 未完成",
            **payload,
        )

    def _save(self, progress: OperationProgress) -> None:
        self._store.write(PROGRESS_DOC, progress.as_dict())

    def _clear(self) -> None:
        self._progress = None
        self._last_decision = None
        if self._store.exists(PROGRESS_DOC):
            self._store.remove(PROGRESS_DOC)

    def _require_progress(self) -> OperationProgress:
        if self._progress is None:
            raise RecoveryBlocked("没有未收尾操作，无需续做", decision={})
        return self._progress

    @staticmethod
    def _block_message(decision: dict) -> str:
        action = decision["action"]
        if action == ACTION_MANUAL:
            return f"阶段 {decision.get('phase')} 需要操作工现场核对确认后才能续做"
        reasons = ", ".join(decision.get("reasons", [])) or "未通过续做核对"
        return f"续做被阻塞：{reasons}"


# 每个操作涉及设备的"检查点片段键 -> 设备名"映射；恢复只重建本操作相关设备。
_RESTORE_MAP: dict[str, dict[str, str]] = {
    "charge": {"charger": "charger", "collector": "collector", "flap": "flap"},
    "push": {"pusher": "pusher", "guide": "guide"},
    "quench": {"quench": "quench"},
    "reverse": {"valves": "valves", "temperature": "temperature"},
    "clean": {"door": "door"},
}
