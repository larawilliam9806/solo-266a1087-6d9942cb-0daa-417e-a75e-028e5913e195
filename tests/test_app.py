"""核心规则测试：合规判定、计分唯一、更正/撤销重核、失败沿用原账、多来源。"""
import json
import sqlite3

import pytest

from app import create_app

BASE = {
    "courses": [
        {"course_id": "A", "name": "课程A", "credits": 2},
        {"course_id": "B", "name": "课程B", "credits": 3},
        {"course_id": "C", "name": "课程C", "credits": 4},
    ],
    "prerequisites": [
        {"course_id": "B", "prereq_course_id": "A"},
        {"course_id": "C", "prereq_course_id": "B"},
    ],
}


@pytest.fixture()
def client(tmp_path):
    app = create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.db"),
        "BASE_DATA_FILE": "",          # 测试不自动播种，走导入接口
        "SECRET_KEY": "test",
    })
    with app.test_client() as c:
        r = c.post("/api/admin/base", json=BASE)
        assert r.status_code == 200, r.get_json()
        yield c


def upload(client, student_id, records):
    return client.post("/api/transcripts", json={"student_id": student_id, "records": records})


def rec(rid, course, start, end, passed=True, source=None):
    out = {"record_id": rid, "course_id": course, "start_date": start, "end_date": end, "passed": passed}
    if source is not None:
        out["source"] = source
    return out


def snap(client, sid):
    return client.get(f"/api/students/{sid}").get_json()


def audit_of(snapshot, rid):
    return next(r for r in snapshot["records"] if r["record_id"] == rid)


# ---------------------------------------------------------------- 合规与计分

def test_chain_compliant_and_total(client):
    r = upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
        rec("R3", "C", "2023-03-01", "2023-07-01"),
    ])
    assert r.status_code == 200
    s = snap(client, "S1")
    assert s["total_credits"] == 9
    assert {c["course_id"] for c in s["credited_courses"]} == {"A", "B", "C"}
    # 逐单前置课依据单号
    ev_b = audit_of(s, "R2")["evidence"]
    assert ev_b == [{"prereq_course_id": "A", "prereq_name": "课程A", "record_id": "R1",
                     "source": "INTERNAL", "missing": False}]
    ev_c = audit_of(s, "R3")["evidence"]
    assert ev_c[0]["record_id"] == "R2"


def test_missing_prereq_is_flagged(client):
    upload(client, "S1", [rec("R2", "B", "2022-09-01", "2023-01-01")])
    s = snap(client, "S1")
    row = audit_of(s, "R2")
    assert row["compliant"] is False
    assert row["counted"] is False
    assert row["evidence"][0]["missing"] is True
    assert row["evidence"][0]["record_id"] is None
    assert s["total_credits"] == 0


def test_prereq_must_end_before_start(client):
    # 前置课结课日 == 开课日：不满足“开课前已结课”
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-09-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
    ])
    assert audit_of(snap(client, "S1"), "R2")["compliant"] is False


def test_failed_prereq_not_usable(client):
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01", passed=False),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
    ])
    s = snap(client, "S1")
    assert audit_of(s, "R2")["compliant"] is False
    assert s["total_credits"] == 0  # A 未通过也不计分


def test_evidence_record_must_itself_be_compliant(client):
    # 再造一条底表：A 需要前置课 P（无成绩），则 A 不合规，B 也不能拿 A 当依据
    client.post("/api/admin/base", json={
        "courses": [{"course_id": "P", "name": "课程P", "credits": 1}],
        "prerequisites": [{"course_id": "A", "prereq_course_id": "P"}],
    })
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
    ])
    s = snap(client, "S1")
    assert audit_of(s, "R1")["compliant"] is False
    assert audit_of(s, "R2")["compliant"] is False
    assert s["total_credits"] == 0


def test_same_course_counted_once(client):
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "A", "2022-07-01", "2022-12-01"),
    ])
    s = snap(client, "S1")
    assert s["total_credits"] == 2
    assert len(s["credited_courses"]) == 1
    assert s["credited_courses"][0]["source_record_id"] == "R1"  # 结课最早者计分


def test_failed_course_not_counted_but_compliant(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01", passed=False)])
    s = snap(client, "S1")
    row = audit_of(s, "R1")
    assert row["compliant"] is True and row["counted"] is False
    assert s["total_credits"] == 0


# ---------------------------------------------------------------- 更正与撤销

def test_correction_by_record_id(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    assert snap(client, "S1")["total_credits"] == 2
    r = upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01", passed=False)])  # 同号更正
    assert r.status_code == 200
    assert r.get_json()["corrected"] == 1
    assert snap(client, "S1")["total_credits"] == 0


def test_correction_rejects_other_student_and_revoked(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    r = upload(client, "S2", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    assert r.status_code == 400
    client.post("/api/records/R1/revoke")
    r = upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    assert r.status_code == 400


def test_revoke_with_substitute_updates_evidence(client):
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R1b", "A", "2022-02-01", "2022-07-01"),   # 合规旧单，可替代
        rec("R2", "B", "2022-09-01", "2023-01-01"),
    ])
    s = snap(client, "S1")
    assert audit_of(s, "R2")["evidence"][0]["record_id"] == "R1"
    r = client.post("/api/records/R1/revoke")
    assert r.status_code == 200
    s = snap(client, "S1")
    # 有合规旧单替代：依据单号更新，学分不变
    assert audit_of(s, "R2")["evidence"][0]["record_id"] == "R1b"
    assert audit_of(s, "R2")["compliant"] is True
    assert s["total_credits"] == 5


def test_revoke_cascades_to_dependents(client):
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
        rec("R3", "C", "2023-03-01", "2023-07-01"),
    ])
    assert snap(client, "S1")["total_credits"] == 9
    client.post("/api/records/R1/revoke")
    s = snap(client, "S1")
    assert audit_of(s, "R2")["compliant"] is False   # 依据缺失
    assert audit_of(s, "R3")["compliant"] is False   # 连锁：B 的依据不合规
    assert s["total_credits"] == 0
    # 补交新 A 单后逐级恢复，且依据单号更新为新单
    upload(client, "S1", [rec("R9", "A", "2022-01-01", "2022-06-01")])
    s = snap(client, "S1")
    assert audit_of(s, "R2")["evidence"][0]["record_id"] == "R9"
    assert audit_of(s, "R3")["compliant"] is True
    assert s["total_credits"] == 9


def test_revoke_twice_conflict(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    assert client.post("/api/records/R1/revoke").status_code == 200
    assert client.post("/api/records/R1/revoke").status_code == 409
    assert client.post("/api/records/NOPE/revoke").status_code == 404


# ---------------------------------------------------------------- 事务与校验

def test_failed_upload_keeps_original_ledger(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    before = snap(client, "S1")
    assert before["total_credits"] == 2
    # 批内一条坏单（结课日早于开课日）→ 整批不入账
    r = upload(client, "S1", [
        rec("R2", "B", "2022-09-01", "2023-01-01"),
        rec("R3", "C", "2023-07-01", "2023-03-01"),
    ])
    assert r.status_code == 400
    after = snap(client, "S1")
    assert after["total_credits"] == 2
    assert [x["record_id"] for x in after["records"]] == ["R1"]


def test_unknown_course_rejected(client):
    r = upload(client, "S1", [rec("R1", "ZZZ", "2022-01-01", "2022-06-01")])
    assert r.status_code == 400
    assert snap(client, "S1")["records"] == []


def test_bad_date_rejected(client):
    r = upload(client, "S1", [rec("R1", "A", "2022-13-01", "2022-06-01")])
    assert r.status_code == 400


def test_duplicate_record_id_in_batch_rejected(client):
    r = upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R1", "A", "2022-01-01", "2022-06-01"),
    ])
    assert r.status_code == 400


def test_student_isolation(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    upload(client, "S2", [rec("R2", "B", "2022-09-01", "2023-01-01")])
    # 补交只复核该生：S2 不能用 S1 的成绩作前置依据
    s2 = snap(client, "S2")
    assert audit_of(s2, "R2")["compliant"] is False
    assert s2["total_credits"] == 0
    assert snap(client, "S1")["total_credits"] == 2


def test_query_reads_committed_ledger(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    r = client.get("/api/students/S1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total_credits"] == 2
    assert body["ledger_updated_at"] is not None
    # 网页查询可渲染
    assert client.get("/students/S1").status_code == 200
    assert client.get("/").status_code == 200


# ---------------------------------------------------------------- 多来源（联合培养）

def upload_src(client, student_id, records, source=None):
    body = {"student_id": student_id, "records": records}
    if source is not None:
        body["source"] = source
    return client.post("/api/transcripts", json=body)


def test_same_record_id_across_sources_is_allowed(client):
    # 校内与两类外校来源都可以用同一单号，互不覆盖
    assert upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")]).status_code == 200
    assert upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")], source="EXT_B").status_code == 200
    assert upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")], source="EXT_A").status_code == 200
    s = snap(client, "S1")
    assert len(s["records"]) == 3
    assert {(r["source"], r["record_id"]) for r in s["records"]} == {
        ("INTERNAL", "R1"), ("EXT_B", "R1"), ("EXT_A", "R1"),
    }
    # 同课只计一次，仍是一张单计分
    assert s["total_credits"] == 2
    assert len(s["credited_courses"]) == 1


def test_omitted_source_defaults_to_internal(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    s = snap(client, "S1")
    assert s["records"][0]["source"] == "INTERNAL"
    # 显式写 INTERNAL 与省略等价（同号即更正）
    r = upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01", passed=False)],
                   source="INTERNAL")
    assert r.status_code == 200 and r.get_json()["corrected"] == 1
    assert len(snap(client, "S1")["records"]) == 1


def test_batch_source_with_per_record_override(client):
    r = upload_src(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
        rec("R3", "C", "2023-03-01", "2023-07-01", source="EXT_A"),
    ], source="EXT_B")
    assert r.status_code == 200
    assert r.get_json()["sources"] == ["EXT_A", "EXT_B"]
    s = snap(client, "S1")
    got = {x["record_id"]: x["source"] for x in s["records"]}
    assert got == {"R1": "EXT_B", "R2": "EXT_B", "R3": "EXT_A"}
    # 前置课依据可以取其他来源的成绩单
    ev_b = audit_of(s, "R2")["evidence"][0]
    assert ev_b["record_id"] == "R1" and ev_b["source"] == "EXT_B"
    assert s["total_credits"] == 9


def test_prereq_evidence_may_come_from_other_source(client):
    upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")], source="EXT_A")
    upload_src(client, "S1", [rec("R2", "B", "2022-09-01", "2023-01-01")])  # 校内单
    s = snap(client, "S1")
    ev = audit_of(s, "R2")["evidence"][0]
    assert ev["source"] == "EXT_A" and ev["record_id"] == "R1"
    assert audit_of(s, "R2")["compliant"] is True
    # 计分单也标明来源
    credits = {c["course_id"]: c for c in s["credited_courses"]}
    assert credits["A"]["source"] == "EXT_A"
    assert credits["B"]["source"] == "INTERNAL"


def test_same_end_and_id_prefers_internal_then_source_order(client):
    # 完全相同的结课日与单号：计分单优先校内
    upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")], source="EXT_B")
    upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")], source="EXT_A")
    upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    s = snap(client, "S1")
    assert s["credited_courses"][0]["source"] == "INTERNAL"

    # 只剩外校时，按来源代码排序取最小
    client.post("/api/records/R1/revoke", json={"source": "INTERNAL"})
    s = snap(client, "S1")
    assert s["credited_courses"][0]["source"] == "EXT_A"


def test_same_evidence_prefers_internal_then_source_order(client):
    # 前置课依据结课日与单号都相同：优先校内
    upload_src(client, "S1", [rec("P1", "A", "2022-01-01", "2022-06-01")], source="EXT_B")
    upload_src(client, "S1", [rec("P1", "A", "2022-01-01", "2022-06-01")], source="EXT_A")
    upload_src(client, "S1", [rec("P1", "A", "2022-01-01", "2022-06-01")])
    upload(client, "S1", [rec("R2", "B", "2022-09-01", "2023-01-01")])
    ev = audit_of(snap(client, "S1"), "R2")["evidence"][0]
    assert ev["source"] == "INTERNAL"

    client.post("/api/records/P1/revoke", json={"source": "INTERNAL"})
    ev = audit_of(snap(client, "S1"), "R2")["evidence"][0]
    assert ev["source"] == "EXT_A"


def test_revoke_is_scoped_to_source(client):
    upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")], source="EXT_A")
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    # 不带来源：只撤销校内单
    assert client.post("/api/records/R1/revoke").status_code == 200
    s = snap(client, "S1")
    by_src = {x["source"]: x for x in s["records"]}
    assert by_src["INTERNAL"]["status"] == "revoked"
    assert by_src["EXT_A"]["status"] == "active"
    # 已撤销校内单不能更正；外校同号单不受影响，仍可更正
    assert upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")]).status_code == 400
    assert upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01", passed=False)],
                      source="EXT_A").status_code == 200
    # 撤销外校单需带来源
    assert client.post("/api/records/R1/revoke", json={"source": "EXT_A"}).status_code == 200
    assert client.post("/api/records/R1/revoke").status_code == 409        # 校内已撤销
    assert client.post("/api/records/NOPE/revoke", json={"source": "EXT_A"}).status_code == 404


def test_same_id_other_source_does_not_block_correction(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    # 外校用同号但属于另一个学生：两者是不同单，不冲突
    r = upload_src(client, "S2", [rec("R1", "A", "2022-01-01", "2022-06-01")], source="EXT_A")
    assert r.status_code == 200
    assert snap(client, "S1")["total_credits"] == 2
    assert snap(client, "S2")["total_credits"] == 2


def test_cross_source_substitute_after_revoke(client):
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
    ])
    upload_src(client, "S1", [rec("R1b", "A", "2022-02-01", "2022-07-01")], source="EXT_A")
    assert audit_of(snap(client, "S1"), "R2")["evidence"][0]["record_id"] == "R1"
    client.post("/api/records/R1/revoke")
    s = snap(client, "S1")
    ev = audit_of(s, "R2")["evidence"][0]
    assert ev["record_id"] == "R1b" and ev["source"] == "EXT_A"
    assert s["total_credits"] == 5


def test_failed_multi_source_upload_keeps_original_ledger(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    r = upload_src(client, "S1", [
        rec("R2", "B", "2022-09-01", "2023-01-01"),
        rec("R3", "C", "2023-07-01", "2023-03-01"),
    ], source="EXT_A")
    assert r.status_code == 400
    s = snap(client, "S1")
    assert s["total_credits"] == 2
    assert [(x["source"], x["record_id"]) for x in s["records"]] == [("INTERNAL", "R1")]


def test_legacy_snapshot_fields_preserved(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    upload_src(client, "S1", [rec("R2", "B", "2022-09-01", "2023-01-01")], source="EXT_A")
    body = snap(client, "S1")
    for key in ("student_id", "total_credits", "ledger_updated_at", "credited_courses", "records"):
        assert key in body
    row = audit_of(body, "R2")
    for key in ("record_id", "course_id", "course_name", "start_date", "end_date",
                "passed", "status", "compliant", "counted", "evidence"):
        assert key in row
    assert set(row["evidence"][0]) >= {"prereq_course_id", "prereq_name", "record_id", "missing"}
    assert row["evidence"][0]["record_id"] == "R1"
    assert row["evidence"][0]["source"] == "INTERNAL"


# ---------------------------------------------------------------- 旧库升级

LEGACY_SCHEMA = """
CREATE TABLE courses (course_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                      credits REAL NOT NULL CHECK (credits >= 0));
CREATE TABLE prerequisites (course_id TEXT NOT NULL, prereq_course_id TEXT NOT NULL,
                            PRIMARY KEY (course_id, prereq_course_id));
CREATE TABLE records (
    record_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, course_id TEXT NOT NULL,
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, passed INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', batch_id TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX idx_records_student ON records(student_id);
CREATE TABLE student_credits (student_id TEXT PRIMARY KEY, total_credits REAL NOT NULL,
                              updated_at TEXT NOT NULL);
CREATE TABLE course_credits (student_id TEXT NOT NULL, course_id TEXT NOT NULL,
                             source_record_id TEXT NOT NULL,
                             PRIMARY KEY (student_id, course_id));
CREATE TABLE record_audit (
    record_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, course_id TEXT NOT NULL,
    passed INTEGER NOT NULL, compliant INTEGER NOT NULL, counted INTEGER NOT NULL,
    evidence TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX idx_audit_student ON record_audit(student_id);
"""


def _make_legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.executemany("INSERT INTO courses VALUES (?,?,?)",
                     [("A", "课程A", 2), ("B", "课程B", 3)])
    conn.executemany("INSERT INTO prerequisites VALUES (?,?)", [("B", "A")])
    conn.executemany(
        "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("R1", "S1", "A", "2022-01-01", "2022-06-01", 1, "active", "b1", "t", "t"),
            ("R2", "S1", "B", "2022-09-01", "2023-01-01", 1, "active", "b1", "t", "t"),
        ],
    )
    conn.execute("INSERT INTO student_credits VALUES ('S1', 5, 't')")
    conn.executemany("INSERT INTO course_credits VALUES (?,?,?)",
                     [("S1", "A", "R1"), ("S1", "B", "R2")])
    conn.executemany(
        "INSERT INTO record_audit VALUES (?,?,?,?,?,?,?,?)",
        [
            ("R1", "S1", "A", 1, 1, 1, json.dumps({}, ensure_ascii=False), "t"),
            ("R2", "S1", "B", 1, 1, 1, json.dumps({"A": "R1"}), "t"),
        ],
    )
    conn.commit()
    conn.close()


def _app_at(path):
    return create_app({
        "TESTING": True, "DATABASE": str(path), "BASE_DATA_FILE": "", "SECRET_KEY": "t",
    })


def test_legacy_db_upgrades_without_reupload(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)
    app = _app_at(db_path)
    with app.test_client() as c:
        s = c.get("/api/students/S1").get_json()
        assert s["total_credits"] == 5
        assert {(x["source"], x["record_id"]) for x in s["records"]} == {
            ("INTERNAL", "R1"), ("INTERNAL", "R2")}
        ev = audit_of(s, "R2")["evidence"][0]
        assert ev["record_id"] == "R1" and ev["source"] == "INTERNAL"
        assert {x["source"] for x in s["credited_courses"]} == {"INTERNAL"}
        assert c.get("/students/S1").status_code == 200
        # 升级后旧单号可按校内单直接更正/撤销
        assert upload(c, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01", passed=False)]).status_code == 200
        assert snap(c, "S1")["total_credits"] == 0
        # 外校可以用同一单号；校内单未通过后，外校同号单可作前置依据
        assert upload_src(c, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")],
                          source="EXT_A").status_code == 200
        s = snap(c, "S1")
        assert len(s["records"]) == 3
        assert s["total_credits"] == 5
        ev = audit_of(s, "R2")["evidence"][0]
        assert ev["record_id"] == "R1" and ev["source"] == "EXT_A"


def test_legacy_upgrade_is_idempotent(tmp_path):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)
    _app_at(db_path)
    with _app_at(db_path).test_client() as c:
        s = c.get("/api/students/S1").get_json()
        assert s["total_credits"] == 5
        assert len(s["records"]) == 2


def test_legacy_upgrade_failure_keeps_old_ledger(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    from app import db as dbmod

    def boom(conn):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(dbmod, "_rebuild_record_audit", boom)
    with pytest.raises(RuntimeError):
        _app_at(db_path)
    monkeypatch.undo()

    # 旧库结构、旧账、旧成绩均保持原样
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(records)")}
        assert "source" not in cols
        assert conn.execute("SELECT total_credits FROM student_credits WHERE student_id='S1'").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 2
        assert json.loads(conn.execute("SELECT evidence FROM record_audit WHERE record_id='R2'").fetchone()[0]) == {"A": "R1"}
    finally:
        conn.close()

    # 修复后再次启动仍能正常升级
    app = _app_at(db_path)
    with app.test_client() as c:
        assert c.get("/api/students/S1").get_json()["total_credits"] == 5


def test_upload_form_accepts_batch_source(client):
    payload = json.dumps({"student_id": "S1", "records": [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
    ]})
    r = client.post("/upload", data={"payload": payload, "source": "EXT_A"})
    assert r.status_code == 302
    s = snap(client, "S1")
    assert {x["source"] for x in s["records"]} == {"EXT_A"}
    assert s["total_credits"] == 5
    # 表单来源可被正文顶层 source 覆盖
    payload2 = json.dumps({"student_id": "S1", "source": "EXT_B", "records": [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
    ]})
    assert client.post("/upload", data={"payload": payload2, "source": "EXT_A"}).status_code == 302
    assert {x["source"] for x in snap(client, "S1")["records"]} == {"EXT_A", "EXT_B"}


def test_revoke_path_with_explicit_source(client):
    upload_src(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")], source="EXT_A")
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    r = client.post("/api/records/EXT_A/R1/revoke")
    assert r.status_code == 200
    s = snap(client, "S1")
    by_src = {x["source"]: x for x in s["records"]}
    assert by_src["EXT_A"]["status"] == "revoked"
    assert by_src["INTERNAL"]["status"] == "active"
    assert s["total_credits"] == 2
