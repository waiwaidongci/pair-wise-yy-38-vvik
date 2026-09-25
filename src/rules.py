from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (DOWNSTREAM_STATES, GATE_STATES, ORDER_STATES, SEVERITIES,
                     ConflictError, ValidationError)

TITLE = "水库泄洪调度单"
ENTITY = "调度单"
ID_PREFIX = "XF"

# 业务硬规则
SEVERE_DISCHARGE_RATIO = 0.5        # 下游严重告警：下泄量不得超过入库流量一半
DEVIATION_TOLERANCE = 0.10          # 实际偏差超过一成退回待复核
DRAWDOWN_PER_METRE = 30.0           # 库位每超汛限1米，目标下泄在入库基础上增加的流量 m3/s
PRERELEASE_PER_METRE = 30.0         # 每小时每上涨1米预泄流量 m3/s
MIN_POSITIVE_FLOW = 1.0

# 状态机：draft 待复核 → authorized 已授权 → executed 已执行 → closed 已闭环；
# 执行偏差超过一成进入 recheck 退回复核，总工补充复核记录后重新授权
TRANSITIONS = {
    "draft": ["authorized"],
    "authorized": ["executed"],
    "executed": ["closed", "recheck"],
    "recheck": ["authorized"],
    "closed": [],
}
# 目标状态需要的角色
TRANSITION_ROLES = {
    "authorized": ["chief_engineer"],
    "executed": ["dispatcher"],
    "closed": ["chief_engineer"],
    "recheck": ["dispatcher"],
}
CREATE_ROLES = {"duty_officer"}
RECORD_ROLES = {"duty_officer", "dispatcher", "chief_engineer"}
AUDIT_ROLES = {"chief_engineer", "viewer"}
VIEW_ROLES = {"duty_officer", "chief_engineer", "dispatcher", "viewer"}
PREVIEW_ROLES = VIEW_ROLES
TERMINAL_STATES = {"closed"}

SEVERITY_WEIGHT = {"routine": 1.0, "attention": 3.0, "urgent": 6.0, "emergency": 9.0}
DEADLINE_HOURS = {"routine": 24, "attention": 12, "urgent": 4, "emergency": 2}


def plan_order(inflow: float, rise: float, level: float, flood_limit: float,
               downstream: str, gates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """依据水情计算下泄流量与选用闸门。纯函数，便于复核。"""
    if downstream not in DOWNSTREAM_STATES:
        raise ValidationError("下游情况取值非法")

    # 第一步：按水情计算目标下泄量
    # 库位超汛限：需要降低库位；库位上涨：预泄腾容；否则维持进出平衡。
    above_limit = max(0.0, level - flood_limit)
    raw_need = inflow
    need_notes = ["库位未超汛限且未上涨，按进出平衡下泄"]
    if above_limit > 0:
        raw_need += DRAWDOWN_PER_METRE * above_limit
        need_notes = [f"库位超汛限{above_limit:.2f}米，按每米{DRAWDOWN_PER_METRE:.0f}m³/s加大下泄"]
    if rise > 0:
        raw_need += PRERELEASE_PER_METRE * rise
        need_notes.append(f"库位上涨{rise:.2f}米/小时，按每米{PRERELEASE_PER_METRE:.0f}m³/s预泄腾容")

    # 第二步：下游严重告警硬约束
    severe_applied = False
    severe_cap = inflow * SEVERE_DISCHARGE_RATIO
    if downstream == "severe":
        severe_applied = raw_need > severe_cap
        if severe_applied:
            raw_need = min(raw_need, severe_cap)

    if raw_need < MIN_POSITIVE_FLOW:
        planned_flow = 0
    else:
        planned_flow = int(round(raw_need))

    # 第三步：选择闸门（检修闸门不可用；受限闸门仅在可用闸门不足时补充）
    usable = [g for g in gates if g["state"] != "maintenance"]
    available = [g for g in gates if g["state"] == "available"]
    restricted = [g for g in gates if g["state"] == "restricted"]
    maintenance = [g for g in gates if g["state"] == "maintenance"]

    chosen: List[Dict[str, Any]] = []
    remaining = float(planned_flow)
    for gate in sorted(available, key=lambda g: g["capacity"], reverse=True):
        if remaining <= 0:
            break
        chosen.append(gate)
        remaining -= gate["capacity"]

    restricted_needed = remaining > 0
    if restricted_needed:
        for gate in sorted(restricted, key=lambda g: g["capacity"], reverse=True):
            chosen.append(gate)
            remaining -= gate["capacity"]

    total_capacity = sum(g["capacity"] for g in usable)
    capacity_shortfall = total_capacity < planned_flow
    uses_restricted = any(g["state"] == "restricted" for g in chosen)

    # 第四步：是否需要总工复核授权（库位仍上涨，或需要启用受限闸门）
    level_rising = rise > 0
    chief_review_required = level_rising or uses_restricted

    notes = list(need_notes)
    if downstream == "severe":
        notes.append(
            f"下游严重告警，下泄量硬上限为入库流量的{SEVERE_DISCHARGE_RATIO:.0%}"
            f"（{severe_cap:.0f}m³/s）" + ("，已触发限泄" if severe_applied else "，当前方案未超限")
        )
    if restricted_needed:
        notes.append("可用闸门能力不足，方案补充启用受限闸门，须总工复核授权")
    if maintenance:
        notes.append("检修闸门不参与选择：" + "、".join(g["name"] for g in maintenance))
    if capacity_shortfall:
        notes.append(f"全部非检修闸门能力仅{total_capacity:.0f}m³/s，无法满足目标下泄，须总工处置")
    if planned_flow == 0:
        notes.append("目标下泄量不足1m³/s，本次不开闸泄洪")

    return {
        "planned_flow": planned_flow,
        "gate_count": len(chosen) if planned_flow > 0 else 0,
        "selected_gates": [g["name"] for g in chosen] if planned_flow > 0 else [],
        "selected_gate_details": chosen if planned_flow > 0 else [],
        "chief_review_required": chief_review_required,
        "level_rising": level_rising,
        "uses_restricted_gates": uses_restricted,
        "restricted_needed": restricted_needed,
        "severe_cap_applied": severe_applied,
        "capacity_shortfall": capacity_shortfall,
        "available_capacity": round(total_capacity, 2),
        "notes": notes,
    }


def compute_severity(level: float, flood_limit: float, rise: float,
                     downstream: str) -> str:
    if downstream == "severe" or level - flood_limit >= 1.0:
        return "emergency"
    if downstream == "warning" or rise > 0.5 or level > flood_limit:
        return "urgent"
    if rise > 0 or level >= flood_limit - 0.5:
        return "attention"
    return "routine"


def priority_score(severity: str, level: float = 0.0, flood_limit: float = 1.0,
                   rise: float = 0.0) -> int:
    if severity not in SEVERITY_WEIGHT:
        raise ValidationError("unknown severity")
    ratio = (level / flood_limit) if flood_limit > 0 else 1.0
    return max(0, min(10, int(round(
        SEVERITY_WEIGHT[severity] + min(3.0, max(0.0, ratio - 1.0) * 6.0)
        + min(2.0, max(0.0, rise) * 2.0)))))


def response_deadline_hours(severity: str) -> int:
    if severity not in DEADLINE_HOURS:
        raise ValidationError("unknown severity")
    return DEADLINE_HOURS[severity]


def deviation_ratio(planned_flow: float, actual_flow: float) -> float:
    """|实际-计划|/计划；计划为0时按绝对量是否大于容限判断（交给调用方按>10%处理）。"""
    if planned_flow <= 0:
        return 1.0 if actual_flow and abs(actual_flow) > 0 else 0.0
    return abs(actual_flow - planned_flow) / planned_flow


def deviation_within_tolerance(planned_flow: float, actual_flow: float) -> bool:
    return deviation_ratio(planned_flow, actual_flow) <= DEVIATION_TOLERANCE


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, [])


def validate_transition(current: str, target: str):
    if current not in ORDER_STATES or target not in ORDER_STATES:
        raise ValidationError("未知状态")
    if not can_transition(current, target):
        raise ConflictError(f"不能从{current}转换到{target}")


def role_for_transition(target: str):
    return set(TRANSITION_ROLES.get(target, []))
