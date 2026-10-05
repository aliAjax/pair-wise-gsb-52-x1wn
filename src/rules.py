"""特殊教育支持计划合规领域规则与状态转换。"""
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import (
    Conflict,
    LedgerMismatch,
    NotFound,
    ValidationError,
    boolean,
    integer,
    text,
    text_list,
)


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {'consent': {'parent_rep'}, 'activate': {'case_manager'}, 'log_service': {'case_manager', 'specialist'}, 'void_voucher': {'case_manager', 'specialist'}, 'review': {'administrator'}, 'amend': {'case_manager'}, 'close': {'administrator'}}
TRANSITIONS = {'consent': {'draft': 'consented'}, 'activate': {'consented': 'active'}, 'log_service': {'active': 'active'}, 'void_voucher': {'active': 'active'}, 'review': {'active': 'under_review'}, 'amend': {'under_review': 'active'}, 'close': {'active': 'closed', 'under_review': 'closed'}}
VOUCHER_STATUSES = {'valid', 'void'}
VOUCHER_TYPES = {'service', 'migration'}
MIGRATION_PROVIDER = "历史数据迁移"


def started_at(data: Dict[str, Any], key: str = "started_at") -> str:
    """服务开始时间，接受ISO 8601字符串。"""
    value = text(data, key)
    normalized = value.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError("%s必须是ISO 8601日期时间" % key) from exc
    return value


def valid_voucher_total(vouchers: Optional[Iterable[Dict[str, Any]]]) -> int:
    """按有效凭证重新加总分钟数，撤销凭证不计入。"""
    return sum(int(item["minutes"]) for item in (vouchers or []) if item["status"] == "valid")


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "student_id")
        text(p, "disability")
        integer(p, "service_minutes", 1)
        integer(p, "delivered_minutes", 0)
        integer(p, "review_due_days", 0)
        integer(p, "goals_count", 1)
        boolean(p, "consent")
        if p["delivered_minutes"] > p["service_minutes"]:
            raise ValidationError("已提供服务不能超过计划服务")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["missing_minutes"] = max(0, int(p["service_minutes"]) - int(p["delivered_minutes"]))
        p["compliance_rate"] = round(int(p["delivered_minutes"]) / int(p["service_minutes"]) * 100, 2)
        p["review_overdue"] = int(p["review_due_days"]) <= 0
        p["plan_status"] = "draft"
        return p

    def migration_voucher(self, delivered_minutes: int, reference: str, started_at_value: str, actor_id: str) -> Dict[str, Any]:
        """旧数据升级：按已有分钟数生成一条迁移凭证。"""
        return {
            "voucher_no": "MIG-%s" % reference,
            "minutes": int(delivered_minutes),
            "started_at": started_at_value,
            "provider": MIGRATION_PROVIDER,
            "voucher_type": "migration",
            "status": "valid",
            "created_by": actor_id,
        }

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def require_ledger_match(self, payload: Dict[str, Any], vouchers: Iterable[Dict[str, Any]], action: str) -> int:
        """复查或结案前，计划汇总必须与有效凭证台账一致，差额随异常返回。"""
        ledger_total = valid_voucher_total(vouchers)
        summary_total = int(payload.get("delivered_minutes", 0))
        if ledger_total != summary_total:
            difference = summary_total - ledger_total
            raise LedgerMismatch(
                "计划汇总与凭证台账相差%s分钟（汇总%s，台账%s），禁止%s" % (difference, summary_total, ledger_total, action),
                difference=difference,
            )
        return ledger_total

    @staticmethod
    def _recalculate(payload: Dict[str, Any], delivered: int) -> Dict[str, Any]:
        changes: Dict[str, Any] = {}
        changes["delivered_minutes"] = int(delivered)
        changes["missing_minutes"] = int(payload["service_minutes"]) - int(delivered)
        changes["compliance_rate"] = round(int(delivered) / int(payload["service_minutes"]) * 100, 2)
        return changes

    def apply_action(
        self,
        record: Dict[str, Any],
        action: str,
        data: Dict[str, Any],
        vouchers: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, Dict[str, Any], str, Optional[Dict[str, Any]]]:
        """返回新状态、新payload、摘要和台账变更。

        台账变更形如{"op":"insert"|"void", ...}，由仓储层在同一事务内落库；
        汇总分钟数以事务内对有效凭证重新SUM的结果为准。
        """
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        ledger: Optional[Dict[str, Any]] = None
        if action == "consent":
            if not boolean(data, "guardian_confirmed"):
                raise ValidationError("监护人尚未确认")
            if not text(data, "consent_scope"):
                raise ValidationError("同意范围不能为空")
            changes["consent"] = True
            changes["consent_scope"] = data["consent_scope"]
            summary = "监护人同意已记录"
        elif action == "activate":
            if not p.get("consent"):
                raise ValidationError("缺少有效同意")
            if int(p["goals_count"]) <= 0:
                raise ValidationError("计划必须包含目标")
            changes["plan_status"] = "active"
            summary = "支持计划生效"
        elif action == "log_service":
            voucher_no = text(data, "voucher_no")
            if "minutes" in data:
                minutes = integer(data, "minutes", 1)
            else:
                minutes = integer(data, "session_minutes", 1)
            started = started_at(data, "started_at")
            provider = text(data, "provider")
            if minutes + int(p["delivered_minutes"]) > int(p["service_minutes"]):
                raise ValidationError("凭证服务时长超过计划分钟数")
            changes["last_provider"] = provider
            ledger = {"op": "insert", "voucher_no": voucher_no, "minutes": minutes, "started_at": started, "provider": provider, "voucher_type": "service"}
            summary = "服务凭证%s已登记" % voucher_no
        elif action == "void_voucher":
            voucher_no = text(data, "voucher_no")
            reason = text(data, "reason")
            target = next((item for item in (vouchers or []) if item["voucher_no"] == voucher_no), None)
            if target is None:
                raise NotFound("凭证%s不存在" % voucher_no)
            if target["status"] == "void":
                raise ValidationError("凭证%s已撤销，不能重复撤销" % voucher_no)
            ledger = {"op": "void", "voucher_no": voucher_no, "reason": reason, "minutes": int(target["minutes"])}
            summary = "服务凭证%s已撤销并反向重算" % voucher_no
        elif action == "review":
            self.require_ledger_match(p, vouchers or [], "复查")
            changes["progress_note"] = text(data, "progress_note")
            changes["review_overdue"] = False
            summary = "进入计划复查"
        elif action == "amend":
            changes["amendment_reason"] = text(data, "amendment_reason")
            changes["updated_goals"] = text_list(data, "updated_goals", 1)
            changes["goals_count"] = len(changes["updated_goals"])
            changes["plan_status"] = "active"
            summary = "计划已修订"
        elif action == "close":
            self.require_ledger_match(p, vouchers or [], "结案")
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action), ledger
