"""加热换向顺序的阶段表：阀组切换 → 炉温确认。

阀组切换是全平台唯一一个 ``manual`` 阶段：废气阀与煤气阀互斥，停机时可能
停在"废气阀已关、煤气阀未开"的切换缝里，平台无权自行决定接着开哪一个，
必须由操作工核对阀位后确认续做。炉温确认是幂等收敛型，重新回读一次即可。
"""

from __future__ import annotations

from ..heating.machine import HeatingReversal
from ..heating.temperature import OvenTemperature
from ..heating.valvetrain import ValveTrain
from ..recovery.phase import POLICY_MANUAL, POLICY_RESUME, Phase, PhaseContext, PhaseResult


def _guard(ctx: PhaseContext) -> str | None:
    reversal: HeatingReversal = ctx.machines["reversal"]
    return None if reversal.due() else "reversal_not_due"


def _switch(ctx: PhaseContext) -> PhaseResult:
    valves: ValveTrain = ctx.machines["valves"]
    # 方向必须幂等：半途掉电后两阀可能都关着，方向取自已记下的切换路径；
    # 全新执行时按当前阀位决定。只允许操作工确认后续做，平台不自行猜方向。
    direction = ctx.params.get("direction")
    if direction not in ("waste_to_gas", "gas_to_waste"):
        direction = "waste_to_gas" if valves.waste_open else "gas_to_waste"
        ctx.params["direction"] = direction

    if direction == "waste_to_gas":
        if valves.waste_open:
            valves.close_waste_gas()
            ctx.step(
                {"component": "heating", "action": "close_waste_gas", "detail": {"open": valves.waste_open}},
                {"valves": {"waste_open": valves.waste_open, "gas_open": valves.gas_open}},
            )
        if not valves.gas_open:
            valves.open_gas()
            ctx.step(
                {"component": "heating", "action": "open_gas", "detail": {"open": valves.gas_open}},
                {"valves": {"waste_open": valves.waste_open, "gas_open": valves.gas_open}},
            )
    else:
        if valves.gas_open:
            valves.close_gas()
            ctx.step(
                {"component": "heating", "action": "close_gas", "detail": {"open": valves.gas_open}},
                {"valves": {"waste_open": valves.waste_open, "gas_open": valves.gas_open}},
            )
        if not valves.waste_open:
            valves.open_waste_gas()
            ctx.step(
                {"component": "heating", "action": "open_waste_gas", "detail": {"open": valves.waste_open}},
                {"valves": {"waste_open": valves.waste_open, "gas_open": valves.gas_open}},
            )
    return PhaseResult(extra={"direction": direction})


def _confirm(ctx: PhaseContext) -> PhaseResult:
    reversal: HeatingReversal = ctx.machines["reversal"]
    temperature: OvenTemperature = ctx.machines["temperature"]
    temperature.update(ctx.params["temperature_c"])
    confirmed = temperature.confirmed()
    ctx.step(
        {
            "component": "heating",
            "action": "confirm_temperature",
            "detail": {"temperature_c": round(temperature.value_c, 2), "confirmed": confirmed},
        },
        {"temperature": {"temperature_c": round(temperature.value_c, 2), "confirmed": confirmed}},
    )
    if confirmed:
        # 幂等：只在本操作尚未记过换向时累加，避免重启后把同一次换向记两次。
        if not ctx.params.get("reversal_marked"):
            reversal.mark_reversed()
            ctx.params["reversal_marked"] = True
        return PhaseResult(extra={"reversal_count": reversal.count()})
    return PhaseResult(violation="temp_not_confirmed")


PHASES: tuple[Phase, ...] = (
    Phase("switch", POLICY_MANUAL, _switch, ("close_waste_gas", "open_gas", "close_gas", "open_waste_gas"),
          guard=_guard),
    Phase("confirm", POLICY_RESUME, _confirm, ("confirm_temperature",)),
)


def reverse_heating(
    reversal: HeatingReversal,
    valves: ValveTrain,
    temperature: OvenTemperature,
    oven: str,
    next_temperature_c: float,
) -> dict:
    """执行一次换向（无持久化的本地校验入口），返回带步骤与违约项的报告。"""

    from .local import run_local

    return run_local(
        "reverse",
        oven,
        1.0,
        {"temperature_c": next_temperature_c},
        {"reversal": reversal, "valves": valves, "temperature": temperature},
    )
