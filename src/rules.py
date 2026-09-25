from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='水库防汛调度与操作确认'; ENTITY='调度指令'; ID_PREFIX='RF'
SEVERITIES=['routine', 'attention', 'urgent', 'emergency']; STATES=['draft', 'checked', 'authorized', 'executed', 'closed']; TRANSITIONS={'draft': ['checked'], 'checked': ['authorized'], 'authorized': ['executed'], 'executed': ['closed', 'checked'], 'closed': []}; TRANSITION_ROLES={'checked': ['duty_officer'], 'authorized': ['chief_engineer'], 'executed': ['dispatcher'], 'closed': ['chief_engineer']}
CREATE_ROLES=set(['duty_officer']); RECORD_ROLES=set(['duty_officer', 'dispatcher', 'chief_engineer']); AUDIT_ROLES=set(['chief_engineer', 'viewer']); VIEW_ROLES=set(['duty_officer', 'chief_engineer', 'dispatcher', 'viewer'])
FEEDBACK_ROLES=set(['dispatcher']); REVIEW_ROLES=set(['chief_engineer'])
REVIEW_KIND='review'; FEEDBACK_KIND='feedback'; DEVIATION_KIND='deviation'; RESERVED_RECORD_KINDS=set([FEEDBACK_KIND, DEVIATION_KIND])
DOWNSTREAM_ALARMS=['normal', 'warning', 'severe']; DEVIATION_TOLERANCE=0.10
# 基础下泄流量 = 入库流量 + 超汛限水位(m) x 500 + 涨幅(m/h) x 200
LEVEL_DISCHARGE_FACTOR=500.0; RISE_DISCHARGE_FACTOR=200.0
SEVERITY_WEIGHT={'routine': 1.0, 'attention': 3.0, 'urgent': 6.0, 'emergency': 9.0}; DEADLINE_HOURS={'routine': 72, 'attention': 24, 'urgent': 8, 'emergency': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def normalize_downstream_alarm(value):
    if value not in DOWNSTREAM_ALARMS: raise ValidationError("downstream_alarm必须是normal/warning/severe")
    return value
def parse_gates(value):
    if not isinstance(value,list) or not value: raise ValidationError("gates必须是非空闸门列表")
    gates=[]; seen=set()
    for entry in value:
        if not isinstance(entry,dict): raise ValidationError("闸门必须是对象")
        gate_id=entry.get("id")
        if not isinstance(gate_id,str) or not gate_id.strip(): raise ValidationError("闸门编号不能为空")
        gate_id=gate_id.strip()
        if gate_id in seen: raise ValidationError(f"闸门编号重复:{gate_id}")
        seen.add(gate_id)
        capacity=entry.get("capacity")
        if isinstance(capacity,bool): raise ValidationError("闸门泄流能力必须是数字")
        try: capacity=float(capacity)
        except (TypeError,ValueError): raise ValidationError("闸门泄流能力必须是数字")
        if capacity<=0: raise ValidationError("闸门泄流能力必须大于0")
        gates.append({"id":gate_id,"capacity":capacity,"restricted":bool(entry.get("restricted",False))})
    return gates
def compute_discharge_plan(reservoir_level,flood_limit,inflow,rise_rate,downstream_alarm,gates):
    over_limit=max(0.0,reservoir_level-flood_limit)
    planned=inflow+LEVEL_DISCHARGE_FACTOR*over_limit+RISE_DISCHARGE_FACTOR*max(0.0,rise_rate)
    reasons=[]; capped=False
    if downstream_alarm=='severe':
        half=inflow/2.0
        if planned>half:
            planned=half; capped=True; reasons.append("下游严重告警，下泄量按入库流量一半封顶")
    planned=round(planned,1)
    selected=[]; total=0.0
    for gate in sorted((g for g in gates if not g.get("restricted")),key=lambda g:-g["capacity"]):
        if total>=planned: break
        selected.append(gate["id"]); total+=gate["capacity"]
    needs_restricted=False
    if total<planned:
        for gate in sorted((g for g in gates if g.get("restricted")),key=lambda g:-g["capacity"]):
            if total>=planned: break
            selected.append(gate["id"]); total+=gate["capacity"]; needs_restricted=True
    if needs_restricted: reasons.append("可用闸门不足，需启用受限闸门")
    level_rising=rise_rate>0
    if level_rising: reasons.append("库位仍上涨")
    return {"planned_discharge":planned,"gate_ids":selected,"gate_count":len(selected),
            "capped_by_downstream":capped,"needs_restricted":needs_restricted,
            "level_rising":level_rising,"review_required":bool(level_rising or needs_restricted),
            "reasons":reasons}
def discharge_deviation(planned,actual):
    if planned<=0: return 0.0 if actual<=0 else 1.0
    return abs(actual-planned)/planned
def derive_severity(base,plan,downstream_alarm):
    index=SEVERITIES.index(base)
    if plan["level_rising"]: index=max(index,SEVERITIES.index('attention'))
    if downstream_alarm=='severe' or plan["needs_restricted"]: index=max(index,SEVERITIES.index('urgent'))
    return SEVERITIES[index]
