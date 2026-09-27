"""人工关联裁决的端到端 API 测试。

覆盖：必须同轨传递闭包、禁止同轨 409 与冲突裁决链、乐观锁、
同叶片引用校验、请求幂等、新增观察后裁决继续生效。
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


class AdjudicationApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_file = os.path.join(self._tmp.name, "test_adj.db")
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

    def _recompute(self, turbine="WT", blade="B"):
        return self.client.post(
            "/api/v1/reviews/recompute",
            json={"turbine_id": turbine, "blade_id": blade},
        )

    # ----------------------------------------------------- must_link 生效

    def test_must_link_merges_disjoint_observations(self):
        # 三条几何互不相交的观察 -> 初始 3 簇
        self._write([_obs("o1", 0, 2), _obs("o2", 5, 7), _obs("o3", 9, 11)],
                    auto_recompute=True)
        review = self.client.get("/api/v1/reviews/WT/B").json()
        self.assertEqual(review["trajectory_count"], 3)
        self.assertEqual(review["adjudication_revision"], 0)

        r = self._adj("o1", "o2", "must_link", 0)
        self.assertEqual(r.status_code, 200, r.text)
        adj1 = r.json()["adjudication_id"]
        self.assertEqual(r.json()["revision"], 1)

        body = self._recompute().json()
        self.assertEqual(body["trajectory_count"], 2)
        self.assertEqual(body["adjudication_revision"], 1)
        merged = next(t for t in body["trajectories"]
                      if t["observation_count"] == 2)
        self.assertEqual(merged["adjudication_ids"], [adj1])

    def test_must_link_transitive_closure_and_hits(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 5, 7), _obs("o3", 9, 11)])
        r = self._adj("o1", "o2", "must_link", 0)
        adj1 = r.json()["adjudication_id"]
        r = self._adj("o2", "o3", "must_link", 1)
        adj2 = r.json()["adjudication_id"]
        self.assertEqual(r.json()["revision"], 2)

        body = self._recompute().json()
        self.assertEqual(body["trajectory_count"], 1)
        trj = body["trajectories"][0]
        self.assertEqual(sorted(trj["adjudication_ids"]),
                         sorted([adj1, adj2]))
        self.assertEqual(trj["observation_count"], 3)

        # 查询接口同样回出生效 revision 与命中裁决
        review = self.client.get("/api/v1/reviews/WT/B").json()
        self.assertTrue(review["up_to_date"])
        self.assertEqual(review["adjudication_revision"], 2)
        self.assertEqual(review["trajectories"][0]["adjudication_ids"],
                         sorted([adj1, adj2]))

    # ------------------------------------------------- cannot_link 冲突

    def test_cannot_link_conflict_returns_409_chain_and_keeps_old(self):
        # o1/o2 几何同簇；o3 独立。先建立必须同轨 o1-o3 使三者同闭包
        self._write([_obs("o1", 0, 5), _obs("o2", 4, 9), _obs("o3", 9, 12,
                                                               c0=7, c1=8)])
        # 几何：o1~o2 同簇，o3 独立 -> 2 条
        self._recompute()
        adj1 = self._adj("o2", "o3", "must_link", 0).json()["adjudication_id"]
        body = self._recompute().json()
        self.assertEqual(body["trajectory_count"], 1)  # 强制合并后 1 条

        cl = self._adj("o1", "o3", "cannot_link", 1).json()["adjudication_id"]

        resp = self._recompute()
        self.assertEqual(resp.status_code, 409)
        payload = resp.json()
        self.assertEqual(payload["cannot_link_adjudication_id"], cl)
        # 冲突链必须经过 o2~o3 的 must_link 裁决
        self.assertEqual(payload["must_link_chain"], [adj1])

        # 旧结果未改写：查询仍是合并后的 1 条，并携带冲突标记
        review = self.client.get("/api/v1/reviews/WT/B").json()
        self.assertEqual(review["trajectory_count"], 1)
        self.assertFalse(review["up_to_date"])
        self.assertIsNotNone(review["adjudication_conflict"])
        self.assertEqual(
            review["adjudication_conflict"]["cannot_link_adjudication_id"], cl)

        # 再重算仍然 409（结果稳定未被部分写入）
        self.assertEqual(self._recompute().status_code, 409)

    def test_cannot_link_on_geometry_only_cluster_has_empty_chain(self):
        self._write([_obs("o1", 0, 5), _obs("o2", 4, 9)])
        self._adj("o1", "o2", "cannot_link", 0)
        resp = self._recompute()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["must_link_chain"], [])

    def test_cannot_link_between_separate_clusters_is_satisfied(self):
        # 几何分离、也无 must_link：禁止同轨天然满足，正常出两条轨迹
        self._write([_obs("o1", 0, 2), _obs("o2", 9, 11)])
        self._adj("o1", "o2", "cannot_link", 0)
        resp = self._recompute()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["trajectory_count"], 2)

    # -------------------------------------------------------- 乐观锁

    def test_stale_revision_rejected_with_current(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 5, 7)])
        self.assertEqual(self._adj("o1", "o2", "must_link", 0).status_code, 200)
        resp = self._adj("o1", "o2", "cannot_link", 0)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("revision", resp.json())
        self.assertEqual(resp.json()["revision"], 1)

    # -------------------------------------------------------- 引用校验

    def test_reference_must_be_existing_same_blade_observation(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 5, 7)])
        # 引用不存在观察 -> 404
        resp = self._adj("o1", "ghost", "must_link", 0)
        self.assertEqual(resp.status_code, 404)
        # 引用其他叶片的观察 -> 422
        self._write([_obs("x1", 0, 2)], batch="b-other",
                    turbine="WT", blade="OTHER")
        resp = self._adj("o1", "x1", "must_link", 0, blade="B")
        self.assertEqual(resp.status_code, 422)
        # 同一观察与自身 -> 422
        resp = self._adj("o1", "o1", "must_link", 0)
        self.assertEqual(resp.status_code, 422)

    # ---------------------------------------------------------- 幂等

    def test_same_request_idempotent_revision_not_bumped(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 5, 7)])
        r1 = self._adj("o1", "o2", "must_link", 0, adjudication_id="adj-x")
        self.assertEqual(r1.status_code, 200)
        # 反向观察对 + 相同关系 = 相同请求；携带任意/过期 revision 也幂等
        r2 = self._adj("o2", "o1", "must_link", 99, adjudication_id="adj-x")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["adjudication_id"], "adj-x")
        self.assertEqual(r2.json()["revision"], 1)

        # 同观察对不同关系是不同裁决，需要正确 revision
        r3 = self._adj("o1", "o2", "cannot_link", 0)
        self.assertEqual(r3.status_code, 409)
        r4 = self._adj("o1", "o2", "cannot_link", 1)
        self.assertEqual(r4.status_code, 200)
        self.assertEqual(r4.json()["revision"], 2)

        # 相同自然键但换一个 adjudication_id -> 冲突
        r5 = self._adj("o1", "o2", "must_link", 2, adjudication_id="adj-y")
        self.assertEqual(r5.status_code, 409)

        lst = self.client.get("/api/v1/adjudications/WT/B").json()
        self.assertEqual(lst["revision"], 2)
        self.assertEqual(lst["count"], 2)

    # ---------------------------------------- 新增观察后裁决继续生效

    def test_rulings_remain_effective_after_new_observations(self):
        self._write([_obs("o1", 0, 2), _obs("o2", 9, 11)])
        self._adj("o1", "o2", "must_link", 0)
        body = self._recompute().json()
        self.assertEqual(body["trajectory_count"], 1)

        # 新增观察（跨批次），不自动重算；裁决仍生效，实时计算也是 1 条
        self._write([_obs("o3", 20, 22)], batch="b2")
        review = self.client.get("/api/v1/reviews/WT/B").json()
        self.assertTrue(review["needs_recompute"])
        self.assertEqual(review["trajectory_count"], 2)
        hit = next(t for t in review["trajectories"]
                   if t["observation_count"] == 2)
        self.assertEqual(len(hit["adjudication_ids"]), 1)

        # 显式重算后落库，revision 沿用
        body = self._recompute().json()
        self.assertTrue(body["changed"])
        self.assertEqual(body["trajectory_count"], 2)
        self.assertEqual(body["adjudication_revision"], 1)

    def test_auto_recompute_conflict_does_not_lose_observations(self):
        # 初始几何同簇 + 禁止同轨，写入即冲突
        resp = self._write(
            [_obs("o1", 0, 5), _obs("o2", 4, 9)],
            auto_recompute=False,
        )
        self._adj("o1", "o2", "cannot_link", 0)
        resp = self.client.post(
            "/api/v1/observations/batch",
            json={
                "batch_id": "b2", "turbine_id": "WT", "blade_id": "B",
                "auto_recompute": True,
                "observations": [_obs("o3", 20, 22)],
            },
        )
        # 观察已落库，仅自动重算触发 409
        self.assertEqual(resp.status_code, 409)
        review = self.client.get("/api/v1/reviews/WT/B").json()
        self.assertEqual(review["observation_count"], 3)
        self.assertIsNotNone(review["adjudication_conflict"])


if __name__ == "__main__":
    unittest.main()
