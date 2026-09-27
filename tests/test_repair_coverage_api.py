"""修补覆盖核对端到端测试（FastAPI TestClient + 临时 SQLite 文件）。"""

import os
import tempfile
import unittest


class RepairCoverageApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_file = os.path.join(self._tmp.name, "test.db")
        os.environ["CRACK_DB_PATH"] = db_file

        from fastapi.testclient import TestClient
        from app import db as db_mod
        from app.main import app

        db_mod.DB_PATH = db_file
        db_mod.init_db()
        self.client = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()

    def _obs(self, oid, r0, r1, c0, c1, grade=2, conf=0.8, inspection="insp-1"):
        return {
            "observation_id": oid,
            "inspection_id": inspection,
            "radius_start": r0,
            "radius_end": r1,
            "clock_start": c0,
            "clock_end": c1,
            "grade": grade,
            "confidence": conf,
        }

    def _write(self, batch_id, observations, turbine="WT-01", blade="B3"):
        resp = self.client.post("/api/v1/observations/batch", json={
            "batch_id": batch_id,
            "turbine_id": turbine,
            "blade_id": blade,
            "auto_recompute": False,
            "observations": observations,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _coverage(self, payload, turbine="WT-01", blade="B3"):
        body = {
            "turbine_id": turbine,
            "blade_id": blade,
            "inspection_id": "insp-1",
            "regions": [],
        }
        body.update(payload)
        return self.client.post("/api/v1/repair-coverage", json=body)

    def _by_id(self, body):
        return {r["observation_id"]: r for r in body["results"]}

    # ----------------------------------------------------------- 基本核对

    def test_empty_regions_all_uncovered(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3)])
        resp = self._coverage({"regions": []})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["inspection_id"], "insp-1")
        self.assertEqual(body["region_count"], 0)
        item = self._by_id(body)["o1"]
        self.assertAlmostEqual(item["original_area"], 20.0)
        self.assertAlmostEqual(item["covered_area"], 0.0)
        self.assertEqual(item["coverage_ratio"], 0.0)
        self.assertEqual(item["status"], "uncovered")

        s = body["summary"]
        self.assertEqual(s["observation_count"], 1)
        self.assertEqual(s["checked_count"], 1)
        self.assertEqual(s["excluded_count"], 0)
        self.assertEqual(s["uncovered_count"], 1)
        self.assertEqual(s["partial_count"], 0)
        self.assertEqual(s["full_count"], 0)
        self.assertAlmostEqual(s["total_original_area"], 20.0)
        self.assertAlmostEqual(s["total_coverage_ratio"], 0.0)

    def test_full_and_partial_and_uncovered_statuses(self):
        self._write("b1", [
            self._obs("o-full", 0, 10, 1, 3),
            self._obs("o-part", 0, 10, 5, 7),
            self._obs("o-none", 20, 30, 9, 10),
        ])
        resp = self._coverage({"regions": [
            {"radius_start": 0, "radius_end": 10,
             "clock_start": 1, "clock_end": 3},      # 全覆盖 o-full
            {"radius_start": 0, "radius_end": 5,
             "clock_start": 5, "clock_end": 7},       # 半覆盖 o-part
        ]})
        self.assertEqual(resp.status_code, 200, resp.text)
        items = self._by_id(resp.json())
        self.assertEqual(items["o-full"]["status"], "full")
        self.assertEqual(items["o-part"]["status"], "partial")
        self.assertAlmostEqual(items["o-part"]["covered_area"], 10.0)
        self.assertAlmostEqual(items["o-part"]["coverage_ratio"], 0.5)
        self.assertEqual(items["o-none"]["status"], "uncovered")
        self.assertEqual(items["o-none"]["covered_area"], 0.0)

        s = resp.json()["summary"]
        self.assertEqual(s["observation_count"], 3)
        self.assertEqual(s["checked_count"], 3)
        self.assertEqual(s["uncovered_count"], 1)
        self.assertEqual(s["partial_count"], 1)
        self.assertEqual(s["full_count"], 1)
        self.assertAlmostEqual(s["total_original_area"], 50.0)  # 20+20+10
        self.assertAlmostEqual(s["total_covered_area"], 30.0)   # 20+10
        self.assertAlmostEqual(s["total_coverage_ratio"], 0.6)

    def test_overlapping_repairs_counted_once(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3)])
        resp = self._coverage({"regions": [
            {"radius_start": 0, "radius_end": 10,
             "clock_start": 1, "clock_end": 3},
            {"radius_start": 0, "radius_end": 10,
             "clock_start": 1, "clock_end": 3},
        ]})
        item = self._by_id(resp.json())["o1"]
        self.assertEqual(item["status"], "full")
        self.assertAlmostEqual(item["covered_area"], 20.0)

    def test_diagonal_strips_union_not_bounding_box(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3)])
        resp = self._coverage({"regions": [
            {"radius_start": 0, "radius_end": 5,
             "clock_start": 1, "clock_end": 2},
            {"radius_start": 5, "radius_end": 10,
             "clock_start": 2, "clock_end": 3},
        ]})
        item = self._by_id(resp.json())["o1"]
        self.assertEqual(item["status"], "partial")
        self.assertAlmostEqual(item["covered_area"], 10.0)

    def test_wrapping_clocks_allowed(self):
        self._write("b1", [self._obs("o1", 0, 10, 11, 1)])
        resp = self._coverage({"regions": [
            {"radius_start": 0, "radius_end": 10,
             "clock_start": 11.5, "clock_end": 0.5},
        ]})
        item = self._by_id(resp.json())["o1"]
        self.assertEqual(item["status"], "partial")
        self.assertAlmostEqual(item["covered_area"], 10.0)
        self.assertAlmostEqual(item["coverage_ratio"], 0.5)

    def test_only_reads_specified_inspection(self):
        self._write("b1", [
            self._obs("o1", 0, 10, 1, 3, inspection="insp-1"),
            self._obs("o2", 0, 10, 1, 3, inspection="insp-2"),
        ])
        resp = self._coverage({"inspection_id": "insp-1", "regions": []})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual([r["observation_id"] for r in body["results"]], ["o1"])

    # ----------------------------------------------------- 零面积单列

    def test_zero_area_observations_excluded(self):
        self._write("b1", [
            self._obs("o1", 0, 10, 1, 3),               # 正面积 20
            self._obs("o2", 5, 5, 1, 3),                # 径向零长
            self._obs("o3", 0, 10, 2, 2),               # 钟位单点
        ])
        resp = self._coverage({"regions": []})
        self.assertEqual(resp.status_code, 200, resp.text)
        items = self._by_id(resp.json())
        self.assertEqual(items["o1"]["status"], "uncovered")
        self.assertEqual(items["o2"]["status"], "excluded")
        self.assertEqual(items["o2"]["excluded_reason"], "zero_area")
        self.assertIsNone(items["o2"]["coverage_ratio"])
        self.assertEqual(items["o3"]["status"], "excluded")
        self.assertEqual(items["o3"]["excluded_reason"], "zero_area")

        s = resp.json()["summary"]
        self.assertEqual(s["observation_count"], 3)
        self.assertEqual(s["checked_count"], 1)
        self.assertEqual(s["excluded_count"], 2)
        self.assertAlmostEqual(s["total_original_area"], 20.0)
        self.assertAlmostEqual(s["total_coverage_ratio"], 0.0)

    def test_all_zero_area_summary_ratio_null(self):
        self._write("b1", [self._obs("o1", 5, 5, 1, 3)])
        resp = self._coverage({"regions": []})
        s = resp.json()["summary"]
        self.assertEqual(s["checked_count"], 0)
        self.assertEqual(s["excluded_count"], 1)
        self.assertIsNone(s["total_coverage_ratio"])

    # ----------------------------------------------------------- 错误分支

    def test_inspection_not_found_404(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3)])
        self.assertEqual(
            self._coverage({"inspection_id": "nope"}).status_code, 404)
        self.assertEqual(
            self._coverage({"inspection_id": "insp-1"},
                           turbine="WT-X", blade="B9").status_code, 404)

    def test_invalid_region_returns_422(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3)])
        # radius_end < radius_start
        r1 = self._coverage({"regions": [
            {"radius_start": 10, "radius_end": 5,
             "clock_start": 1, "clock_end": 3}]})
        self.assertEqual(r1.status_code, 422)
        # 钟位越界
        r2 = self._coverage({"regions": [
            {"radius_start": 0, "radius_end": 5,
             "clock_start": 1, "clock_end": 13}]})
        self.assertEqual(r2.status_code, 422)
        # 径向为负
        r3 = self._coverage({"regions": [
            {"radius_start": -1, "radius_end": 5,
             "clock_start": 1, "clock_end": 3}]})
        self.assertEqual(r3.status_code, 422)

    def test_missing_required_fields_422(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3)])
        resp = self.client.post("/api/v1/repair-coverage", json={
            "turbine_id": "WT-01", "blade_id": "B3",
            # 缺 inspection_id / regions
        })
        self.assertEqual(resp.status_code, 422)

    def test_unknown_field_422(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3)])
        resp = self.client.post("/api/v1/repair-coverage", json={
            "turbine_id": "WT-01", "blade_id": "B3",
            "inspection_id": "insp-1", "regions": [], "extra": 1,
        })
        self.assertEqual(resp.status_code, 422)

    # ----------------------------------------------------------- 只读语义

    def test_read_only_does_not_modify_anything(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3)])
        # 先落库一份复核结果
        rc = self.client.post("/api/v1/reviews/recompute",
                              json={"turbine_id": "WT-01", "blade_id": "B3"})
        self.assertEqual(rc.status_code, 200, rc.text)
        review_before = self.client.get("/api/v1/reviews/WT-01/B3").json()
        adj_before = self.client.get(
            "/api/v1/adjudications/WT-01/B3").json()

        resp = self._coverage({"regions": [
            {"radius_start": 0, "radius_end": 10,
             "clock_start": 1, "clock_end": 3}]})
        self.assertEqual(resp.status_code, 200, resp.text)
        # 多次调用结果一致
        again = self._coverage({"regions": [
            {"radius_start": 0, "radius_end": 10,
             "clock_start": 1, "clock_end": 3}]})
        self.assertEqual(resp.json(), again.json())

        review_after = self.client.get("/api/v1/reviews/WT-01/B3").json()
        adj_after = self.client.get("/api/v1/adjudications/WT-01/B3").json()
        self.assertEqual(review_before, review_after)
        self.assertEqual(adj_before, adj_after)

    def test_openapi_lists_endpoint(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertIn("/api/v1/repair-coverage", paths)


if __name__ == "__main__":
    unittest.main()
