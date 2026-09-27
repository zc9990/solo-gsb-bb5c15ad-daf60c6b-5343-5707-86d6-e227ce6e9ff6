"""巡检变化报告端到端测试（FastAPI TestClient + 临时 SQLite 文件）。"""

import os
import tempfile
import unittest

from app.clustering import Observation, build_all


def _mk_obs(oid, r0, r1, c0, c1, inspection, turbine="WT-01", blade="B3"):
    return Observation(
        observation_id=oid, inspection_id=inspection, inspected_at=None,
        batch_id="b", turbine_id=turbine, blade_id=blade,
        radius_start=r0, radius_end=r1,
        clock_start=c0, clock_end=c1, grade=2, confidence=0.8,
    )


class InspectionChangesTest(unittest.TestCase):
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
            "observations": observations,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _changes(self, before, after, turbine="WT-01", blade="B3"):
        return self.client.get(
            f"/api/v1/inspection-changes/{turbine}/{blade}",
            params={"before": before, "after": after},
        )

    # ----------------------------------------------------- 基本分类与字段

    def test_persisting_new_disappeared(self):
        self._write("b1", [
            self._obs("o1", 0, 10, 1, 3, grade=2, conf=0.9, inspection="i1"),
            self._obs("o2", 20, 30, 5, 7, grade=4, conf=0.7, inspection="i1"),
        ])
        self._write("b2", [
            self._obs("p1", 5, 15, 2, 4, grade=3, conf=0.95, inspection="i2"),
            self._obs("p2", 40, 50, 8, 10, grade=1, conf=0.6, inspection="i2"),
        ])
        resp = self._changes("i1", "i2")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()

        self.assertEqual(body["turbine_id"], "WT-01")
        self.assertEqual(body["blade_id"], "B3")
        self.assertEqual(body["before_inspection_id"], "i1")
        self.assertEqual(body["after_inspection_id"], "i2")
        self.assertEqual(body["before_observation_count"], 2)
        self.assertEqual(body["after_observation_count"], 2)
        self.assertEqual(body["before_trajectory_count"], 2)
        self.assertEqual(body["after_trajectory_count"], 2)
        self.assertEqual(body["persisting_count"], 1)
        self.assertEqual(body["new_count"], 1)
        self.assertEqual(body["disappeared_count"], 1)

        pair = body["persisting"][0]
        # 权重 = 径向重叠 [5,10]=5 × 钟位重叠 [2,3]=1
        self.assertAlmostEqual(pair["weight"], 5.0)
        self.assertEqual(pair["before_trajectory_id"],
                         pair["before"]["trajectory_id"])
        self.assertEqual(pair["after_trajectory_id"],
                         pair["after"]["trajectory_id"])
        # 持续轨迹：代表等级与径向范围前后值、双方来源观察 ID
        self.assertEqual(pair["before"]["representative_grade"], 2)
        self.assertEqual(pair["after"]["representative_grade"], 3)
        self.assertEqual(pair["before"]["radius_start"], 0.0)
        self.assertEqual(pair["before"]["radius_end"], 10.0)
        self.assertEqual(pair["after"]["radius_start"], 5.0)
        self.assertEqual(pair["after"]["radius_end"], 15.0)
        self.assertEqual(pair["before"]["observation_ids"], ["o1"])
        self.assertEqual(pair["after"]["observation_ids"], ["p1"])

        self.assertEqual(body["disappeared"][0]["observation_ids"], ["o2"])
        self.assertEqual(body["disappeared"][0]["representative_grade"], 4)
        self.assertEqual(body["new"][0]["observation_ids"], ["p2"])

    def test_multi_source_weight_is_area_sum(self):
        self._write("b1", [
            self._obs("o1", 0, 10, 1, 3, inspection="i1"),
            self._obs("o2", 5, 15, 2, 4, inspection="i1"),  # 与 o1 同期同簇
        ])
        self._write("b2", [
            self._obs("p1", 7, 12, 2.5, 3.5, inspection="i2"),
        ])
        body = self._changes("i1", "i2").json()
        self.assertEqual(body["before_trajectory_count"], 1)
        self.assertEqual(body["persisting_count"], 1)
        pair = body["persisting"][0]
        # o1∩p1 = 3×0.5 = 1.5；o2∩p1 = 5×1 = 5 -> 权重 6.5
        self.assertAlmostEqual(pair["weight"], 6.5)
        self.assertEqual(pair["before"]["observation_ids"], ["o1", "o2"])
        self.assertEqual(pair["after"]["observation_ids"], ["p1"])

    # ----------------------------------------------------- 正长度重叠要求

    def test_touching_endpoints_do_not_pair(self):
        # 径向与钟位均仅端点相接：同期聚类会相接成簇，跨期配对不算
        self._write("b1", [self._obs("o1", 0, 10, 1, 3, inspection="i1")])
        self._write("b2", [self._obs("p1", 10, 20, 3, 5, inspection="i2")])
        body = self._changes("i1", "i2").json()
        self.assertEqual(body["persisting_count"], 0)
        self.assertEqual(body["new_count"], 1)
        self.assertEqual(body["disappeared_count"], 1)

    # ----------------------------------------------------- 全局最优匹配

    def test_global_matching_avoids_greedy_mismatch(self):
        # w(o1,p1)=10 > w(o1,p2)=8、w(o2,p1)=6；贪心先配 o1-p1 后 o2 无配，
        # 总权 10；全局最优为 o1-p2 + o2-p1 = 14
        self._write("b1", [
            self._obs("o1", 0, 10, 1, 3, inspection="i1"),
            self._obs("o2", 0, 8, 5, 7, inspection="i1"),
        ])
        self._write("b2", [
            self._obs("p1", 5, 15, 1, 7, inspection="i2"),
            self._obs("p2", 0, 4, 1, 3, inspection="i2"),
        ])
        body = self._changes("i1", "i2").json()
        self.assertEqual(body["persisting_count"], 2)
        self.assertEqual(body["new_count"], 0)
        self.assertEqual(body["disappeared_count"], 0)
        by_before = {p["before"]["observation_ids"][0]: p
                     for p in body["persisting"]}
        self.assertEqual(by_before["o1"]["after"]["observation_ids"], ["p2"])
        self.assertEqual(by_before["o2"]["after"]["observation_ids"], ["p1"])
        self.assertAlmostEqual(by_before["o1"]["weight"], 8.0)
        self.assertAlmostEqual(by_before["o2"]["weight"], 6.0)

    def test_tie_break_stable_by_trajectory_id(self):
        # 两种完美匹配总权相同（各 18）：按轨迹 ID 字典序稳定裁决
        self._write("b1", [
            self._obs("o1", 0, 10, 1, 3, inspection="i1"),
            self._obs("o2", 0, 10, 5, 7, inspection="i1"),
        ])
        self._write("b2", [
            self._obs("p1", 5, 15, 1, 7, inspection="i2"),
            self._obs("p2", 0, 4, 1, 7, inspection="i2"),
        ])
        body = self._changes("i1", "i2").json()
        self.assertEqual(body["persisting_count"], 2)

        # 用同一聚类实现离线算出四条轨迹 ID，验证配对按 ID 顺序保持
        before_ids = sorted(t["trajectory_id"] for t in build_all(
            "WT-01", "B3", [
                _mk_obs("o1", 0, 10, 1, 3, "i1"),
                _mk_obs("o2", 0, 10, 5, 7, "i1"),
            ]))
        after_ids = sorted(t["trajectory_id"] for t in build_all(
            "WT-01", "B3", [
                _mk_obs("p1", 5, 15, 1, 7, "i2"),
                _mk_obs("p2", 0, 4, 1, 7, "i2"),
            ]))
        expected = {(before_ids[0], after_ids[0]),
                    (before_ids[1], after_ids[1])}
        got = {(p["before_trajectory_id"], p["after_trajectory_id"])
               for p in body["persisting"]}
        self.assertEqual(got, expected)

        # 结果可复现：重复调用完全一致
        again = self._changes("i1", "i2").json()
        self.assertEqual(body, again)

    # ----------------------------------------------------- 不混入人工裁决

    def test_adjudications_not_mixed_in(self):
        self._write("b1", [
            self._obs("o1", 0, 5, 1, 2, inspection="i1"),
            self._obs("o2", 10, 15, 5, 6, inspection="i1"),  # 与 o1 几何分离
        ])
        self._write("b2", [self._obs("p1", 0, 5, 1, 2, inspection="i2")])
        # 人工裁决 o1、o2 必须同轨 -> 复核结果并为一条轨迹
        resp = self.client.post("/api/v1/adjudications", json={
            "turbine_id": "WT-01", "blade_id": "B3",
            "observation_a": "o1", "observation_b": "o2",
            "relation": "must_link", "revision": 0,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        review = self.client.get("/api/v1/reviews/WT-01/B3").json()
        self.assertEqual(review["trajectory_count"], 1)

        # 变化报告仍按纯几何聚类：前期两条轨迹，互不影响
        body = self._changes("i1", "i2").json()
        self.assertEqual(body["before_trajectory_count"], 2)
        self.assertEqual(body["persisting_count"], 1)
        self.assertEqual(body["disappeared_count"], 1)
        self.assertEqual(body["persisting"][0]["before"]["observation_ids"],
                         ["o1"])
        self.assertEqual(body["disappeared"][0]["observation_ids"], ["o2"])

    # ----------------------------------------------------- 只读语义

    def test_read_only_does_not_touch_review_state(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3, inspection="i1")])
        self._write("b2", [self._obs("p1", 5, 15, 2, 4, inspection="i2")])
        before_review = self.client.get("/api/v1/reviews/WT-01/B3").json()
        resp = self._changes("i1", "i2")
        self.assertEqual(resp.status_code, 200, resp.text)
        after_review = self.client.get("/api/v1/reviews/WT-01/B3").json()
        self.assertEqual(before_review, after_review)

    # ----------------------------------------------------- 错误分支

    def test_same_inspection_returns_422(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3, inspection="i1")])
        resp = self._changes("i1", "i1")
        self.assertEqual(resp.status_code, 422)

    def test_missing_inspection_returns_404(self):
        self._write("b1", [self._obs("o1", 0, 10, 1, 3, inspection="i1")])
        self.assertEqual(self._changes("i1", "nope").status_code, 404)
        self.assertEqual(self._changes("nope", "i1").status_code, 404)
        self.assertEqual(
            self._changes("i1", "i2", turbine="WT-X", blade="B9").status_code,
            404)

    def test_openapi_lists_changes_endpoint(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertIn("/api/v1/inspection-changes/{turbine_id}/{blade_id}",
                      paths)


if __name__ == "__main__":
    unittest.main()
