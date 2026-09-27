"""跨期最大权匹配单元测试（匈牙利算法 + 同权稳定裁决 + 配对权重）。"""

import random
import unittest

from app.clustering import Observation
from app.matching import cluster_pair_weight, maximum_weight_matching

_SCALE = 1_000_000  # 与 app.matching 的量化精度一致


def obs(oid, r0, r1, c0, c1):
    return Observation(
        observation_id=oid, inspection_id="insp", inspected_at=None,
        batch_id="b", turbine_id="T", blade_id="B",
        radius_start=r0, radius_end=r1,
        clock_start=c0, clock_end=c1, grade=1, confidence=0.5,
    )


def brute_force_match(n_b, n_a, weights, before_ids, after_ids):
    """参考实现：枚举全部一对一匹配，按同一规则（量化总权重最大、
    同权按轨迹 ID 字典序）取最优。"""
    edges = sorted(weights, key=lambda e: (before_ids[e[0]], after_ids[e[1]]))
    best = None

    def rec(i, used_cols, chosen, qsum):
        nonlocal best
        if i == n_b:
            bits = tuple(1 if e in chosen else 0 for e in edges)
            key = (qsum, bits)
            if best is None or key > best[0]:
                best = (key, sorted(
                    chosen, key=lambda e: (before_ids[e[0]], after_ids[e[1]])))
            return
        rec(i + 1, used_cols, chosen, qsum)  # 行 i 不参与匹配
        for j in range(n_a):
            e = (i, j)
            if j in used_cols or e not in weights:
                continue
            q = max(1, int(round(weights[e] * _SCALE)))
            rec(i + 1, used_cols | {j}, chosen + [e], qsum + q)

    rec(0, set(), [], 0)
    return best[1] if best else []


class TestMaximumWeightMatching(unittest.TestCase):
    def test_global_optimum_beats_greedy(self):
        # 贪心先占最大边 w(0,0)=10 后，行 1 无边可配，总权仅 10；
        # 全局最优为 (0,1)+(1,0) = 14
        weights = {(0, 0): 10.0, (0, 1): 8.0, (1, 0): 6.0}
        pairs = maximum_weight_matching(2, 2, weights,
                                        ["b1", "b2"], ["a1", "a2"])
        self.assertEqual(set(pairs), {(0, 1), (1, 0)})

    def test_tie_break_by_trajectory_id_not_index(self):
        # 全部候选边同权：必须按轨迹 ID 字典序裁决。
        # 最小 ID 对为 ("c","a") = (1,0)，若错误地按下标顺序则会得到 {(0,0),(1,1)}
        weights = {(0, 0): 1.0, (0, 1): 1.0, (1, 0): 1.0, (1, 1): 1.0}
        pairs = maximum_weight_matching(2, 2, weights,
                                        ["z", "c"], ["a", "b"])
        self.assertEqual(set(pairs), {(1, 0), (0, 1)})

    def test_unmatched_when_no_positive_edge(self):
        self.assertEqual(
            maximum_weight_matching(2, 1, {}, ["b1", "b2"], ["a1"]), [])

    def test_rectangular_padding(self):
        # 行多于列：只有部分行能配对
        weights = {(0, 0): 3.0, (1, 0): 2.0, (2, 0): 1.0}
        pairs = maximum_weight_matching(3, 1, weights,
                                        ["b1", "b2", "b3"], ["a1"])
        self.assertEqual(pairs, [(0, 0)])

    def test_random_against_brute_force(self):
        rng = random.Random(20260926)
        for _ in range(300):
            n_b = rng.randint(1, 4)
            n_a = rng.randint(1, 4)
            weights = {}
            for i in range(n_b):
                for j in range(n_a):
                    if rng.random() < 0.7:
                        weights[(i, j)] = rng.choice([0.5, 1.0, 1.5, 2.0, 3.0])
            # ID 顺序刻意与下标顺序无关，验证稳定裁决只认轨迹 ID
            before_ids = rng.sample([f"tb{k}" for k in range(10)], n_b)
            after_ids = rng.sample([f"ta{k}" for k in range(10)], n_a)
            got = maximum_weight_matching(n_b, n_a, weights,
                                          before_ids, after_ids)
            want = brute_force_match(n_b, n_a, weights, before_ids, after_ids)
            self.assertEqual(got, want, msg=f"weights={weights}")

    def test_input_order_independent(self):
        # 打乱行/列输入顺序（ID 随行走），按 ID 的匹配结果不变
        weights = {(0, 0): 2.0, (0, 1): 2.0, (1, 0): 2.0, (1, 1): 2.0}
        ids_b, ids_a = ["b1", "b2"], ["a1", "a2"]
        p1 = maximum_weight_matching(2, 2, weights, ids_b, ids_a)
        swapped_w = {(1 - i, 1 - j): w for (i, j), w in weights.items()}
        p2 = maximum_weight_matching(2, 2, swapped_w,
                                     ["b2", "b1"], ["a2", "a1"])
        names1 = {(ids_b[i], ids_a[j]) for i, j in p1}
        names2 = {("b2" if i == 0 else "b1", "a2" if j == 0 else "a1")
                  for i, j in p2}
        self.assertEqual(names1, names2)


class TestClusterPairWeight(unittest.TestCase):
    def test_weight_is_sum_of_overlap_areas(self):
        before = [obs("o1", 0, 10, 1, 3), obs("o2", 5, 15, 2, 4)]
        after = [obs("p1", 7, 12, 2.5, 3.5)]
        # o1∩p1：径向 [7,10]=3，钟位 [2.5,3]=0.5 -> 1.5
        # o2∩p1：径向 [7,12]=5，钟位 [2.5,3.5]=1 -> 5
        self.assertAlmostEqual(cluster_pair_weight(before, after), 6.5)

    def test_touching_endpoints_not_counted(self):
        # 径向、钟位均仅端点相接：零长度重叠，不可配对
        before = [obs("o1", 0, 10, 1, 3)]
        after = [obs("p1", 10, 20, 3, 5)]
        self.assertEqual(cluster_pair_weight(before, after), 0.0)

    def test_single_dimension_touch_not_counted(self):
        # 径向有正重叠但钟位仅端点相接 -> 仍不可配对
        before = [obs("o1", 0, 10, 1, 3)]
        after = [obs("p1", 5, 15, 3, 6)]
        self.assertEqual(cluster_pair_weight(before, after), 0.0)

    def test_wrap_twelve_overlap(self):
        before = [obs("o1", 0, 10, 11, 1)]  # 跨 12 点弧
        after = [obs("p1", 5, 15, 0, 2)]    # 与 [0,1] 段重叠 1 小时
        self.assertAlmostEqual(cluster_pair_weight(before, after), 5.0)

    def test_zero_length_clock_arc_not_counted(self):
        before = [obs("o1", 0, 10, 2, 2)]  # 单点钟位（零长度弧）
        after = [obs("p1", 5, 15, 1, 3)]
        self.assertEqual(cluster_pair_weight(before, after), 0.0)


if __name__ == "__main__":
    unittest.main()
