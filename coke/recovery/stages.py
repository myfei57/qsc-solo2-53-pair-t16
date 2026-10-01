"""阶段口径：每个编排的有序阶段与恢复策略（续做／退回）。

判定粒度在这张表里定死——恢复按阶段判，绝不按步骤判：

* 阶段完成前崩溃：按本阶段策略处理（前置类阶段退回，已产生不可逆物理动作的
  阶段一律续做）；
* 阶段完成后崩溃：本阶段已闭环，从下一阶段续做。

阶段内部记录的给煤量、杆位、走行距离等只是幂等续做用的进度量，不改变判定。
"""

from __future__ import annotations

CONTINUE = "continue"
ROLLBACK = "rollback"

# 仅出现在恢复判定结论里，不是阶段策略。
NONE = "none"
REVERIFY = "reverify"
BLOCKED = "blocked"

#: 五个编排的有序阶段；阶段名与编排报告里的步骤 action 对齐。
OPERATION_STAGES: dict[str, list[str]] = {
    "charge": ["align", "regulate", "flap", "feed", "close_flap"],
    "push": ["align", "lock", "extend", "retract", "unlock"],
    "quench": ["receive", "travel", "spray"],
    "reverse": ["switch", "confirm"],
    "clean": ["release", "open", "clean", "mark", "close"],
}

#: 阶段策略写死：只允许取 CONTINUE / ROLLBACK。
#: 退回只用于"尚未产生不可逆物理动作的前置阶段"：对位失败重来、导焦栅未锁
#: 直接复位、闩锁松开后重新压紧；一旦煤已给出、杆已伸出、焦已接走、阀已切
#: 换、炉门已打开，就只能把本阶段做完，禁止半途反向操作。
STAGE_POLICY: dict[str, dict[str, str]] = {
    "charge": {
        "align": ROLLBACK,       # 仅对位，未动翻板与给煤，退回重来
        "regulate": CONTINUE,    # 调压无副作用，调到目标带即可
        "flap": CONTINUE,        # 翻板已开（荒煤气已接通），只能继续给煤
        "feed": CONTINUE,        # 煤已入炉，按已给量幂等续给
        "close_flap": CONTINUE,  # 收尾，继续关闭
    },
    "push": {
        "align": ROLLBACK,       # 仅导焦栅对位，未锁定未推焦，退回复位
        "lock": CONTINUE,        # 导焦栅已锁，安全联锁已建立，继续推焦
        "extend": CONTINUE,      # 推焦杆已伸出、焦炭已在途中，只能走完
        "retract": CONTINUE,     # 退杆收尾，继续退到位
        "unlock": CONTINUE,      # 收尾，继续解锁
    },
    "quench": {
        "receive": ROLLBACK,     # 刚接焦未走行：退回让车回焦侧重接（保守）
        "travel": CONTINUE,      # 红焦在车上去塔途中，必须走到塔下喷淋
        "spray": CONTINUE,       # 已开始喷淋，按已喷量幂等续喷
    },
    "reverse": {
        "switch": CONTINUE,      # 阀组已动，两阀互斥必须切到稳定终态
        "confirm": CONTINUE,     # 只补炉温确认，无反向操作
    },
    "clean": {
        "release": ROLLBACK,     # 只松了闩锁，退回即重新压紧
        "open": CONTINUE,        # 炉门已打开，必须清扫后复位
        "clean": CONTINUE,       # 清扫按已累计时长幂等续扫
        "mark": CONTINUE,        # 标记后关门压紧
        "close": CONTINUE,       # 收尾，继续关门压紧
    },
}

#: 各阶段为什么这么判，控制台展示与审计落同一口径。
STAGE_RATIONALE: dict[str, dict[str, str]] = {
    "charge": {
        "align": "仅完成煤斗对位，未开翻板未给煤，退回重新对位最安全",
        "regulate": "集气管调压无不可逆动作，继续调到目标带即可",
        "flap": "翻板已开、荒煤气已接通，禁止关阀倒退，继续给煤",
        "feed": "煤已进入炭化室，按已给吨位幂等续给，不重复给煤",
        "close_flap": "收尾阶段，继续关闭翻板即可",
    },
    "push": {
        "align": "导焦栅仅对位未锁定，退回复位后重新对位",
        "lock": "导焦栅已锁定、安全联锁已建立，继续推焦",
        "extend": "推焦杆已伸出，必须走完行程再退杆，禁止半途停杆",
        "retract": "继续退杆到零位即可",
        "unlock": "收尾阶段，继续解锁导焦栅",
    },
    "quench": {
        "receive": "接焦后尚未走行，退回让熄焦车回焦侧重接，杜绝红焦留轨",
        "travel": "红焦在去熄焦塔途中，必须走行到位后喷淋",
        "spray": "喷淋已开始，按已喷水量幂等续喷到设定时长",
    },
    "reverse": {
        "switch": "阀组已切换，必须落到煤气／废气单一稳定终态，禁止两阀同开",
        "confirm": "补一次炉温回读确认即可",
    },
    "clean": {
        "release": "仅松开闩锁，退回即重新压紧闩锁",
        "open": "炉门已打开，必须完成清扫再关门，不能敞门退出",
        "clean": "清扫按已累计时长幂等续扫，不重复计时",
        "mark": "继续标记完成",
        "close": "收尾阶段，继续关门并压紧闩锁",
    },
}


def stages_for(operation: str) -> list[str]:
    """返回某编排的有序阶段；未知编排直接报错，不允许静默无阶段恢复。"""

    try:
        return list(OPERATION_STAGES[operation])
    except KeyError:
        raise KeyError(f"未知编排 {operation!r}，没有阶段口径") from None


def policy_for(operation: str, stage: str) -> str:
    """返回某编排某阶段的续做／退回策略（定死，不可运行时改写）。"""

    stages_for(operation)
    policy = STAGE_POLICY[operation].get(stage)
    if policy is None:
        raise KeyError(f"编排 {operation!r} 没有阶段 {stage!r}")
    return policy


def rationale_for(operation: str, stage: str) -> str:
    """返回阶段策略的文字依据，供控制台与审计共用。"""

    return STAGE_RATIONALE[operation][stage]


def stage_index(operation: str, stage: str) -> int:
    """返回阶段在编排中的序号（从 0 起）。"""

    return stages_for(operation).index(stage)
