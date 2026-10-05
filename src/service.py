"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, NotFound, PermissionDenied, integer, text, timestamp
from .repository import Repository
from .rules import GUARDED_ACTIONS, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        data = dict(data or {})
        ledger_total: Optional[int] = None
        voucher: Optional[Dict[str, Any]] = None
        voucher_op: Optional[Dict[str, Any]] = None
        if action == "log_service":
            voucher_no = data.get("voucher_no")
            if isinstance(voucher_no, str) and voucher_no.strip():
                existing = self.repository.find_valid_voucher(record_id, voucher_no.strip())
                if existing is not None:
                    self.audit.note(record_id, actor.user_id, "duplicate_voucher", {"voucher_no": voucher_no.strip(), "summary": "同一凭证号重复提交，已忽略"})
                    return self.repository.get(record_id)
            ledger_total = self.repository.voucher_sum(record_id)
        elif action == "void_voucher":
            voucher_no = text({"voucher_no": data.get("voucher_no", "")}, "voucher_no")
            voucher = self.repository.find_valid_voucher(record_id, voucher_no)
            if voucher is None:
                raise NotFound("有效凭证不存在或已撤销")
            ledger_total = self.repository.voucher_sum(record_id)
        elif action in GUARDED_ACTIONS:
            self.rules.require_summary_consistent(record, self.repository.voucher_sum(record_id))
        new_state, new_payload, summary = self.rules.apply_action(record, action, data, ledger_total=ledger_total, voucher=voucher)
        if action == "log_service":
            voucher_op = {
                "kind": "log",
                "voucher_no": text(data, "voucher_no"),
                "started_at": timestamp(data, "started_at"),
                "minutes": integer(data, "session_minutes", 1),
                "provider": text(data, "provider"),
            }
        elif action == "void_voucher":
            voucher_op = {"kind": "void", "voucher_no": text(data, "voucher_no"), "reason": text(data, "void_reason")}
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
            voucher=voucher_op,
        )

    def vouchers(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_vouchers(record_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
