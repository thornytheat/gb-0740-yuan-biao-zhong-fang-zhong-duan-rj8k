"""差额说明账本：只追加、永不更新、永不删除。

- 说明编号由计划编号与说明正文的规范序列化哈希决定，同一份重试结果哈希不变，
  INSERT OR IGNORE 保证重放重试不会新增说明。
- 表结构无更新/删除通道；另设只追加的审计流水，记录每次签发尝试与查询，
  旧说明即使从页面入口丢失，仍可通过账本与审计记录回查。
"""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


def audit_hash(statement_id, document, plan_id):
    serial = json.dumps(
        [statement_id, plan_id, document], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(serial).hexdigest()


class StatementStore:
    def __init__(self, path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connection()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS variance_statements ("
                "id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, document TEXT NOT NULL, "
                "created_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS variance_audit ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, "
                "statement_id TEXT NOT NULL, plan_id TEXT NOT NULL, "
                "entry_hash TEXT NOT NULL, created_at TEXT NOT NULL)"
            )

    def connection(self):
        return sqlite3.connect(self.path, timeout=10)

    def _log(self, db, event, statement_id, plan_id, document):
        db.execute(
            "INSERT INTO variance_audit (event, statement_id, plan_id, entry_hash, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                event,
                statement_id,
                plan_id,
                audit_hash(statement_id, document, plan_id),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )

    def issue(self, payload):
        statement_id = payload["说明编号"]
        plan_id = payload["批次编号"]
        document = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with closing(self.connection()) as db, db:
            op = db.execute(
                "INSERT OR IGNORE INTO variance_statements (id, plan_id, document, created_at) "
                "VALUES (?,?,?,?)",
                (
                    statement_id,
                    plan_id,
                    document,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            self._log(db, "ISSUE_REUSED" if op.rowcount == 0 else "ISSUE_CREATED", statement_id, plan_id, payload)
            row = db.execute(
                "SELECT document FROM variance_statements WHERE id=?", (statement_id,)
            ).fetchone()[0]
        return json.loads(row), op.rowcount > 0

    def get(self, statement_id):
        with closing(self.connection()) as db, db:
            row = db.execute(
                "SELECT document FROM variance_statements WHERE id=?", (statement_id,)
            ).fetchone()
            if row:
                document = json.loads(row[0])
                self._log(db, "GET", statement_id, document["批次编号"], document)
                return document
        return None

    def get_by_plan(self, plan_id):
        with closing(self.connection()) as db:
            row = db.execute(
                "SELECT document FROM variance_statements WHERE plan_id=? ORDER BY rowid", (plan_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def history(self):
        with closing(self.connection()) as db:
            return [
                {
                    "说明编号": row[0],
                    "批次编号": row[1],
                    "签发时间": row[2],
                    "勾稽一致": json.loads(row[3])["守恒校验"]["勾稽一致"],
                    "未解释行数": json.loads(row[3])["未解释余量"]["行数"],
                }
                for row in db.execute(
                    "SELECT id, plan_id, created_at, document FROM variance_statements ORDER BY rowid"
                )
            ]

    def audit_trail(self):
        with closing(self.connection()) as db:
            return [
                {"序号": row[0], "事件": row[1], "说明编号": row[2], "批次编号": row[3],
                 "流水哈希": row[4], "时间": row[5]}
                for row in db.execute(
                    "SELECT seq, event, statement_id, plan_id, entry_hash, created_at "
                    "FROM variance_audit ORDER BY seq"
                )
            ]
