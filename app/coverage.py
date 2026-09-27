"""只读修补覆盖核对：被修补区域并集对原始观察面积的精确覆盖。

核对口径
--------
- 观察与修补区域均为「径向区间 × 顺时针钟位弧」的柱面片，面积单位为
  *径向米 × 钟位小时*（非物理曲面面积，不按半径折算）。
- 多条修补区域先求**并集**再覆盖：相互重叠的修补只计一次。
- 覆盖面积通过**径向切片**精确求解：在全部相关径向边界（观察与各修补区域
  的起/止端）切出的每个径向条带内，活动修补集合不变，条带内覆盖弧为
  各修补钟位弧与观察钟位弧交集的合并（环向去重），条带覆盖面积
  = 条带径向长 × 覆盖弧长。全程为精确区间算术，**不使用外包框或采样**。
- 钟位沿用观察的约定：0/12 等价、区间沿顺时针、允许跨越 12 点、
  ``clock_start == clock_end`` 为零长度单点。

零面积观察（径向长度为 0，或钟位弧长度为 0 的单点/零径向裂纹）单列，
不参与覆盖率核对。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from . import geometry as geo
from .clustering import Observation

# “全覆盖 / 零面积”判定阈值：不超过该差值视为相等（端点/浮点尘埃）
_EQUAL_EPS = 1e-7


@dataclass(frozen=True)
class Region:
    """一个修补区域：径向区间 × 顺时针钟位弧。"""

    radius_start: float
    radius_end: float
    clock_start: float
    clock_end: float

    @property
    def arc(self) -> geo.Arc:
        return geo.to_arc(self.clock_start, self.clock_end)


def make_region(radius_start: float, radius_end: float,
                clock_start: float, clock_end: float) -> Region:
    """构造修补区域；与观察写入一致，12 点钟位统一归一为 0。"""
    norm = geo.normalize_clock
    return Region(radius_start, radius_end,
                  norm(clock_start), norm(clock_end))


def rectangle_area(radius_start: float, radius_end: float,
                   arc: geo.Arc) -> float:
    """径向区间 × 钟位弧的面积（径向米 × 钟位小时）。"""
    _, clock_len = geo.merge_arcs([arc])
    return max(0.0, radius_end - radius_start) * clock_len


def is_zero_area(obs: Observation) -> bool:
    """观察是否为零面积（径向零长，或钟位单点零弧长）。"""
    return rectangle_area(obs.radius_start, obs.radius_end,
                          to_obs_arc(obs)) <= _EQUAL_EPS


def to_obs_arc(obs: Observation) -> geo.Arc:
    return geo.to_arc(obs.clock_start, obs.clock_end)


def covered_area(obs: Observation, regions: Sequence[Region]) -> float:
    """修补区域并集在观察面积内的精确覆盖面积。

    径向切片法：收集观察与各修补区域在观察径向范围内的所有边界作为切分点，
    每个条带内活动修补集合恒定；条带内取各修补弧与观察弧的正长度交集并
    线性合并（环向去重），累加 条带长 × 合并覆盖弧长。
    """
    if not regions:
        return 0.0

    o_r0, o_r1 = obs.radius_start, obs.radius_end
    if o_r1 <= o_r0:
        return 0.0
    o_arc = to_obs_arc(obs)

    # 仅保留与观察径向区间正长度重叠的修补区域，边界裁剪到观察范围
    clipped: List[Tuple[float, float, geo.Arc]] = []
    cuts = [o_r0, o_r1]
    for reg in regions:
        lo = max(o_r0, reg.radius_start)
        hi = min(o_r1, reg.radius_end)
        if hi <= lo:
            continue  # 不相交或仅端点相接：不贡献面积
        clipped.append((lo, hi, reg.arc))
        cuts.extend((lo, hi))

    if not clipped:
        return 0.0

    total = 0.0
    boundaries = sorted(set(cuts))
    for k in range(len(boundaries) - 1):
        r_lo, r_hi = boundaries[k], boundaries[k + 1]
        band = r_hi - r_lo
        if band <= 0.0:
            continue
        # 条带中点落在修补径向区间内 ⇔ 该修补在整条带内活动
        mid = (r_lo + r_hi) / 2.0
        covered_parts: List[geo.Segment] = []
        for lo, hi, arc in clipped:
            if lo <= mid <= hi:
                covered_parts.extend(geo.arc_intersection(o_arc, arc))
        if not covered_parts:
            continue
        merged = geo.merge_segments(covered_parts)
        clock_len = sum(hi - lo for lo, hi in merged)
        total += band * clock_len
    return total


def coverage_status(original: float, covered: float) -> str:
    """逐条核对状态：uncovered / partial / full。"""
    if covered <= _EQUAL_EPS:
        return "uncovered"
    if original - covered <= _EQUAL_EPS:
        return "full"
    return "partial"


def check_observation(obs: Observation,
                      regions: Sequence[Region]) -> dict:
    """计算单条观察的原面积、覆盖面积、覆盖率与状态。

    零面积观察返回 ``status="excluded"`` 并给出原因，不计覆盖率。
    """
    original = rectangle_area(obs.radius_start, obs.radius_end, to_obs_arc(obs))
    if original <= _EQUAL_EPS:
        return {
            "observation_id": obs.observation_id,
            "inspection_id": obs.inspection_id,
            "original_area": 0.0,
            "covered_area": 0.0,
            "coverage_ratio": None,
            "status": "excluded",
            "excluded_reason": "zero_area",
        }

    covered = min(covered_area(obs, regions), original)
    return {
        "observation_id": obs.observation_id,
        "inspection_id": obs.inspection_id,
        "original_area": round(original, 4),
        "covered_area": round(covered, 4),
        "coverage_ratio": round(covered / original, 4),
        "status": coverage_status(original, covered),
        "excluded_reason": None,
    }
