from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DEVIATION_KIND,
                    DEVIATION_TOLERANCE, ENTITY, FEEDBACK_KIND, FEEDBACK_ROLES,
                    RECORD_ROLES, RESERVED_RECORD_KINDS, REVIEW_KIND,
                    REVIEW_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    compute_discharge_plan, derive_severity,
                    discharge_deviation, escalation_required,
                    normalize_downstream_alarm, parse_gates, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)

FLOOD_FIELDS = ("reservoir_level", "flood_limit", "inflow", "rise_rate",
                "downstream_alarm", "gates")


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        inputs = None
        plan = None
        if any(field in payload for field in FLOOD_FIELDS):
            inputs, plan = self._build_plan(payload)
            severity = derive_severity(severity, plan, inputs["downstream_alarm"])
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, inputs, plan)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
            "plan": plan,
        })
        return self.enrich(item)

    @staticmethod
    def _build_plan(payload: Dict[str, Any]):
        inputs = {
            "reservoir_level": require_number(payload.get("reservoir_level"), "reservoir_level"),
            "flood_limit": require_number(payload.get("flood_limit"), "flood_limit"),
            "inflow": require_number(payload.get("inflow"), "inflow"),
            "rise_rate": require_number(payload.get("rise_rate", 0), "rise_rate", -50.0),
            "downstream_alarm": normalize_downstream_alarm(payload.get("downstream_alarm", "normal")),
            "gates": parse_gates(payload.get("gates")),
        }
        plan = compute_discharge_plan(
            inputs["reservoir_level"], inputs["flood_limit"], inputs["inflow"],
            inputs["rise_rate"], inputs["downstream_alarm"], inputs["gates"])
        return inputs, plan

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        if kind in RESERVED_RECORD_KINDS:
            raise ValidationError("该记录类型由系统生成，不能手工登记")
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        if kind == REVIEW_KIND:
            ensure_role(role, REVIEW_ROLES)
            status = "closed"
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        if item["status"] == "executed" and target == "checked":
            raise ConflictError("执行退回须通过反馈接口登记偏差原因")
        if target == "authorized":
            self._require_chief_review(item)
        if target == "closed" and item.get("plan") and \
                self.repository.latest_record_id(item_id, FEEDBACK_KIND) == 0:
            raise ConflictError("执行后须先登记现场反馈才能闭环")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def _require_chief_review(self, item: Dict[str, Any]) -> None:
        plan = item.get("plan")
        latest_deviation = self.repository.latest_record_id(item["id"], DEVIATION_KIND)
        if not (plan and plan.get("review_required")) and latest_deviation == 0:
            return
        if self.repository.latest_record_id(item["id"], REVIEW_KIND) <= latest_deviation:
            raise ConflictError("库位仍上涨或需启用受限闸门，须总工复核后才能授权")

    def feedback(self, item_id: int, actual: Any, reason: Optional[str],
                 actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, FEEDBACK_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "executed":
            raise ConflictError("只有已执行的调度单才能登记反馈")
        plan = item.get("plan")
        if not plan:
            raise ConflictError("该调度单没有泄洪计划")
        actual = require_number(actual, "actual_discharge")
        planned = float(plan.get("planned_discharge", 0.0))
        deviation = discharge_deviation(planned, actual)
        percent = f"{deviation * 100:.1f}%"
        self.repository.set_feedback(item_id, actual)
        self.repository.add_record(
            item_id, FEEDBACK_KIND,
            f"实际下泄{actual}m³/s，计划{planned}m³/s，偏差{percent}",
            "closed", None, actor)
        self.repository.append_audit("feedback", ENTITY, item_id, actor, {
            "planned": planned, "actual": actual, "deviation": round(deviation, 4),
        })
        if deviation > DEVIATION_TOLERANCE:
            reason = require_text(reason, "reason")
            self.repository.add_record(
                item_id, DEVIATION_KIND,
                f"偏差{percent}超过一成：{reason}", "open", None, actor)
            current = self.repository.get_item(item_id)
            updated = self.repository.transition_item(item_id, "checked",
                                                      current["version"], actor)
            self.repository.append_audit("rework", ENTITY, item_id, actor, {
                "reason": reason, "deviation": round(deviation, 4), "to": "checked",
            })
            return self.enrich(updated)
        self.repository.close_records(item_id, DEVIATION_KIND)
        return self.enrich(self.repository.get_item(item_id))

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        plan = result.get("plan")
        actual = result.get("actual_discharge")
        if plan and actual is not None:
            result["deviation"] = discharge_deviation(
                float(plan.get("planned_discharge", 0.0)), actual)
            result["deviation_exceeded"] = result["deviation"] > DEVIATION_TOLERANCE
        return result
