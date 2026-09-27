# 风机叶片裂纹复核 API

基于 **Python + FastAPI + SQLite** 的风机叶片裂纹巡检观察复核服务：

- 接收**多次巡检**的裂纹观察：距叶根径向起止位置、**顺时针钟位区间**（允许跨越 12 点）、等级、置信度；
- 将**径向区间与钟位弧同时相交**的观察按连通关系聚为同一连通簇（并查集传递闭包）；
- 每个连通簇合并为一条**裂纹轨迹**：径向/钟位外包范围、等级与置信度汇总、**等级冲突标记**、完整**来源清单**；
- 提供**观察写入、复核结果查询、按批次幂等重算**三类接口；
- 支持**人工关联裁决**：按叶片声明两条观察“必须同轨 / 禁止同轨”，携带 `revision` 乐观锁；
  重算时先应用必须同轨关系的传递闭包，再校验禁止同轨关系，冲突时返回 `409` 与冲突裁决链且**不改写旧结果**；
- 提供**只读巡检变化报告**：输入风机、叶片与前后两个 `inspection_id`，对比两期轨迹的
  **持续 / 新增 / 消失**，跨期配对按“正长度重叠面积”加权并求**全局最大权一对一匹配**；
- 提供**只读修补覆盖核对**：指定风机、叶片、巡检与修补区域列表，对该巡检的**原始观察**
  逐条计算被修补区域**并集**覆盖的面积（径向米 × 钟位小时），重叠修补只计一次，
  输出未覆盖 / 部分覆盖 / 全覆盖状态及零面积观察单列；
- 支持**复核报告签发与核验**：携带预期裁决 `revision` 为叶片签发**不可变**报告——
  同一 SQLite 事务内读取当前观察与裁决、实时生成轨迹，并固化轨迹及来源、
  **两类输入指纹**（观察 / 裁决）与签发时间；版本不符或裁决冲突时拒绝签发且**不留半成品**；
  已签发快照不随后续数据变化而改变，可随时只读核验报告是否仍为当前版本；
- 依赖安装、SQLite 建表与数据库初始化均随容器启动自动完成。

## 单条命令启动

```bash
docker compose up -d --build
```

启动后：

| 项目 | 地址 |
| --- | --- |
| API 基址 | `http://localhost:8000` |
| 健康检查 | `GET http://localhost:8000/health` |
| Swagger 交互文档 | `http://localhost:8000/docs` |
| OpenAPI Schema | `http://localhost:8000/openapi.json` |

> 端口可通过环境变量覆盖，例如 `API_PORT=9000 docker compose up -d --build`，则访问 `http://localhost:9000`。

数据持久化在 Docker 命名卷 `crack-data`（容器内 `/data/crack_review.db`），容器重建不丢数据。

## 配置（均为可选项）

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_PORT` | `8000` | 宿主机映射端口（compose 层） |
| `CRACK_DB_PATH` | `/data/crack_review.db` | SQLite 数据库文件路径；启动时自动建库建表 |

除环境变量外无需任何额外配置：镜像构建时安装依赖，容器启动（FastAPI startup 钩子）时自动执行 `CREATE TABLE IF NOT EXISTS ...` 初始化。

## 钟位与聚类规则

- 钟面 0–12 小时；`0` 与 `12` 等价，均表示 **12 点**。
- 钟位区间沿**顺时针**从 `clock_start` 到 `clock_end`：
  - `1 → 3` 表示 1 点到 3 点的普通弧；
  - `11 → 1` 表示 **跨越 12 点** 的弧（11 点 → 12 点 → 1 点）；
  - `clock_start == clock_end` 表示单点。
- 两条观察**连通**当且仅当：
  1. 径向区间相交：`max(r_start) ≤ min(r_end)`（**端点相接算相交**，裂纹接续）；
  2. 钟位弧相交（环向，同样端点相接算相交）。
- 连通关系可传递（A–B、B–C 则 A、B、C 同簇）。
- 轨迹合并：径向取最小/最大外包；钟位弧在 [0,12] 上线性合并，首尾在 0/12 相接时还原为一条 `wraps_twelve=true` 的跨 12 点弧，全覆盖时 `full_circle=true`。
- **等级冲突**：簇内出现 2 个及以上不同等级时 `grade_conflict=true`；同时给出各等级的最高置信度 `grade_confidence`，代表等级 `representative_grade` 按“最高置信度优先、并列取更大等级（更保守）”确定。
- 轨迹 ID 由成员观察 ID 集合哈希得到，同输入重算保持稳定。

## 人工关联裁决

复核员可在几何聚类之上叠加人工判断，每个叶片维护一个单调递增的 `revision`（乐观锁 + 结果生效版本号）：

- `must_link`（**必须同轨**）：声明两条观察必须属于同一轨迹；
- `cannot_link`（**禁止同轨**）：声明两条观察不得属于同一轨迹。

重算顺序固定：

1. 先做纯几何连通并查集；
2. 在其上应用全部 `must_link` 并集（**传递闭包**：A~B、B~C 则 A、B、C 同轨）；
3. 再逐条校验 `cannot_link`：若两端落入同一闭包，返回 **`409 Conflict`**，响应给出
   `cannot_link_adjudication_id` 与 `must_link_chain`（把两端连入同一闭包的、按路径相邻的必须同轨裁决 ID；
   若仅由几何连通直接同簇则为空数组）。冲突时**旧轨迹结果保持不变**，查询接口会在
   `adjudication_conflict` 字段提示冲突并继续返回旧轨迹。

其余语义：

- 裁决只允许引用**同一叶片的现存观察**（引用不存在观察 → `404`，引用其他叶片观察或自引用 → `422`）；
- 相同裁决请求**幂等**：同叶片 + 同一无序观察对 + 同关系（`o1,o2` 与 `o2,o1` 等价）重复提交不重复落库、不递增 revision；
- `revision` 为乐观锁：提交时须携带该叶片当前 revision（首笔传 `0`），过期返回 `409` 并回带当前值；
- 观察新增后，有效裁决继续沿用，下次重算自动生效；
- 每条轨迹的 `adjudication_ids` 给出**命中该轨迹的必须同轨裁决 ID**；查询/重算响应的
  `adjudication_revision` 给出当前生效 revision（无裁决时为 `0`）。

## API 一览

基址 `http://localhost:8000/api/v1`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/observations/batch` | 按批次写入观察（幂等），默认写入后自动重算 |
| POST | `/reviews/recompute` | 按 `batch_id`（或 `turbine_id`+`blade_id`）幂等重算 |
| GET | `/reviews/{turbine_id}/{blade_id}` | 查询某叶片裂纹轨迹复核结果 |
| POST | `/adjudications` | 提交人工关联裁决（必须同轨/禁止同轨，乐观锁，幂等） |
| GET | `/adjudications/{turbine_id}/{blade_id}` | 查询某叶片的全部裁决与当前 revision |
| GET | `/inspection-changes/{turbine_id}/{blade_id}` | 只读巡检变化报告（两期轨迹持续/新增/消失对比） |
| POST | `/repair-coverage` | 只读修补覆盖核对（修补区域并集对巡检原始观察面积的覆盖率） |
| POST | `/review-reports` | 签发叶片复核报告（同一事务固化不可变快照，乐观锁，幂等） |
| GET | `/review-reports/{report_id}` | 查询已签发的复核报告快照（不可变） |
| GET | `/review-reports/{report_id}/verify` | 只读核验报告是否仍为当前版本（分别比对两类指纹） |

### 1. 写入观察批次

`POST /api/v1/observations/batch`

```json
{
  "batch_id": "b-2026-09-18-01",
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "auto_recompute": true,
  "observations": [
    {
      "observation_id": "s1",
      "inspection_id": "drone-0901",
      "inspected_at": "2026-09-01T10:00:00Z",
      "radius_start": 2.0,
      "radius_end": 6.0,
      "clock_start": 11.0,
      "clock_end": 1.0,
      "grade": 2,
      "confidence": 0.82
    },
    {
      "observation_id": "s2",
      "inspection_id": "drone-0901",
      "inspected_at": "2026-09-01T10:00:00Z",
      "radius_start": 5.5,
      "radius_end": 9.0,
      "clock_start": 12.0,
      "clock_end": 2.0,
      "grade": 3,
      "confidence": 0.91
    }
  ]
}
```

字段约束：

| 字段 | 类型/范围 |
| --- | --- |
| `radius_start` / `radius_end` | 数值，米，`>= 0`，且 `end >= start` |
| `clock_start` / `clock_end` | `[0, 12]`，顺时针区间，允许跨 12 点 |
| `grade` | 整数 `1–5` |
| `confidence` | `[0, 1]` |
| `observation_id` | 可空，缺省服务端生成；同一叶片内唯一 |
| `auto_recompute` | 默认 `true` |

幂等语义：

- 相同 `batch_id` + **相同请求内容**重复提交 → `status="idempotent_replay"`，不重复写库（`written=0`）；
- 相同 `batch_id` + **不同内容** → `409 Conflict`。

响应中 `trajectories` 即写入后（可选）重算得到的轨迹，结构同查询接口。

### 2. 幂等重算

`POST /api/v1/reviews/recompute`

```json
{ "batch_id": "b-2026-09-18-01" }
```

或直接指定叶片：

```json
{ "turbine_id": "WT-A01", "blade_id": "B2" }
```

- 以该叶片**当前全部观察**重算（跨批次、跨巡检），事务内整体替换旧结果；
- 输入指纹未变化时不写库，返回 `changed=false`，可安全重试；
- `batch_id` 不存在返回 `404`。

### 3. 查询复核结果

`GET /api/v1/reviews/WT-A01/B2`

```json
{
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "up_to_date": true,
  "needs_recompute": false,
  "observation_count": 2,
  "trajectory_count": 1,
  "adjudication_revision": 0,
  "adjudication_conflict": null,
  "trajectories": [
    {
      "trajectory_id": "trj_...",
      "radius_start": 2.0,
      "radius_end": 9.0,
      "clock": {
        "full_circle": false,
        "span": 3.0,
        "ranges": [
          {
            "start": 11.0,
            "end": 2.0,
            "wraps_twelve": true,
            "start_label": "11点",
            "end_label": "2点"
          }
        ]
      },
      "max_confidence": 0.91,
      "grades": [2, 3],
      "grade_confidence": {"2": 0.82, "3": 0.91},
      "representative_grade": 3,
      "grade_conflict": true,
      "observation_count": 2,
      "adjudication_ids": [],
      "sources": [
        { "observation_id": "s1", "inspection_id": "drone-0901", "grade": 2, "confidence": 0.82, "...": "该观察的完整字段与所属 batch_id" },
        { "observation_id": "s2", "inspection_id": "drone-0901", "grade": 3, "confidence": 0.91, "...": "..." }
      ]
    }
  ]
}
```

- `sources` 为构成轨迹的全部来源观察（含批次、巡检、原始范围、等级、置信度）。
- `adjudication_revision` 为当前生效的裁决版本；`adjudication_ids` 为命中该轨迹的必须同轨裁决 ID。
- 观察有新增但尚未重算时，接口仍返回基于当前观察**实时计算**的轨迹，并以 `needs_recompute=true` / `up_to_date=false` 提示。
- 实时计算若因禁止同轨裁决冲突而无法成轨，返回**旧轨迹**并在 `adjudication_conflict` 中给出冲突裁决链（旧结果未被改写）。

### 4. 提交人工关联裁决

`POST /api/v1/adjudications`

```json
{
  "adjudication_id": "adj-2026-09-18-01",
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "observation_a": "s1",
  "observation_b": "s3",
  "relation": "must_link",
  "revision": 0
}
```

| 字段 | 类型/说明 |
| --- | --- |
| `adjudication_id` | 可空，缺省服务端生成 |
| `relation` | `must_link`（必须同轨）或 `cannot_link`（禁止同轨） |
| `revision` | 客户端已知的叶片裁决版本（乐观锁），首笔传 `0` |

成功响应回带提交后生效的 `revision`：

```json
{
  "adjudication_id": "adj-2026-09-18-01",
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "observation_a": "s1",
  "observation_b": "s3",
  "relation": "must_link",
  "revision": 1,
  "created_at": "2026-09-18T12:00:00+00:00"
}
```

错误与幂等：

- **幂等重放**：同叶片 + 同一无序观察对 + 同关系重复提交（`observation_a/b` 对调等价），
  返回既有裁决且 **revision 不递增**；
- 同一观察对+关系已存在但携带了不同的 `adjudication_id` → `409`；
- `revision` 过期 → `409`，响应体回带当前 `revision`；
- 观察不存在 → `404`；观察不属于该叶片或两条观察相同 → `422`；
- 提交本身只校验引用与锁，不立即重算；随后的重算若发现禁止同轨冲突，返回：

```json
{
  "detail": "人工裁决冲突：……",
  "cannot_link_adjudication_id": "adj-cl-01",
  "must_link_chain": ["adj-2026-09-18-01", "adj-2026-09-18-02"],
  "chain_adjudication_ids": ["adj-2026-09-18-01", "adj-2026-09-18-02"]
}
```

### 5. 查询裁决列表

`GET /api/v1/adjudications/WT-A01/B2`

```json
{
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "revision": 1,
  "count": 1,
  "adjudications": [ { "...": "裁决完整字段（含 revision）" } ]
}
```

### 6. 巡检变化报告（只读）

`GET http://localhost:8000/api/v1/inspection-changes/{turbine_id}/{blade_id}?before=<前期inspection_id>&after=<后期inspection_id>`

对比同一叶片**两期巡检**的裂纹轨迹，输出**持续 / 新增 / 消失**三类结果。接口只读、实时计算，
不改写任何已落库的复核结果（端口随 `API_PORT` 环境变量调整，默认 `8000`）。

规则：

- 各期轨迹仅由**当期观察**经现有几何聚类（径向相交 ∧ 钟位弧相交的连通簇）生成，
  **不混入人工关联裁决**；
- 跨期轨迹可配对，当且仅当源观察间存在**正长度**的径向重叠与钟位弧重叠
  （端点相接不计——区别于同期聚类的“端点相接算相交”）；
- 配对权重 = 所有相交源观察对的「径向重叠长 × 钟位重叠长」（重叠面积）之和；
- 在所有候选配对中求**总权重最大的全局一对一匹配**（匈牙利算法，权重按 1e-6 量化后精确求解），
  不做局部贪心，避免“先占大边、后无可配”的错配；总权重相同时按
  （前期轨迹 ID, 后期轨迹 ID）字典序稳定裁决，结果与输入顺序无关、可复现；
- `before` 与 `after` 必须不同（相同 → `422`）；任一期在该叶片下无观察 → `404`。

调用示例（先写入两期观察，再对比）：

```bash
curl -s -X POST http://localhost:8000/api/v1/observations/batch \
  -H 'Content-Type: application/json' -d @- <<'JSON'
{"batch_id":"demo-c1","turbine_id":"WT-A01","blade_id":"B2","observations":[
  {"observation_id":"c1","inspection_id":"drone-0901","radius_start":2.0,"radius_end":6.0,"clock_start":11.0,"clock_end":1.0,"grade":2,"confidence":0.82},
  {"observation_id":"c2","inspection_id":"drone-0901","radius_start":20.0,"radius_end":26.0,"clock_start":5.0,"clock_end":6.0,"grade":4,"confidence":0.77}
]}
JSON
curl -s -X POST http://localhost:8000/api/v1/observations/batch \
  -H 'Content-Type: application/json' -d @- <<'JSON'
{"batch_id":"demo-c2","turbine_id":"WT-A01","blade_id":"B2","observations":[
  {"observation_id":"c3","inspection_id":"drone-1001","radius_start":3.0,"radius_end":7.0,"clock_start":11.5,"clock_end":1.5,"grade":3,"confidence":0.9},
  {"observation_id":"c4","inspection_id":"drone-1001","radius_start":40.0,"radius_end":44.0,"clock_start":8.0,"clock_end":9.0,"grade":1,"confidence":0.66}
]}
JSON
curl -s "http://localhost:8000/api/v1/inspection-changes/WT-A01/B2?before=drone-0901&after=drone-1001"
```

响应（节选）：

```json
{
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "before_inspection_id": "drone-0901",
  "after_inspection_id": "drone-1001",
  "before_observation_count": 2,
  "after_observation_count": 2,
  "before_trajectory_count": 2,
  "after_trajectory_count": 2,
  "persisting_count": 1,
  "new_count": 1,
  "disappeared_count": 1,
  "persisting": [
    {
      "before_trajectory_id": "trj_5296be486c4594a6",
      "after_trajectory_id": "trj_7e8a4e5a145802bc",
      "weight": 4.5,
      "before": {
        "trajectory_id": "trj_5296be486c4594a6",
        "radius_start": 2.0, "radius_end": 6.0,
        "representative_grade": 2,
        "observation_ids": ["c1"], "...": "grades/grade_conflict/max_confidence/observation_count"
      },
      "after": {
        "trajectory_id": "trj_7e8a4e5a145802bc",
        "radius_start": 3.0, "radius_end": 7.0,
        "representative_grade": 3,
        "observation_ids": ["c3"], "...": "..."
      }
    }
  ],
  "new": [ { "trajectory_id": "trj_...", "observation_ids": ["c4"], "...": "仅后期出现的轨迹" } ],
  "disappeared": [ { "trajectory_id": "trj_...", "observation_ids": ["c2"], "...": "仅前期出现的轨迹" } ]
}
```

- 持续轨迹给出**代表等级与径向范围的前后值**以及**双方来源观察 ID**；`weight` 为配对权重（重叠面积之和）。
- 轨迹 ID 与复核结果一致（同成员观察集合哈希），可与 `GET /reviews/...` 对照。

### 7. 修补覆盖核对（只读）

`POST /api/v1/repair-coverage`

指定风机、叶片、巡检（`inspection_id`）与修补区域列表，读取该巡检的**原始观察**，
逐条核对被修补区域**并集**覆盖的面积。接口只读、实时计算，**不修改观察、人工裁决
或任何已落库的复核结果**。

- 面积单位为**径向米 × 钟位小时**：原面积 = 径向长 × 钟位弧长；
- 修补区域沿用观察的径向区间与**顺时针钟位区间**字段及校验（`0/12` 等价、
  `radius_end >= radius_start`、钟位 `[0,12]`、**允许跨 12 点**、起止相等为单点）；
- 多条修补先求**并集**再覆盖：相互重叠（含跨 12 点后两侧重叠）的修补只计一次；
- 覆盖面积通过**径向切片**精确求解（条带内合并钟位交集弧长 × 条带径向长），
  **不使用外包框或采样近似**；
- 每条观察返回：`observation_id`、`original_area`（原面积）、`covered_area`
  （覆盖面积）、`coverage_ratio`（覆盖率）、`status`：
  - `uncovered` 未覆盖（覆盖面积为 0）；
  - `partial` 部分覆盖；
  - `full` 全覆盖；
  - `excluded` **零面积观察**（径向零长或钟位单点）单列，不参与核对，
    覆盖率为 `null`，`excluded_reason="zero_area"`；
- **空区域列表** `regions: []` 表示该巡检无修补（全部正面积观察为 `uncovered`）；
- 巡检在该风机/叶片下不存在 → `404`；非法区域（径向倒挂、钟位越界等）→ `422`。

请求示例：

```json
{
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "inspection_id": "drone-0901",
  "regions": [
    {"radius_start": 0.0, "radius_end": 5.0, "clock_start": 11.0, "clock_end": 12.0},
    {"radius_start": 5.0, "radius_end": 10.0, "clock_start": 0.0, "clock_end": 1.0}
  ]
}
```

> 上例两条修补在径向 `5 米` 处拼接、各覆盖跨 12 点弧的一侧：对
> `[0,10] × 11点→1点`（原面积 `10 × 2 = 20`）的观察，覆盖面积精确为
> `5 × 1 + 5 × 1 = 10`（`partial`，覆盖率 `0.5`）——而不是外包框近似的 20。

响应（节选）：

```json
{
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "inspection_id": "drone-0901",
  "region_count": 2,
  "summary": {
    "observation_count": 3,
    "checked_count": 2,
    "excluded_count": 1,
    "uncovered_count": 1,
    "partial_count": 1,
    "full_count": 0,
    "total_original_area": 60.0,
    "total_covered_area": 10.0,
    "total_coverage_ratio": 0.1667
  },
  "results": [
    {
      "observation_id": "s1",
      "inspection_id": "drone-0901",
      "original_area": 20.0,
      "covered_area": 10.0,
      "coverage_ratio": 0.5,
      "status": "partial",
      "excluded_reason": null
    }
  ]
}
```

`summary` 汇总仅统计参与核对的正面积观察（`total_coverage_ratio =
覆盖面积合计 / 原面积合计`，无正面积观察时为 `null`）；`results` 按观察 ID 排序。

### 8. 签发复核报告

`POST /api/v1/review-reports`

为风机叶片的当前复核结果签发一份**不可变报告**：在**同一 SQLite 事务**
（`BEGIN IMMEDIATE`）中读取该叶片当前全部观察与裁决、实时生成轨迹，并把
**轨迹及来源清单、两类输入指纹、签发时间**整体固化。报告只增不改，
不随后续观察或裁决的变化而变化。

```json
{
  "report_id": "rr-2026-09-27-01",
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "expected_adjudication_revision": 0
}
```

| 字段 | 类型/说明 |
| --- | --- |
| `report_id` | 可空，缺省服务端生成（`rr_` 前缀）；相同 `report_id` + 相同请求（同叶片 + 同预期 revision）重复提交为幂等重放 |
| `expected_adjudication_revision` | 客户端已知的叶片裁决版本（乐观锁），无裁决时传 `0` |

成功响应（即报告快照，`GET` 返回同一结构）：

```json
{
  "report_id": "rr-2026-09-27-01",
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "adjudication_revision": 0,
  "observation_fingerprint": "9f86d081884c7d659a2feaa0c55ad015...",
  "adjudication_fingerprint": "d04e929b8f6a7b6d5c4b3a291807f6e5...",
  "observation_count": 2,
  "trajectory_count": 1,
  "issued_at": "2026-09-27T08:00:00+00:00",
  "trajectories": [ { "...": "签发时刻实时生成的轨迹及完整来源清单，结构同查询接口" } ]
}
```

- 两类输入指纹：`observation_fingerprint` 为当前全部观察的规范化哈希；
  `adjudication_fingerprint` 为**生效 revision + 裁决集合**的规范化哈希；
- 叶片无观察 → `404`；`expected_adjudication_revision` 与当前生效版本不符 →
  `409`，响应回带当前 `adjudication_revision`：

```json
{ "detail": "裁决 revision 不符：请求携带 0，当前生效 1", "adjudication_revision": 1 }
```

- 实时成轨若触发**禁止同轨冲突** → `409`，响应给出
  `cannot_link_adjudication_id` 与 `must_link_chain`（格式同重算接口）；
- 版本不符、裁决冲突或任何校验失败时事务整体回滚，**不留下半成品报告**
  （以该 `report_id` 查询仍为 `404`）；
- 相同 `report_id` 但叶片或预期 revision 与已签发报告不同 → `409`。

### 9. 查询复核报告快照

`GET /api/v1/review-reports/{report_id}`

返回签发时固化的报告快照（轨迹、来源、指纹、签发时间）。即使签发后该叶片
新增了观察或裁决，本接口返回内容仍与签发响应**逐字节一致**；报告不存在 → `404`。

### 10. 核验复核报告（只读）

`GET /api/v1/review-reports/{report_id}/verify`

重新计算该叶片**当前**的两类输入指纹，与报告快照分别比对，指出观察或裁决
是否已变化、报告是否仍为当前版本。接口**只读、实时计算**，不修改观察、
裁决、复核结果或报告本身：

```json
{
  "report_id": "rr-2026-09-27-01",
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "observation_changed": true,
  "adjudication_changed": false,
  "is_current": false,
  "report_observation_fingerprint": "9f86d081...",
  "current_observation_fingerprint": "e3b0c442...",
  "report_adjudication_fingerprint": "d04e929b...",
  "current_adjudication_fingerprint": "d04e929b...",
  "report_adjudication_revision": 0,
  "current_adjudication_revision": 0,
  "verified_at": "2026-09-27T09:30:00+00:00"
}
```

- `observation_changed`：当前观察指纹与报告不一致（新增/变更过观察）；
- `adjudication_changed`：当前裁决指纹（含 revision）与报告不一致；
- `is_current`：两类指纹均与报告一致时为 `true`，即报告仍为当前版本；
- 报告不存在 → `404`。

## curl 快速验证

```bash
curl -s http://localhost:8000/health
curl -s -X POST http://localhost:8000/api/v1/observations/batch \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "batch_id": "demo-1",
  "turbine_id": "WT-A01",
  "blade_id": "B2",
  "observations": [
    {"observation_id":"s1","inspection_id":"drone-0901","radius_start":2.0,"radius_end":6.0,"clock_start":11.0,"clock_end":1.0,"grade":2,"confidence":0.82},
    {"observation_id":"s2","inspection_id":"drone-0901","radius_start":5.5,"radius_end":9.0,"clock_start":12.0,"clock_end":2.0,"grade":3,"confidence":0.91}
  ]
}
JSON
curl -s http://localhost:8000/api/v1/reviews/WT-A01/B2
```

提交一条“必须同轨”裁决（首笔 `revision=0`，响应回带新生效版本号）：

```bash
curl -s -X POST http://localhost:8000/api/v1/adjudications \
  -H 'Content-Type: application/json' \
  -d '{"turbine_id":"WT-A01","blade_id":"B2","observation_a":"s1","observation_b":"s2","relation":"must_link","revision":0}'
curl -s http://localhost:8000/api/v1/adjudications/WT-A01/B2
```

修补覆盖核对（空区域列表表示无修补）：

```bash
curl -s -X POST http://localhost:8000/api/v1/repair-coverage \
  -H 'Content-Type: application/json' \
  -d '{"turbine_id":"WT-A01","blade_id":"B2","inspection_id":"drone-0901","regions":[{"radius_start":2.0,"radius_end":6.0,"clock_start":11.0,"clock_end":1.0}]}'
```

签发不可变复核报告（携带预期裁决 revision，无裁决传 `0`），随后查询快照并只读核验：

```bash
curl -s -X POST http://localhost:8000/api/v1/review-reports \
  -H 'Content-Type: application/json' \
  -d '{"report_id":"rr-demo-1","turbine_id":"WT-A01","blade_id":"B2","expected_adjudication_revision":0}'
curl -s http://localhost:8000/api/v1/review-reports/rr-demo-1
curl -s http://localhost:8000/api/v1/review-reports/rr-demo-1/verify
```

## 本地开发（不使用 Docker）

```bash
pip install -r requirements.txt
export CRACK_DB_PATH=data/crack_review.db   # 可选，默认即此值
uvicorn app.main:app --reload --port 8000
```

运行测试（无需额外服务，使用临时 SQLite 文件）：

```bash
python -m unittest discover -s tests -v
```

## 项目结构

```
.
├── app/
│   ├── main.py         # FastAPI 入口、路由、startup 建表
│   ├── schemas.py      # Pydantic 请求/响应模型与字段校验
│   ├── services.py     # 写入幂等、重算、查询、巡检变化报告、复核报告签发与核验业务逻辑
│   ├── clustering.py   # 并查集聚类 + 轨迹合并
│   ├── matching.py     # 跨期轨迹配对：重叠面积加权 + 全局最大权一对一匹配
│   ├── coverage.py     # 只读修补覆盖核对：径向切片精确求修补区域并集覆盖面积
│   ├── geometry.py     # 径向/钟位弧相交、重叠长度与合并（含跨 12 点）
│   └── db.py           # SQLite 连接、schema 初始化、数据访问
├── tests/              # 几何 / 聚类 / 匹配 / 覆盖核对 / 复核报告 / 端到端 API 测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
