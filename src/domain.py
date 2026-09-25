from __future__ import annotations

from typing import Any, Dict, List


class ErrorKind:
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"


class DomainError(Exception):
    kind = ErrorKind.VALIDATION

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    kind = ErrorKind.VALIDATION


class NotFoundError(DomainError):
    kind = ErrorKind.NOT_FOUND


class PermissionDenied(DomainError):
    kind = ErrorKind.FORBIDDEN


class ConflictError(DomainError):
    kind = ErrorKind.CONFLICT


# 角色
ROLES = ["duty_officer", "chief_engineer", "dispatcher", "viewer"]
ROLE_LABELS = {
    "duty_officer": "值班员",
    "chief_engineer": "总工",
    "dispatcher": "调度执行员",
    "viewer": "观摩",
}

# 下游情况
DOWNSTREAM_STATES = ["normal", "warning", "severe"]
DOWNSTREAM_LABELS = {
    "normal": "正常",
    "warning": "警戒",
    "severe": "严重告警",
}

# 闸门状态：可用 / 受限（施工限制，需总工授权）/ 检修（不可用）
GATE_STATES = ["available", "restricted", "maintenance"]
GATE_STATE_LABELS = {
    "available": "可用",
    "restricted": "受限(施工限制)",
    "maintenance": "检修",
}

# 调度单状态
ORDER_STATES = ["draft", "authorized", "executed", "recheck", "closed"]
ORDER_STATE_LABELS = {
    "draft": "待复核",
    "authorized": "已授权",
    "executed": "已执行",
    "recheck": "退回复核",
    "closed": "已闭环",
}

# 紧迫度
SEVERITIES = ["routine", "attention", "urgent", "emergency"]
SEVERITY_LABELS = {
    "routine": "常规",
    "attention": "关注",
    "urgent": "紧急",
    "emergency": "特急",
}

# 记录类型
RECORD_KINDS = ["review", "note"]


def require_text(value, field, max_length=2000):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value


def require_number(value, field, minimum=0.0):
    """解析数值；minimum=None 时允许负值（如水位回落的涨幅）。"""
    if isinstance(value, bool):
        raise ValidationError(f"{field}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if number != number or number in (float("inf"), float("-inf")):
        raise ValidationError(f"{field}必须是有效数字")
    if minimum is not None and number < minimum:
        raise ValidationError(f"{field}不能小于{minimum}")
    return number


def require_choice(value, field, choices):
    if value not in choices:
        raise ValidationError(f"{field}取值非法：{value}")
    return value


def ensure_role(role, allowed):
    if role not in allowed:
        raise PermissionDenied("当前角色无权执行该操作")


def parse_gates(raw: Any) -> List[Dict[str, Any]]:
    """解析可用闸门清单：[{name, state, capacity}]。"""
    if not isinstance(raw, list) or not raw:
        raise ValidationError("闸门清单至少需要1扇闸门")
    if len(raw) > 16:
        raise ValidationError("闸门清单不能超过16扇闸门")
    gates: List[Dict[str, Any]] = []
    seen = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(f"第{index + 1}扇闸门数据格式错误")
        name = require_text(item.get("name"), f"第{index + 1}扇闸门名称", 20)
        if name in seen:
            raise ValidationError(f"闸门名称重复：{name}")
        seen.add(name)
        state = require_choice(item.get("state"), f"闸门{name}的状态", GATE_STATES)
        capacity = require_number(item.get("capacity"), f"闸门{name}的单孔能力", 0.0)
        gates.append({"name": name, "state": state, "capacity": capacity})
    return gates
