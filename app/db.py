"""SQLite 持久化：schema 初始化与数据访问。

数据库文件路径由环境变量 ``CRACK_DB_PATH`` 指定，随应用启动自动建表。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

DB_PATH = os.environ.get("CRACK_DB_PATH", "data/crack_review.db")

SCHEMA: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS batches (
        batch_id   TEXT PRIMARY KEY,
        turbine_id TEXT NOT NULL,
        blade_id   TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observations (
        observation_id TEXT PRIMARY KEY,
        batch_id    TEXT NOT NULL REFERENCES batches(batch_id),
        turbine_id  TEXT NOT NULL,
        blade_id    TEXT NOT NULL,
        inspection_id TEXT NOT NULL,
        inspected_at TEXT,
        radius_start REAL NOT NULL,
        radius_end   REAL NOT NULL,
        clock_start  REAL NOT NULL,
        clock_end    REAL NOT NULL,
        grade        INTEGER NOT NULL,
        confidence   REAL NOT NULL,
        seq          INTEGER NOT NULL,
        created_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_obs_blade ON observations(turbine_id, blade_id)",
    "CREATE INDEX IF NOT EXISTS idx_obs_batch ON observations(batch_id)",
    """
    CREATE TABLE IF NOT EXISTS review_state (
        turbine_id TEXT NOT NULL,
        blade_id   TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        trajectory_count INTEGER NOT NULL,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (turbine_id, blade_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trajectories (
        turbine_id TEXT NOT NULL,
        blade_id   TEXT NOT NULL,
        trajectory_id TEXT NOT NULL,
        ord INTEGER NOT NULL,
        data_json TEXT NOT NULL,
        PRIMARY KEY (turbine_id, blade_id, trajectory_id)
    )
    """,
    # 人工关联裁决：must_link=必须同轨 / cannot_link=禁止同轨。
    # pair_key 为无序观察对的规范化键，配合唯一索引保证“相同裁决请求幂等”。
    """
    CREATE TABLE IF NOT EXISTS adjudications (
        adjudication_id TEXT PRIMARY KEY,
        turbine_id TEXT NOT NULL,
        blade_id   TEXT NOT NULL,
        observation_a TEXT NOT NULL,
        observation_b TEXT NOT NULL,
        pair_key  TEXT NOT NULL,
        relation  TEXT NOT NULL CHECK (relation IN ('must_link', 'cannot_link')),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_adj_pair
    ON adjudications(turbine_id, blade_id, pair_key, relation)
    """,
    "CREATE INDEX IF NOT EXISTS idx_adj_blade ON adjudications(turbine_id, blade_id)",
    # 每个叶片一条裁决状态：revision 单调递增，用作乐观锁与结果生效版本号
    """
    CREATE TABLE IF NOT EXISTS adjudication_state (
        turbine_id TEXT NOT NULL,
        blade_id   TEXT NOT NULL,
        revision   INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (turbine_id, blade_id)
    )
    """,
)

# 旧库平滑升级：review_state 增补裁决生效版本列
_MIGRATIONS: Sequence[str] = (
    "ALTER TABLE review_state ADD COLUMN adjudication_revision INTEGER NOT NULL DEFAULT 0",
)

_init_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """建表（幂等），应用启动时执行。"""
    with _init_lock:
        conn = _connect()
        try:
            with conn:
                for stmt in SCHEMA:
                    conn.execute(stmt)
                _apply_migrations(conn)
        finally:
            conn.close()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """对既有数据库做幂等的轻量结构升级。"""
    existing = {r["name"] for r in conn.execute(
        "PRAGMA table_info(review_state)"
    ).fetchall()}
    for stmt in _MIGRATIONS:
        column = stmt.split("ADD COLUMN")[1].split()[0]
        if column not in existing:
            conn.execute(stmt)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- batches

def get_batch(conn: sqlite3.Connection, batch_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()


def insert_batch(conn: sqlite3.Connection, batch_id: str, turbine_id: str,
                 blade_id: str, content_hash: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO batches(batch_id, turbine_id, blade_id, content_hash, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (batch_id, turbine_id, blade_id, content_hash, created_at),
    )


def count_observations(conn: sqlite3.Connection, batch_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM observations WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    return int(row["c"])


def insert_observation(conn: sqlite3.Connection, obs: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO observations(
            observation_id, batch_id, turbine_id, blade_id, inspection_id,
            inspected_at, radius_start, radius_end, clock_start, clock_end,
            grade, confidence, seq, created_at)
        VALUES (:observation_id, :batch_id, :turbine_id, :blade_id, :inspection_id,
                :inspected_at, :radius_start, :radius_end, :clock_start, :clock_end,
                :grade, :confidence, :seq, :created_at)
        """,
        obs,
    )


def get_observation(conn: sqlite3.Connection, observation_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM observations WHERE observation_id = ?", (observation_id,)
    ).fetchone()


# ------------------------------------------------------------- blade data

def list_observations(conn: sqlite3.Connection,
                      turbine_id: str, blade_id: str) -> List[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT * FROM observations
        WHERE turbine_id = ? AND blade_id = ?
        ORDER BY radius_start, clock_start, seq, observation_id
        """,
        (turbine_id, blade_id),
    ).fetchall())


def blade_observation_count(conn: sqlite3.Connection,
                            turbine_id: str, blade_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM observations WHERE turbine_id = ? AND blade_id = ?",
        (turbine_id, blade_id),
    ).fetchone()
    return int(row["c"])


# ----------------------------------------------------------- review state

def get_review_state(conn: sqlite3.Connection,
                     turbine_id: str, blade_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_state WHERE turbine_id = ? AND blade_id = ?",
        (turbine_id, blade_id),
    ).fetchone()


def replace_review(conn: sqlite3.Connection, turbine_id: str, blade_id: str,
                   input_hash: str, trajectories: List[Dict[str, Any]],
                   computed_at: str, adjudication_revision: int = 0) -> None:
    """在同一事务内整体替换叶片复核结果（幂等重算的核心）。"""
    conn.execute(
        "DELETE FROM trajectories WHERE turbine_id = ? AND blade_id = ?",
        (turbine_id, blade_id),
    )
    for ord_, trj in enumerate(trajectories):
        import json
        conn.execute(
            "INSERT INTO trajectories(turbine_id, blade_id, trajectory_id, ord, data_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (turbine_id, blade_id, trj["trajectory_id"], ord_,
             json.dumps(trj, ensure_ascii=False, sort_keys=True)),
        )
    conn.execute(
        """
        INSERT INTO review_state(turbine_id, blade_id, input_hash,
                                 trajectory_count, computed_at,
                                 adjudication_revision)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(turbine_id, blade_id) DO UPDATE SET
            input_hash=excluded.input_hash,
            trajectory_count=excluded.trajectory_count,
            computed_at=excluded.computed_at,
            adjudication_revision=excluded.adjudication_revision
        """,
        (turbine_id, blade_id, input_hash, len(trajectories), computed_at,
         adjudication_revision),
    )


def list_trajectories(conn: sqlite3.Connection,
                      turbine_id: str, blade_id: str) -> List[Dict[str, Any]]:
    import json
    rows = conn.execute(
        "SELECT data_json FROM trajectories"
        " WHERE turbine_id = ? AND blade_id = ? ORDER BY ord",
        (turbine_id, blade_id),
    ).fetchall()
    result: List[Dict[str, Any]] = []
    for r in rows:
        trj = json.loads(r["data_json"])
        # 旧版本落库结果没有裁决命中字段，读取时补空，保持接口形态一致
        trj.setdefault("adjudication_ids", [])
        result.append(trj)
    return result


# --------------------------------------------------------- adjudications

def list_adjudications(conn: sqlite3.Connection,
                       turbine_id: str, blade_id: str) -> List[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT * FROM adjudications
        WHERE turbine_id = ? AND blade_id = ?
        ORDER BY created_at, adjudication_id
        """,
        (turbine_id, blade_id),
    ).fetchall())


def get_adjudication_by_pair(conn: sqlite3.Connection, turbine_id: str,
                             blade_id: str, pair_key: str,
                             relation: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM adjudications"
        " WHERE turbine_id = ? AND blade_id = ?"
        " AND pair_key = ? AND relation = ?",
        (turbine_id, blade_id, pair_key, relation),
    ).fetchone()


def insert_adjudication(conn: sqlite3.Connection, adj: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO adjudications(adjudication_id, turbine_id, blade_id,
                                  observation_a, observation_b, pair_key,
                                  relation, created_at)
        VALUES (:adjudication_id, :turbine_id, :blade_id,
                :observation_a, :observation_b, :pair_key,
                :relation, :created_at)
        """,
        adj,
    )


def get_adjudication_revision(conn: sqlite3.Connection, turbine_id: str,
                              blade_id: str) -> int:
    row = conn.execute(
        "SELECT revision FROM adjudication_state"
        " WHERE turbine_id = ? AND blade_id = ?",
        (turbine_id, blade_id),
    ).fetchone()
    return int(row["revision"]) if row is not None else 0


def bump_adjudication_revision(conn: sqlite3.Connection, turbine_id: str,
                               blade_id: str, updated_at: str,
                               expected_revision: Optional[int] = None
                               ) -> Optional[int]:
    """叶片裁决版本号 +1（不存在则从 1 起），返回新 revision。

    ``expected_revision`` 非空时作为乐观锁条件：仅当库中当前版本等于该值
    才递增（首笔插入隐含当前版本为 0）；不匹配返回 None，调用方应回滚事务。
    """
    if expected_revision is None:
        conn.execute(
            """
            INSERT INTO adjudication_state(turbine_id, blade_id, revision, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(turbine_id, blade_id) DO UPDATE SET
                revision = adjudication_state.revision + 1,
                updated_at = excluded.updated_at
            """,
            (turbine_id, blade_id, updated_at),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO adjudication_state(turbine_id, blade_id, revision, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(turbine_id, blade_id) DO UPDATE SET
                revision = adjudication_state.revision + 1,
                updated_at = excluded.updated_at
            WHERE adjudication_state.revision = ?
            """,
            (turbine_id, blade_id, updated_at, expected_revision),
        )
        if cursor.rowcount == 0:
            return None
    return int(conn.execute(
        "SELECT revision FROM adjudication_state"
        " WHERE turbine_id = ? AND blade_id = ?",
        (turbine_id, blade_id),
    ).fetchone()["revision"])
