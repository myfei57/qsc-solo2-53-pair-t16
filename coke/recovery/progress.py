"""编排进度的落盘与核对。

进度是一份原子替换的 JSON 文档（复用 :class:`coke.store.jsonfile.JsonStore`
的"临时文件 + ``os.replace``"语义），崩溃时要么是上一阶段的完整内容，要么
是本阶段的完整内容，不会出现半截进度。

文档自身带哈希链（``prev_hash`` / ``content_hash``），任何一条阶段记录被改
都能在恢复核对时发现；同时落一份开工时的配置指纹，停机期间参数被改属于
"迁移"，恢复前必须重新核对（见 :class:`coke.recovery.manager.RecoveryManager`）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..config import PlatformConfig, config_to_mapping
from ..eventbus import Event, EventBus
from ..store.jsonfile import JsonStore

DOC_ID = "recovery_progress"

#: 阶段进度记录结构。``state`` 是按组件分组的进度量（给煤量、杆位……）。
StageRecord = dict[str, Any]


def _canonical(payload: Any) -> str:
    """稳定序列化：键排序、无多余空白，保证同内容同哈希。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(payload: Any) -> str:
    """对任意可 JSON 化内容求一个短哈希（审计与进度核对共用）。"""

    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]


def fingerprint_config(config: PlatformConfig) -> str:
    """求生效配置的指纹；停机期间参数改动会让指纹不一致。"""

    return digest(config_to_mapping(config)["limits"])


@dataclass(frozen=True)
class Progress:
    """一次中断编排的落盘进度。"""

    op_id: str
    operation: str
    oven: str
    params: dict
    config_fingerprint: str
    started_ts: float
    stages: list[StageRecord] = field(default_factory=list)
    version: int = 1
    prev_hash: str = ""
    content_hash: str = ""

    @property
    def stage(self) -> str | None:
        """当前（最近进入的）阶段；无阶段记录时为 ``None``。"""

        return self.stages[-1]["stage"] if self.stages else None

    @property
    def interrupted_stage(self) -> str | None:
        """中断时所处阶段：最后一条阶段记录就是尚未确认完成的阶段。"""

        return self.stage

    def latest_state(self) -> dict:
        """合并出最新一份按组件分组的进度量（后写覆盖先写）。"""

        merged: dict[str, dict] = {}
        for record in self.stages:
            for component, values in record.get("state", {}).items():
                merged.setdefault(component, {}).update(dict(values))
        return merged

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "op_id": self.op_id,
            "operation": self.operation,
            "oven": self.oven,
            "params": dict(self.params),
            "config_fingerprint": self.config_fingerprint,
            "started_ts": self.started_ts,
            "stages": [dict(record) for record in self.stages],
            "prev_hash": self.prev_hash,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Progress":
        return cls(
            version=int(raw.get("version", 1)),
            op_id=str(raw.get("op_id", "")),
            operation=str(raw.get("operation", "")),
            oven=str(raw.get("oven", "")),
            params=dict(raw.get("params", {})),
            config_fingerprint=str(raw.get("config_fingerprint", "")),
            started_ts=float(raw.get("started_ts", 0.0)),
            stages=[dict(item) for item in raw.get("stages", [])],
            prev_hash=str(raw.get("prev_hash", "")),
            content_hash=str(raw.get("content_hash", "")),
        )


class ProgressStore:
    """进度文档的原子读写与哈希链核对。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    def exists(self) -> bool:
        return self._store.exists(DOC_ID)

    def load(self) -> Progress | None:
        """读取并校验哈希链；文档不存在返回 ``None``，被篡改抛 :class:`ValueError`。"""

        raw = self._store.read(DOC_ID)
        if not isinstance(raw, dict):
            return None
        progress = Progress.from_dict(raw)
        if not self.verify(progress):
            raise ValueError("恢复进度哈希链核对失败，文档可能被改动")
        return progress

    def save(self, progress: Progress) -> None:
        """原子落盘一份带哈希链的进度。"""

        self._store.write(DOC_ID, progress.to_dict())

    def clear(self) -> bool:
        """编排闭环后删除进度文档。"""

        return self._store.remove(DOC_ID)

    @staticmethod
    def verify(progress: Progress) -> bool:
        """逐条重算哈希链，任何一环对不上即判失败。"""

        expected_prev = ""
        for index, record in enumerate(progress.stages):
            if record.get("prev_hash", "") != expected_prev:
                return False
            payload = {key: value for key, value in record.items() if key not in ("prev_hash", "content_hash")}
            if digest(payload) != record.get("content_hash", ""):
                return False
            expected_prev = str(record["content_hash"])
        # 文档尾部哈希必须等于最后一条阶段记录（无阶段记录则为空串）。
        return progress.content_hash == (expected_prev if progress.stages else "")


class ProgressTracker:
    """编排侧的进度记录器：进入阶段、刷新进度量、闭环清除。

    每次阶段进入、进度量刷新与闭环都会向事件总线发布 ``recovery`` 事件，
    审计流水因此与落盘进度同源；恢复核对拿流水里的阶段轨迹对照落盘文档。
    """

    def __init__(self, store: ProgressStore, bus: EventBus | None = None) -> None:
        self._store = store
        self._bus = bus
        self._progress: Progress | None = None
        self.mode = "fresh"

    @property
    def progress(self) -> Progress | None:
        return self._progress

    def _publish(self, action: str, severity: str = "notice", **payload: Any) -> None:
        if self._bus is None or self._progress is None:
            return
        self._bus.publish(
            Event.of(
                "recovery",
                "recovery",
                action,
                oven=self._progress.oven,
                op_id=self._progress.op_id,
                severity=severity,
                mode=self.mode,
                **payload,
            )
        )

    def start(
        self,
        op_id: str,
        operation: str,
        oven: str,
        params: Mapping[str, Any],
        config_fingerprint: str,
        started_ts: float,
    ) -> Progress:
        """开工时建立进度文档（还没有任何阶段记录）。"""

        self.mode = "fresh"
        self._progress = Progress(
            op_id=op_id,
            operation=operation,
            oven=oven,
            params=dict(params),
            config_fingerprint=config_fingerprint,
            started_ts=started_ts,
        )
        self._store.save(self._progress)
        self._publish("progress_started", operation=operation, config_fingerprint=config_fingerprint)
        return self._progress

    def enter(self, stage: str, ts: float | None = None, state: Mapping[str, Any] | None = None) -> None:
        """进入一个阶段：追加一条带哈希链的阶段记录并原子落盘。"""

        if self._progress is None:
            raise ValueError("尚未开工，不能记录阶段进度")
        prev_hash = self._progress.stages[-1]["content_hash"] if self._progress.stages else ""
        record: StageRecord = {
            "stage": stage,
            "ts": ts if ts is not None else 0.0,
            "state": {component: dict(values) for component, values in (state or {}).items()},
            "prev_hash": prev_hash,
        }
        record["content_hash"] = digest(
            {key: value for key, value in record.items() if key not in ("prev_hash", "content_hash")}
        )
        self._progress = Progress(
            op_id=self._progress.op_id,
            operation=self._progress.operation,
            oven=self._progress.oven,
            params=self._progress.params,
            config_fingerprint=self._progress.config_fingerprint,
            started_ts=self._progress.started_ts,
            stages=[*self._progress.stages, record],
            version=self._progress.version,
            prev_hash=prev_hash,
            content_hash=record["content_hash"],
        )
        self._store.save(self._progress)
        self._publish("progress_stage", stage=stage, stage_index=len(self._progress.stages) - 1)

    def checkpoint(self, state: Mapping[str, Any]) -> None:
        """刷新当前阶段的进度量（阶段内部幂等续做用），仍原子落盘。"""

        if self._progress is None or not self._progress.stages:
            raise ValueError("尚未进入阶段，不能刷新进度量")
        stages = [dict(record) for record in self._progress.stages]
        stages[-1]["state"] = {component: dict(values) for component, values in state.items()}
        prev_hash = stages[-1]["prev_hash"]
        stages[-1]["content_hash"] = digest(
            {key: value for key, value in stages[-1].items() if key not in ("prev_hash", "content_hash")}
        )
        # 后续阶段还没出现；改最后一条不会破坏链尾，content_hash 随之更新。
        self._progress = Progress(
            op_id=self._progress.op_id,
            operation=self._progress.operation,
            oven=self._progress.oven,
            params=self._progress.params,
            config_fingerprint=self._progress.config_fingerprint,
            started_ts=self._progress.started_ts,
            stages=stages,
            version=self._progress.version,
            prev_hash=prev_hash,
            content_hash=stages[-1]["content_hash"],
        )
        self._store.save(self._progress)

    def finish(self) -> None:
        """编排正常闭环，清除进度文档。"""

        self._publish("progress_finished")
        self._progress = None
        self._store.clear()
        self.mode = "fresh"

    def bind(self, progress: Progress, *, mode: str = "fresh") -> None:
        """恢复分析后把已落盘进度绑定到本记录器（不重写文档）。"""

        self._progress = progress
        self.mode = mode
