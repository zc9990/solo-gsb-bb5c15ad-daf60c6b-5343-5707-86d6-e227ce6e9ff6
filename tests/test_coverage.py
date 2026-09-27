"""修补覆盖核对的精确几何单元测试（纯标准库，可离线运行）。

覆盖面积必须是修补区域**并集**的精确面积：
- 重叠修补只计一次（不用外包框、不用采样近似）；
- 钟位弧允许跨 12 点；
- 零面积观察单列 excluded。
"""

import random
import unittest

from app import coverage as cov
from app import geometry as geo
from app.clustering import Observation


def _obs(oid, r0, r1, c0, c1, inspection="insp-1"):
    return Observation(
        observation_id=oid, inspection_id=inspection, inspected_at=None,
        batch_id="b", turbine_id="T", blade_id="B",
        radius_start=r0, radius_end=r1,
        clock_start=c0, clock_end=c1, grade=2, confidence=0.8,
    )


def _reg(r0, r1, c0, c1):
    return cov.make_region(r0, r1, c0, c1)


def _grid_covered_area(obs, regions, step=0.02):
    """独立参考实现：细密网格点采样统计覆盖比例（仅用于交叉验证，
    业务实现不得使用采样）。"""
    r0, r1 = obs.radius_start, obs.radius_end
    if r1 <= r0:
        return 0.0
    o_segs = [s for s in geo.to_arc(obs.clock_start, obs.clock_end)
              if s[1] > s[0]]
    if not o_segs:
        return 0.0

    clipped = []
    for reg in regions:
        lo, hi = max(r0, reg.radius_start), min(r1, reg.radius_end)
        if hi <= lo:
            continue
        clipped.append((lo, hi, [s for s in reg.arc if s[1] > s[0]]))

    hits = total = 0
    n_r = int(round((r1 - r0) / step))
    n_c = int(round(12.0 / step))
    for i in range(n_r):
        r = r0 + (i + 0.5) * step
        for j in range(n_c):
            c = (j + 0.5) * step
            if c >= 12.0:
                c = 11.9999
            in_obs = any(lo <= c <= hi for lo, hi in o_segs)
            if not in_obs:
                # 观察钟位弧外的网格点不进入面积基数
                continue
            total += 1
            for lo, hi, segs in clipped:
                if lo <= r <= hi and any(a <= c <= b for a, b in segs):
                    hits += 1
                    break
    full_area = (r1 - r0) * sum(b - a for a, b in o_segs)
    return full_area * hits / total if total else 0.0


class TestRepairCoverage(unittest.TestCase):
    def test_empty_regions_means_no_repair(self):
        o = _obs("o1", 0, 10, 1, 3)  # 面积 10 × 2 = 20
        self.assertAlmostEqual(cov.covered_area(o, []), 0.0)
        item = cov.check_observation(o, [])
        self.assertEqual(item["status"], "uncovered")
        self.assertEqual(item["covered_area"], 0.0)
        self.assertEqual(item["coverage_ratio"], 0.0)
        self.assertAlmostEqual(item["original_area"], 20.0)

    def test_exact_full_coverage_single_region(self):
        o = _obs("o1", 0, 10, 1, 3)
        item = cov.check_observation(o, [_reg(0, 10, 1, 3)])
        self.assertEqual(item["status"], "full")
        self.assertAlmostEqual(item["covered_area"], 20.0)
        self.assertAlmostEqual(item["coverage_ratio"], 1.0)

    def test_radial_partial(self):
        o = _obs("o1", 0, 10, 1, 3)  # 20
        item = cov.check_observation(o, [_reg(5, 10, 1, 3)])  # 5 × 2 = 10
        self.assertEqual(item["status"], "partial")
        self.assertAlmostEqual(item["covered_area"], 10.0)
        self.assertAlmostEqual(item["coverage_ratio"], 0.5)

    def test_clock_partial(self):
        o = _obs("o1", 0, 10, 1, 3)  # 20
        item = cov.check_observation(o, [_reg(0, 10, 1, 2)])  # 10 × 1
        self.assertEqual(item["status"], "partial")
        self.assertAlmostEqual(item["covered_area"], 10.0)

    def test_identical_overlapping_regions_counted_once(self):
        o = _obs("o1", 0, 10, 1, 3)
        regions = [_reg(0, 10, 1, 3), _reg(0, 10, 1, 3), _reg(0, 10, 1, 3)]
        item = cov.check_observation(o, regions)
        self.assertEqual(item["status"], "full")
        self.assertAlmostEqual(item["covered_area"], 20.0)  # 不是 60

    def test_clock_adjacent_regions_merge_to_full(self):
        # 两条修补在钟位 2 点处相接：并集弧长 2，非外包框近似
        o = _obs("o1", 0, 10, 1, 3)
        item = cov.check_observation(o, [_reg(0, 10, 1, 2),
                                         _reg(0, 10, 2, 3)])
        self.assertEqual(item["status"], "full")
        self.assertAlmostEqual(item["covered_area"], 20.0)

    def test_radial_adjacent_regions_merge(self):
        o = _obs("o1", 0, 10, 1, 3)
        item = cov.check_observation(o, [_reg(0, 5, 1, 3),
                                         _reg(5, 10, 1, 3)])
        self.assertEqual(item["status"], "full")
        self.assertAlmostEqual(item["covered_area"], 20.0)

    def test_diagonal_strips_union_not_bounding_box(self):
        # 反对外包框/采样的关键用例：
        # 修补 A 覆盖 [0,5]×钟位[1,2]（面积 5），
        # 修补 B 覆盖 [5,10]×钟位[2,3]（面积 5），并集仅 10；
        # 外包框会错算成整个观察面积 20。
        o = _obs("o1", 0, 10, 1, 3)
        item = cov.check_observation(o, [_reg(0, 5, 1, 2),
                                         _reg(5, 10, 2, 3)])
        self.assertEqual(item["status"], "partial")
        self.assertAlmostEqual(item["covered_area"], 10.0)
        self.assertAlmostEqual(item["coverage_ratio"], 0.5)

    def test_radial_overlap_with_clock_split_union(self):
        # 同一径向带内两条钟位分离的修补：环向去重后弧长 1.5
        o = _obs("o1", 0, 10, 1, 4)  # 面积 30
        item = cov.check_observation(o, [_reg(0, 10, 1, 2),
                                         _reg(0, 10, 2.5, 3)])
        self.assertEqual(item["status"], "partial")
        self.assertAlmostEqual(item["covered_area"], 15.0)

    def test_regions_clipped_to_observation(self):
        # 修补范围完全包住观察：超出部分裁掉，覆盖 = 观察面积
        o = _obs("o1", 5, 10, 1, 3)  # 10
        item = cov.check_observation(o, [_reg(0, 20, 0, 4)])
        self.assertEqual(item["status"], "full")
        self.assertAlmostEqual(item["covered_area"], 10.0)

    def test_partial_radial_overlap_only_half_band(self):
        # 修补在径向 [2,7] 与观察 [0,10] 正重叠 5
        o = _obs("o1", 0, 10, 1, 3)
        item = cov.check_observation(o, [_reg(2, 7, 1, 3)])
        self.assertAlmostEqual(item["covered_area"], 10.0)

    def test_disjoint_region_covers_nothing(self):
        o = _obs("o1", 0, 10, 1, 3)
        self.assertAlmostEqual(cov.covered_area(o, [_reg(20, 30, 1, 3)]), 0.0)
        self.assertAlmostEqual(cov.covered_area(o, [_reg(0, 10, 5, 6)]), 0.0)
        # 仅径向端点相接：零面积贡献
        self.assertAlmostEqual(cov.covered_area(o, [_reg(10, 20, 1, 3)]), 0.0)

    def test_wrapping_observation_and_regions(self):
        # 观察 11->1（跨 12，弧长 2），修补 11.5->0.5（弧长 1）
        o = _obs("o1", 0, 10, 11, 1)  # 面积 20
        item = cov.check_observation(o, [_reg(0, 10, 11.5, 0.5)])
        self.assertEqual(item["status"], "partial")
        self.assertAlmostEqual(item["covered_area"], 10.0)
        self.assertAlmostEqual(item["coverage_ratio"], 0.5)

    def test_wrapping_region_encloses_observation(self):
        o = _obs("o1", 0, 10, 11, 1)  # 弧长 2，面积 20
        item = cov.check_observation(o, [_reg(0, 10, 10, 2)])  # 弧长 4 包住
        self.assertEqual(item["status"], "full")
        self.assertAlmostEqual(item["covered_area"], 20.0)

    def test_wrapping_region_diagonal_strips(self):
        # 跨 12 的对角条带并集：A [0,5]×11->12，B [5,10]×0->1
        o = _obs("o1", 0, 10, 11, 1)  # 面积 20
        item = cov.check_observation(o, [_reg(0, 5, 11, 12),
                                         _reg(5, 10, 0, 1)])
        self.assertEqual(item["status"], "partial")
        self.assertAlmostEqual(item["covered_area"], 10.0)

    def test_region_clock_twelve_normalized(self):
        # 修补钟位 12 与 0 等价
        o = _obs("o1", 0, 10, 11, 1)
        item = cov.check_observation(o, [_reg(0, 10, 11.5, 12),
                                         _reg(0, 10, 0, 0.5)])
        self.assertAlmostEqual(item["covered_area"], 10.0)

    def test_zero_radius_observation_excluded(self):
        o = _obs("o1", 5, 5, 1, 3)
        item = cov.check_observation(o, [_reg(0, 10, 1, 3)])
        self.assertEqual(item["status"], "excluded")
        self.assertEqual(item["excluded_reason"], "zero_area")
        self.assertEqual(item["original_area"], 0.0)
        self.assertIsNone(item["coverage_ratio"])

    def test_zero_clock_observation_excluded(self):
        o = _obs("o1", 0, 10, 2, 2)  # 单点钟位
        item = cov.check_observation(o, [_reg(0, 10, 1, 3)])
        self.assertEqual(item["status"], "excluded")
        self.assertEqual(item["excluded_reason"], "zero_area")
        self.assertIsNone(item["coverage_ratio"])

    def test_random_cases_against_grid_reference(self):
        rng = random.Random(20260927)
        for case in range(12):
            r0 = rng.uniform(0, 8)
            r1 = r0 + rng.uniform(0.5, 10)
            c0 = rng.uniform(0, 12)
            c1 = c0 + rng.uniform(0.5, 6)
            c0, c1 = c0 % 12, c1 % 12
            o = _obs(f"o{case}", r0, r1, c0, c1)

            regions = []
            for _ in range(rng.randint(1, 5)):
                rr0 = rng.uniform(0, r1 + 2)
                rr1 = rr0 + rng.uniform(0.2, 6)
                cc0 = rng.uniform(0, 12)
                cc1 = cc0 + rng.uniform(0.2, 5)
                regions.append(_reg(rr0, rr1, cc0 % 12, cc1 % 12))

            exact = cov.covered_area(o, regions)
            reference = _grid_covered_area(o, regions, step=0.02)
            original = cov.rectangle_area(
                o.radius_start, o.radius_end,
                geo.to_arc(o.clock_start, o.clock_end))
            # 网格误差：网格面积 × 相对步长量级
            tol = max(0.05, original * 0.01)
            self.assertAlmostEqual(
                exact, reference, delta=tol,
                msg=f"case {case}: exact={exact} grid={reference}")
            self.assertLessEqual(exact, original + 1e-9)


if __name__ == "__main__":
    unittest.main()
