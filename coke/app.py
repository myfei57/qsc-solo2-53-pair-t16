"""平台装配与控制循环。

``Platform`` 把各组件按真实调用链接起来，并暴露四件事：操作编排（装煤、
推焦、熄焦、换向、炉门清扫）、控制循环、并发仲裁探测和状态快照。控制循环
每个周期做三件事：把集气管压力调向目标带、按推焦联动时序驱动除尘挡板、
采集趋势；换向与推焦这类跨模块动作都由编排一次性走完并写审计。
"""

from __future__ import annotations

import dataclasses
import threading

from .audit.log import AuditLog
from .audit.record import AuditRecord
from .charger.machine import ChargingCar
from .clock import Clock, SystemClock
from .config import PlatformConfig, default_config
from .door.latch import DoorLatch
from .door.machine import DoorCleaner
from .dust.damper import DustDamper
from .dust.schedule import DamperSchedule
from .dust.snapshot import DustSnapshot
from .dust.system import DustSystem
from .errors import CokeError
from .eventbus import Event, EventBus
from .gas.collector import CollectorMain
from .gas.flap import BridgeFlap
from .gas.latch import FlapLatch
from .guide.machine import GuideCar
from .heating.machine import HeatingReversal
from .heating.snapshot import HeatingSnapshot
from .heating.temperature import OvenTemperature
from .heating.valvetrain import ValveTrain
from .ids import IdFactory
from .ns.registry import NamespaceRegistry
from .ns.topology import Topology, build_default_topology
from .pusher.machine import PusherMachine
from .pusher.speed import SpeedArbiter
from .pusher.stroke import StrokeBaseline
from .quench.machine import QuenchCar
from .recovery import CrashInjection, RecoveryBlocked, RecoveryRunner
from .store.journal import JsonlJournal
from .store.jsonfile import JsonStore
from .telemetry import TrendBuffer

DEFAULT_UNIT = "battery1"
DEFAULT_OVEN = "oven01"


class Platform:
    """焦炉推装熄顺序控制平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str = DEFAULT_UNIT,
    ) -> None:
        self.config = config or default_config()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.audit = AuditLog(self.clock, self.bus, JsonlJournal(store.root, "audit", segment_limit=512))
        self.namespaces = NamespaceRegistry(unit)
        self.topology: Topology = build_default_topology()
        self.trend = TrendBuffer(period_s=5.0)
        self.ids = IdFactory("op")

        self.pusher = PusherMachine(self.config.pusher)
        self.stroke = StrokeBaseline(
            self.config.pusher.stroke_baseline_mm, self.config.pusher.stroke_tolerance_mm
        )
        self.speed = SpeedArbiter(
            self.config.pusher.speed_min_mm_s,
            self.config.pusher.speed_max_mm_s,
            self.config.pusher.speed_default_mm_s,
        )
        self.guide = GuideCar(self.config.guide)
        self.charger = ChargingCar(self.config.charger)
        self.quench_car = QuenchCar(self.config.quench)
        self.collector = CollectorMain(self.config.collector)
        self.flap = BridgeFlap(FlapLatch())
        self.door = DoorCleaner(self.config.door, DoorLatch(self.config.door.latch_travel_mm))
        self.reversal = HeatingReversal(self.config.heating, self.clock)
        self.valves = ValveTrain()
        self.temperature = OvenTemperature(self.config.heating)
        self.dust = DustSystem()
        self.damper = DustDamper(self.config.dust.damper_open_deg, self.config.dust.damper_rate_deg_per_s)
        self.schedule = DamperSchedule(self.config.dust.linkage_lag_s)
        self.recovery = RecoveryRunner(
            self.config, self.clock, self.store, self.audit, self.bus, self._machines_for
        )

        self._cycles = 0
        self._operations = 0
        self._lock = threading.RLock()
        self._register_namespaces()
        self._restore()

    def _register_namespaces(self) -> None:
        """登记现场设备命名空间（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("battery", "pusher", "pusher-machine"),
            ("battery", "guide", "guide-car"),
            ("battery", "charger", "charging-car"),
            ("battery", "quench", "quench-car"),
            ("battery", "collector", "collector-main"),
            ("battery", "bridge_flap", "bridge-flap"),
            ("battery", "door", "door-cleaner"),
            ("battery", "reversal", "reversing-valve"),
            ("battery", "dust", "dust-damper"),
        ):
            self.namespaces.register(area, device, kind)

    def _machines_for(self, operation: str) -> dict:
        """按操作返回阶段函数需要的设备表（与恢复映射口径一致）。"""

        if operation == "charge":
            return {"charger": self.charger, "collector": self.collector, "flap": self.flap}
        if operation == "push":
            return {"pusher": self.pusher, "guide": self.guide}
        if operation == "quench":
            return {"quench": self.quench_car}
        if operation == "reverse":
            return {"reversal": self.reversal, "valves": self.valves, "temperature": self.temperature}
        if operation == "clean":
            return {"door": self.door}
        raise KeyError(operation)

    def _restore(self) -> None:
        """从落盘审计回放换向计时，并恢复未收尾编排的续做决定。"""

        try:
            records = self.audit.to_records(self.audit.replay_from_journal(limit=100_000))
        except CokeError:
            records = []
        reversals = [item for item in records if item.action == "reverse_complete"]
        # 只接受不晚于当前时钟的回放记录；可推进时钟重置后，上一轮留下的未来
        # 时间戳属于过期状态，不能拿它把换向间隔计时顶到当前时刻之前。
        if reversals and reversals[-1].ts <= self.clock.now():
            self.reversal.mark_reversed(reversals[-1].ts)
        state = self.store.read("platform_state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))
        # 编排半途恢复：重建设备现场、双源核对，决定落审计但不自动续做。
        self.recovery.restore()

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform_state",
            {"operations": self._operations, "cycles": self._cycles, "unit": self.unit},
        )

    def _settle_report(self, op_id: str, operation: str, report: dict) -> dict:
        """把运行器报告补上操作标识、计数与汇总字段。

        步骤事件、开始/结束事件、联锁告警与续做决定都由恢复运行器发布；这里
        只补推焦、熄焦、换向报告特有的汇总字段，并在计数落盘后返回合并报告。
        """

        violations = list(report.get("violations", []))
        summary = {
            "op_id": op_id,
            "operation": operation,
            "oven": str(report.get("oven", "")),
            "ok": bool(report.get("ok")),
            "violations": violations,
            "resumed": bool(report.get("resumed")),
        }
        self._operations += 1
        self._persist()
        return {**report, **summary}

    def start(self) -> dict:
        """启动平台：启动除尘风机并核对拓扑。"""

        with self._lock:
            self.dust.start()
            report = self.topology.verify(set())
            return {
                "fan_running": self.dust.is_running(),
                "topology_missing": report["missing"],
                "operations": self._operations,
            }

    def stop(self) -> dict:
        """停运平台：停除尘风机。"""

        with self._lock:
            self.dust.stop()
            return {"fan_running": self.dust.is_running(), "operations": self._operations}

    def charge(
        self,
        oven: str = DEFAULT_OVEN,
        hopper_target_mm: float = 0.0,
        hopper_measured_mm: float = 0.0,
        dt: float = 1.0,
    ) -> dict:
        """装煤编排：煤斗对位 → 调压 → 开翻板 → 给煤（阶段级检查点）。"""

        with self._lock:
            op_id = self.ids.next()
            report = self.recovery.start(
                op_id,
                "charge",
                oven,
                {"hopper_target_mm": hopper_target_mm, "hopper_measured_mm": hopper_measured_mm},
                dt,
            )
            return self._settle_report(op_id, "charge", report)

    def push(
        self,
        oven: str = DEFAULT_OVEN,
        guide_target_mm: float = 0.0,
        guide_measured_mm: float = 0.0,
        stroke_target_mm: float | None = None,
        speed_mm_s: float | None = None,
        dt: float = 1.0,
    ) -> dict:
        """推焦编排：导焦栅对位 → 锁定 → 推焦 → 退杆；同时触发除尘联动。"""

        with self._lock:
            op_id = self.ids.next()
            target = self.stroke.baseline_mm if stroke_target_mm is None else stroke_target_mm
            speed = speed_mm_s or self.config.pusher.speed_default_mm_s
            arbitration_box: dict = {}

            def prepare() -> None:
                self.speed.request("auto_cycle", speed, priority=50)
                arbitration_box["outcome"] = self.speed.resolve()
                self.schedule.start_link(self.clock.now())

            report = self.recovery.start(
                op_id,
                "push",
                oven,
                {
                    "guide_target_mm": guide_target_mm,
                    "guide_measured_mm": guide_measured_mm,
                    "stroke_target_mm": target,
                    "speed_mm_s": speed,
                },
                dt,
                prepare=prepare,
            )
            report["speed_arbitration"] = arbitration_box.get("outcome", self.speed.resolve()).as_dict()
            report["stroke_check"] = self.stroke.check(self.pusher.ram_mm)
            return self._settle_report(op_id, "push", report)

    def quench(self, oven: str = DEFAULT_OVEN, dt: float = 1.0) -> dict:
        """熄焦编排：接焦 → 走行 → 喷淋（阶段级检查点）。"""

        with self._lock:
            op_id = self.ids.next()
            report = self.recovery.start(op_id, "quench", oven, {}, dt)
            report["travel_eta_s"] = self.quench_car.plan.eta_s()
            return self._settle_report(op_id, "quench", report)

    def reverse(self, oven: str = DEFAULT_OVEN, temperature_c: float | None = None) -> dict:
        """换向编排：废气阀 → 煤气阀 → 炉温确认（阀组切换需人工确认）。"""

        with self._lock:
            op_id = self.ids.next()
            target_temp = self.config.heating.temp_target_c if temperature_c is None else temperature_c
            report = self.recovery.start(
                op_id, "reverse", oven, {"temperature_c": target_temp}, 1.0
            )
            report["offset_c"] = self.temperature.offset_c()
            return self._settle_report(op_id, "reverse", report)

    def clean(self, oven: str = DEFAULT_OVEN, dt: float = 1.0) -> dict:
        """炉门清扫编排：松开闩锁 → 开炉门 → 清扫 → 复位闩锁（半途回退重扫）。"""

        with self._lock:
            op_id = self.ids.next()
            report = self.recovery.start(op_id, "clean", oven, {}, dt)
            return self._settle_report(op_id, "clean", report)

    def resume_operation(self) -> dict:
        """按落审计的续做决定接着做（或先回退再重做）。"""

        with self._lock:
            progress = self.recovery.progress()
            if progress is None:
                raise RecoveryBlocked("没有未收尾操作")
            op_id = progress.op_id
            operation = progress.operation
            report = self.recovery.resume()
            if operation == "push":
                report["stroke_check"] = self.stroke.check(self.pusher.ram_mm)
            if operation == "reverse":
                report["offset_c"] = self.temperature.offset_c()
            if operation == "quench":
                report["travel_eta_s"] = self.quench_car.plan.eta_s()
            return self._settle_report(op_id, operation, report)

    def acknowledge_recovery(self, phase: str | None = None) -> dict:
        """操作工核对现场后确认危险阶段可续做。"""

        with self._lock:
            return self.recovery.ack(phase)

    def reverify_recovery(self) -> dict:
        """参数迁移后的重新核对，通过后解除迁移阻塞。"""

        with self._lock:
            return self.recovery.reverify()

    def recovery_status(self) -> dict:
        """返回续做决定与编排进度（供控制台展示与落审计核对）。"""

        with self._lock:
            return self.recovery.status()

    def abandon_recovery(self) -> dict:
        """人工核对确认后废弃未收尾操作。"""

        with self._lock:
            removed = self.recovery.clear()
            return {"abandoned": removed}

    def crash_drill(
        self,
        operation: str = "push",
        oven: str = DEFAULT_OVEN,
        phase: str = "extend",
        action: str = "@tick",
        **params: object,
    ) -> dict:
        """演练：把指定操作跑到目标阶段（步骤或循环心跳）处模拟掉电。

        ``action="@tick"``（默认）表示在该阶段的循环心跳处掉电，设备会停在
        真正的半程（如推焦杆半程、清扫进行中）；给具体动作名则在该离散步骤
        落审计后掉电。演练在进程内完成：拦截掉电异常，保留已落盘的进度与
        检查点，供随后构造的新平台实例验证重启恢复。
        """

        with self._lock:
            op_id = self.ids.next()
            if action == "@tick":
                self.recovery.arm_tick_crash(op_id, phase)
            else:
                self.recovery.arm_crash(op_id, phase, action)
            crashed: dict
            try:
                if operation == "push":
                    self.recovery.start(op_id, "push", oven, {
                        "guide_target_mm": float(params.get("guide_target_mm", 0.0)),
                        "guide_measured_mm": float(params.get("guide_measured_mm", 0.0)),
                        "stroke_target_mm": float(params.get("stroke_target_mm", self.stroke.baseline_mm)),
                        "speed_mm_s": float(params.get("speed_mm_s", self.config.pusher.speed_default_mm_s)),
                    }, 1.0)
                elif operation == "quench":
                    self.recovery.start(op_id, "quench", oven, {}, 1.0)
                elif operation == "charge":
                    self.recovery.start(op_id, "charge", oven, {
                        "hopper_target_mm": float(params.get("hopper_target_mm", 0.0)),
                        "hopper_measured_mm": float(params.get("hopper_measured_mm", 0.0)),
                    }, 1.0)
                elif operation == "clean":
                    self.recovery.start(op_id, "clean", oven, {}, 1.0)
                elif operation == "reverse":
                    self.recovery.start(op_id, "reverse", oven, {
                        "temperature_c": float(params.get(
                            "temperature_c", self.config.heating.temp_target_c
                        )),
                    }, 1.0)
                else:
                    raise RecoveryBlocked(f"未知演练操作 {operation!r}")
            except CrashInjection as error:
                crashed = {"op_id": op_id, "operation": operation, "at": f"{phase}/{action}", "error": str(error)}
            else:
                raise RecoveryBlocked("演练注入点未触发，操作已正常跑完")
            status = self.recovery.status()
            return {"crashed": crashed, "recovery": status}

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        with self._lock:
            self._cycles += 1
            moment = self.clock.now()
            before = self.collector.pressure_pa
            pressure = self.collector.regulate(dt)
            linkage = self.schedule.due(moment)
            target_deg = self.damper.open_deg if self.schedule.links() else 0.0
            angle = self.damper.drive_to(target_deg, dt)
            if self.damper.at_open():
                self.schedule.clear()
            self.trend.append("collector_pressure", moment, pressure)
            self.trend.append("damper_angle", moment, angle)
            self._persist()
            return {
                "cycle": self._cycles,
                "pressure_pa": round(pressure, 2),
                "pressure_delta": round(pressure - before, 3),
                "settled": self.collector.settled(),
                "damper_deg": round(angle, 2),
                "linkage_due": linkage,
                "linkage_elapsed_s": round(self.schedule.elapsed_s(moment), 2),
            }

    def concurrency_probe(self, rounds: int = 24) -> dict:
        """用真实并行调用探测推焦速度仲裁的收敛结果。

        两个线程分别代表自动循环与手动控制台，同时向推焦杆提交速度。探测只
        读取最终提交值，用来确认共享执行器始终收敛到唯一值。
        """

        if rounds < 1:
            raise ValueError("探测轮数必须为正")

        def worker(writer: str, speed: float, priority: int) -> None:
            for _ in range(rounds):
                self.speed.request(writer, speed, priority)

        threads = [
            threading.Thread(target=worker, args=("auto_cycle", self.config.pusher.speed_max_mm_s, 50)),
            threading.Thread(target=worker, args=("manual_console", self.config.pusher.speed_min_mm_s, 80)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcome = self.speed.resolve()
        self.pusher.set_speed(outcome.speed_mm_s)
        return {
            "rounds": rounds,
            "winner": outcome.winner,
            "committed_mm_s": round(outcome.speed_mm_s, 2),
            "contenders": outcome.contenders,
            "writers": self.speed.writers(),
            "committed_on_machine": round(self.pusher.committed_speed(), 2),
        }

    def calibrate_stroke(self, value_mm: float) -> dict:
        """重新标定推焦行程基线，返回标定前后的差值。"""

        if value_mm <= 0:
            raise CokeError("标定值必须为正", component="pusher")
        before = self.stroke.baseline_mm
        after = self.stroke.calibrate(value_mm)
        self.bus.publish(
            Event.of(
                "pusher",
                "pusher",
                "stroke_recalibrated",
                severity="notice",
                baseline_before_mm=before,
                baseline_after_mm=after,
                epoch=self.stroke.epoch,
            )
        )
        return {"before_mm": before, "after_mm": after, "epoch": self.stroke.epoch}

    def disturb_collector(self, delta_pa: float) -> dict:
        """模拟一次集气管扰动，返回扰动后压力。"""

        before = self.collector.pressure_pa
        after = self.collector.disturb(delta_pa)
        return {"before_pa": round(before, 2), "after_pa": round(after, 2), "settled": self.collector.settled()}

    def note_error(self, error: CokeError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        payload = error.as_record()
        record = self.audit.record(
            AuditRecord.create(
                self.clock.now(),
                "",
                payload["component"],
                "error",
                severity="warning",
                message=payload["message"],
            )
        )
        return record

    def reset_state(self) -> bool:
        """清掉落盘的平台计数与编排进度（交付前重新标定用）。"""

        removed = False
        if self.store.exists("platform_state"):
            removed = self.store.remove("platform_state")
        self.recovery.clear()
        return removed

    def heating_snapshot(self) -> HeatingSnapshot:
        """返回加热换向状态快照。"""

        return HeatingSnapshot(
            waste_gas_open=self.valves.waste_open,
            gas_open=self.valves.gas_open,
            temperature_c=self.temperature.value_c,
            confirmed=self.temperature.confirmed(),
            reversals=self.reversal.count(),
            seconds_to_due=self.reversal.seconds_to_due(),
        )

    def dust_snapshot(self) -> DustSnapshot:
        """返回除尘状态快照。"""

        return DustSnapshot(
            fan_running=self.dust.is_running(),
            damper_deg=self.damper.angle_deg,
            damper_open=self.damper.at_open(),
            linkage_pending=self.schedule.links() > 0,
            links=self.schedule.links(),
        )

    def topology_summary(self) -> dict:
        """返回工艺链拓扑摘要。"""

        return self.topology.summary()

    def namespaces_describe(self) -> dict:
        """返回命名空间摘要。"""

        return self.namespaces.describe()

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        return {
            "platform": self.config.name,
            "unit": self.unit,
            "operations": self._operations,
            "cycles": self._cycles,
            "now": round(self.clock.now(), 3),
            "utc": self.clock.utc_iso(),
            "pusher": self.pusher.snapshot().as_dict(),
            "speed": {"committed_mm_s": round(self.speed.committed(), 2)},
            "stroke": {
                "baseline_mm": self.stroke.baseline_mm,
                "tolerance_mm": self.stroke.tolerance_mm,
                "epoch": self.stroke.epoch,
            },
            "guide": self.guide.snapshot().as_dict(),
            "charger": self.charger.snapshot().as_dict(),
            "quench": self.quench_car.snapshot().as_dict(),
            "gas": {
                "pressure_pa": round(self.collector.pressure_pa, 2),
                "target_pa": self.collector.limits.pressure_target_pa,
                "settled": self.collector.settled(),
                "flap_deg": round(self.flap.angle_deg, 2),
                "flap_open": self.flap.is_open(),
                "latch_engaged": self.flap.latch.is_engaged(),
                "latch_cycles": self.flap.latch.cycles(),
                "latch_resets": self.flap.latch.resets(),
            },
            "door": {**self.door.snapshot().as_dict(), "latch_cycles": self.door.latch.cycles()},
            "heating": self.heating_snapshot().as_dict(),
            "dust": self.dust_snapshot().as_dict(),
            "dust_starts": self.dust.starts(),
            "recovery": self.recovery.status(),
            "audit": self.audit.summary(),
            "ids_issued": self.ids.issued(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 拓扑 + 命名空间 + 趋势）。"""

        return {
            "status": self.status(),
            "topology": self.topology_summary(),
            "namespaces": self.namespaces_describe(),
            "trend": self.trend.snapshot(),
        }


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str = DEFAULT_UNIT,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = dataclasses.replace(base, storage_root=storage_root)
    return Platform(base, clock=clock, unit=unit)
