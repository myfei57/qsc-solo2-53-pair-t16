"""配置指纹：停机期间参数是否被改过的核对依据。

指纹不是整份文件的哈希，而是把**会影响现场动作安全**的极限参数按稳定口径
序列化后取 SHA-256。操作开始时把指纹写进进度文档，重启恢复时与当前生效配置
重算的指纹比对：不一致就说明停机期间有人改过参数（属于参数迁移），该操作不
得直接续做，必须先重新核对（``reverify``）通过才能继续。
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from ..config import PlatformConfig, config_to_mapping

# 真正影响动作执行的关键参数都在 limits 段；platform 段只有名称与存储目录，
# 与现场动作无关，改名或换目录不应触发迁移阻塞。
FINGERPRINT_SECTION = "limits"


def fingerprint_config(config: PlatformConfig) -> str:
    """计算当前生效配置的迁移指纹。"""

    mapping = config_to_mapping(config)
    payload = _canonicalize(mapping.get(FINGERPRINT_SECTION, {}))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def verify_params(config: PlatformConfig, operation: str, params: dict[str, Any]) -> list[str]:
    """按当前配置重新核对一次操作的入参，返回不通过项（空列表表示通过）。

    停机期间改参数走的是"迁移后重新核对"：旧进度里记下的目标值在新极限下
    未必还合法（最典型的就是推焦目标超过新的最大行程），这里只核对这种会
    直接影响安全的取值。
    """

    problems: list[str] = []
    if operation == "push":
        target = params.get("stroke_target_mm")
        if target is not None and float(target) > config.pusher.max_stroke_mm:
            problems.append("stroke_target_exceeds_max")
        speed = params.get("speed_mm_s")
        if speed is not None:
            speed = float(speed)
            if not config.pusher.speed_min_mm_s <= speed <= config.pusher.speed_max_mm_s:
                problems.append("speed_out_of_limits")
    return problems


def _canonicalize(value: Any) -> str:
    """稳定序列化：键排序、无多余空白，浮点也走 JSON 字面量。"""

    return json.dumps(copy.deepcopy(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
