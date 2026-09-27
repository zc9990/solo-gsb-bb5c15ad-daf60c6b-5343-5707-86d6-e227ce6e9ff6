"""连通簇聚类与裂纹轨迹合并。

规则：两条观察的 *径向区间* 与 *顺时针钟位弧* 同时相交（端点相接算相交）
则并入同一连通簇；连通关系按并查集传递闭包。
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import geometry as geo


@dataclass
class Ruling:
    """一条人工关联裁决。"""

    adjudication_id: str
    observation_a: str
    observation_b: str
    relation: str  # 'must_link' | 'cannot_link'

    @property
    def pair(self) -> Tuple[str, str]:
        return self.observation_a, self.observation_b


class RulingConflict(Exception):
    """禁止同轨裁决的两端落入同一闭包时抛出。

    :param cannot_link_id: 被违反的禁止同轨裁决
    :param chain: 冲突裁决链（按顺序相邻的 must_link 裁决；纯几何连通为空）
    """

    def __init__(self, cannot_link_id: str, chain: List[str]):
        self.cannot_link_id = cannot_link_id
        self.chain = chain
        super().__init__(
            f"禁止同轨裁决 {cannot_link_id} 的两条观察被必须同轨关系"
            f"（及几何连通）闭包合并；冲突裁决链: {chain}"
        )


@dataclass
class Observation:
    observation_id: str
    inspection_id: str
    inspected_at: str
    batch_id: str
    turbine_id: str
    blade_id: str
    radius_start: float
    radius_end: float
    clock_start: float
    clock_end: float
    grade: int
    confidence: float

    @property
    def arc(self) -> geo.Arc:
        return to_arc_cached(self)


def to_arc_cached(obs: "Observation") -> geo.Arc:
    arc = getattr(obs, "_arc", None)
    if arc is None:
        arc = geo.to_arc(obs.clock_start, obs.clock_end)
        object.__setattr__(obs, "_arc", arc)
    return arc


class DSU:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _compatible(a: Observation, b: Observation) -> bool:
    if not geo.radial_intersects(a.radius_start, a.radius_end,
                                 b.radius_start, b.radius_end):
        return False
    return geo.arcs_intersect(to_arc_cached(a), to_arc_cached(b))


def _geometry_dsu(observations: Sequence[Observation]) -> DSU:
    """仅按“径向相交 ∧ 钟位弧相交”构建并查集。"""
    n = len(observations)
    dsu = DSU(n)
    for i in range(n):
        oi = observations[i]
        for j in range(i + 1, n):
            if _compatible(oi, observations[j]):
                dsu.union(i, j)
    return dsu


def cluster_observations(observations: Sequence[Observation]) -> List[List[Observation]]:
    """按“径向相交 ∧ 钟位弧相交”求连通簇。

    观察数量为叶片级（通常数十至数百条），O(n²) 两两判定足够。
    """
    return _groups_from_dsu(_geometry_dsu(observations), observations)


def _groups_from_dsu(dsu: DSU,
                     observations: Sequence[Observation]) -> List[List[Observation]]:
    groups: Dict[int, List[Observation]] = {}
    for i, obs in enumerate(observations):
        groups.setdefault(dsu.find(i), []).append(obs)

    clusters = list(groups.values())
    for cluster in clusters:
        cluster.sort(key=lambda o: (o.radius_start, o.radius_end,
                                    o.clock_start, o.clock_end,
                                    o.observation_id))
    clusters.sort(key=lambda c: (c[0].radius_start, c[0].observation_id))
    return clusters


def _must_link_index(rulings: Sequence[Ruling]) -> Dict[Tuple[str, str], List[Ruling]]:
    """无序观察对 -> 该对上的全部必须同轨裁决（按 ID 排序，保证确定性）。"""
    index: Dict[Tuple[str, str], List[Ruling]] = {}
    for r in rulings:
        if r.relation != "must_link":
            continue
        key = tuple(sorted(r.pair))
        index.setdefault(key, []).append(r)
    for items in index.values():
        items.sort(key=lambda r: r.adjudication_id)
    return index


def _conflict_chain(index_a: int, index_b: int, observations: Sequence[Observation],
                    geometry_roots: Dict[int, int],
                    must_index: Dict[Tuple[str, str], List[Ruling]]
                    ) -> Optional[List[str]]:
    """在 must_link 边 ∪ 几何连通 的无向图上求 index_a -> index_b 的最短路径。

    返回路径上按顺序相邻的 must_link 裁决 ID；若两观察仅靠几何连通直接同簇
    （不经过任何 must_link 裁决），返回空列表。不存在路径时返回 None。
    """
    n = len(observations)

    def neighbors(x: int) -> List[Tuple[int, Optional[str]]]:
        out: List[Tuple[int, Optional[str]]] = []
        # 几何连通：与 x 同几何簇的其余观察，零成本直连
        root_x = geometry_roots[x]
        for y in range(n):
            if y != x and geometry_roots[y] == root_x:
                out.append((y, None))
        # 必须同轨裁决边
        for y in range(n):
            if y == x:
                continue
            key = tuple(sorted((observations[x].observation_id,
                                observations[y].observation_id)))
            for ruling in must_index.get(key, []):
                out.append((y, ruling.adjudication_id))
        return out

    # BFS（边无序权：几何边与裁决边同为 1，先到即最短）
    prev: Dict[int, Tuple[int, Optional[str]]] = {index_a: (index_a, None)}
    queue = deque([index_a])
    while queue:
        x = queue.popleft()
        if x == index_b:
            break
        for y, edge_id in neighbors(x):
            if y not in prev:
                prev[y] = (x, edge_id)
                queue.append(y)

    if index_b not in prev:
        return None
    chain: List[str] = []
    cur = index_b
    while cur != index_a:
        prev_node, edge_id = prev[cur]
        if edge_id is not None:
            chain.append(edge_id)
        cur = prev_node
    chain.reverse()
    return chain


def cluster_with_rulings(observations: Sequence[Observation],
                         rulings: Sequence[Ruling]
                         ) -> Tuple[List[List[Observation]], Dict[frozenset, List[str]]]:
    """裁决感知聚类。

    步骤（顺序固定）：
      1. 几何连通并查集；
      2. 先应用 *必须同轨* 关系：在其上做并集，得到传递闭包；
      3. 再校验 *禁止同轨* 关系：若两端落入同一闭包，抛 :class:`RulingConflict`
         （附带冲突裁决链）。

    返回 ``(簇列表, 簇成员集合 -> 命中的必须同轨裁决 ID 列表)``。
    """
    dsu = _geometry_dsu(observations)
    pos = {o.observation_id: i for i, o in enumerate(observations)}
    must_index = _must_link_index(rulings)

    # 纯几何连通快照（冲突链 BFS 只把它当作零成本边），须在叠加 must_link 前取得
    geometry_roots = {i: dsu.find(i) for i in range(len(observations))}

    # 1) 必须同轨：传递闭包（只引用现存观察的裁决生效）
    for (oid_a, oid_b) in must_index:
        if oid_a in pos and oid_b in pos:
            dsu.union(pos[oid_a], pos[oid_b])

    # 2) 禁止同轨：闭包校验（裁决 ID 排序，保证确定性）。
    # BFS 只把“原始几何连通”当作零成本边，故在叠加 must_link 之前
    # 先快照一份纯几何根。
    cannot = sorted((r for r in rulings if r.relation == "cannot_link"),
                    key=lambda r: r.adjudication_id)
    for ruling in cannot:
        if ruling.observation_a not in pos or ruling.observation_b not in pos:
            continue  # 观察不存在的裁决不参与（提交时即已拦截，此处兜底）
        ia, ib = pos[ruling.observation_a], pos[ruling.observation_b]
        if dsu.find(ia) == dsu.find(ib):
            chain = _conflict_chain(ia, ib, observations,
                                    geometry_roots, must_index)
            raise RulingConflict(ruling.adjudication_id, chain or [])

    clusters = _groups_from_dsu(dsu, observations)

    # 每个闭包命中的必须同轨裁决（两端均在簇内）
    hits: Dict[frozenset, List[str]] = {}
    for cluster in clusters:
        member_ids = {o.observation_id for o in cluster}
        matched: List[str] = []
        for (oid_a, oid_b), items in must_index.items():
            if oid_a in member_ids and oid_b in member_ids:
                matched.extend(r.adjudication_id for r in items)
        hits[frozenset(member_ids)] = sorted(set(matched))
    return clusters, hits


def _signature(turbine_id: str, blade_id: str,
               members: Sequence[Observation]) -> str:
    """由簇成员稳定生成轨迹标识：同成员集合重算后 ID 不变。"""
    digest_src = turbine_id + "|" + blade_id + "|" + ",".join(
        sorted(m.observation_id for m in members)
    )
    return "trj_" + hashlib.sha1(digest_src.encode("utf-8")).hexdigest()[:16]


def build_trajectory(turbine_id: str, blade_id: str,
                     members: Sequence[Observation],
                     adjudication_ids: Optional[Sequence[str]] = None
                     ) -> Dict[str, Any]:
    """把一个连通簇合并为一条裂纹轨迹。"""
    radius_start = min(m.radius_start for m in members)
    radius_end = max(m.radius_end for m in members)

    merged_segments, clock_span = geo.merge_arcs([to_arc_cached(m) for m in members])
    clock = geo.describe_clock(merged_segments)
    clock["span"] = round(clock_span, 4)

    # 等级 -> 该等级下的最高置信度
    grade_conf: Dict[int, float] = {}
    for m in members:
        grade_conf[m.grade] = max(grade_conf.get(m.grade, -1.0), m.confidence)
    grades = sorted(grade_conf)
    has_conflict = len(grades) > 1

    # 代表等级：最高置信度优先；并列时取等级数值更大（更保守）
    representative = max(grades, key=lambda g: (grade_conf[g], g))

    sources = [
        {
            "observation_id": m.observation_id,
            "inspection_id": m.inspection_id,
            "inspected_at": m.inspected_at,
            "batch_id": m.batch_id,
            "radius_start": round(m.radius_start, 4),
            "radius_end": round(m.radius_end, 4),
            "clock_start": round(m.clock_start, 4),
            "clock_end": round(m.clock_end, 4),
            "grade": m.grade,
            "confidence": round(m.confidence, 4),
        }
        for m in members
    ]
    sources.sort(key=lambda s: (s["radius_start"], s["clock_start"],
                                s["observation_id"]))

    return {
        "trajectory_id": _signature(turbine_id, blade_id, members),
        "turbine_id": turbine_id,
        "blade_id": blade_id,
        "radius_start": round(radius_start, 4),
        "radius_end": round(radius_end, 4),
        "clock": clock,
        "max_confidence": round(max(m.confidence for m in members), 4),
        "grades": grades,
        "grade_confidence": {str(g): round(c, 4) for g, c in grade_conf.items()},
        "representative_grade": representative,
        "grade_conflict": has_conflict,
        "observation_count": len(members),
        "adjudication_ids": sorted(adjudication_ids or []),
        "sources": sources,
    }


def build_all(turbine_id: str, blade_id: str,
              observations: Sequence[Observation],
              rulings: Optional[Sequence[Ruling]] = None
              ) -> List[Dict[str, Any]]:
    """聚类并合并为轨迹；提供裁决时按裁决感知规则聚类。"""
    if not rulings:
        return [build_trajectory(turbine_id, blade_id, cluster)
                for cluster in cluster_observations(observations)]

    clusters, hits = cluster_with_rulings(observations, rulings)
    result: List[Dict[str, Any]] = []
    for cluster in clusters:
        member_key = frozenset(o.observation_id for o in cluster)
        result.append(build_trajectory(turbine_id, blade_id, cluster,
                                       adjudication_ids=hits[member_key]))
    return result
