"""裁决感知聚类的单元测试：必须同轨闭包、禁止同轨冲突链。"""

import unittest

from app.clustering import (
    Observation,
    Ruling,
    RulingConflict,
    build_all,
    cluster_with_rulings,
)


def obs(oid, r0=0.0, r1=2.0, c0=1.0, c1=2.0):
    return Observation(
        observation_id=oid, inspection_id="i1", inspected_at=None,
        batch_id="b1", turbine_id="T1", blade_id="B1",
        radius_start=r0, radius_end=r1,
        clock_start=c0, clock_end=c1, grade=1, confidence=0.5,
    )


def ruling(rid, a, b, relation):
    return Ruling(adjudication_id=rid, observation_a=a,
                  observation_b=b, relation=relation)


class TestRulingClustering(unittest.TestCase):
    def test_no_rulings_matches_geometry(self):
        data = [obs("A", 0, 2), obs("B", 9, 11)]
        self.assertEqual(len(build_all("T1", "B1", data, [])), 2)

    def test_must_link_transitive_closure(self):
        data = [obs("A", 0, 2), obs("B", 5, 7), obs("C", 9, 11)]
        rulings = [ruling("r1", "A", "B", "must_link"),
                   ruling("r2", "B", "C", "must_link")]
        result = build_all("T1", "B1", data, rulings)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["observation_count"], 3)
        self.assertEqual(result[0]["adjudication_ids"], ["r1", "r2"])

    def test_must_link_hits_only_listed_for_each_trajectory(self):
        data = [obs("A", 0, 2), obs("B", 5, 7),
                obs("C", 9, 11), obs("D", 14, 16)]
        rulings = [ruling("r1", "A", "B", "must_link"),
                   ruling("r2", "C", "D", "must_link")]
        result = build_all("T1", "B1", data, rulings)
        self.assertEqual(len(result), 2)
        hits = sorted(tuple(t["adjudication_ids"]) for t in result)
        self.assertEqual(hits, [("r1",), ("r2",)])

    def test_cannot_link_violation_via_must_link_chain(self):
        data = [obs("A", 0, 2), obs("B", 5, 7), obs("C", 9, 11)]
        rulings = [ruling("r1", "A", "B", "must_link"),
                   ruling("r2", "B", "C", "must_link"),
                   ruling("cl", "A", "C", "cannot_link")]
        with self.assertRaises(RulingConflict) as ctx:
            cluster_with_rulings(data, rulings)
        self.assertEqual(ctx.exception.cannot_link_id, "cl")
        self.assertEqual(ctx.exception.chain, ["r1", "r2"])

    def test_cannot_link_violation_via_geometry_has_empty_chain(self):
        # 几何相交的两条观察之间没有 must_link 裁决 -> 链为空
        data = [obs("A", 0, 5), obs("B", 4, 9)]
        rulings = [ruling("cl", "A", "B", "cannot_link")]
        with self.assertRaises(RulingConflict) as ctx:
            cluster_with_rulings(data, rulings)
        self.assertEqual(ctx.exception.chain, [])

    def test_cannot_link_satisfied_when_separate(self):
        data = [obs("A", 0, 2), obs("B", 9, 11)]
        rulings = [ruling("cl", "A", "B", "cannot_link")]
        clusters, _ = cluster_with_rulings(data, rulings)
        self.assertEqual(len(clusters), 2)

    def test_must_link_applied_before_cannot_link_check(self):
        # A-C 禁止同轨，但其闭包由 A~B（几何）+ B~C（裁决）构成：
        # 必须先完成 must_link 闭包，校验才会发现冲突
        data = [obs("A", 0, 5), obs("B", 4, 9, c0=1, c1=2),
                obs("C", 20, 22, c0=7, c1=8)]
        rulings = [ruling("m", "B", "C", "must_link"),
                   ruling("cl", "A", "C", "cannot_link")]
        with self.assertRaises(RulingConflict) as ctx:
            cluster_with_rulings(data, rulings)
        self.assertEqual(ctx.exception.cannot_link_id, "cl")
        self.assertEqual(ctx.exception.chain, ["m"])


if __name__ == "__main__":
    unittest.main()
