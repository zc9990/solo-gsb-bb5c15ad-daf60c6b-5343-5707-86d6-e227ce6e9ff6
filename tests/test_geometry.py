"""几何工具单元测试：python -m unittest tests.test_geometry -v"""

import unittest

from app import geometry as geo


class TestArc(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(geo.normalize_clock(12.0), 0.0)
        self.assertEqual(geo.normalize_clock(0.0), 0.0)
        self.assertAlmostEqual(geo.normalize_clock(3.5), 3.5)

    def test_to_arc_plain_and_wrap(self):
        self.assertEqual(geo.to_arc(1, 3), [(1.0, 3.0)])
        self.assertEqual(geo.to_arc(11, 1), [(11.0, 12.0), (0.0, 1.0)])
        self.assertEqual(geo.to_arc(12, 2), [(0.0, 2.0)])  # 12 归一
        self.assertEqual(geo.to_arc(3, 3), [(3.0, 3.0)])    # 单点

    def test_arc_intersection_plain(self):
        self.assertTrue(geo.arcs_intersect(geo.to_arc(1, 2), geo.to_arc(2, 3)))
        self.assertFalse(geo.arcs_intersect(geo.to_arc(1, 2), geo.to_arc(3, 4)))

    def test_arc_intersection_wraps_twelve(self):
        # 11点->1点 与 11.5点->0.5点：都跨 12
        self.assertTrue(
            geo.arcs_intersect(geo.to_arc(11, 1), geo.to_arc(11.5, 0.5))
        )
        # 跨12 弧 与 对侧 5->7 不相交
        self.assertFalse(geo.arcs_intersect(geo.to_arc(11, 1), geo.to_arc(5, 7)))
        # 端点在 1 点相接
        self.assertTrue(geo.arcs_intersect(geo.to_arc(11, 1), geo.to_arc(1, 2)))

    def test_radial_intersection(self):
        self.assertTrue(geo.radial_intersects(0, 10, 10, 20))
        self.assertFalse(geo.radial_intersects(0, 10, 10.5, 20))

    def test_merge_arcs_closes_into_wrap(self):
        # 11->12(存为11->0两段) + 0->1 => 一条跨越12点的弧 11->1
        segs, total = geo.merge_arcs([geo.to_arc(11, 12), geo.to_arc(0, 1)])
        desc = geo.describe_clock(segs)
        self.assertFalse(desc["full_circle"])
        self.assertEqual(len(desc["ranges"]), 1)
        rng = desc["ranges"][0]
        self.assertTrue(rng["wraps_twelve"])
        self.assertAlmostEqual(rng["start"], 11.0)
        self.assertAlmostEqual(rng["end"], 1.0)
        self.assertAlmostEqual(total, 2.0)

    def test_merge_arcs_full_circle(self):
        segs, total = geo.merge_arcs([
            geo.to_arc(0, 6), geo.to_arc(6, 12),
        ])
        desc = geo.describe_clock(segs)
        self.assertTrue(desc["full_circle"])
        self.assertAlmostEqual(total, 12.0)

    def test_merge_arcs_keeps_disjoint(self):
        segs, total = geo.merge_arcs([geo.to_arc(1, 2), geo.to_arc(4, 5)])
        desc = geo.describe_clock(segs)
        self.assertEqual(len(desc["ranges"]), 2)
        self.assertAlmostEqual(total, 2.0)


class TestOverlapLength(unittest.TestCase):
    def test_radial_overlap_length(self):
        self.assertAlmostEqual(geo.radial_overlap_length(0, 10, 5, 15), 5.0)
        self.assertEqual(geo.radial_overlap_length(0, 10, 10, 20), 0.0)  # 端点相接
        self.assertEqual(geo.radial_overlap_length(0, 5, 6, 8), 0.0)     # 分离
        self.assertAlmostEqual(geo.radial_overlap_length(2, 8, 3, 5), 2.0)  # 包含

    def test_arc_overlap_length_plain(self):
        self.assertAlmostEqual(
            geo.arc_overlap_length(geo.to_arc(1, 3), geo.to_arc(2, 4)), 1.0)
        # 仅端点相接：零长度重叠
        self.assertEqual(
            geo.arc_overlap_length(geo.to_arc(1, 3), geo.to_arc(3, 5)), 0.0)
        self.assertEqual(
            geo.arc_overlap_length(geo.to_arc(1, 2), geo.to_arc(4, 5)), 0.0)

    def test_arc_overlap_length_wraps_twelve(self):
        # 跨 12 点弧 11->1 与 0->2：仅 [0,1] 段重叠 1 小时
        self.assertAlmostEqual(
            geo.arc_overlap_length(geo.to_arc(11, 1), geo.to_arc(0, 2)), 1.0)
        # 两条都跨 12 点：两侧各重叠 0.5
        self.assertAlmostEqual(
            geo.arc_overlap_length(geo.to_arc(11, 1), geo.to_arc(11.5, 0.5)),
            1.0)


if __name__ == "__main__":
    unittest.main()
