from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, PermissionDenied, ensure_role, parse_gates,
                     require_choice, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, VIEW_ROLES,
                    DEVIATION_TOLERANCE, compute_severity, deviation_ratio,
                    plan_order, priority_score, response_deadline_hours,
                    validate_transition, role_for_transition)
from .domain import DOWNSTREAM_STATES


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    # ---------- 录入解析 ----------
    def parse_hydrology(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        level = require_number(payload.get("level"), "库位(m)")
        flood_limit = require_number(payload.get("flood_limit"), "汛限(m)")
        inflow = require_number(payload.get("inflow"), "入库流量(m³/s)")
        rise = require_number(payload.get("rise", 0), "涨幅(m/h)", minimum=None)
        downstream = require_choice(payload.get("downstream"), "下游情况", DOWNSTREAM_STATES)
        construction_limit = payload.get("construction_limit", "")
        construction_limit = (
            require_text(construction_limit, "施工限制说明", 500)
            if construction_limit not in (None, "") else "")
        gates = parse_gates(payload.get("gates"))
        return {
            "level": level, "flood_limit": flood_limit, "inflow": inflow,
            "rise": rise, "downstream": downstream,
            "construction_limit": construction_limit, "gates": gates,
        }

    def preview(self, payload: Dict[str, Any], role: str) -> Dict[str, Any]:
        """试算：只计算不落库。"""
        ensure_role(role, VIEW_ROLES)
        data = self.parse_hydrology(payload)
        plan = plan_order(data["inflow"], data["rise"], data["level"],
                          data["flood_limit"], data["downstream"], data["gates"])
        plan["severity"] = compute_severity(
            data["level"], data["flood_limit"], data["rise"], data["downstream"])
        return {"input": data, "plan": self._with_meta(data, plan)}

    # ---------- 调度单 ----------
    def create_order(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "X-Actor(操作人)", 100)
        data = self.parse_hydrology(payload)
        plan = plan_order(data["inflow"], data["rise"], data["level"],
                          data["flood_limit"], data["downstream"], data["gates"])
        plan["severity"] = compute_severity(
            data["level"], data["flood_limit"], data["rise"], data["downstream"])

        record = {
            **data,
            "planned_flow": plan["planned_flow"],
            "gate_count": plan["gate_count"],
            "selected_gates": plan["selected_gates"],
            "chief_review_required": plan["chief_review_required"],
            "level_rising": plan["level_rising"],
            "uses_restricted_gates": plan["uses_restricted_gates"],
            "severe_cap_applied": plan["severe_cap_applied"],
            "capacity_shortfall": plan["capacity_shortfall"],
            "plan_notes": plan["notes"],
            "severity": plan["severity"],
        }
        order = self.repository.create_order(record, actor)
        self.repository.append_audit("create", ENTITY, order["id"], actor, {
            "order_no": order["order_no"],
            "planned_flow": plan["planned_flow"],
            "gate_count": plan["gate_count"],
            "selected_gates": plan["selected_gates"],
            "downstream": data["downstream"],
            "chief_review_required": plan["chief_review_required"],
            "uses_restricted_gates": plan["uses_restricted_gates"],
        })
        return self.enrich(order)

    def add_record(self, order_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "X-Actor(操作人)", 100)
        kind = require_choice(payload.get("kind", "note"), "记录类型", ("review", "note"))
        if kind == "review" and role != "chief_engineer":
            raise PermissionDenied("只有总工可以登记复核意见")
        detail = require_text(payload.get("detail"), "记录内容")
        record = self.repository.add_record(order_id, kind, detail, actor)
        self.repository.append_audit("record", ENTITY, order_id, actor, {
            "record_id": record["id"], "kind": kind})
        return record

    def close_record(self, record_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, {"chief_engineer", "dispatcher"})
        actor = require_text(actor, "X-Actor(操作人)", 100)
        record = self.repository.close_record(record_id)
        self.repository.append_audit("close_record", ENTITY, record["order_id"], actor, {
            "record_id": record_id})
        return record

    def authorize(self, order_id: int, expected_version: int, actor: str,
                  role: str) -> Dict[str, Any]:
        """总工复核授权：draft/recheck → authorized。授权前必须有总工复核记录。"""
        ensure_role(role, role_for_transition("authorized"))
        actor = require_text(actor, "X-Actor(操作人)", 100)
        order = self.repository.get_order(order_id)
        validate_transition(order["status"], "authorized")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ConflictError("expected_version必须是正整数")
        if self.repository.open_record_count(order_id, "review") == 0:
            raise ConflictError("授权前必须先登记总工复核意见")
        updated = self.repository.transition_order(
            order_id, "authorized", expected_version, actor)
        # 已据以授权的复核意见闭环
        for rec in self.repository.list_records(order_id):
            if rec["kind"] == "review" and rec["status"] == "open":
                self.repository.close_record(rec["id"])
        self.repository.append_audit("authorize", ENTITY, order_id, actor, {
            "from": order["status"], "to": "authorized",
            "order_no": order["order_no"]})
        return self.enrich(updated)

    def execute(self, order_id: int, payload: Dict[str, Any], actor: str,
                role: str) -> Dict[str, Any]:
        """调度执行员回填实际流量：authorized → executed / recheck。"""
        ensure_role(role, role_for_transition("executed"))
        actor = require_text(actor, "X-Actor(操作人)", 100)
        order = self.repository.get_order(order_id)
        validate_transition(order["status"], "executed")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ConflictError("expected_version必须是正整数")
        actual_flow = require_number(payload.get("actual_flow"), "实际下泄流量(m³/s)")
        ratio = deviation_ratio(order["planned_flow"], actual_flow)
        within = ratio <= DEVIATION_TOLERANCE
        deviation_reason = None
        target = "executed"
        if not within:
            target = "recheck"
            deviation_reason = require_text(
                payload.get("deviation_reason"),
                f"实际偏差{ratio:.0%}超过一成，必须说明偏差原因")
        extra = {"actual_flow": actual_flow, "deviation_ratio": round(ratio, 4),
                 "deviation_reason": deviation_reason}
        updated = self.repository.transition_order(
            order_id, target, expected_version, actor, extra)
        self.repository.append_audit("execute", ENTITY, order_id, actor, {
            "planned_flow": order["planned_flow"], "actual_flow": actual_flow,
            "deviation_ratio": round(ratio, 4), "to": target,
            "deviation_reason": deviation_reason})
        return self.enrich(updated)

    def close_order(self, order_id: int, expected_version: int, actor: str,
                    role: str) -> Dict[str, Any]:
        """总工闭环：executed → closed，要求无未关闭记录。"""
        ensure_role(role, role_for_transition("closed"))
        actor = require_text(actor, "X-Actor(操作人)", 100)
        order = self.repository.get_order(order_id)
        validate_transition(order["status"], "closed")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ConflictError("expected_version必须是正整数")
        if self.repository.open_record_count(order_id) > 0:
            raise ConflictError("仍有未关闭事项，不能闭环")
        updated = self.repository.transition_order(
            order_id, "closed", expected_version, actor)
        self.repository.append_audit("close", ENTITY, order_id, actor, {
            "order_no": order["order_no"]})
        return self.enrich(updated)

    # ---------- 查询 ----------
    def get_order(self, order_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self.enrich(self.repository.get_order(order_id))

    def list_orders(self, role: str, status: Optional[str] = None) -> list:
        ensure_role(role, VIEW_ROLES)
        return [self.enrich(o) for o in self.repository.list_orders(status)]

    def list_records(self, order_id: int, role: str) -> list:
        ensure_role(role, VIEW_ROLES)
        return self.repository.list_records(order_id)

    def audit(self, role: str, order_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(order_id)

    def verify_audit(self, role: str) -> bool:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.verify_audit_chain()

    # ---------- 组装 ----------
    @staticmethod
    def _with_meta(data: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
        plan["priority"] = priority_score(
            plan.get("severity", "routine"), data["level"],
            data["flood_limit"], data["rise"])
        plan["deadline_hours"] = response_deadline_hours(plan.get("severity", "routine"))
        return plan

    @staticmethod
    def enrich(order: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(order)
        result["priority"] = priority_score(
            order["severity"], order["level"], order["flood_limit"], order["rise"])
        result["deadline_hours"] = response_deadline_hours(order["severity"])
        result["within_tolerance"] = (
            result["deviation_ratio"] is None
            or result["deviation_ratio"] <= DEVIATION_TOLERANCE)
        return result
