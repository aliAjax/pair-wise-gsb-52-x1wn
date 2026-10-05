"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound, VoucherConflict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    @staticmethod
    def clock() -> str:
        return _now()

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
                CREATE TABLE IF NOT EXISTS service_vouchers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    voucher_no TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    voucher_type TEXT NOT NULL DEFAULT 'service',
                    status TEXT NOT NULL DEFAULT 'valid',
                    void_reason TEXT,
                    created_by TEXT NOT NULL,
                    updated_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(record_id, voucher_no)
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
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_vouchers_record ON service_vouchers(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _voucher_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create(
        self,
        reference: str,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        voucher: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                if voucher is not None:
                    connection.execute(
                        "INSERT INTO service_vouchers(record_id,voucher_no,started_at,minutes,provider,voucher_type,status,void_reason,created_by,updated_by,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (record_id, voucher["voucher_no"], voucher["started_at"], int(voucher["minutes"]), voucher["provider"], voucher.get("voucher_type", "service"), "valid", None, actor_id, actor_id, now, now),
                    )
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (record_id, "ledger_migrated", actor_id, 1, json.dumps({"voucher_no": voucher["voucher_no"], "minutes": int(voucher["minutes"]), "reason": "新建时按已有分钟数生成迁移凭证"}, ensure_ascii=False, sort_keys=True), now),
                    )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def migrate_legacy_ledgers(self, voucher_builder: Callable[[str, int, str], Dict[str, Any]]) -> int:
        """旧数据升级：为尚无凭证的计划按已有分钟数各补一条迁移凭证。

        每条计划的凭证写入与审计在同一事务语义下完成；失败整体回滚，
        不会只留下半套结果。返回生成的迁移凭证数量。
        """
        migrated = 0
        with self._connect() as connection:
            rows = connection.execute("SELECT id, reference, version, payload, created_at FROM records ORDER BY id").fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                delivered = int(payload.get("delivered_minutes", 0))
                existing = connection.execute("SELECT COUNT(*) AS n FROM service_vouchers WHERE record_id=?", (row["id"],)).fetchone()
                if int(existing["n"]) > 0 or delivered <= 0:
                    continue
                now = _now()
                voucher = voucher_builder(str(row["reference"]), delivered, str(row["created_at"]))
                try:
                    connection.execute(
                        "INSERT INTO service_vouchers(record_id,voucher_no,started_at,minutes,provider,voucher_type,status,void_reason,created_by,updated_by,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row["id"], voucher["voucher_no"], voucher["started_at"], delivered, voucher["provider"], voucher.get("voucher_type", "migration"), "valid", None, voucher["created_by"], voucher["created_by"], now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict("迁移凭证号冲突: %s" % voucher["voucher_no"]) from exc
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], "ledger_migrated", voucher["created_by"], int(row["version"]), json.dumps({"voucher_no": voucher["voucher_no"], "minutes": delivered, "reason": "旧数据升级，按已有分钟数生成迁移凭证"}, ensure_ascii=False, sort_keys=True), now),
                )
                migrated += 1
        return migrated

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_vouchers(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM service_vouchers WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._voucher_row(row) for row in rows]

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _apply_ledger(payload: Dict[str, Any], delivered: int) -> Dict[str, Any]:
        """计划汇总必须由有效凭证重新加总，不信任传入的分钟数。"""
        result = dict(payload)
        result["delivered_minutes"] = int(delivered)
        result["missing_minutes"] = int(result["service_minutes"]) - int(delivered)
        result["compliance_rate"] = round(int(delivered) / int(result["service_minutes"]) * 100, 2)
        return result

    def mutate(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        ledger: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """版本检查、凭证写入、汇总重算、审计在同一个立即事务内完成。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    raise NotFound("记录不存在")
                if int(row["version"]) != int(expected_version):
                    connection.rollback()
                    raise Conflict("版本冲突，请刷新后重试")

                if ledger is not None and ledger["op"] == "insert":
                    try:
                        connection.execute(
                            "INSERT INTO service_vouchers(record_id,voucher_no,started_at,minutes,provider,voucher_type,status,void_reason,created_by,updated_by,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (record_id, ledger["voucher_no"], ledger["started_at"], int(ledger["minutes"]), ledger["provider"], ledger.get("voucher_type", "service"), "valid", None, actor_id, actor_id, now, now),
                        )
                    except sqlite3.IntegrityError as exc:
                        connection.rollback()
                        raise VoucherConflict("凭证号%s已存在，重复提交只计算一次" % ledger["voucher_no"]) from exc
                elif ledger is not None and ledger["op"] == "void":
                    cursor = connection.execute(
                        "UPDATE service_vouchers SET status='void', void_reason=?, updated_by=?, updated_at=? "
                        "WHERE record_id=? AND voucher_no=? AND status='valid'",
                        (ledger["reason"], actor_id, now, record_id, ledger["voucher_no"]),
                    )
                    if cursor.rowcount == 0:
                        connection.rollback()
                        raise NotFound("凭证%s不存在或已撤销" % ledger["voucher_no"])

                if action in {"log_service", "void_voucher"}:
                    total_row = connection.execute(
                        "SELECT COALESCE(SUM(minutes), 0) AS total FROM service_vouchers WHERE record_id=? AND status='valid'",
                        (record_id,),
                    ).fetchone()
                    payload = self._apply_ledger(payload, int(total_row["total"]))

                version = int(expected_version) + 1
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
            except Exception:
                connection.rollback()
                raise
        return self._row(result)

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
