"""编排恢复：阶段定义与共享类型。

恢复的判定粒度在本模块定死为**阶段（phase）**，不是步骤：续做、回退、人工
确认都按阶段策略走，步骤只是阶段内部的审计留痕。每个阶段在阶段表里登记一条
固定策略：

``resume``   幂等收敛型。阶段动作可重复下发，已到位的部分不再产生效果，只把
             现场收敛到阶段目标（调压、对位、推进、走行、喷淋都属此类）。
``rollback`` 半途不可信。恢复时先执行阶段的回退动作，把设备退回该阶段入口的
             安全态，然后把整个阶段重做一遍（如清扫头必须全程连续清扫）。
``manual``   危险中间态。停机时该阶段可能停在两个互斥设备的切换缝里，平台不
             自行决定，必须由操作工核对确认后续做（如废气阀/煤气阀换向）。

阶段函数只与 :class:`PhaseContext` 打交道：离散动作用 ``ctx.step`` 留痕并刷
检查点，长循环用 ``ctx.tick`` 刷检查点。函数本身不直接落盘、不直接发审计，
保证同一套阶段函数既能被平台带持久化运行，也能被无持久化的本地校验运行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import CokeError

# 续做粒度标识，控制台与审计共用这一个常量，口径定死。
GRANULARITY = "phase"

POLICY_RESUME = "resume"
POLICY_ROLLBACK = "rollback"
POLICY_MANUAL = "manual"
POLICIES = (POLICY_RESUME, POLICY_ROLLBACK, POLICY_MANUAL)

# 阶段状态：未开始 / 进行中（有半途检查点）/ 已完成 / 联锁失败。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# 恢复决定动作。
ACTION_NONE = "none"
ACTION_COMPLETE = "already_complete"
ACTION_RESUME = "resume"
ACTION_ROLLBACK = "rollback"
ACTION_MANUAL = "manual_ack_required"
ACTION_BLOCKED = "blocked"

OPERATIONS = ("charge", "push", "quench", "reverse", "clean")


class RecoveryBlocked(CokeError):
    """续做决定为阻塞，续做被拒绝（配置迁移、证据不符、待人工确认）。"""

    def __init__(self, message: str, *, decision: dict | None = None) -> None:
        super().__init__(message, component="recovery")
        self.decision = decision or {}


class CrashInjection(RuntimeError):
    """演练/验证用：模拟平台在编排半途掉电。

    生产路径不会构造它；它必须是普通运行时异常而非联锁异常，确保抛出时不会
    被当成"设备被联锁拒绝"写进审计——掉电就是掉电。
    """


@dataclass
class PhaseResult:
    """阶段函数执行结果。

    离散步骤已通过 ``ctx.step`` 即时留痕，这里只回报是否撞联锁、要并入报告的
    附加字段，以及（失败时）最后一次现场片段。
    """

    violation: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    fragment: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseContext:
    """阶段函数的执行上下文：碳化室号、步长、操作参数与设备表。"""

    oven: str
    dt: float
    params: dict[str, Any]
    machines: dict[str, Any]
    # 由运行器绑定：离散步骤留痕 / 循环心跳。阶段函数不允许自己落盘。
    on_step: Callable[[dict, dict], None] | None = None
    on_tick: Callable[[dict], None] | None = None

    def step(self, step: dict, fragment: dict) -> None:
        """记录一个离散步骤（即时落检查点并写审计），并更新现场片段。"""

        if self.on_step is not None:
            self.on_step(step, fragment)

    def tick(self, fragment: dict) -> None:
        """长循环心跳：只刷半途检查点，不产生审计步骤。"""

        if self.on_tick is not None:
            self.on_tick(fragment)


@dataclass(frozen=True)
class Phase:
    """阶段表中的一条：键名、固定策略、执行函数与该阶段产出的审计动作。"""

    key: str
    policy: str
    run: Callable[[PhaseContext], PhaseResult]
    actions: tuple[str, ...]
    # rollback 策略专用：把设备退回阶段入口安全态；回退步骤同样经 ctx.step
    # 留痕落审计，然后整个阶段从头重做。
    rollback: Callable[[PhaseContext], None] | None = None
    # 操作开始前的一次性联锁前提（续做时不再重复判定）。
    guard: Callable[[PhaseContext], str | None] | None = None

    def __post_init__(self) -> None:
        if self.policy not in POLICIES:
            raise ValueError(f"未知阶段策略: {self.policy}")
        if self.policy == POLICY_ROLLBACK and self.rollback is None:
            raise ValueError(f"阶段 {self.key} 声明 rollback 却没有回退函数")
