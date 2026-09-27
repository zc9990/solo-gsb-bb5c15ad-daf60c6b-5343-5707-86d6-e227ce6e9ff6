"""Pydantic 请求/响应模型。"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ObservationIn(BaseModel):
    """单条巡检观察输入。"""

    model_config = ConfigDict(extra="forbid")

    observation_id: Optional[str] = Field(
        default=None, max_length=64,
        description="观察外部编号；缺省时由服务端生成",
    )
    inspection_id: str = Field(..., max_length=64, description="巡检批次/航次编号")
    inspected_at: Optional[str] = Field(
        default=None,
        description="巡检时间 ISO-8601；缺省使用写入时刻",
    )
    radius_start: float = Field(..., ge=0.0, description="距叶根起始位置（米，沿展向）")
    radius_end: float = Field(..., ge=0.0, description="距叶根终止位置（米，沿展向）")
    clock_start: float = Field(..., ge=0.0, le=12.0, description="起始钟位（0/12 均为 12 点）")
    clock_end: float = Field(..., ge=0.0, le=12.0, description="终止钟位，沿顺时针；起止相等为单点")
    grade: int = Field(..., ge=1, le=5, description="裂纹等级（1-5）")
    confidence: float = Field(..., ge=0.0, le=1.0, description="置信度（0-1）")

    @field_validator("radius_end")
    @classmethod
    def _radius_order(cls, v, info):
        start = info.data.get("radius_start")
        if start is not None and v < start:
            raise ValueError("radius_end 必须 >= radius_start")
        return v

    @field_validator("inspected_at")
    @classmethod
    def _strip(cls, v):
        if v is not None and not v.strip():
            return None
        return v


class BatchWriteRequest(BaseModel):
    """观察写入（按批次幂等）。"""

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(..., max_length=64, description="写入批次号；同号重放为幂等")
    turbine_id: str = Field(..., max_length=64, description="风机编号")
    blade_id: str = Field(..., max_length=64, description="叶片编号")
    observations: List[ObservationIn] = Field(..., min_length=1)
    auto_recompute: bool = Field(
        default=True,
        description="写入后是否立即对该叶片重算复核（默认开启）",
    )


class SourceItem(BaseModel):
    observation_id: str
    inspection_id: str
    inspected_at: Optional[str] = None
    batch_id: str
    radius_start: float
    radius_end: float
    clock_start: float
    clock_end: float
    grade: int
    confidence: float


class TrajectoryOut(BaseModel):
    trajectory_id: str
    turbine_id: str
    blade_id: str
    radius_start: float
    radius_end: float
    clock: dict
    max_confidence: float
    grades: List[int]
    grade_confidence: dict
    representative_grade: Optional[int] = None
    grade_conflict: bool
    observation_count: int
    adjudication_ids: List[str] = Field(
        default_factory=list,
        description="命中该轨迹的必须同轨裁决 ID 列表",
    )
    sources: List[SourceItem]


class BatchWriteResponse(BaseModel):
    batch_id: str
    turbine_id: str
    blade_id: str
    written: int
    skipped_duplicates: int
    recomputed: bool
    changed: Optional[bool] = None
    trajectories: List[TrajectoryOut] = []
    status: Literal["created", "idempotent_replay", "duplicate_batch_conflict"]


class RecomputeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: Optional[str] = Field(
        default=None,
        description="按批次重算（解析到所属风机/叶片）；与 turbine_id+blade_id 二选一",
    )
    turbine_id: Optional[str] = None
    blade_id: Optional[str] = None


class RecomputeResponse(BaseModel):
    turbine_id: str
    blade_id: str
    changed: bool
    trajectory_count: int
    adjudication_revision: Optional[int] = None
    trajectories: List[TrajectoryOut] = []


class AdjudicationRequest(BaseModel):
    """人工关联裁决提交（按叶片，乐观锁）。"""

    model_config = ConfigDict(extra="forbid")

    adjudication_id: Optional[str] = Field(
        default=None, max_length=64,
        description="裁决编号；缺省服务端生成。相同裁决（同叶片/观察对/关系）"
                    "重复提交为幂等重放",
    )
    turbine_id: str = Field(..., max_length=64, description="风机编号")
    blade_id: str = Field(..., max_length=64, description="叶片编号")
    observation_a: str = Field(..., max_length=64, description="观察 A 的 ID")
    observation_b: str = Field(..., max_length=64, description="观察 B 的 ID")
    relation: Literal["must_link", "cannot_link"] = Field(
        ..., description="must_link=必须同轨，cannot_link=禁止同轨",
    )
    revision: int = Field(
        ..., ge=0,
        description="客户端已知的该叶片裁决 revision（乐观锁）；首笔裁决传 0",
    )


class AdjudicationOut(BaseModel):
    adjudication_id: str
    turbine_id: str
    blade_id: str
    observation_a: str
    observation_b: str
    relation: Literal["must_link", "cannot_link"]
    revision: int = Field(..., description="提交后生效的裁决 revision")
    created_at: str


class AdjudicationListResponse(BaseModel):
    turbine_id: str
    blade_id: str
    revision: int
    count: int
    adjudications: List[AdjudicationOut] = []


class ReviewResponse(BaseModel):
    turbine_id: str
    blade_id: str
    up_to_date: bool
    observation_count: int
    trajectory_count: int
    needs_recompute: bool
    adjudication_revision: Optional[int] = Field(
        default=None,
        description="当前生效的裁决 revision（无裁决时为 0）",
    )
    adjudication_conflict: Optional[dict] = Field(
        default=None,
        description="实时重算若触发禁止同轨冲突，此处给出冲突裁决链；"
                    "已落库的旧轨迹仍随 trajectories 返回且不被改写",
    )
    trajectories: List[TrajectoryOut] = []


class RepairRegion(BaseModel):
    """一个修补区域：沿用观察的径向区间 + 顺时针钟位字段与校验。"""

    model_config = ConfigDict(extra="forbid")

    radius_start: float = Field(..., ge=0.0, description="修补区距叶根起始位置（米）")
    radius_end: float = Field(..., ge=0.0, description="修补区距叶根终止位置（米）")
    clock_start: float = Field(..., ge=0.0, le=12.0, description="修补区起始钟位（0/12 均为 12 点）")
    clock_end: float = Field(..., ge=0.0, le=12.0,
                             description="修补区终止钟位，沿顺时针，允许跨 12 点；起止相等为单点")

    @field_validator("radius_end")
    @classmethod
    def _radius_order(cls, v, info):
        start = info.data.get("radius_start")
        if start is not None and v < start:
            raise ValueError("radius_end 必须 >= radius_start")
        return v


class RepairCoverageRequest(BaseModel):
    """只读修补覆盖核对请求。"""

    model_config = ConfigDict(extra="forbid")

    turbine_id: str = Field(..., max_length=64, description="风机编号")
    blade_id: str = Field(..., max_length=64, description="叶片编号")
    inspection_id: str = Field(..., max_length=64, description="被核对巡检的 inspection_id")
    regions: List[RepairRegion] = Field(
        default_factory=list,
        description="修补区域列表；空列表表示该巡检无修补（全部未覆盖）",
    )


class RepairCoverageItem(BaseModel):
    """单条观察的修补覆盖核对结果。"""

    observation_id: str
    inspection_id: str
    original_area: float = Field(
        ..., description="原始观察面积（径向米 × 钟位小时）",
    )
    covered_area: float = Field(
        ..., description="被修补区域并集覆盖的面积（重叠修补只计一次）",
    )
    coverage_ratio: Optional[float] = Field(
        default=None, description="覆盖率 = 覆盖面积 / 原面积；零面积观察为 null",
    )
    status: Literal["uncovered", "partial", "full", "excluded"] = Field(
        ..., description="uncovered=未覆盖，partial=部分覆盖，full=全覆盖，"
                         "excluded=零面积观察不参与核对",
    )
    excluded_reason: Optional[Literal["zero_area"]] = Field(
        default=None, description="excluded 时的原因（当前仅 zero_area）",
    )


class RepairCoverageSummary(BaseModel):
    """覆盖核对汇总（仅统计参与核对的正面积观察）。"""

    observation_count: int = Field(
        ..., description="该巡检在该叶片下的原始观察总数（含零面积）",
    )
    checked_count: int = Field(..., description="参与核对的正面积观察条数")
    excluded_count: int = Field(..., description="零面积、不参与核对的观察条数")
    uncovered_count: int
    partial_count: int
    full_count: int
    total_original_area: float
    total_covered_area: float
    total_coverage_ratio: Optional[float] = Field(
        default=None,
        description="总覆盖率 = 覆盖面积合计 / 原面积合计；无正面积观察时为 null",
    )


class RepairCoverageResponse(BaseModel):
    """只读修补覆盖核对响应（不修改任何观察、裁决或复核结果）。"""

    turbine_id: str
    blade_id: str
    inspection_id: str
    region_count: int = Field(..., description="请求中的修补区域数量")
    summary: RepairCoverageSummary
    results: List[RepairCoverageItem] = Field(
        default_factory=list,
        description="逐条观察核对结果，按观察 ID 排序",
    )


class ChangeTrajectorySide(BaseModel):
    """变化报告中一侧（前期或后期）轨迹的摘要。"""

    trajectory_id: str
    radius_start: float
    radius_end: float
    representative_grade: Optional[int] = None
    grades: List[int]
    grade_conflict: bool
    max_confidence: float
    observation_count: int
    observation_ids: List[str] = Field(
        default_factory=list, description="该轨迹的全部来源观察 ID",
    )


class PersistingTrajectory(BaseModel):
    """一对跨期持续轨迹。"""

    before_trajectory_id: str
    after_trajectory_id: str
    weight: float = Field(
        ..., description="配对权重：两轨迹所有相交源观察对的重叠面积之和",
    )
    before: ChangeTrajectorySide
    after: ChangeTrajectorySide


class InspectionChangeReport(BaseModel):
    """两期巡检的裂纹轨迹变化报告（只读，实时计算）。"""

    turbine_id: str
    blade_id: str
    before_inspection_id: str
    after_inspection_id: str
    before_observation_count: int
    after_observation_count: int
    before_trajectory_count: int
    after_trajectory_count: int
    persisting_count: int
    new_count: int
    disappeared_count: int
    persisting: List[PersistingTrajectory] = Field(
        default_factory=list,
        description="持续轨迹：全局最大权一对一匹配成功的跨期轨迹对",
    )
    new: List[ChangeTrajectorySide] = Field(
        default_factory=list, description="新增轨迹：仅后期出现的轨迹",
    )
    disappeared: List[ChangeTrajectorySide] = Field(
        default_factory=list, description="消失轨迹：仅前期出现的轨迹",
    )
