"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, migrate: bool = True) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        if migrate:
            self.migrate_legacy_ledgers()

    def migrate_legacy_ledgers(self) -> int:
        """旧数据升级：按已有分钟数为每个计划补一条迁移凭证。"""
        return self.repository.migrate_legacy_ledgers(
            lambda reference, delivered, created_at: self.rules.migration_voucher(
                delivered, reference, created_at, "system-migration"
            )
        )

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
        voucher = None
        if int(prepared["delivered_minutes"]) > 0:
            voucher = self.rules.migration_voucher(
                prepared["delivered_minutes"], reference, Repository.clock(), actor.user_id
            )
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, voucher=voucher)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def vouchers(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_vouchers(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        # 撤销凭证、复查、结案都需要台账；服务登记的上限校验只依赖汇总，凭证由事务内写入。
        vouchers = None
        if action in {"void_voucher", "review", "close"}:
            vouchers = self.repository.list_vouchers(record_id)
        new_state, new_payload, summary, ledger = self.rules.apply_action(record, action, data or {}, vouchers)
        details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        if ledger is not None:
            details["ledger"] = ledger
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
            ledger=ledger,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
