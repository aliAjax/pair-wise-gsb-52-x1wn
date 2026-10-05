"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_vouchers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    voucher_no TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'valid',
                    source TEXT NOT NULL DEFAULT 'entry',
                    void_reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    voided_by TEXT,
                    voided_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_vouchers_valid_no
                    ON service_vouchers(record_id, voucher_no) WHERE status='valid';
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_vouchers_record ON service_vouchers(record_id, id);
                """
            )
            self._migrate_vouchers(connection)

    @staticmethod
    def _migrate_vouchers(connection: sqlite3.Connection) -> None:
        """为升级前的旧数据补迁移凭证，并把计划汇总改为按有效凭证重新加总。"""
        rows = connection.execute("SELECT id, payload, created_at FROM records").fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            delivered = int(payload.get("delivered_minutes", 0) or 0)
            existing = connection.execute(
                "SELECT COUNT(*) AS total FROM service_vouchers WHERE record_id=?", (row["id"],)
            ).fetchone()["total"]
            if delivered > 0 and int(existing) == 0:
                connection.execute(
                    "INSERT INTO service_vouchers(record_id,voucher_no,started_at,minutes,provider,status,source,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (row["id"], "MIG-%d" % int(row["id"]), row["created_at"], delivered,
                     str(payload.get("last_provider") or "legacy-migration"), "valid", "migration", "system-migration", _now()),
                )
            total = int(connection.execute(
                "SELECT COALESCE(SUM(minutes),0) AS total FROM service_vouchers WHERE record_id=? AND status='valid'", (row["id"],)
            ).fetchone()["total"])
            if delivered != total:
                payload["delivered_minutes"] = total
                payload["missing_minutes"] = max(0, int(payload.get("service_minutes", 0) or 0) - total)
                service_minutes = int(payload.get("service_minutes", 0) or 0)
                payload["compliance_rate"] = round(total / service_minutes * 100, 2) if service_minutes else 0.0
                connection.execute(
                    "UPDATE records SET payload=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["id"]),
                )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                delivered = int(payload.get("delivered_minutes", 0) or 0)
                if delivered > 0:
                    connection.execute(
                        "INSERT INTO service_vouchers(record_id,voucher_no,started_at,minutes,provider,status,source,created_by,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (record_id, "MIG-%d" % record_id, now, delivered,
                         str(payload.get("last_provider") or "legacy-migration"), "valid", "migration", actor_id, now),
                    )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], voucher: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            if voucher is not None:
                self._apply_voucher(connection, record_id, voucher, actor_id, now)
                ledger_total = int(connection.execute(
                    "SELECT COALESCE(SUM(minutes),0) AS total FROM service_vouchers WHERE record_id=? AND status='valid'", (record_id,)
                ).fetchone()["total"])
                if ledger_total != int(payload.get("delivered_minutes", 0)):
                    connection.rollback()
                    raise Conflict("台账重算结果与计划汇总不一致，已回滚")
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    @staticmethod
    def _apply_voucher(connection: sqlite3.Connection, record_id: int, voucher: Dict[str, Any], actor_id: str, now: str) -> None:
        if voucher["kind"] == "log":
            try:
                connection.execute(
                    "INSERT INTO service_vouchers(record_id,voucher_no,started_at,minutes,provider,status,source,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (record_id, voucher["voucher_no"], voucher["started_at"], int(voucher["minutes"]),
                     voucher["provider"], "valid", "entry", actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("凭证号已存在，重复提交不计入") from exc
        elif voucher["kind"] == "void":
            cursor = connection.execute(
                "UPDATE service_vouchers SET status='voided',void_reason=?,voided_by=?,voided_at=?"
                " WHERE record_id=? AND voucher_no=? AND status='valid'",
                (voucher["reason"], actor_id, now, record_id, voucher["voucher_no"]),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                raise Conflict("凭证不存在或已撤销")
        else:
            connection.rollback()
            raise Conflict("未知的凭证操作")

    def voucher_sum(self, record_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(minutes),0) AS total FROM service_vouchers WHERE record_id=? AND status='valid'", (record_id,)
            ).fetchone()
        return int(row["total"])

    def find_valid_voucher(self, record_id: int, voucher_no: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM service_vouchers WHERE record_id=? AND voucher_no=? AND status='valid'",
                (record_id, voucher_no),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_vouchers(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM service_vouchers WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
