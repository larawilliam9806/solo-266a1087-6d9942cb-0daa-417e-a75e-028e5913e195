"""核心业务：底表导入、成绩单入账（上传/更正/撤销）、按学生重核并写入核定底表。

成绩唯一键是 (来源校代码 source, 单号 record_id)：同一来源内单号全库唯一，
不同来源可以同号。未提供来源的请求按校内（INTERNAL）处理。

规则：
- 修读合规：开课前已结课（前置课结课日 < 本课开课日）且通过所有必修前置课，
  且作为依据的前置课成绩单本身也须修读合规（递归，由开课日先后自然成 DAG）。
  前置课依据可以取任意来源的成绩单。
- 合规且通过才计学分；同一学生同一门课只计一次。
- 选择计分单或前置课依据时，结课日与单号都相同则优先校内单，其余按来源代码排序。
- 依据被撤销后重核：有合规旧单可替代则依据自动更新，否则受影响的后续课
  逐级重核为不合规，学分相应回收。
- 成绩与核定底表在同一事务入账；任何失败整体回滚，沿用原账。
"""
import json
from collections import defaultdict
from datetime import datetime, timezone

from .db import INTERNAL_SOURCE


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value, field):
    if not isinstance(value, str):
        raise ApiError(f"{field} 必须是 YYYY-MM-DD 字符串")
    value = value.strip()
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ApiError(f"{field} 格式应为 YYYY-MM-DD：{value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(f"{field} 不是有效日期：{value!r}")
    return value


def parse_passed(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ApiError(f"passed 必须是 true/false：{value!r}")


def parse_source(value, default=INTERNAL_SOURCE):
    """来源校代码：可省略（按校内）；null/空串或 INTERNAL 同样按校内。"""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ApiError(f"来源校代码 source 必须是字符串：{value!r}")
    value = value.strip()
    if not value or value.upper() == INTERNAL_SOURCE:
        return INTERNAL_SOURCE
    return value


def source_rank(source):
    """来源排序：校内优先，其余按代码升序。用于同日同号时的稳定取舍。"""
    return (0 if source == INTERNAL_SOURCE else 1, source)


def record_sort_key(rec):
    """单号排序：结课日 → 单号 → 来源（校内优先）。"""
    return (rec["end_date"], rec["record_id"], source_rank(rec["source"]))


# ---------------------------------------------------------------- 底表导入

def import_base_data(db, payload):
    """校验并导入课程/学分/前置课（幂等 upsert）。调用方负责事务。"""
    if not isinstance(payload, dict):
        raise ApiError("底表 JSON 顶层必须是对象")
    courses = payload.get("courses")
    if not isinstance(courses, list) or not courses:
        raise ApiError("courses 必须是非空数组")

    seen = set()
    rows = []
    for i, c in enumerate(courses, 1):
        if not isinstance(c, dict):
            raise ApiError(f"courses[{i}] 必须是对象")
        cid = c.get("course_id")
        name = c.get("name")
        if not isinstance(cid, str) or not cid.strip():
            raise ApiError(f"courses[{i}] 缺少 course_id")
        cid = cid.strip()
        if cid in seen:
            raise ApiError(f"课程号重复：{cid}")
        seen.add(cid)
        if not isinstance(name, str) or not name.strip():
            raise ApiError(f"课程 {cid} 缺少名称 name")
        try:
            credits = float(c.get("credits"))
        except (TypeError, ValueError):
            raise ApiError(f"课程 {cid} 的学分 credits 必须是数字")
        if credits < 0:
            raise ApiError(f"课程 {cid} 的学分不能为负")
        rows.append((cid, name.strip(), credits))

    prereqs = payload.get("prerequisites", [])
    if not isinstance(prereqs, list):
        raise ApiError("prerequisites 必须是数组")
    known = set(seen)
    known.update(r["course_id"] for r in db.execute("SELECT course_id FROM courses"))
    preq_rows = []
    for i, p in enumerate(prereqs, 1):
        if not isinstance(p, dict):
            raise ApiError(f"prerequisites[{i}] 必须是对象")
        cid, pcid = p.get("course_id"), p.get("prereq_course_id")
        if not isinstance(cid, str) or not isinstance(pcid, str):
            raise ApiError(f"prerequisites[{i}] 缺少 course_id / prereq_course_id")
        cid, pcid = cid.strip(), pcid.strip()
        if cid == pcid:
            raise ApiError(f"课程 {cid} 不能以自身为前置课")
        if cid not in known:
            raise ApiError(f"前置课定义中的课程 {cid} 不在课程底表内")
        if pcid not in known:
            raise ApiError(f"前置课 {pcid} 不在课程底表内")
        preq_rows.append((cid, pcid))

    for cid, name, credits in rows:
        db.execute(
            "INSERT INTO courses (course_id, name, credits) VALUES (?,?,?) "
            "ON CONFLICT(course_id) DO UPDATE SET name=excluded.name, credits=excluded.credits",
            (cid, name, credits),
        )
    for cid, pcid in preq_rows:
        db.execute(
            "INSERT OR IGNORE INTO prerequisites (course_id, prereq_course_id) VALUES (?,?)",
            (cid, pcid),
        )
    return {"courses": len(rows), "prerequisites": len(preq_rows)}


# ---------------------------------------------------------------- 重核引擎

def recompute_student(db, student_id):
    """重核一名学生并整体重写其核定底表。调用方负责事务（失败需回滚）。"""
    now = now_iso()
    credits_of = {r["course_id"]: r["credits"] for r in db.execute("SELECT course_id, credits FROM courses")}
    prereqs = defaultdict(list)
    for r in db.execute("SELECT course_id, prereq_course_id FROM prerequisites"):
        prereqs[r["course_id"]].append(r["prereq_course_id"])

    rows = db.execute(
        "SELECT source, record_id, course_id, start_date, end_date, passed FROM records "
        "WHERE student_id = ? AND status = 'active' ORDER BY start_date, record_id, source",
        (student_id,),
    ).fetchall()

    # 按来源分组的前置课候选：不同来源的成绩单都可以作为依据。
    by_course = defaultdict(list)
    for r in rows:
        by_course[r["course_id"]].append(r)

    # 按开课日升序处理：依据单（结课日 < 本课开课日）必然先于本单被核定，
    # 因此“依据单本身也须合规”在此自然满足，且不会成环。
    compliant, evidences = {}, {}
    for r in rows:
        key = (r["source"], r["record_id"])
        evidence, ok = {}, True
        for pc in prereqs.get(r["course_id"], []):
            best = None
            for c in by_course.get(pc, []):
                if (c["end_date"] < r["start_date"] and c["passed"]
                        and compliant.get((c["source"], c["record_id"]))):
                    if best is None or record_sort_key(c) < record_sort_key(best):
                        best = c
            evidence[pc] = (
                {"record_id": best["record_id"], "source": best["source"]} if best else None
            )
            if best is None:
                ok = False
        compliant[key] = ok
        evidences[key] = evidence

    # 同一门课只计一次：结课最早、单号最小；同日同号优先校内，其次来源代码序。
    source_of = {}
    for r in sorted(rows, key=record_sort_key):
        if compliant[(r["source"], r["record_id"])] and r["passed"] and r["course_id"] not in source_of:
            source_of[r["course_id"]] = r

    db.execute("DELETE FROM record_audit WHERE student_id = ?", (student_id,))
    db.execute("DELETE FROM course_credits WHERE student_id = ?", (student_id,))
    db.execute("DELETE FROM student_credits WHERE student_id = ?", (student_id,))

    for r in rows:
        rid, src = r["record_id"], r["source"]
        picked = source_of.get(r["course_id"])
        counted = 1 if picked is not None and (picked["source"], picked["record_id"]) == (src, rid) else 0
        db.execute(
            "INSERT INTO record_audit (source, record_id, student_id, course_id, passed, compliant, counted, evidence, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (src, rid, student_id, r["course_id"], r["passed"],
             1 if compliant[(src, rid)] else 0, counted,
             json.dumps(evidences[(src, rid)], ensure_ascii=False), now),
        )
    for cid, r in source_of.items():
        db.execute(
            "INSERT INTO course_credits (student_id, course_id, source, source_record_id) VALUES (?,?,?,?)",
            (student_id, cid, r["source"], r["record_id"]),
        )
    total = round(sum(credits_of.get(cid, 0.0) for cid in source_of), 6)
    db.execute(
        "INSERT INTO student_credits (student_id, total_credits, updated_at) VALUES (?,?,?)",
        (student_id, total, now),
    )
    return {
        "student_id": student_id,
        "total_credits": total,
        "credited_courses": len(source_of),
        "records_checked": len(rows),
    }


# ---------------------------------------------------------------- 成绩单入账

def _validate_record_payload(db, rec, idx):
    if not isinstance(rec, dict):
        raise ApiError(f"records[{idx}] 必须是对象")
    rid = rec.get("record_id")
    if not isinstance(rid, str) or not rid.strip():
        raise ApiError(f"records[{idx}] 缺少单号 record_id")
    rid = rid.strip()

    cid = rec.get("course_id")
    if not isinstance(cid, str) or not cid.strip():
        raise ApiError(f"单号 {rid} 缺少课程 course_id")
    cid = cid.strip()
    if not db.execute("SELECT 1 FROM courses WHERE course_id = ?", (cid,)).fetchone():
        raise ApiError(f"单号 {rid} 的课程 {cid} 不在课程底表内")

    start = parse_date(rec.get("start_date"), f"单号 {rid} 的开课日 start_date")
    end = parse_date(rec.get("end_date"), f"单号 {rid} 的结课日 end_date")
    if end < start:
        raise ApiError(f"单号 {rid} 结课日早于开课日")
    passed = parse_passed(rec.get("passed"))
    return rid, cid, start, end, passed


def apply_transcript_batch(db, payload, batch_id):
    """分批上传：新单入账、同来源同号单更正；随后只复核该生。调用方负责事务。

    来源可在批次顶层给默认值（source），每条记录可用 source 覆盖；
    两者都省略时按校内处理。
    """
    if not isinstance(payload, dict):
        raise ApiError("成绩单 JSON 顶层必须是对象")
    student_id = payload.get("student_id")
    if not isinstance(student_id, str) or not student_id.strip():
        raise ApiError("缺少学号 student_id")
    student_id = student_id.strip()
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ApiError("records 必须是非空数组")
    batch_source = parse_source(payload.get("source"))

    now = now_iso()
    seen, added, corrected = set(), 0, 0
    sources = set()
    for i, rec in enumerate(records, 1):
        rid, cid, start, end, passed = _validate_record_payload(db, rec, i)
        source = batch_source
        if "source" in rec:
            source = parse_source(rec.get("source"), default=batch_source)
        key = (source, rid)
        if key in seen:
            raise ApiError(f"同一来源批内单号重复：{source} / {rid}")
        seen.add(key)
        sources.add(source)

        old = db.execute(
            "SELECT student_id, status FROM records WHERE source = ? AND record_id = ?",
            (source, rid),
        ).fetchone()
        if old is None:
            db.execute(
                "INSERT INTO records (source, record_id, student_id, course_id, start_date, end_date,"
                " passed, status, batch_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,'active',?,?,?)",
                (source, rid, student_id, cid, start, end, passed, batch_id, now, now),
            )
            added += 1
        else:
            if old["student_id"] != student_id:
                raise ApiError(f"来源 {source} 的单号 {rid} 属于其他学生，不能更正")
            if old["status"] != "active":
                raise ApiError(f"来源 {source} 的单号 {rid} 已撤销，不能更正")
            db.execute(
                "UPDATE records SET course_id=?, start_date=?, end_date=?, passed=?, batch_id=?, updated_at=?"
                " WHERE source = ? AND record_id = ?",
                (cid, start, end, passed, batch_id, now, source, rid),
            )
            corrected += 1

    result = recompute_student(db, student_id)
    result.update({
        "batch_id": batch_id,
        "added": added,
        "corrected": corrected,
        "sources": sorted(sources, key=source_rank),
    })
    return result


def revoke_record(db, record_id, source=INTERNAL_SOURCE):
    """按来源+单号撤销，随后只复核该生（替代/连锁重核在 recompute 内完成）。"""
    row = db.execute(
        "SELECT student_id, status FROM records WHERE source = ? AND record_id = ?",
        (source, record_id),
    ).fetchone()
    if row is None:
        raise ApiError(f"来源 {source} 的单号 {record_id} 不存在", 404)
    if row["status"] == "revoked":
        raise ApiError(f"来源 {source} 的单号 {record_id} 已是撤销状态", 409)
    db.execute(
        "UPDATE records SET status='revoked', updated_at=? WHERE source = ? AND record_id = ?",
        (now_iso(), source, record_id),
    )
    return recompute_student(db, row["student_id"])


# ---------------------------------------------------------------- 查询（只读核定底表）

def student_snapshot(db, student_id):
    courses = {r["course_id"]: r for r in db.execute("SELECT * FROM courses")}
    sc = db.execute(
        "SELECT total_credits, updated_at FROM student_credits WHERE student_id = ?", (student_id,)
    ).fetchone()
    cc = db.execute(
        "SELECT course_id, source, source_record_id FROM course_credits"
        " WHERE student_id = ? ORDER BY course_id",
        (student_id,),
    ).fetchall()
    recs = db.execute(
        "SELECT r.source, r.record_id, r.course_id, r.start_date, r.end_date, r.passed, r.status,"
        "       r.batch_id, a.compliant, a.counted, a.evidence"
        " FROM records r LEFT JOIN record_audit a"
        "   ON a.source = r.source AND a.record_id = r.record_id"
        " WHERE r.student_id = ?"
        " ORDER BY r.start_date, r.record_id,"
        "          CASE WHEN r.source = ? THEN 0 ELSE 1 END, r.source",
        (student_id, INTERNAL_SOURCE),
    ).fetchall()

    out_records = []
    for r in recs:
        evidence = []
        if r["evidence"]:
            for pc, item in json.loads(r["evidence"]).items():
                prow = courses.get(pc)
                rid = item.get("record_id") if isinstance(item, dict) else item
                src = item.get("source") if isinstance(item, dict) else None
                evidence.append({
                    "prereq_course_id": pc,
                    "prereq_name": prow["name"] if prow else pc,
                    "record_id": rid,          # None 表示缺项
                    "source": src,             # 依据单来源；缺项时 None
                    "missing": rid is None,
                })
        out_records.append({
            "source": r["source"],
            "record_id": r["record_id"],
            "course_id": r["course_id"],
            "course_name": courses[r["course_id"]]["name"] if r["course_id"] in courses else r["course_id"],
            "start_date": r["start_date"],
            "end_date": r["end_date"],
            "passed": bool(r["passed"]),
            "status": r["status"],
            "compliant": None if r["compliant"] is None else bool(r["compliant"]),
            "counted": None if r["counted"] is None else bool(r["counted"]),
            "evidence": evidence,
        })

    return {
        "student_id": student_id,
        "total_credits": sc["total_credits"] if sc else 0,
        "ledger_updated_at": sc["updated_at"] if sc else None,
        "credited_courses": [
            {
                "course_id": row["course_id"],
                "name": courses[row["course_id"]]["name"] if row["course_id"] in courses else row["course_id"],
                "credits": courses[row["course_id"]]["credits"] if row["course_id"] in courses else None,
                "source_record_id": row["source_record_id"],
                "source": row["source"],
            }
            for row in cc
        ],
        "records": out_records,
    }
