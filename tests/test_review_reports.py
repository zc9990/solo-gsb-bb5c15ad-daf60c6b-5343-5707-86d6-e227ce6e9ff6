"""复核报告签发与核验的端到端 API 测试。

覆盖：签发固化快照、乐观锁版本校验、裁决冲突拒绝签发且不留半成品、
快照不可变、核验分别指出观察/裁决变化、核验只读、签发幂等。
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

    def _adj(self, a, b, relation, revision, turbine="WT", blade="B"):
        return self.client.post("/api/v1/adjudications", json={
            "turbine_id": turbine,
            "blade_id": blade,
            "observation_a": a,
            "observation_b": b,
            "relation": relation,
            "revision": revision,
        })

    def _issue(self, expected_revision, turbine="WT", blade="B",
               report_id=None):
        body = {
            "turbine_id": turbine,
            "blade_id": blade,
            "expected_adjudication_revision": expected_revision,
        }
        if report_id is not None:
            body["report_id"] = report_id
        return self.client.post("/api/v1/review-reports", json=body)

    # ---------------------------------------------------------- 签发

    def test_issue_freezes_snapshot(self):
        self._write([_obs("o1", 0, 5), _obs("o2", 4, 9, c0=2, c1=3)])
        resp = self._issue(0, report_id="rr-1")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["report_id"], "rr-1")
        self.assertEqual(body["turbine_id"], "WT")
        self.assertEqual(body["blade_id"], "B")
        self.assertEqual(body["adjudication_revision"], 0)
        self.assertEqual(body["observation_count"], 2)
        self.assertEqual(body["trajectory_count"], 1)
        self.assertEqual(len(body["trajectories"]), 1)
        self.assertEqual(len(body["observation_fingerprint"]), 64)
        self.assertEqual(len(body["adjudication_fingerprint"]), 64)
        self.assertTrue(body["issued_at"])
        # 轨迹带来源清单
        self.assertEqual(len(body["trajectories"][0]["sources"]), 2)

        # GET 返回与签发响应一致的快照
        got = self.client.get("/api/v1/review-reports/rr-1")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json(), body)

    def test_issue_generates_report_id_when_absent(self):
        self._write([_obs("o1", 0, 2)])
        resp = self._issue(0)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["report_id"].startswith("rr_"))

    def test_issue_unknown_blade_404(self):
        resp = self._issue(0, turbine="WT", blade="GHOST")
        self.assertEqual(resp.status_code, 404)

    # -------------------------------------------------------- 乐观锁

    def test_stale_revision_rejected_without_partial_report(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 9, 11)])
        r = self._adj("o1", "o2", "must_link", 0)
        self.assertEqual(r.status_code, 200)  # revision -> 1

        resp = self._issue(0, report_id="rr-2")  # 过期版本
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["adjudication_revision"], 1)
        # 不留半成品
        self.assertEqual(
            self.client.get("/api/v1/review-reports/rr-2").status_code, 404)

        # 携带正确版本签发成功
        ok = self._issue(1, report_id="rr-2")
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["adjudication_revision"], 1)

    # ------------------------------------------------------ 裁决冲突

    def test_ruling_conflict_rejected_without_partial_report(self):
        self._write([_obs("o1", 0, 5), _obs("o2", 4, 9)])
        self._adj("o1", "o2", "cannot_link", 0)

        resp = self._issue(1, report_id="rr-3")
        self.assertEqual(resp.status_code, 409)
        payload = resp.json()
        self.assertIn("cannot_link_adjudication_id", payload)
        self.assertIn("must_link_chain", payload)
        # 不留半成品
        self.assertEqual(
            self.client.get("/api/v1/review-reports/rr-3").status_code, 404)

    # ------------------------------------------------------ 快照不可变

    def test_snapshot_immutable_after_later_changes(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 9, 11)])
        issued = self._issue(0, report_id="rr-4").json()
        self.assertEqual(issued["trajectory_count"], 2)

        # 签发后新增观察与新裁决
        self._write([_obs("o3", 20, 22)], batch="b2")
        self._adj("o1", "o2", "must_link", 0)

        got = self.client.get("/api/v1/review-reports/rr-4")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json(), issued)  # 完全保持签发时原样

    # ---------------------------------------------------------- 核验

    def test_verify_distinguishes_observation_and_adjudication_changes(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 9, 11)])
        self._issue(0, report_id="rr-5")

        v0 = self.client.get("/api/v1/review-reports/rr-5/verify").json()
        self.assertFalse(v0["observation_changed"])
        self.assertFalse(v0["adjudication_changed"])
        self.assertTrue(v0["is_current"])
        self.assertEqual(v0["report_adjudication_revision"], 0)
        self.assertEqual(v0["current_adjudication_revision"], 0)
        self.assertEqual(v0["report_observation_fingerprint"],
                         v0["current_observation_fingerprint"])
        self.assertEqual(v0["report_adjudication_fingerprint"],
                         v0["current_adjudication_fingerprint"])

        # 仅新增观察 -> 仅观察变化
        self._write([_obs("o3", 20, 22)], batch="b2")
        v1 = self.client.get("/api/v1/review-reports/rr-5/verify").json()
        self.assertTrue(v1["observation_changed"])
        self.assertFalse(v1["adjudication_changed"])
        self.assertFalse(v1["is_current"])

        # 再新增裁决 -> 两类均变化
        self._adj("o1", "o2", "must_link", 0)
        v2 = self.client.get("/api/v1/review-reports/rr-5/verify").json()
        self.assertTrue(v2["observation_changed"])
        self.assertTrue(v2["adjudication_changed"])
        self.assertFalse(v2["is_current"])
        self.assertEqual(v2["report_adjudication_revision"], 0)
        self.assertEqual(v2["current_adjudication_revision"], 1)
        self.assertNotEqual(v2["report_observation_fingerprint"],
                            v2["current_observation_fingerprint"])
        self.assertNotEqual(v2["report_adjudication_fingerprint"],
                            v2["current_adjudication_fingerprint"])

    def test_verify_is_read_only(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 9, 11)])
        self._issue(0, report_id="rr-6")

        before_adj = self.client.get("/api/v1/adjudications/WT/B").json()
        before_review = self.client.get("/api/v1/reviews/WT/B").json()
        for _ in range(2):
            resp = self.client.get("/api/v1/review-reports/rr-6/verify")
            self.assertEqual(resp.status_code, 200)
        after_adj = self.client.get("/api/v1/adjudications/WT/B").json()
        after_review = self.client.get("/api/v1/reviews/WT/B").json()
        self.assertEqual(before_adj, after_adj)
        self.assertEqual(before_review, after_review)

    def test_get_and_verify_unknown_report_404(self):
        self.assertEqual(
            self.client.get("/api/v1/review-reports/nope").status_code, 404)
        self.assertEqual(
            self.client.get("/api/v1/review-reports/nope/verify").status_code,
            404)

    # ---------------------------------------------------------- 幂等

    def test_issue_idempotent_replay(self):
        self._write([_obs("o1", 0, 2)])
        r1 = self._issue(0, report_id="rr-7")
        self.assertEqual(r1.status_code, 200, r1.text)
        r2 = self._issue(0, report_id="rr-7")
        self.assertEqual(r2.status_code, 200, r2.text)
        # 含签发时间在内完全一致（未重新生成）
        self.assertEqual(r1.json(), r2.json())

        # 同 report_id 但请求不同（叶片或预期版本不一致）-> 409
        self.assertEqual(self._issue(0, report_id="rr-7",
                                     blade="OTHER").status_code, 409)
        self.assertEqual(self._issue(3, report_id="rr-7").status_code, 409)


if __name__ == "__main__":
    unittest.main()
