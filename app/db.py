"""SQLite 连接与建表。底表(课程/学分/前置课) + 成绩单 + 核定底表。

成绩单与核定底表都以 (来源校代码, 单号) 为键：同一来源内单号全库唯一，
不同来源可以使用同一个单号。旧库（仅有全局单号）启动时在事务内迁移，
历史成绩及其前置依据一律归为校内来源。
"""
import json
import os
import sqlite3

from flask import current_app, g

# 校内成绩的来源校代码；未填写来源的请求一律按校内处理。
INTERNAL_SOURCE = "INTERNAL"

RECORDS_COLUMNS = """
    source     TEXT NOT NULL DEFAULT 'INTERNAL',  -- 来源校代码，INTERNAL=校内
    record_id  TEXT NOT NULL,                     -- 单号（同一来源内全库唯一）
    student_id TEXT NOT NULL,
    course_id  TEXT NOT NULL REFERENCES courses(course_id),
    start_date TEXT NOT NULL,                     -- 开课日 YYYY-MM-DD
    end_date   TEXT NOT NULL,                     -- 结课日 YYYY-MM-DD
    passed     INTEGER NOT NULL,                  -- 是否通过 1/0
    status     TEXT NOT NULL DEFAULT 'active',    -- active / revoked
    batch_id   TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, record_id)
"""

COURSE_CREDITS_COLUMNS = """
    student_id       TEXT NOT NULL,
    course_id        TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'INTERNAL',  -- 计分单来源
    source_record_id TEXT NOT NULL,                     -- 计分所依据的成绩单号
    PRIMARY KEY (student_id, course_id)
"""

RECORD_AUDIT_COLUMNS = """
    source     TEXT NOT NULL DEFAULT 'INTERNAL',  -- 被核对成绩单的来源
    record_id  TEXT NOT NULL,
    student_id TEXT NOT NULL,
    course_id  TEXT NOT NULL,
    passed     INTEGER NOT NULL,
    compliant  INTEGER NOT NULL,            -- 修读是否合规
    counted    INTEGER NOT NULL,            -- 是否计入学分
    evidence   TEXT NOT NULL,               -- JSON: {前置课号: {record_id, source} 或 null}
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, record_id)
"""

SCHEMA = f"""
-- 底表：课程与学分
CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    credits   REAL NOT NULL CHECK (credits >= 0)
);

-- 底表：必修前置课
CREATE TABLE IF NOT EXISTS prerequisites (
    course_id        TEXT NOT NULL REFERENCES courses(course_id),
    prereq_course_id TEXT NOT NULL REFERENCES courses(course_id),
    PRIMARY KEY (course_id, prereq_course_id)
);

-- 学生成绩单（含已撤销，留痕）；同一来源内单号唯一
CREATE TABLE IF NOT EXISTS records ({RECORDS_COLUMNS});
CREATE INDEX IF NOT EXISTS idx_records_student ON records(student_id);

-- 核定底表：学生总学分（查询直接读这里）
CREATE TABLE IF NOT EXISTS student_credits (
    student_id    TEXT PRIMARY KEY,
    total_credits REAL NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 核定底表：已认定课程（同一生同一课只计一次）
CREATE TABLE IF NOT EXISTS course_credits ({COURSE_CREDITS_COLUMNS});

-- 核定底表：逐单核对结果（合规性 + 前置课依据单号/来源/缺项）
CREATE TABLE IF NOT EXISTS record_audit ({RECORD_AUDIT_COLUMNS});
CREATE INDEX IF NOT EXISTS idx_audit_student ON record_audit(student_id);
"""


def connect(db_path):
    """独立连接（自动提交模式，事务由 BEGIN/COMMIT 显式控制）。"""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_db():
    """请求内共享连接。"""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def rollback_quietly(db):
    try:
        db.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- 旧库迁移

def _table_columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone())


def _evidence_with_internal_source(raw):
    """把旧版 evidence（{前置课: 单号或 null}）转成带来源的新结构。"""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return "{}"
    if not isinstance(data, dict):
        return "{}"
    out = {}
    for prereq, value in data.items():
        if value is None:
            out[prereq] = None
        elif isinstance(value, dict):
            rid = value.get("record_id")
            if rid is None:
                out[prereq] = None
            else:
                out[prereq] = {
                    "record_id": rid,
                    "source": value.get("source") or INTERNAL_SOURCE,
                }
        else:
            out[prereq] = {"record_id": value, "source": INTERNAL_SOURCE}
    return json.dumps(out, ensure_ascii=False)


def _rebuild_records(conn):
    conn.execute(f"CREATE TABLE records__new ({RECORDS_COLUMNS})")
    conn.execute(
        "INSERT INTO records__new"
        " (source, record_id, student_id, course_id, start_date, end_date, passed,"
        "  status, batch_id, created_at, updated_at)"
        " SELECT ?, record_id, student_id, course_id, start_date, end_date, passed,"
        "        status, batch_id, created_at, updated_at FROM records",
        (INTERNAL_SOURCE,),
    )
    conn.execute("DROP TABLE records")
    conn.execute("ALTER TABLE records__new RENAME TO records")


def _rebuild_record_audit(conn):
    conn.execute(f"CREATE TABLE record_audit__new ({RECORD_AUDIT_COLUMNS})")
    for row in conn.execute("SELECT * FROM record_audit"):
        conn.execute(
            "INSERT INTO record_audit__new"
            " (source, record_id, student_id, course_id, passed, compliant, counted,"
            "  evidence, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                INTERNAL_SOURCE, row["record_id"], row["student_id"], row["course_id"],
                row["passed"], row["compliant"], row["counted"],
                _evidence_with_internal_source(row["evidence"]), row["updated_at"],
            ),
        )
    conn.execute("DROP TABLE record_audit")
    conn.execute("ALTER TABLE record_audit__new RENAME TO record_audit")


def _rebuild_course_credits(conn):
    conn.execute(f"CREATE TABLE course_credits__new ({COURSE_CREDITS_COLUMNS})")
    conn.execute(
        "INSERT INTO course_credits__new"
        " (student_id, course_id, source, source_record_id)"
        " SELECT student_id, course_id, ?, source_record_id FROM course_credits",
        (INTERNAL_SOURCE,),
    )
    conn.execute("DROP TABLE course_credits")
    conn.execute("ALTER TABLE course_credits__new RENAME TO course_credits")


def _migrate_legacy_schema(conn):
    """把旧版仅按单号建键的库升级为按 (来源, 单号) 建键。

    整个升级在一个事务内完成；任何一步失败都回滚，原成绩与核定结果保持不变。
    返回是否确实执行了迁移。
    """
    needs = []
    if _table_exists(conn, "records") and "source" not in _table_columns(conn, "records"):
        needs.append("records")
    if _table_exists(conn, "record_audit") and "source" not in _table_columns(conn, "record_audit"):
        needs.append("record_audit")
    if _table_exists(conn, "course_credits") and "source" not in _table_columns(conn, "course_credits"):
        needs.append("course_credits")
    if not needs:
        return False

    # SQLite 的 PRAGMA foreign_keys 在事务内无效，必须先关闭再开启事务。
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if "records" in needs:
            _rebuild_records(conn)
        if "record_audit" in needs:
            _rebuild_record_audit(conn)
        if "course_credits" in needs:
            _rebuild_course_credits(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_student ON records(student_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_student ON record_audit(student_id)")
        conn.execute("COMMIT")
    except Exception:
        rollback_quietly(conn)
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def init_db_file(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = connect(db_path)
    try:
        # 旧库在这里仍是旧表（CREATE IF NOT EXISTS 不会改结构），随后事务化迁移。
        conn.executescript(SCHEMA)
        return _migrate_legacy_schema(conn)
    finally:
        conn.close()
