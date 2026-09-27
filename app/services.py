"""业务逻辑：观察写入、幂等重算、复核查询。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import db
from . import coverage as cov
from .clustering import (
    Observation,
    Ruling,
    RulingConflict,
    build_all,
    build_trajectory,
    cluster_observations,
)
from .matching import match_trajectory_clusters
from .schemas import (
    AdjudicationRequest,
    BatchWriteRequest,
    ObservationIn,
    RepairCoverageRequest,
)


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str,
                 extra: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self.detail = detail
        self.extra = extra
        super().__init__(detail)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_inspected_at(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise ApiError(422, f"inspected_at 不是合法 ISO-8601 时间: {value}") from exc
    return value.strip()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def content_hash(req: BatchWriteRequest) -> str:
    """请求内容指纹：同内容重放视为幂等。"""
    payload = {
        "turbine_id": req.turbine_id,
        "blade_id": req.blade_id,
        "observations": [
            {
                "observation_id": o.observation_id,
                "inspection_id": o.inspection_id,
                "inspected_at": _parse_inspected_at(o.inspected_at),
                "radius_start": o.radius_start,
                "radius_end": o.radius_end,
                "clock_start": _normalize_clock_input(o.clock_start),
                "clock_end": _normalize_clock_input(o.clock_end),
                "grade": o.grade,
                "confidence": o.confidence,
            }
            for o in req.observations
        ],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _row_to_observation(row: sqlite3.Row) -> Observation:
    return Observation(
        observation_id=row["observation_id"],
        inspection_id=row["inspection_id"],
        inspected_at=row["inspected_at"],
        batch_id=row["batch_id"],
        turbine_id=row["turbine_id"],
        blade_id=row["blade_id"],
        radius_start=row["radius_start"],
        radius_end=row["radius_end"],
        clock_start=row["clock_start"],
        clock_end=row["clock_end"],
        grade=row["grade"],
        confidence=row["confidence"],
    )


def blade_input_hash(conn: sqlite3.Connection,
                     turbine_id: str, blade_id: str) -> Tuple[str, int]:
    """叶片当前输入指纹（观察 + 裁决）与观察条数。"""
    rows = db.list_observations(conn, turbine_id, blade_id)
    payload = [
        {
            "observation_id": r["observation_id"],
            "inspection_id": r["inspection_id"],
            "inspected_at": r["inspected_at"],
            "radius_start": r["radius_start"],
            "radius_end": r["radius_end"],
            "clock_start": r["clock_start"],
            "clock_end": r["clock_end"],
            "grade": r["grade"],
            "confidence": r["confidence"],
        }
        for r in rows
    ]
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return digest, len(rows)


def _rulings_fingerprint(conn: sqlite3.Connection,
                         turbine_id: str, blade_id: str) -> str:
    """当前生效裁决的指纹；裁决集合未变则结果不变。"""
    payload = [
        {
            "adjudication_id": r["adjudication_id"],
            "observation_a": r["observation_a"],
            "observation_b": r["observation_b"],
            "pair_key": r["pair_key"],
            "relation": r["relation"],
        }
        for r in db.list_adjudications(conn, turbine_id, blade_id)
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _load_rulings(conn: sqlite3.Connection, turbine_id: str,
                  blade_id: str) -> List[Ruling]:
    return [
        Ruling(
            adjudication_id=r["adjudication_id"],
            observation_a=r["observation_a"],
            observation_b=r["observation_b"],
            relation=r["relation"],
        )
        for r in db.list_adjudications(conn, turbine_id, blade_id)
    ]


def _compute_live(conn: sqlite3.Connection,
                  turbine_id: str, blade_id: str) -> List[Dict[str, Any]]:
    rows = db.list_observations(conn, turbine_id, blade_id)
    observations = [_row_to_observation(r) for r in rows]
    rulings = _load_rulings(conn, turbine_id, blade_id)
    try:
        return build_all(turbine_id, blade_id, observations, rulings)
    except RulingConflict as exc:
        conflict = {
            "cannot_link_adjudication_id": exc.cannot_link_id,
            "must_link_chain": exc.chain,
            "chain_adjudication_ids": exc.chain,
        }
        raise ApiError(
            409,
            f"人工裁决冲突：{exc}",
            extra=conflict,
        ) from exc


# ------------------------------------------------------------------ write

def write_batch(req: BatchWriteRequest) -> Dict[str, Any]:
    """写入观察批次。

    - 相同 batch_id + 相同内容：幂等重放，不重复写入；
    - 相同 batch_id + 不同内容：409；
    - observation_id 与已有观察冲突（跨批次）：409。
    """
    now = utcnow()
    fingerprint = content_hash(req)

    # 提前校验时间格式
    for o in req.observations:
        _parse_inspected_at(o.inspected_at)

    with db.get_conn() as conn:
        existing = db.get_batch(conn, req.batch_id)
        if existing is not None:
            if existing["content_hash"] != fingerprint or \
                    existing["turbine_id"] != req.turbine_id or \
                    existing["blade_id"] != req.blade_id:
                raise ApiError(
                    409,
                    f"batch_id={req.batch_id} 已存在但内容与本次请求不一致",
                )
            trajectories = db.list_trajectories(
                conn, req.turbine_id, req.blade_id)
            return {
                "batch_id": req.batch_id,
                "turbine_id": req.turbine_id,
                "blade_id": req.blade_id,
                "written": 0,
                "skipped_duplicates": len(req.observations),
                "recomputed": False,
                "changed": None,
                "trajectories": trajectories,
                "status": "idempotent_replay",
            }

        # 同一 blade 内的观察外部编号不得重复
        existing_ids = {
            r["observation_id"]
            for r in db.list_observations(conn, req.turbine_id, req.blade_id)
        }
        seen: set[str] = set()
        prepared: List[Dict[str, Any]] = []
        for seq, o in enumerate(req.observations):
            obs_id = o.observation_id or f"obs_{uuid.uuid4().hex[:16]}"
            if obs_id in seen:
                raise ApiError(422, f"批次内 observation_id 重复: {obs_id}")
            if obs_id in existing_ids:
                raise ApiError(409,
                               f"observation_id 已存在: {obs_id}")
            seen.add(obs_id)
            prepared.append({
                "observation_id": obs_id,
                "batch_id": req.batch_id,
                "turbine_id": req.turbine_id,
                "blade_id": req.blade_id,
                "inspection_id": o.inspection_id,
                "inspected_at": _parse_inspected_at(o.inspected_at),
                "radius_start": o.radius_start,
                "radius_end": o.radius_end,
                "clock_start": _normalize_clock_input(o.clock_start),
                "clock_end": _normalize_clock_input(o.clock_end),
                "grade": o.grade,
                "confidence": o.confidence,
                "seq": seq,
                "created_at": now,
            })

        try:
            with conn:
                db.insert_batch(conn, req.batch_id, req.turbine_id,
                                req.blade_id, fingerprint, now)
                for row in prepared:
                    db.insert_observation(conn, row)
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, f"写入冲突（可能为并发重复提交）: {exc}") from exc

        recomputed = False
        changed: Optional[bool] = None
        trajectories: List[Dict[str, Any]] = []
        if req.auto_recompute:
            changed, trajectories, _revision = _recompute_conn(
                conn, req.turbine_id, req.blade_id)
            recomputed = True

        return {
            "batch_id": req.batch_id,
            "turbine_id": req.turbine_id,
            "blade_id": req.blade_id,
            "written": len(prepared),
            "skipped_duplicates": 0,
            "recomputed": recomputed,
            "changed": changed,
            "trajectories": trajectories,
            "status": "created",
        }


def _normalize_clock_input(v: float) -> float:
    # 12.0 统一存为 0.0，几何层按同一语义处理
    return 0.0 if abs(v - 12.0) < 1e-9 else v


# -------------------------------------------------------------- recompute

def _resolve_blade(conn: sqlite3.Connection, batch_id: Optional[str],
                   turbine_id: Optional[str], blade_id: Optional[str]
                   ) -> Tuple[str, str]:
    if batch_id:
        row = db.get_batch(conn, batch_id)
        if row is None:
            raise ApiError(404, f"batch_id 不存在: {batch_id}")
        return row["turbine_id"], row["blade_id"]
    if turbine_id and blade_id:
        return turbine_id, blade_id
    raise ApiError(422, "必须提供 batch_id，或同时提供 turbine_id 与 blade_id")


def _recompute_conn(conn: sqlite3.Connection,
                    turbine_id: str, blade_id: str
                    ) -> Tuple[bool, List[Dict[str, Any]], int]:
    count = db.blade_observation_count(conn, turbine_id, blade_id)
    if count == 0:
        raise ApiError(404,
                       f"叶片无观察记录: turbine={turbine_id} blade={blade_id}")

    obs_hash, _ = blade_input_hash(conn, turbine_id, blade_id)
    revision = db.get_adjudication_revision(conn, turbine_id, blade_id)
    # 输入指纹 = 观察 + 裁决；任一变化都会触发重算
    input_hash = hashlib.sha256(
        f"{obs_hash}|{revision}|{_rulings_fingerprint(conn, turbine_id, blade_id)}"
        .encode("utf-8")
    ).hexdigest()

    state = db.get_review_state(conn, turbine_id, blade_id)
    if state is not None and state["input_hash"] == input_hash:
        return False, db.list_trajectories(conn, turbine_id, blade_id), revision

    # 计算可能因禁止同轨裁决抛 409；调用方事务边界保证旧结果不被改写
    trajectories = _compute_live(conn, turbine_id, blade_id)
    with conn:
        db.replace_review(conn, turbine_id, blade_id, input_hash,
                          trajectories, utcnow(),
                          adjudication_revision=revision)
    return True, trajectories, revision


def recompute(batch_id: Optional[str],
              turbine_id: Optional[str],
              blade_id: Optional[str]) -> Dict[str, Any]:
    with db.get_conn() as conn:
        turbine_id, blade_id = _resolve_blade(conn, batch_id,
                                              turbine_id, blade_id)
        changed, trajectories, revision = _recompute_conn(
            conn, turbine_id, blade_id)
        return {
            "turbine_id": turbine_id,
            "blade_id": blade_id,
            "changed": changed,
            "trajectory_count": len(trajectories),
            "adjudication_revision": revision,
            "trajectories": trajectories,
        }


# ------------------------------------------------------------------ query

def get_review(turbine_id: str, blade_id: str) -> Dict[str, Any]:
    with db.get_conn() as conn:
        count = db.blade_observation_count(conn, turbine_id, blade_id)
        if count == 0:
            raise ApiError(404,
                           f"叶片无观察记录: turbine={turbine_id} blade={blade_id}")

        revision = db.get_adjudication_revision(conn, turbine_id, blade_id)
        obs_hash, _ = blade_input_hash(conn, turbine_id, blade_id)
        input_hash = hashlib.sha256(
            f"{obs_hash}|{revision}|{_rulings_fingerprint(conn, turbine_id, blade_id)}"
            .encode("utf-8")
        ).hexdigest()
        state = db.get_review_state(conn, turbine_id, blade_id)
        up_to_date = state is not None and state["input_hash"] == input_hash

        conflict: Optional[Dict[str, Any]] = None
        if up_to_date:
            trajectories = db.list_trajectories(conn, turbine_id, blade_id)
        else:
            # 不落库地实时计算；若当前裁决集合自相矛盾（禁止同轨落入闭包），
            # 不返回错误，而是保留旧轨迹并显式提示冲突，旧结果同样未被改写
            try:
                trajectories = _compute_live(conn, turbine_id, blade_id)
            except ApiError as exc:
                if exc.status_code != 409 or not exc.extra:
                    raise
                conflict = {
                    "cannot_link_adjudication_id":
                        exc.extra["cannot_link_adjudication_id"],
                    "must_link_chain": exc.extra["must_link_chain"],
                }
                trajectories = db.list_trajectories(
                    conn, turbine_id, blade_id)

        return {
            "turbine_id": turbine_id,
            "blade_id": blade_id,
            "up_to_date": up_to_date,
            "observation_count": count,
            "trajectory_count": len(trajectories),
            "needs_recompute": not up_to_date,
            "adjudication_revision": revision,
            "adjudication_conflict": conflict,
            "trajectories": trajectories,
        }


# -------------------------------------------------------- inspection changes

def _change_side(trj: Dict[str, Any]) -> Dict[str, Any]:
    """变化报告中一侧轨迹的摘要。"""
    return {
        "trajectory_id": trj["trajectory_id"],
        "radius_start": trj["radius_start"],
        "radius_end": trj["radius_end"],
        "representative_grade": trj["representative_grade"],
        "grades": trj["grades"],
        "grade_conflict": trj["grade_conflict"],
        "max_confidence": trj["max_confidence"],
        "observation_count": trj["observation_count"],
        "observation_ids": sorted(s["observation_id"] for s in trj["sources"]),
    }


def get_inspection_changes(turbine_id: str, blade_id: str,
                           before_inspection_id: str,
                           after_inspection_id: str) -> Dict[str, Any]:
    """只读巡检变化报告：对比同一叶片两期巡检的裂纹轨迹。

    - 各期轨迹仅由当期观察经纯几何聚类生成，不混入人工关联裁决；
    - 跨期配对要求源观察的径向与钟位弧均为正长度重叠，
      权重为所有相交源观察对的重叠面积之和；
    - 匹配为总权重最大的全局一对一匹配，同权按轨迹 ID 稳定裁决；
    - 全程只读，不改写任何已落库的复核结果；任一期无观察返回 404。
    """
    if before_inspection_id == after_inspection_id:
        raise ApiError(422, "前后两个 inspection_id 必须不同")

    with db.get_conn() as conn:
        rows = db.list_observations(conn, turbine_id, blade_id)

    before_obs = [_row_to_observation(r) for r in rows
                  if r["inspection_id"] == before_inspection_id]
    after_obs = [_row_to_observation(r) for r in rows
                 if r["inspection_id"] == after_inspection_id]
    if not before_obs:
        raise ApiError(404, f"前期巡检在该叶片下无观察记录: "
                            f"inspection_id={before_inspection_id}")
    if not after_obs:
        raise ApiError(404, f"后期巡检在该叶片下无观察记录: "
                            f"inspection_id={after_inspection_id}")

    # 各期独立做纯几何聚类（不应用人工裁决），生成当期轨迹
    before_clusters = cluster_observations(before_obs)
    after_clusters = cluster_observations(after_obs)
    before_trj = [build_trajectory(turbine_id, blade_id, c)
                  for c in before_clusters]
    after_trj = [build_trajectory(turbine_id, blade_id, c)
                 for c in after_clusters]

    matches = match_trajectory_clusters(
        [(t["trajectory_id"], c) for t, c in zip(before_trj, before_clusters)],
        [(t["trajectory_id"], c) for t, c in zip(after_trj, after_clusters)],
    )
    matched_before = {i for i, _, _ in matches}
    matched_after = {j for _, j, _ in matches}

    persisting = [
        {
            "before_trajectory_id": before_trj[i]["trajectory_id"],
            "after_trajectory_id": after_trj[j]["trajectory_id"],
            "weight": round(w, 4),
            "before": _change_side(before_trj[i]),
            "after": _change_side(after_trj[j]),
        }
        for i, j, w in matches
    ]
    disappeared = sorted(
        (_change_side(t) for k, t in enumerate(before_trj)
         if k not in matched_before),
        key=lambda s: s["trajectory_id"],
    )
    new = sorted(
        (_change_side(t) for k, t in enumerate(after_trj)
         if k not in matched_after),
        key=lambda s: s["trajectory_id"],
    )

    return {
        "turbine_id": turbine_id,
        "blade_id": blade_id,
        "before_inspection_id": before_inspection_id,
        "after_inspection_id": after_inspection_id,
        "before_observation_count": len(before_obs),
        "after_observation_count": len(after_obs),
        "before_trajectory_count": len(before_trj),
        "after_trajectory_count": len(after_trj),
        "persisting_count": len(persisting),
        "new_count": len(new),
        "disappeared_count": len(disappeared),
        "persisting": persisting,
        "new": new,
        "disappeared": disappeared,
    }


# --------------------------------------------------------- repair coverage

def repair_coverage(req: RepairCoverageRequest) -> Dict[str, Any]:
    """只读修补覆盖核对。

    - 读取该巡检在指定风机/叶片下的**原始观察**（不使用轨迹/复核结果），
      逐条计算被修补区域并集覆盖的面积；
    - 修补区域沿用观察的径向区间 + 顺时针钟位弧字段（允许跨 12 点），
      覆盖面积按径向切片精确求并集，重叠修补只计一次；
    - 空区域列表表示无修补；零面积观察单列为 ``excluded``，不参与核对；
    - 巡检（风机/叶片 + inspection_id）不存在返回 404；
      非法区域由 pydantic 校验返回 422；
    - 全程只读，不修改观察、人工裁决或复核结果。
    """
    with db.get_conn() as conn:
        rows = db.list_observations(conn, req.turbine_id, req.blade_id)

    observations = [
        _row_to_observation(r) for r in rows
        if r["inspection_id"] == req.inspection_id
    ]
    if not observations:
        raise ApiError(
            404,
            f"该巡检在叶片下无观察记录: turbine={req.turbine_id} "
            f"blade={req.blade_id} inspection_id={req.inspection_id}",
        )

    regions = [
        cov.make_region(
            r.radius_start, r.radius_end, r.clock_start, r.clock_end)
        for r in req.regions
    ]

    items = [cov.check_observation(o, regions) for o in observations]
    items.sort(key=lambda x: x["observation_id"])

    checked = [x for x in items if x["status"] != "excluded"]
    total_original = sum(x["original_area"] for x in checked)
    total_covered = sum(x["covered_area"] for x in checked)
    counts = {"uncovered": 0, "partial": 0, "full": 0}
    for x in checked:
        counts[x["status"]] += 1

    summary = {
        "observation_count": len(items),
        "checked_count": len(checked),
        "excluded_count": len(items) - len(checked),
        "uncovered_count": counts["uncovered"],
        "partial_count": counts["partial"],
        "full_count": counts["full"],
        "total_original_area": round(total_original, 4),
        "total_covered_area": round(total_covered, 4),
        "total_coverage_ratio": (
            round(total_covered / total_original, 4)
            if total_original > 1e-9 else None
        ),
    }

    return {
        "turbine_id": req.turbine_id,
        "blade_id": req.blade_id,
        "inspection_id": req.inspection_id,
        "region_count": len(regions),
        "summary": summary,
        "results": items,
    }


# ------------------------------------------------------------ adjudications
def _pair_key(a: str, b: str) -> str:
    """无序观察对的规范化键：同一对观察正反向提交视为相同裁决请求。"""
    lo, hi = sorted((a, b))
    return _canonical_json([lo, hi])


def _row_to_adjudication(row: sqlite3.Row, revision: int) -> Dict[str, Any]:
    return {
        "adjudication_id": row["adjudication_id"],
        "turbine_id": row["turbine_id"],
        "blade_id": row["blade_id"],
        "observation_a": row["observation_a"],
        "observation_b": row["observation_b"],
        "relation": row["relation"],
        "revision": revision,
        "created_at": row["created_at"],
    }


def submit_adjudication(req: AdjudicationRequest) -> Dict[str, Any]:
    """提交人工关联裁决。

    - 仅允许引用同一叶片的现存观察；
    - 相同裁决请求（同叶片 + 同一观察对 + 同关系，观察对不计方向）幂等；
    - ``revision`` 为乐观锁：必须等于该叶片当前裁决版本（首笔传 0）；
    - 成功后叶片 revision +1（幂等重放不递增）。
    """
    if req.observation_a == req.observation_b:
        raise ApiError(422, "observation_a 与 observation_b 不能相同")

    with db.get_conn() as conn:
        # 引用校验：两条观察都必须存在且属于同一叶片
        for ref in (req.observation_a, req.observation_b):
            row = db.get_observation(conn, ref)
            if row is None:
                raise ApiError(404, f"observation 不存在: {ref}")
            if row["turbine_id"] != req.turbine_id or \
                    row["blade_id"] != req.blade_id:
                raise ApiError(
                    422,
                    f"observation {ref} 不属于叶片 "
                    f"{req.turbine_id}/{req.blade_id}，裁决只允许引用同一叶片的观察",
                )

        pair_key = _pair_key(req.observation_a, req.observation_b)
        existing = db.get_adjudication_by_pair(
            conn, req.turbine_id, req.blade_id, pair_key, req.relation)
        current_revision = db.get_adjudication_revision(
            conn, req.turbine_id, req.blade_id)

        if existing is not None:
            # 相同裁决请求幂等：携带的裁决 ID 必须兼容（缺省或一致）
            if req.adjudication_id is not None and \
                    req.adjudication_id != existing["adjudication_id"]:
                raise ApiError(
                    409,
                    f"观察对 {req.observation_a}/{req.observation_b} 的 "
                    f"{req.relation} 裁决已存在（adjudication_id="
                    f"{existing['adjudication_id']}）",
                    extra={
                        "adjudication_id": existing["adjudication_id"],
                        "revision": current_revision,
                    },
                )
            return _row_to_adjudication(existing, current_revision)

        adjudication_id = req.adjudication_id or \
            f"adj_{uuid.uuid4().hex[:16]}"
        now = utcnow()
        try:
            with conn:
                db.insert_adjudication(conn, {
                    "adjudication_id": adjudication_id,
                    "turbine_id": req.turbine_id,
                    "blade_id": req.blade_id,
                    "observation_a": req.observation_a,
                    "observation_b": req.observation_b,
                    "pair_key": pair_key,
                    "relation": req.relation,
                    "created_at": now,
                })
                # 原子乐观锁：仅当库中 revision 仍等于请求值时才递增，
                # 并发情况下败者随事务回滚并返回 409
                new_revision = db.bump_adjudication_revision(
                    conn, req.turbine_id, req.blade_id, now,
                    expected_revision=req.revision)
                if new_revision is None:
                    actual = db.get_adjudication_revision(
                        conn, req.turbine_id, req.blade_id)
                    raise ApiError(
                        409,
                        f"revision 冲突：请求携带 {req.revision}，当前生效 {actual}",
                        extra={"revision": actual},
                    )
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, f"裁决写入冲突（可能为并发重复提交）: {exc}") from exc

        row = db.get_adjudication_by_pair(
            conn, req.turbine_id, req.blade_id, pair_key, req.relation)
        assert row is not None
        return _row_to_adjudication(row, new_revision)


def list_blade_adjudications(turbine_id: str, blade_id: str) -> Dict[str, Any]:
    with db.get_conn() as conn:
        revision = db.get_adjudication_revision(conn, turbine_id, blade_id)
        rows = db.list_adjudications(conn, turbine_id, blade_id)
        return {
            "turbine_id": turbine_id,
            "blade_id": blade_id,
            "revision": revision,
            "count": len(rows),
            "adjudications": [_row_to_adjudication(r, revision) for r in rows],
        }
