"""FastAPI 应用入口与路由。

启动即完成：依赖检查、SQLite 建表（见 :func:`app.db.init_db`）。
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import db, services
from .schemas import (
    AdjudicationListResponse,
    AdjudicationRequest,
    AdjudicationOut,
    BatchWriteRequest,
    BatchWriteResponse,
    InspectionChangeReport,
    RecomputeRequest,
    RecomputeResponse,
    RepairCoverageRequest,
    RepairCoverageResponse,
    ReviewReportIssueRequest,
    ReviewReportOut,
    ReviewReportVerifyResponse,
    ReviewResponse,
)

app = FastAPI(
    title="风机叶片裂纹复核 API",
    description=(
        "接收多次巡检观察（距叶根径向区间 + 顺时针钟位区间 + 等级 + 置信度），"
        "将径向与钟位范围相交的观察聚为连通簇，合并为带来源清单的裂纹轨迹，"
        "并标注等级冲突。支持观察写入、复核查询、按批次幂等重算，"
        "以及两期巡检间轨迹持续/新增/消失的只读变化报告。"
        "支持将叶片复核结果签发为不可变报告并随时只读核验。"
    ),
    version="1.0.0",
)


@app.on_event("startup")
def _startup() -> None:
    db.DB_PATH = os.environ.get("CRACK_DB_PATH", db.DB_PATH)
    db.init_db()


@app.exception_handler(services.ApiError)
async def _api_error_handler(_request: Request, exc: services.ApiError) -> JSONResponse:
    content = {"detail": exc.detail}
    if exc.extra:
        content.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=content)


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/v1/observations/batch",
    tags=["观察写入"],
    response_model=BatchWriteResponse,
    summary="按批次写入巡检观察（幂等）",
)
def write_batch(payload: BatchWriteRequest) -> dict:
    return services.write_batch(payload)


@app.post(
    "/api/v1/reviews/recompute",
    tags=["复核重算"],
    response_model=RecomputeResponse,
    summary="按批次（或风机+叶片）幂等重算裂纹轨迹",
)
def recompute(payload: RecomputeRequest) -> dict:
    return services.recompute(payload.batch_id,
                              payload.turbine_id, payload.blade_id)


@app.post(
    "/api/v1/adjudications",
    tags=["人工裁决"],
    response_model=AdjudicationOut,
    summary="提交裂纹观察人工关联裁决（必须同轨 / 禁止同轨，乐观锁，幂等）",
)
def submit_adjudication(payload: AdjudicationRequest) -> dict:
    return services.submit_adjudication(payload)


@app.get(
    "/api/v1/adjudications/{turbine_id}/{blade_id}",
    tags=["人工裁决"],
    response_model=AdjudicationListResponse,
    summary="查询某叶片的全部人工关联裁决与当前 revision",
)
def list_adjudications(turbine_id: str, blade_id: str) -> dict:
    return services.list_blade_adjudications(turbine_id, blade_id)


@app.get(
    "/api/v1/reviews/{turbine_id}/{blade_id}",
    tags=["复核查询"],
    response_model=ReviewResponse,
    summary="查询某叶片的裂纹轨迹复核结果",
)
def get_review(turbine_id: str, blade_id: str) -> dict:
    return services.get_review(turbine_id, blade_id)


@app.get(
    "/api/v1/inspection-changes/{turbine_id}/{blade_id}",
    tags=["巡检变化"],
    response_model=InspectionChangeReport,
    summary="只读巡检变化报告：两期巡检间裂纹轨迹的持续/新增/消失",
)
def get_inspection_changes(
    turbine_id: str,
    blade_id: str,
    before: str = Query(..., description="前期巡检 inspection_id"),
    after: str = Query(..., description="后期巡检 inspection_id"),
) -> dict:
    return services.get_inspection_changes(turbine_id, blade_id, before, after)


@app.post(
    "/api/v1/repair-coverage",
    tags=["修补覆盖核对"],
    response_model=RepairCoverageResponse,
    summary="只读修补覆盖核对：修补区域并集对巡检原始观察面积的覆盖率",
)
def repair_coverage(payload: RepairCoverageRequest) -> dict:
    return services.repair_coverage(payload)


@app.post(
    "/api/v1/review-reports",
    tags=["复核报告"],
    response_model=ReviewReportOut,
    summary="签发叶片复核报告（同一事务固化不可变快照，乐观锁，幂等）",
)
def issue_review_report(payload: ReviewReportIssueRequest) -> dict:
    return services.issue_review_report(payload)


@app.get(
    "/api/v1/review-reports/{report_id}",
    tags=["复核报告"],
    response_model=ReviewReportOut,
    summary="查询已签发的复核报告快照（不可变）",
)
def get_review_report(report_id: str) -> dict:
    return services.get_review_report(report_id)


@app.get(
    "/api/v1/review-reports/{report_id}/verify",
    tags=["复核报告"],
    response_model=ReviewReportVerifyResponse,
    summary="只读核验复核报告是否仍为当前版本",
)
def verify_review_report(report_id: str) -> dict:
    return services.verify_review_report(report_id)
