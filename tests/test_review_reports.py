"""叶片复核报告签发与核验的端到端 API 测试。

覆盖：
- 同事务签发：实时轨迹与来源、两类指纹、签发时间固化；
- 快照不可变：后续观察/裁决变化后 GET 仍返回签发原样；
- 核验只读：分别指出观察/裁决变化、revision 是否当前、is_current；
- 拒绝签发不留半成品：revision 不符、裁决冲突、空叶片、report_id 冲突；
- 核验为只读，不修改任何数据。
"""

import os
import tempfile
import unittest


def _obs(oid, r0, r1, c0=1, c1=2, grade=1, conf=0.5, inspection="insp-1"):
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


class ReviewReportApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_file = os.path.join(self._tmp.name, "test_report.db")
        os.environ["CRACK_DB_PATH"] = db_file

        from fastapi.testclient import TestClient
        from app import db as db_mod
        from app.main import app

        db_mod.DB_PATH = db_file
        db_mod.init_db()
        self.client = TestClient(app)
        self.db_mod = db_mod

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, observations, batch="b1", turbine="WT", blade="B",
               auto_recompute=False):
        payload = {
            "batch_id": batch,
            "turbine_id": turbine,
            "blade_id": blade,
            "auto_recompute": auto_recompute,
            "observations": observations,
        }
        resp = self.client.post("/api/v1/observations/batch", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _adj(self, a, b, relation, revision, turbine="WT", blade="B",
             adjudication_id=None):
        body = {
            "turbine_id": turbine,
            "blade_id": blade,
            "observation_a": a,
            "observation_b": b,
            "relation": relation,
            "revision": revision,
        }
        if adjudication_id is not None:
            body["adjudication_id"] = adjudication_id
        return self.client.post("/api/v1/adjudications", json=body)

    def _issue(self, revision=0, turbine="WT", blade="B", report_id=None):
        body = {
            "turbine_id": turbine,
            "blade_id": blade,
            "expected_adjudication_revision": revision,
        }
        if report_id is not None:
            body["report_id"] = report_id
        return self.client.post("/api/v1/review-reports", json=body)

    # ------------------------------------------------------------ 签发

    def test_issue_snapshot_contains_live_trajectories_and_fingerprints(self):
        self._write([_obs("o1", 0, 5), _obs("o2", 4, 9, grade=3)],
                    auto_recompute=False)
        resp = self._issue(0, report_id="rep-1")
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()

        self.assertEqual(body["report_id"], "rep-1")
        self.assertEqual(body["turbine_id"], "WT")
        self.assertEqual(body["blade_id"], "B")
        self.assertEqual(body["adjudication_revision"], 0)
        self.assertEqual(body["trajectory_count"], 1)
        # 轨迹实时生成：o1/o2 几何相交同簇，等级冲突且带来源清单
        trj = body["trajectories"][0]
        self.assertTrue(trj["grade_conflict"])
        self.assertEqual(trj["grades"], [1, 3])
        self.assertEqual(
            sorted(s["observation_id"] for s in trj["sources"]),
            ["o1", "o2"],
        )
        for src in trj["sources"]:
            self.assertIn("batch_id", src)
            self.assertIn("inspection_id", src)
        # 两类输入指纹与签发时间均已固化
        fps = body["fingerprints"]
        self.assertEqual(len(fps["observation_fingerprint"]), 64)
        self.assertEqual(len(fps["ruling_fingerprint"]), 64)
        self.assertTrue(body["issued_at"])

    def test_issue_generates_report_id_when_absent(self):
        self._write([_obs("o1", 0, 5)])
        resp = self._issue(0)
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertTrue(resp.json()["report_id"].startswith("report_"))

    def test_issue_reflects_must_link_adjudications(self):
        # 两条几何分离观察 + must_link -> 签发时实时合并为 1 条
        self._write([_obs("o1", 0, 2), _obs("o2", 5, 7)])
        adj_id = self._adj("o1", "o2", "must_link", 0).json()["adjudication_id"]

        resp = self._issue(1)
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["adjudication_revision"], 1)
        self.assertEqual(body["trajectory_count"], 1)
        self.assertEqual(body["trajectories"][0]["adjudication_ids"],
                         [adj_id])

    # ------------------------------------------------------ 快照不可变

    def test_get_snapshot_immutable_after_observations_change(self):
        self._write([_obs("o1", 0, 5)], auto_recompute=False)
        issued = self._issue(0, report_id="rep-imm-1").json()

        # 新增观察（与 o1 几何分离 -> 现在应有 2 条实时轨迹）
        self._write([_obs("o2", 40, 45)], batch="b2")
        review = self.client.get("/api/v1/reviews/WT/B").json()
        self.assertEqual(review["trajectory_count"], 2)

        got = self.client.get("/api/v1/review-reports/rep-imm-1")
        self.assertEqual(got.status_code, 200, got.text)
        # 报告快照原样不变
        self.assertEqual(got.json(), issued)
        self.assertEqual(got.json()["trajectory_count"], 1)

    def test_get_snapshot_immutable_after_rulings_change(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 5, 7)])
        issued = self._issue(0, report_id="rep-imm-2").json()
        self.assertEqual(issued["trajectory_count"], 2)

        # 新增 must_link 裁决：实时复核变为 1 条
        self._adj("o1", "o2", "must_link", 0)
        review = self.client.get("/api/v1/reviews/WT/B").json()
        self.assertEqual(review["trajectory_count"], 1)
        self.assertEqual(review["adjudication_revision"], 1)

        got = self.client.get("/api/v1/review-reports/rep-imm-2").json()
        self.assertEqual(got, issued)
        self.assertEqual(got["adjudication_revision"], 0)

    def test_get_unknown_report_404(self):
        resp = self.client.get("/api/v1/review-reports/nope")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("detail", resp.json())

    # -------------------------------------------------------------- 核验

    def test_verify_fresh_report_is_current(self):
        self._write([_obs("o1", 0, 5)])
        rid = self._issue(0).json()["report_id"]

        resp = self.client.get(f"/api/v1/review-reports/{rid}/verify")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["observations_changed"])
        self.assertFalse(body["rulings_changed"])
        self.assertTrue(body["revision_current"])
        self.assertTrue(body["is_current"])
        self.assertEqual(body["current_adjudication_revision"], 0)
        self.assertEqual(body["report_adjudication_revision"], 0)
        self.assertEqual(
            body["report_fingerprints"]["observation_fingerprint"],
            body["current_fingerprints"]["observation_fingerprint"],
        )

    def test_verify_detects_observation_change_only(self):
        self._write([_obs("o1", 0, 5)])
        rid = self._issue(0).json()["report_id"]
        self._write([_obs("o2", 40, 45)], batch="b2")

        body = self.client.get(f"/api/v1/review-reports/{rid}/verify").json()
        self.assertTrue(body["observations_changed"])
        self.assertFalse(body["rulings_changed"])
        self.assertTrue(body["revision_current"])  # 无裁决，revision 仍为 0
        self.assertFalse(body["is_current"])
        self.assertNotEqual(
            body["report_fingerprints"]["observation_fingerprint"],
            body["current_fingerprints"]["observation_fingerprint"],
        )

    def test_verify_detects_ruling_change_only(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 5, 7)])
        rid = self._issue(0).json()["report_id"]
        self._adj("o1", "o2", "must_link", 0)

        body = self.client.get(f"/api/v1/review-reports/{rid}/verify").json()
        self.assertFalse(body["observations_changed"])
        self.assertTrue(body["rulings_changed"])
        self.assertFalse(body["revision_current"])
        self.assertEqual(body["current_adjudication_revision"], 1)
        self.assertEqual(body["report_adjudication_revision"], 0)
        self.assertFalse(body["is_current"])

    def test_verify_unknown_report_404(self):
        resp = self.client.get("/api/v1/review-reports/ghost/verify")
        self.assertEqual(resp.status_code, 404)

    def test_verify_is_read_only(self):
        self._write([_obs("o1", 0, 5)])
        rid = self._issue(0).json()["report_id"]
        before = self.client.get(f"/api/v1/review-reports/{rid}").json()

        self._write([_obs("o2", 40, 45)], batch="b2")
        for _ in range(2):
            self.client.get(f"/api/v1/review-reports/{rid}/verify")

        # 报告快照与库内行数均不变
        after = self.client.get(f"/api/v1/review-reports/{rid}").json()
        self.assertEqual(before, after)
        with self.db_mod.get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM review_reports"
            ).fetchone()["c"]
        self.assertEqual(n, 1)

    # ---------------------------------------------------- 拒绝签发

    def test_issue_stale_revision_rejected_without_partial_report(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 5, 7)])
        self._adj("o1", "o2", "must_link", 0)  # 当前 revision=1

        resp = self._issue(0, report_id="rep-stale")
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body["current_revision"], 1)
        self.assertEqual(body["expected_revision"], 0)

        # 没有任何报告行（含被拒绝的编号）
        self.assertEqual(
            self.client.get("/api/v1/review-reports/rep-stale").status_code,
            404,
        )
        with self.db_mod.get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM review_reports"
            ).fetchone()["c"]
        self.assertEqual(n, 0)

        # 带正确 revision 可正常签发
        self.assertEqual(self._issue(1, report_id="rep-ok").status_code, 201)

    def test_issue_ruling_conflict_rejected_without_partial_report(self):
        # o1/o2 几何同簇，禁止同轨 -> 实时成轨必然冲突
        self._write([_obs("o1", 0, 5), _obs("o2", 4, 9)])
        adj_id = self._adj("o1", "o2", "cannot_link", 0).json()[
            "adjudication_id"]

        resp = self._issue(1, report_id="rep-conflict")
        self.assertEqual(resp.status_code, 409)
        # 沿用现有裁决冲突错误格式
        self.assertEqual(
            resp.json()["cannot_link_adjudication_id"], adj_id)
        self.assertEqual(resp.json()["must_link_chain"], [])

        self.assertEqual(
            self.client.get(
                "/api/v1/review-reports/rep-conflict").status_code,
            404,
        )
        with self.db_mod.get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM review_reports"
            ).fetchone()["c"]
        self.assertEqual(n, 0)

    def test_issue_on_empty_blade_404(self):
        resp = self._issue(0, turbine="WT", blade="EMPTY")
        self.assertEqual(resp.status_code, 404)
        with self.db_mod.get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM review_reports"
            ).fetchone()["c"]
        self.assertEqual(n, 0)

    def test_issue_duplicate_report_id_409(self):
        self._write([_obs("o1", 0, 5)])
        self.assertEqual(
            self._issue(0, report_id="rep-dup").status_code, 201)
        resp = self._issue(0, report_id="rep-dup")
        self.assertEqual(resp.status_code, 409)
        with self.db_mod.get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM review_reports"
            ).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_issue_validation_error_422(self):
        self._write([_obs("o1", 0, 5)])
        # 缺少必填的 expected_adjudication_revision
        resp = self.client.post("/api/v1/review-reports",
                                json={"turbine_id": "WT", "blade_id": "B"})
        self.assertEqual(resp.status_code, 422)
        # 多余字段拒绝（extra="forbid"）
        resp = self.client.post("/api/v1/review-reports", json={
            "turbine_id": "WT", "blade_id": "B",
            "expected_adjudication_revision": 0, "bogus": 1,
        })
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
