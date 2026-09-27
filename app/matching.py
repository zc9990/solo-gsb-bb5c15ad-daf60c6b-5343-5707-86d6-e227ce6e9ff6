"""跨期轨迹配对：正长度重叠面积加权 + 全局最大权一对一匹配。

配对规则
--------
- 两条跨期轨迹可配对，当且仅当它们的源观察之间存在 *正长度* 的径向重叠
  与钟位弧重叠（端点相接不计——区别于同期聚类的“端点相接算相交”）；
- 配对权重 = 所有相交源观察对的“径向重叠长 × 钟位重叠长”（重叠面积）之和；
- 在所有候选配对中求 *总权重最大* 的全局一对一匹配（匈牙利算法，O(n^3)），
  不做局部贪心，避免“先占大边、后无可配”的错配；
- 总权重相同（按 1e-6 量化）时按（前期轨迹 ID, 后期轨迹 ID）字典序稳定裁决：
  每条候选边按轨迹 ID 对排名，名次越小扰动位越高；扰动取 2 的幂次位且
  总和小于权重量化单位，故只打破平局、不改变主目标，且任意匹配的总扰动
  唯一——结果与输入顺序无关、可复现。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from . import geometry as geo
from .clustering import Observation, to_arc_cached

# 权重量化精度：重叠面积按 1e-6 量化转整数，匈牙利算法在整数上精确求解
_SCALE = 1_000_000
# “正长度”阈值：不超过该值视为零长度（端点相接或浮点尘埃）
_POSITIVE_EPS = 1e-9


def cluster_pair_weight(before: Sequence[Observation],
                        after: Sequence[Observation]) -> float:
    """两条跨期轨迹（簇）的配对权重：所有相交源观察对的重叠面积之和。

    仅当某对源观察的径向重叠与钟位弧重叠 *都是正长度* 时，
    该对才以“径向重叠长 × 钟位重叠长”计入权重。
    """
    total = 0.0
    for ob in before:
        arc_b = to_arc_cached(ob)
        for oa in after:
            radial = geo.radial_overlap_length(
                ob.radius_start, ob.radius_end,
                oa.radius_start, oa.radius_end)
            if radial <= _POSITIVE_EPS:
                continue
            clock = geo.arc_overlap_length(arc_b, to_arc_cached(oa))
            if clock <= _POSITIVE_EPS:
                continue
            total += radial * clock
    return total


def _hungarian_max_perfect(weight: List[List[int]]) -> List[int]:
    """方阵最大权完美匹配（匈牙利算法，O(n^3)，整数权重精确）。

    返回 ``match_row``：``match_row[i] = j`` 表示第 i 行与第 j 列配对。
    """
    n = len(weight)
    u = [0] * (n + 1)
    v = [0] * (n + 1)
    p = [0] * (n + 1)  # p[j] = 与列 j 匹配的行（1 起始，0 为虚拟列）
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv: List[Optional[int]] = [None] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta: Optional[int] = None
            j1 = 0
            row = weight[i0 - 1]
            for j in range(1, n + 1):
                if used[j]:
                    continue
                # 最大化权重 <=> 最小化相反数代价
                cur = -row[j - 1] - u[i0] - v[j]
                if minv[j] is None or cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if delta is None or minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            assert delta is not None
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    assert minv[j] is not None
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    match_row = [0] * n
    for j in range(1, n + 1):
        match_row[p[j] - 1] = j - 1
    return match_row


def maximum_weight_matching(
        n_before: int,
        n_after: int,
        weights: Dict[Tuple[int, int], float],
        before_ids: Sequence[str],
        after_ids: Sequence[str]) -> List[Tuple[int, int]]:
    """最大权全局一对一匹配。

    :param weights: 候选边 ``{(前期下标, 后期下标): 正权重}``
    :return: 配对下标列表，按（前期轨迹 ID, 后期轨迹 ID）字典序排列

    主目标为总权重最大（权重按 1e-6 量化转整数，匈牙利算法精确求解）；
    同权时按轨迹 ID 字典序稳定裁决：候选边按（前期 ID, 后期 ID）排名，
    名次越小扰动位越高，匹配中优先包含字典序更小的配对。
    """
    edges = sorted((e for e, w in weights.items() if w > 0.0),
                   key=lambda e: (before_ids[e[0]], after_ids[e[1]]))
    if not edges:
        return []
    size = max(n_before, n_after)
    # 扰动占低 bit_span 位：任意匹配扰动和 < 2^bit_span = 一个权重量化单位，
    # 故扰动只在总权重完全相同时生效；2 的幂次位使每个匹配的总扰动唯一
    bit_span = len(edges) + 1
    matrix = [[0] * size for _ in range(size)]
    for rank, (i, j) in enumerate(edges):
        quantized = max(1, int(round(weights[(i, j)] * _SCALE)))
        perturb = 1 << (bit_span - 1 - rank)
        matrix[i][j] = (quantized << bit_span) | perturb
    match_row = _hungarian_max_perfect(matrix)
    pairs = [
        (i, j)
        for i, j in enumerate(match_row)
        if i < n_before and j < n_after and (i, j) in weights
    ]
    pairs.sort(key=lambda e: (before_ids[e[0]], after_ids[e[1]]))
    return pairs


def match_trajectory_clusters(
        before: Sequence[Tuple[str, Sequence[Observation]]],
        after: Sequence[Tuple[str, Sequence[Observation]]]
        ) -> List[Tuple[int, int, float]]:
    """对两期轨迹簇求最大权全局一对一匹配。

    :param before: 前期 ``[(轨迹ID, 簇成员观察)]``
    :param after:  后期 ``[(轨迹ID, 簇成员观察)]``
    :return: ``[(前期下标, 后期下标, 配对权重)]``，按轨迹 ID 字典序排列
    """
    weights: Dict[Tuple[int, int], float] = {}
    for i, (_, cluster_b) in enumerate(before):
        for j, (_, cluster_a) in enumerate(after):
            w = cluster_pair_weight(cluster_b, cluster_a)
            if w > _POSITIVE_EPS:
                weights[(i, j)] = w
    pairs = maximum_weight_matching(
        len(before), len(after), weights,
        [tid for tid, _ in before], [tid for tid, _ in after])
    return [(i, j, weights[(i, j)]) for i, j in pairs]
