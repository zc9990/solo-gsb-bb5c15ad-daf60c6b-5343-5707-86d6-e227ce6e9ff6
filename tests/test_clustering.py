"""连通簇聚类单元测试。"""

import unittest

from app.clustering import Observation, build_all


def obs(oid, r0, r1, c0, c1, grade, confidence,
        inspection="insp-1", batch="b1"):
    return Observation(
        observation_id=oid, inspection_id=inspection, inspected_at=None,
        batch_id=batch, turbine_id="T1", blade_id="B1",
        radius_start=r0, radius_end=r1,
        clock_start=c0, clock_end=c1,
        grade=grade, confidence=confidence,
    )


class TestClustering(unittest.TestCase):
    def test_radial_and_clock_both_required(self):
        # A/B 径向相交但钟位分离；A/C 钟位相交但径向分离 -> 3 簇
        data = [
            obs("A", 0, 10, 1, 2, 1, 0.5),
            obs("B", 5, 15, 4, 5, 1, 0.5),
            obs("C", 20, 30, 1, 2, 1, 0.5),
        ]
        result = build_all("T1", "B1", data)
        self.assertEqual(len(result), 3)

    def test_transitive_chain(self):
        # A~B（1-3 与 3-5 端点相接），B~C，但 A 与 C 不直接接触 -> 同簇
        data = [
            obs("A", 0, 10, 1, 3, 2, 0.6),
            obs("B", 0, 10, 3, 5, 2, 0.7),
            obs("C", 0, 10, 5, 7, 2, 0.8),
        ]
        result = build_all("T1", "B1", data)
        self.assertEqual(len(result), 1)
        trj = result[0]
        self.assertEqual(trj["observation_count"], 3)
        self.assertAlmostEqual(trj["radius_start"], 0.0)
        self.assertAlmostEqual(trj["radius_end"], 10.0)
        self.assertFalse(trj["grade_conflict"])
        self.assertEqual(trj["representative_grade"], 2)
        self.assertAlmostEqual(trj["max_confidence"], 0.8)
        self.assertEqual({s["observation_id"] for s in trj["sources"]},
                         {"A", "B", "C"})

    def test_wrap_twelve_connection(self):
        # 两条观察在 12 点两侧接续
        data = [
            obs("A", 0, 10, 11, 12, 2, 0.9),
            obs("B", 0, 10, 0, 1, 3, 0.9),
        ]
        result = build_all("T1", "B1", data)
        self.assertEqual(len(result), 1)
        trj = result[0]
        self.assertTrue(trj["grade_conflict"])
        self.assertEqual(trj["grades"], [2, 3])
        self.assertEqual(trj["representative_grade"], 3)  # 并列取更大等级
        self.assertEqual(len(trj["clock"]["ranges"]), 1)
        self.assertTrue(trj["clock"]["ranges"][0]["wraps_twelve"])

    def test_representative_grade_by_confidence(self):
        data = [
            obs("A", 0, 10, 1, 2, 2, 0.95),
            obs("B", 0, 10, 1, 2, 4, 0.30),
        ]
        trj = build_all("T1", "B1", data)[0]
        self.assertTrue(trj["grade_conflict"])
        self.assertEqual(trj["representative_grade"], 2)
        self.assertAlmostEqual(trj["grade_confidence"]["2"], 0.95)

    def test_trajectory_id_stable(self):
        data = [
            obs("A", 0, 10, 1, 2, 2, 0.9),
            obs("B", 0, 10, 1, 2, 2, 0.8),
        ]
        id1 = build_all("T1", "B1", data)[0]["trajectory_id"]
        id2 = build_all("T1", "B1", list(reversed(data)))[0]["trajectory_id"]
        self.assertEqual(id1, id2)


if __name__ == "__main__":
    unittest.main()
