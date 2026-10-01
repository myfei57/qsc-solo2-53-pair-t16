"""加热换向顺序：废气阀 → 煤气阀 → 炉温确认（两相间互斥切换）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..heating.machine import HeatingReversal
from ..heating.temperature import OvenTemperature
from ..heating.valvetrain import ValveTrain

if TYPE_CHECKING:
    from ..recovery.progress import ProgressTracker


def reverse_heating(
    reversal: HeatingReversal,
    valves: ValveTrain,
    temperature: OvenTemperature,
    oven: str,
    next_temperature_c: float,
    tracker: "ProgressTracker | None" = None,
) -> dict:
    """执行一次换向，返回带步骤与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []

    if not reversal.due():
        violations.append("reversal_not_due")
        if tracker is not None:
            tracker.finish()
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

    if tracker is not None:
        # 切换方向在动作前就定死并落盘，崩溃在两阀都关闭的瞬间也能判出该切到哪一侧。
        planned_direction = "waste_to_gas" if valves.waste_open else "gas_to_waste"
        tracker.enter(
            "switch",
            state={"heating": {"waste_open": valves.waste_open, "gas_open": valves.gas_open, "temperature_c": temperature.value_c, "direction": planned_direction}},
        )
    if valves.waste_open:
        valves.close_waste_gas()
        steps.append({"component": "heating", "action": "close_waste_gas", "detail": {"open": valves.waste_open}})
        if tracker is not None:
            # 关阀后、开阀前是最危险的两阀全关窗口，必须在此留检查点，
            # 重启后才能据此幂等地只补"开煤气阀"。
            tracker.checkpoint({"heating": {"waste_open": False, "gas_open": False,
                                            "temperature_c": temperature.value_c, "direction": "waste_to_gas"}})
        valves.open_gas()
        steps.append({"component": "heating", "action": "open_gas", "detail": {"open": valves.gas_open}})
        direction = "waste_to_gas"
    else:
        valves.close_gas()
        steps.append({"component": "heating", "action": "close_gas", "detail": {"open": valves.gas_open}})
        if tracker is not None:
            tracker.checkpoint({"heating": {"waste_open": False, "gas_open": False,
                                            "temperature_c": temperature.value_c, "direction": "gas_to_waste"}})
        valves.open_waste_gas()
        steps.append({"component": "heating", "action": "open_waste_gas", "detail": {"open": valves.waste_open}})
        direction = "gas_to_waste"

    if tracker is not None:
        tracker.enter(
            "confirm",
            state={"heating": {"waste_open": valves.waste_open, "gas_open": valves.gas_open, "temperature_c": temperature.value_c, "direction": direction}},
        )
    temperature.update(next_temperature_c)
    confirmed = temperature.confirmed()
    steps.append(
        {
            "component": "heating",
            "action": "confirm_temperature",
            "detail": {"temperature_c": round(temperature.value_c, 2), "confirmed": confirmed},
        }
    )
    if confirmed:
        reversal.mark_reversed()
    else:
        violations.append("temp_not_confirmed")
    return {"oven": oven, "ok": confirmed, "steps": steps, "violations": violations, "direction": direction}
