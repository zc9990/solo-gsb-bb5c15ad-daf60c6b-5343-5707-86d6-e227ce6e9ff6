"""端到端 API 测试（FastAPI TestClient + 临时 SQLite 文件）。"""

import json
import os
import tempfile
import unittest


class ApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_file = os.path.join(self._tmp.name, "test.db")
        os.environ["CRACK_DB_PATH"] = db_file

        from fastapi.testclient import TestClient
        from app import db as db_mod
        from app.main import app

        db_mod.DB_PATH = db_file
        db_mod.init_db()
        # startup 钩子只做 init_db；此处已初始化，直接构造客户端
        self.client = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()

    def _obs(self, oid, r0, r1, c0, c1, grade, conf, inspection="insp-1"):
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

    def _batch(self, batch_id, turbine="WT-01", blade="B3",
               observations=None, auto_recompute=True):
        return {
            "batch_id": batch_id,
            "turbine_id": turbine,
            "blade_id": blade,
            "auto_recompute": auto_recompute,
            "observations": observations or [],
        }

    # ------------------------------------------------------------ health

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    # ------------------------------------------------------------- write

    def test_write_and_review_clusters(self):
        payload = self._batch("batch-1", observations=[
            # 1、2 在径向+钟位上相交 -> 同簇且等级冲突
            self._obs("o1", 0, 10, 1, 2, 1, 0.9),
            self._obs("o2", 8, 18, 2, 3, 3, 0.8),
            # 3 径向与 o2 相交但钟位分离 -> 独立簇
            self._obs("o3", 15, 25, 5, 6, 2, 0.7),
        ])
        resp = self.client.post("/api/v1/observations/batch", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "created")
        self.assertEqual(body["written"], 3)
        self.assertTrue(body["recomputed"])
        self.assertTrue(body["changed"])
        self.assertEqual(len(body["trajectories"]), 2)

        conflict_trj = next(t for t in body["trajectories"]
                            if t["observation_count"] == 2)
        self.assertTrue(conflict_trj["grade_conflict"])
        self.assertEqual(conflict_trj["grades"], [1, 3])
        self.assertEqual(len(conflict_trj["sources"]), 2)

        review = self.client.get("/api/v1/reviews/WT-01/B3").json()
        self.assertTrue(review["up_to_date"])
        self.assertEqual(review["trajectory_count"], 2)

    def test_idempotent_replay(self):
        payload = self._batch("batch-9", observations=[
            self._obs("x1", 0, 5, 1, 2, 1, 0.6),
        ])
        r1 = self.client.post("/api/v1/observations/batch", json=payload)
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post("/api/v1/observations/batch", json=payload)
        self.assertEqual(r2.status_code, 200)
        b2 = r2.json()
        self.assertEqual(b2["status"], "idempotent_replay")
        self.assertEqual(b2["written"], 0)
        self.assertEqual(b2["skipped_duplicates"], 1)

        review = self.client.get("/api/v1/reviews/WT-01/B3").json()
        self.assertEqual(review["observation_count"], 1)

    def test_batch_id_conflict_returns_409(self):
        p1 = self._batch("dup", observations=[
            self._obs("y1", 0, 5, 1, 2, 1, 0.6),
        ])
        self.assertEqual(self.client.post("/api/v1/observations/batch",
                                          json=p1).status_code, 200)
        p2 = self._batch("dup", observations=[
            self._obs("y2", 0, 5, 1, 2, 2, 0.6),
        ])
        resp = self.client.post("/api/v1/observations/batch", json=p2)
        self.assertEqual(resp.status_code, 409)

    # -------------------------------------------------------- recompute

    def test_recompute_by_batch_is_idempotent(self):
        payload = self._batch("rc-1", auto_recompute=False, observations=[
            self._obs("z1", 0, 5, 11, 1, 2, 0.7),
        ])
        self.client.post("/api/v1/observations/batch", json=payload)

        review = self.client.get("/api/v1/reviews/WT-01/B3").json()
        self.assertTrue(review["needs_recompute"])
        self.assertFalse(review["up_to_date"])
        self.assertEqual(review["trajectory_count"], 1)  # 实时计算兜底

        r1 = self.client.post("/api/v1/reviews/recompute",
                              json={"batch_id": "rc-1"})
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertTrue(r1.json()["changed"])

        r2 = self.client.post("/api/v1/reviews/recompute",
                              json={"batch_id": "rc-1"})
        self.assertFalse(r2.json()["changed"])
        self.assertEqual(r1.json()["trajectories"],
                         r2.json()["trajectories"])

        review = self.client.get("/api/v1/reviews/WT-01/B3").json()
        self.assertTrue(review["up_to_date"])

    def test_recompute_picks_up_new_observation(self):
        p1 = self._batch("n1", auto_recompute=False, observations=[
            self._obs("n1a", 0, 5, 1, 2, 1, 0.5),
        ])
        self.client.post("/api/v1/observations/batch", json=p1)
        p2 = self._batch("n2", auto_recompute=False, observations=[
            self._obs("n2a", 4, 9, 2, 3, 2, 0.6),  # 与 n1a 相交
        ])
        self.client.post("/api/v1/observations/batch", json=p2)

        resp = self.client.post("/api/v1/reviews/recompute",
                                json={"turbine_id": "WT-01", "blade_id": "B3"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["changed"])
        self.assertEqual(body["trajectory_count"], 1)
        self.assertTrue(body["trajectories"][0]["grade_conflict"])

    def test_recompute_unknown_batch_404(self):
        resp = self.client.post("/api/v1/reviews/recompute",
                                json={"batch_id": "nope"})
        self.assertEqual(resp.status_code, 404)

    def test_review_unknown_blade_404(self):
        resp = self.client.get("/api/v1/reviews/WT-X/B9")
        self.assertEqual(resp.status_code, 404)

    # --------------------------------------------------------- validation

    def test_validation_errors(self):
        # radius_end < radius_start
        bad_radius = self._batch("v1", observations=[
            self._obs("v1a", 10, 5, 1, 2, 1, 0.5),
        ])
        self.assertEqual(self.client.post(
            "/api/v1/observations/batch", json=bad_radius).status_code, 422)

        # confidence 越界
        bad_conf = self._batch("v2", observations=[
            self._obs("v2a", 0, 5, 1, 2, 1, 1.5),
        ])
        self.assertEqual(self.client.post(
            "/api/v1/observations/batch", json=bad_conf).status_code, 422)

        # 钟位越界
        bad_clock = self._batch("v3", observations=[
            self._obs("v3a", 0, 5, 1, 13, 1, 0.5),
        ])
        self.assertEqual(self.client.post(
            "/api/v1/observations/batch", json=bad_clock).status_code, 422)

    def test_openapi_available(self):
        resp = self.client.get("/openapi.json")
        self.assertEqual(resp.status_code, 200)
        paths = resp.json()["paths"]
        self.assertIn("/api/v1/observations/batch", paths)


if __name__ == "__main__":
    unittest.main()
