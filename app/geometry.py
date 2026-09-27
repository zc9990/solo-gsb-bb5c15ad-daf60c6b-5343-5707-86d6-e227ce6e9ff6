"""几何工具：距叶根径向区间与顺时针钟位（环向）区间。

钟位约定
--------
- 钟面为 12 个小时：0.0 与 12.0 等价，均表示 12 点。
- 钟位区间沿 *顺时针* 从 ``clock_start`` 走到 ``clock_end``：
  * 普通区间（如 1 点 -> 3 点）：覆盖 [1, 3]；
  * 跨越 12 点的区间（如 11 点 -> 1 点）：覆盖 [11, 12) ∪ (0, 1]。
- ``clock_start == clock_end`` 视为单点（零长度）区间。

相交判定
--------
两条观察归入同一连通簇，当且仅当它们的径向区间相交 *且* 钟位弧相交
（端点相接也算相交——裂纹在同一位置接续）。
"""

from __future__ import annotations

from typing import List, Tuple

CLOCK_MAX = 12.0
_EPS = 1e-9

# 线性化后的弧段（按环周展开到 [0, 12]）
Segment = Tuple[float, float]
Arc = List[Segment]


def normalize_clock(value: float) -> float:
    """把钟位规范化到 [0, 12)，12.0 归一为 0.0。"""
    v = value % CLOCK_MAX
    if abs(v - CLOCK_MAX) < _EPS:
        v = 0.0
    if v < 0.0:  # 负输入（一般被 pydantic 拦截）的兜底
        v += CLOCK_MAX
    return v


def to_arc(clock_start: float, clock_end: float) -> Arc:
    """把“顺时针起止钟位”展开为 [0, 12] 上的线性弧段集合。"""
    start = normalize_clock(clock_start)
    end = normalize_clock(clock_end)
    if abs(start - end) < _EPS:
        return [(start, end)]  # 单点
    if end < start:  # 顺时针经过 12 点
        return [(start, CLOCK_MAX), (0.0, end)]
    return [(start, end)]


def _segments_intersect(a: Segment, b: Segment) -> bool:
    """两个线性闭区间是否相交（端点相接算相交）。"""
    return a[0] <= b[1] + _EPS and b[0] <= a[1] + _EPS


def arcs_intersect(a: Arc, b: Arc) -> bool:
    """两条钟位弧是否相交（含端点相接）。"""
    for sa in a:
        for sb in b:
            if _segments_intersect(sa, sb):
                return True
    return False


def radial_intersects(start_a: float, end_a: float,
                      start_b: float, end_b: float) -> bool:
    """两条距叶根径向区间是否相交（端点相接算相交）。"""
    return start_a <= end_b + _EPS and start_b <= end_a + _EPS


def radial_overlap_length(start_a: float, end_a: float,
                          start_b: float, end_b: float) -> float:
    """两条径向区间的重叠长度；不相交或仅端点相接时为 0。"""
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def arc_overlap_length(a: Arc, b: Arc) -> float:
    """两条钟位弧在环面上的重叠总长度（仅端点相接不贡献长度）。"""
    total = 0.0
    for sa in a:
        for sb in b:
            lo = max(sa[0], sb[0])
            hi = min(sa[1], sb[1])
            if hi > lo:
                total += hi - lo
    return total


def arc_intersection(a: Arc, b: Arc) -> List[Segment]:
    """两条钟位弧在环面上的交集（[0, 12] 上已合并的线性弧段，按起点排序）。

    与 :func:`arc_overlap_length` 一致仅保留正长度交集（端点相接不产生弧段），
    供修补覆盖核对做精确面积求交，避免外包框近似。
    """
    parts: List[Segment] = []
    for sa in a:
        for sb in b:
            lo = max(sa[0], sb[0])
            hi = min(sa[1], sb[1])
            if hi > lo + _EPS:
                parts.append((lo, hi))
    return merge_segments(parts)


def merge_segments(segments: List[Segment]) -> List[Segment]:
    """合并一组线性闭区间，端点相接即合并。"""
    """合并一组线性闭区间，端点相接即合并。"""
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: (s[0], s[1]))
    merged: List[Segment] = [ordered[0]]
    for lo, hi in ordered[1:]:
        last_lo, last_hi = merged[-1]
        if lo <= last_hi + _EPS:
            if hi > last_hi:
                merged[-1] = (last_lo, hi)
        else:
            merged.append((lo, hi))
    return merged


def merge_arcs(arcs: List[Arc]) -> Tuple[List[Segment], float]:
    """合并同一连通簇内所有观察的钟位弧。

    返回 ``(segments, total_length)``，``segments`` 是 [0, 12] 上已合并的
    线性弧段（按起点排序）；``total_length`` 为覆盖总长度。
    单点（零长度）弧不贡献长度，但保留覆盖位置。
    """
    flat: List[Segment] = []
    for arc in arcs:
        flat.extend(arc)
    merged = merge_segments(flat)
    total = sum(hi - lo for lo, hi in merged)
    return merged, total


def describe_clock(segments: List[Segment]) -> dict:
    """把合并后的线性弧段还原为用户可理解的顺时针钟位描述。

    返回：
      ``ranges``   —— 顺时针区间列表 [{start, end, wraps_twelve, start_label, end_label}]
      ``full_circle``  —— 是否覆盖整圈
    """
    if not segments:
        return {"full_circle": False, "ranges": []}

    # 整圈：单段 [0,12]，或首尾在 0/12 处相接（含 12-0 的零缝隙闭环）。
    closes = (len(segments) >= 2
              and abs(segments[0][0]) < _EPS
              and abs(segments[-1][1] - CLOCK_MAX) < _EPS)
    if (len(segments) == 1
            and abs(segments[0][0]) < _EPS
            and abs(segments[0][1] - CLOCK_MAX) < _EPS):
        return {
            "full_circle": True,
            "ranges": [{
                "start": 0.0,
                "end": 12.0,
                "wraps_twelve": False,
                "start_label": "12点",
                "end_label": "12点",
            }],
        }

    if closes:
        # 末段 [.., 12] 与首段 [0, ..] 相接 => 合成一条跨越 12 点的顺时针弧；
        # 其余中间段本就已互不相交，保持普通弧即可（线性合并已完成）。
        items = [_range_item((segments[-1][0], segments[0][1]), wraps=True)]
        items.extend(_range_item(s, wraps=False) for s in segments[1:-1])
        return {"full_circle": False, "ranges": items}

    return {"full_circle": False,
            "ranges": [_range_item(s, wraps=False) for s in segments]}


def _range_item(seg: Segment, wraps: bool) -> dict:
    lo, hi = seg
    if wraps:
        return {
            "start": _round(lo),
            "end": _round(hi if hi > _EPS else CLOCK_MAX),
            "wraps_twelve": True,
            "start_label": _label(lo),
            "end_label": _label(hi),
        }
    return {
        "start": _round(lo),
        "end": _round(hi),
        "wraps_twelve": False,
        "start_label": _label(lo),
        "end_label": _label(hi if hi > _EPS else 12.0),
    }


def _label(v: float) -> str:
    if abs(v) < _EPS:
        return "12点"
    if abs(v - round(v)) < _EPS:
        return f"{int(round(v))}点"
    return f"{_round(v):g}点"


def _round(v: float) -> float:
    return round(v, 4)
