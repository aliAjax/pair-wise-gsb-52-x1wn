import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, LedgerMismatch, NotFound, ValidationError, VoucherConflict


CM = Actor("cm", "case_manager")
PARENT = Actor("parent", "parent_rep")
ADMIN = Actor("admin", "administrator")
SP1 = Actor("sp1", "specialist")
SP2 = Actor("sp2", "specialist")

CREATE_DATA = {'student_id': 'S-200', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}
STARTED = '2026-10-05T09:00:00+00:00'


def activate_plan(service, ref='IEP-29001'):
    record = service.create(CM, ref, CREATE_DATA)
    record = service.act(PARENT, record["id"], record["version"], 'consent', {'guardian_confirmed': True, 'consent_scope': '个别化服务'})
    record = service.act(CM, record["id"], record["version"], 'activate', {})
    return record


class VoucherLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_with_delivered_minutes_generates_migration_voucher(self):
        record = self.service.create(CM, 'IEP-29001', CREATE_DATA)
        vouchers = self.service.vouchers(CM, record["id"])
        self.assertEqual(len(vouchers), 1)
        v = vouchers[0]
        self.assertEqual(v["voucher_no"], "MIG-IEP-29001")
        self.assertEqual(v["minutes"], 120)
        self.assertEqual(v["status"], "valid")
        self.assertEqual(v["voucher_type"], "migration")
        self.assertEqual(v["provider"], "历史数据迁移")

    def test_service_voucher_carries_identity_fields(self):
        record = activate_plan(self.service)
        record = self.service.act(SP1, record["id"], record["version"], 'log_service', {
            'voucher_no': 'V-001', 'started_at': STARTED, 'minutes': 60, 'provider': 'SP-zhang',
        })
        self.assertEqual(record["payload"]["delivered_minutes"], 180)
        vouchers = self.service.vouchers(SP1, record["id"])
        service_voucher = next(v for v in vouchers if v["voucher_no"] == 'V-001')
        self.assertEqual(service_voucher["started_at"], STARTED)
        self.assertEqual(service_voucher["minutes"], 60)
        self.assertEqual(service_voucher["provider"], 'SP-zhang')
        self.assertEqual(service_voucher["voucher_type"], 'service')
        self.assertEqual(record["payload"]["missing_minutes"], 420)

    def test_duplicate_voucher_number_counts_once(self):
        record = activate_plan(self.service)
        payload = {'voucher_no': 'V-DUP', 'started_at': STARTED, 'minutes': 30, 'provider': 'SP-li'}
        record = self.service.act(SP1, record["id"], record["version"], 'log_service', payload)
        self.assertEqual(record["payload"]["delivered_minutes"], 150)
        with self.assertRaises(VoucherConflict):
            self.service.act(SP2, record["id"], record["version"], 'log_service', dict(payload, started_at='2026-10-05T10:00:00+00:00'))
        fresh = self.service.get_record(CM, record["id"])
        self.assertEqual(fresh["payload"]["delivered_minutes"], 150)
        self.assertEqual(len(self.service.vouchers(CM, record["id"])), 2)

    def test_concurrent_backfill_loser_gets_version_conflict(self):
        record = activate_plan(self.service)
        version = record["version"]
        results = []
        barrier = threading.Barrier(2)

        def submit(actor, voucher_no):
            barrier.wait()
            try:
                updated = self.service.act(actor, record["id"], version, 'log_service', {
                    'voucher_no': voucher_no, 'started_at': STARTED, 'minutes': 45, 'provider': actor.user_id,
                })
                results.append(('ok', updated["version"], voucher_no))
            except Conflict as exc:
                results.append(('conflict', str(exc), voucher_no))

        t1 = threading.Thread(target=submit, args=(SP1, 'V-C1'))
        t2 = threading.Thread(target=submit, args=(SP2, 'V-C2'))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = sorted(item[0] for item in results)
        self.assertEqual(statuses, ['conflict', 'ok'])
        winner = next(item[2] for item in results if item[0] == 'ok')
        fresh = self.service.get_record(CM, record["id"])
        self.assertEqual(fresh["version"], version + 1)
        self.assertEqual(fresh["payload"]["delivered_minutes"], 165)
        voucher_nos = sorted(v["voucher_no"] for v in self.service.vouchers(CM, record["id"]))
        self.assertEqual(voucher_nos, ['MIG-IEP-29001', winner])

    def test_void_voucher_requires_reason_and_recalculates(self):
        record = activate_plan(self.service)
        record = self.service.act(SP1, record["id"], record["version"], 'log_service', {
            'voucher_no': 'V-010', 'started_at': STARTED, 'minutes': 90, 'provider': 'SP-wang',
        })
        self.assertEqual(record["payload"]["delivered_minutes"], 210)
        with self.assertRaises(ValidationError):
            self.service.act(CM, record["id"], record["version"], 'void_voucher', {'voucher_no': 'V-010'})
        record = self.service.act(CM, record["id"], record["version"], 'void_voucher', {
            'voucher_no': 'V-010', 'reason': '重复登记，家长申诉',
        })
        self.assertEqual(record["payload"]["delivered_minutes"], 120)
        self.assertEqual(record["payload"]["missing_minutes"], 480)
        vouchers = self.service.vouchers(CM, record["id"])
        voided = next(v for v in vouchers if v["voucher_no"] == 'V-010')
        self.assertEqual(voided["status"], 'void')
        self.assertEqual(voided["void_reason"], '重复登记，家长申诉')
        # 重复撤销被拒绝，汇总不变
        with self.assertRaises(ValidationError):
            self.service.act(CM, record["id"], record["version"], 'void_voucher', {
                'voucher_no': 'V-010', 'reason': '再撤一次',
            })
        fresh = self.service.get_record(CM, record["id"])
        self.assertEqual(fresh["payload"]["delivered_minutes"], 120)

    def test_void_unknown_voucher_is_not_found(self):
        record = activate_plan(self.service)
        with self.assertRaises(NotFound):
            self.service.act(CM, record["id"], record["version"], 'void_voucher', {
                'voucher_no': 'V-NOPE', 'reason': '错误凭证',
            })

    def test_review_blocked_when_summary_and_ledger_disagree(self):
        record = activate_plan(self.service)
        record = self.service.act(SP1, record["id"], record["version"], 'log_service', {
            'voucher_no': 'V-020', 'started_at': STARTED, 'minutes': 60, 'provider': 'SP-zhao',
        })
        # 人为制造汇总与台账不一致：直接改汇总分钟数
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT payload FROM records WHERE id=?", (record["id"],)).fetchone()
            payload = json.loads(row[0])
            payload["delivered_minutes"] = 200
            conn.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), record["id"]))
        with self.assertRaises(LedgerMismatch) as caught:
            self.service.act(ADMIN, record["id"], record["version"], 'review', {'progress_note': '复查'})
        self.assertEqual(caught.exception.difference, 20)
        fresh = self.service.get_record(CM, record["id"])
        self.assertEqual(fresh["state"], 'active')

    def test_close_blocked_when_summary_and_ledger_disagree(self):
        record = activate_plan(self.service)
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT payload FROM records WHERE id=?", (record["id"],)).fetchone()
            payload = json.loads(row[0])
            payload["delivered_minutes"] = 90
            conn.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), record["id"]))
        with self.assertRaises(LedgerMismatch) as caught:
            self.service.act(ADMIN, record["id"], record["version"], 'close', {'review_complete': True})
        self.assertEqual(caught.exception.difference, -30)
        self.assertEqual(self.service.get_record(CM, record["id"])["state"], 'active')

    def test_invalid_started_at_rejected(self):
        record = activate_plan(self.service)
        with self.assertRaises(ValidationError):
            self.service.act(SP1, record["id"], record["version"], 'log_service', {
                'voucher_no': 'V-BAD', 'started_at': '昨天下午', 'minutes': 30, 'provider': 'SP-x',
            })

    def test_session_minutes_alias_still_supported(self):
        record = activate_plan(self.service)
        record = self.service.act(SP1, record["id"], record["version"], 'log_service', {
            'voucher_no': 'V-ALIAS', 'started_at': STARTED, 'session_minutes': 15, 'provider': 'SP-y',
        })
        self.assertEqual(record["payload"]["delivered_minutes"], 135)


class LegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.temp.cleanup()

    def _seed_legacy_db(self):
        # 旧结构：只有 records/audit_events，没有凭证表，汇总只有 delivered_minutes 一个总数。
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE records (
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
                CREATE TABLE audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            payload = dict(CREATE_DATA)
            payload.update(missing_minutes=480, compliance_rate=20.0, review_overdue=False, plan_status='active')
            conn.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ('OLD-1', 'active', 3, json.dumps(payload, ensure_ascii=False), 'legacy', 'legacy', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00'),
            )
            conn.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (1, 'log_service', 'legacy', 3, '{}', '2026-09-01T00:00:00+00:00'),
            )

    def test_legacy_database_upgraded_with_migration_voucher(self):
        self._seed_legacy_db()
        service = build_service(self.path)  # 启动即升级
        record = service.get_record(CM, 1)
        vouchers = service.vouchers(CM, 1)
        self.assertEqual(len(vouchers), 1)
        self.assertEqual(vouchers[0]["voucher_no"], "MIG-OLD-1")
        self.assertEqual(vouchers[0]["minutes"], 120)
        self.assertEqual(vouchers[0]["voucher_type"], "migration")
        self.assertEqual(vouchers[0]["started_at"], "2026-09-01T00:00:00+00:00")
        self.assertEqual(record["payload"]["delivered_minutes"], 120)
        timeline = service.timeline(CM, 1)
        self.assertTrue(any(event["action"] == "ledger_migrated" for event in timeline))

    def test_migration_is_idempotent(self):
        self._seed_legacy_db()
        first = build_service(self.path)
        count = first.migrate_legacy_ledgers()
        self.assertEqual(count, 0)
        self.assertEqual(len(first.vouchers(CM, 1)), 1)

    def test_new_log_then_review_and_close_recomputes_from_ledger(self):
        service = build_service(self.path)
        record = activate_plan(service, 'IEP-29100')
        record = service.act(SP1, record["id"], record["version"], 'log_service', {
            'voucher_no': 'V-FINAL', 'started_at': STARTED, 'minutes': 480, 'provider': 'SP-fin',
        })
        self.assertEqual(record["payload"]["delivered_minutes"], 600)
        record = service.act(ADMIN, record["id"], record["version"], 'review', {'progress_note': '全部履约'})
        self.assertEqual(record["state"], 'under_review')
        record = service.act(ADMIN, record["id"], record["version"], 'close', {'review_complete': True})
        self.assertEqual(record["state"], 'closed')


if __name__ == '__main__':
    unittest.main()
