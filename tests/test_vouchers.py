import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, ValidationError
from src.repository import Repository


CREATE_DATA = {'student_id': 'S-100', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}
VOUCHER = {'session_minutes': 60, 'provider': 'SP-3', 'voucher_no': 'V-1001', 'started_at': '2026-09-30T09:00:00Z'}


class VoucherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _active_record(self):
        record = self.service.create(Actor("creator", "case_manager"), "IEP-28001", CREATE_DATA)
        record = self.service.act(Actor("parent", "parent_rep"), record["id"], record["version"], "consent", {'guardian_confirmed': True, 'consent_scope': '个别化服务'})
        record = self.service.act(Actor("cm", "case_manager"), record["id"], record["version"], "activate", {})
        return record

    def test_log_service_leaves_voucher_and_recounts(self):
        record = self._active_record()
        record = self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "log_service", VOUCHER)
        self.assertEqual(record["payload"]["delivered_minutes"], 180)
        vouchers = self.service.vouchers(Actor("cm", "case_manager"), record["id"])
        self.assertEqual(len(vouchers), 2)
        migration, entry = vouchers
        self.assertEqual(migration["source"], "migration")
        self.assertEqual(migration["minutes"], 120)
        self.assertEqual(entry["voucher_no"], "V-1001")
        self.assertEqual(entry["started_at"], "2026-09-30T09:00:00Z")
        self.assertEqual(entry["minutes"], 60)
        self.assertEqual(entry["provider"], "SP-3")
        self.assertEqual(entry["status"], "valid")

    def test_duplicate_voucher_counts_once(self):
        record = self._active_record()
        record = self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "log_service", VOUCHER)
        again = self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "log_service", VOUCHER)
        self.assertEqual(again["version"], record["version"])
        self.assertEqual(again["payload"]["delivered_minutes"], 180)
        vouchers = self.service.vouchers(Actor("cm", "case_manager"), record["id"])
        self.assertEqual(len([v for v in vouchers if v["voucher_no"] == "V-1001"]), 1)
        timeline = self.service.timeline(Actor("cm", "case_manager"), record["id"])
        self.assertEqual(timeline[-1]["action"], "duplicate_voucher")

    def test_concurrent_backfill_loser_gets_conflict(self):
        record = self._active_record()
        version = record["version"]
        first = dict(VOUCHER, voucher_no="V-2001")
        second = dict(VOUCHER, voucher_no="V-2002", provider="SP-4")
        record = self.service.act(Actor("sp1", "specialist"), record["id"], version, "log_service", first)
        with self.assertRaises(Conflict):
            self.service.act(Actor("sp2", "specialist"), record["id"], version, "log_service", second)
        record = self.service.get_record(Actor("cm", "case_manager"), record["id"])
        self.assertEqual(record["payload"]["delivered_minutes"], 180)
        vouchers = self.service.vouchers(Actor("cm", "case_manager"), record["id"])
        self.assertEqual([v["voucher_no"] for v in vouchers], ["MIG-%d" % record["id"], "V-2001"])

    def test_void_voucher_requires_reason_and_recounts(self):
        record = self._active_record()
        record = self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "log_service", VOUCHER)
        with self.assertRaises(ValidationError):
            self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "void_voucher", {'voucher_no': 'V-1001'})
        record = self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "void_voucher", {'voucher_no': 'V-1001', 'void_reason': '补录错误，实际未服务'})
        self.assertEqual(record["payload"]["delivered_minutes"], 120)
        self.assertEqual(record["payload"]["missing_minutes"], 480)
        vouchers = self.service.vouchers(Actor("cm", "case_manager"), record["id"])
        voided = [v for v in vouchers if v["voucher_no"] == "V-1001"]
        self.assertEqual(len(voided), 1)
        self.assertEqual(voided[0]["status"], "voided")
        self.assertEqual(voided[0]["void_reason"], '补录错误，实际未服务')
        with self.assertRaises(NotFound):
            self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "void_voucher", {'voucher_no': 'V-1001', 'void_reason': '再次撤销'})
        record = self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "log_service", dict(VOUCHER, session_minutes=45))
        self.assertEqual(record["payload"]["delivered_minutes"], 165)

    def test_review_and_close_blocked_on_ledger_mismatch(self):
        record = self._active_record()
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute("SELECT payload FROM records WHERE id=?", (record["id"],)).fetchone()
            payload = json.loads(row[0])
            payload["delivered_minutes"] = 999
            connection.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False, sort_keys=True), record["id"]))
        with self.assertRaises(Conflict) as ctx:
            self.service.act(Actor("admin", "administrator"), record["id"], record["version"], "review", {'progress_note': '阶段复盘'})
        self.assertIn("差额879分钟", str(ctx.exception))
        with self.assertRaises(Conflict) as ctx:
            self.service.act(Actor("admin", "administrator"), record["id"], record["version"], "close", {'review_complete': True})
        self.assertIn("差额879分钟", str(ctx.exception))
        record = self.service.get_record(Actor("cm", "case_manager"), record["id"])
        self.assertEqual(record["state"], "active")

    def test_migration_generates_voucher_for_legacy_data(self):
        now = "2026-09-01T00:00:00+00:00"
        legacy_payload = {'student_id': 'S-legacy', 'disability': 'visual', 'service_minutes': 300, 'delivered_minutes': 90, 'missing_minutes': 210, 'compliance_rate': 30.0, 'review_due_days': 10, 'goals_count': 2, 'consent': True, 'review_overdue': False, 'plan_status': 'active'}
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("IEP-LEGACY", "active", 1, json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True), "legacy", "legacy", now, now),
            )
        self.service = build_service(self.db_path)
        record = self.service.get_record(Actor("cm", "case_manager"), 1)
        self.assertEqual(record["payload"]["delivered_minutes"], 90)
        vouchers = self.service.vouchers(Actor("cm", "case_manager"), record["id"])
        self.assertEqual(len(vouchers), 1)
        self.assertEqual(vouchers[0]["voucher_no"], "MIG-%d" % record["id"])
        self.assertEqual(vouchers[0]["source"], "migration")
        self.assertEqual(vouchers[0]["minutes"], 90)
        self.assertEqual(vouchers[0]["status"], "valid")
        self.service = build_service(self.db_path)
        vouchers = self.service.vouchers(Actor("cm", "case_manager"), record["id"])
        self.assertEqual(len(vouchers), 1)

    def test_over_plan_check_uses_ledger_total(self):
        record = self._active_record()
        with self.assertRaises(ValidationError):
            self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "log_service", dict(VOUCHER, session_minutes=481))
        record = self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "log_service", dict(VOUCHER, session_minutes=480))
        self.assertEqual(record["payload"]["delivered_minutes"], 600)

    def test_log_service_requires_voucher_fields(self):
        record = self._active_record()
        with self.assertRaises(ValidationError):
            self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "log_service", {'session_minutes': 60, 'provider': 'SP-3', 'started_at': '2026-09-30T09:00:00Z'})
        with self.assertRaises(ValidationError):
            self.service.act(Actor("sp", "specialist"), record["id"], record["version"], "log_service", dict(VOUCHER, started_at="not-a-time"))
