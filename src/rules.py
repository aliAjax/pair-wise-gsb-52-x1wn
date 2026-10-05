"""特殊教育支持计划合规领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list, timestamp


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {'consent': {'parent_rep'}, 'activate': {'case_manager'}, 'log_service': {'case_manager', 'specialist'}, 'void_voucher': {'case_manager', 'specialist'}, 'review': {'administrator'}, 'amend': {'case_manager'}, 'close': {'administrator'}}
TRANSITIONS = {'consent': {'draft': 'consented'}, 'activate': {'consented': 'active'}, 'log_service': {'active': 'active'}, 'void_voucher': {'active': 'active', 'under_review': 'under_review'}, 'review': {'active': 'under_review'}, 'amend': {'under_review': 'active'}, 'close': {'active': 'closed', 'under_review': 'closed'}}
GUARDED_ACTIONS = {'review', 'close'}


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
        return role == "admin" or role in ACTION_ROLES.get(action, set())

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

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def require_summary_consistent(self, record: Dict[str, Any], ledger_total: int) -> None:
        delivered = int(record["payload"].get("delivered_minutes", 0))
        if delivered != int(ledger_total):
            raise Conflict(
                "计划汇总与服务台账不一致：汇总%s分钟，有效凭证合计%s分钟，差额%s分钟"
                % (delivered, int(ledger_total), delivered - int(ledger_total))
            )

    @staticmethod
    def _recount(payload: Dict[str, Any], delivered: int, changes: Dict[str, Any]) -> None:
        changes["delivered_minutes"] = delivered
        changes["missing_minutes"] = int(payload["service_minutes"]) - delivered
        changes["compliance_rate"] = round(delivered / int(payload["service_minutes"]) * 100, 2)

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], ledger_total: Optional[int] = None, voucher: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
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
            session = integer(data, "session_minutes", 1)
            text(data, "voucher_no")
            timestamp(data, "started_at")
            provider = text(data, "provider")
            base = int(ledger_total) if ledger_total is not None else int(p["delivered_minutes"])
            if session + base > int(p["service_minutes"]):
                raise ValidationError("记录服务超过计划分钟数")
            self._recount(p, base + session, changes)
            changes["last_provider"] = provider
            summary = "服务记录已登记"
        elif action == "void_voucher":
            text(data, "voucher_no")
            text(data, "void_reason")
            base = int(ledger_total) if ledger_total is not None else int(p["delivered_minutes"])
            voided = int(voucher["minutes"]) if voucher else 0
            self._recount(p, max(0, base - voided), changes)
            summary = "服务凭证已撤销并反向重算"
        elif action == "review":
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
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
