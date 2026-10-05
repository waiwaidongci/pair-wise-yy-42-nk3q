from __future__ import annotations
from datetime import datetime, timezone
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
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

# 车辆调度：车辆、火线任务与任务区容量绑定
DISPATCH_STATUSES=['waiting','dispatched','invalid']
FIRE_TASK_STATUSES=['scheduled','active','done','cancelled']
WIND_DIRECTIONS=['N','NE','E','SE','S','SW','W','NW']
DISPATCH_ROLES=set(['field_commander','logistics','incident_commander'])
VEHICLE_MANAGE_ROLES=set(['field_commander','logistics','incident_commander'])
AREA_MANAGE_ROLES=set(['incident_commander','logistics'])
TASK_MANAGE_ROLES=set(['field_commander','incident_commander'])
TERMINAL_TASK_STATUSES=set(['done','cancelled'])

def valid_dispatch_status(status): return status in DISPATCH_STATUSES
def valid_fire_task_status(status): return status in FIRE_TASK_STATUSES
def valid_wind_direction(value): return value in WIND_DIRECTIONS
def is_terminal_task(status): return status in TERMINAL_TASK_STATUSES
def can_reschedule(status): return not is_terminal_task(status)

def validate_capacity(capacity):
    if isinstance(capacity,bool) or not isinstance(capacity,int):
        raise ValidationError("任务区容量必须是正整数")
    if capacity<1: raise ValidationError("任务区容量必须大于0")
    return capacity

def _parse_ts(value):
    if not isinstance(value,str): raise ValidationError("时段必须是ISO时间字符串")
    try: dt=datetime.fromisoformat(value)
    except ValueError as exc: raise ValidationError("时段格式不正确") from exc
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    return dt

def validate_time_window(start_at,end_at):
    start=_parse_ts(start_at); end=_parse_ts(end_at)
    if start>=end: raise ValidationError("任务开始时间必须早于结束时间")
    return start_at,end_at

def windows_overlap(start1,end1,start2,end2):
    return _parse_ts(start1)<_parse_ts(end2) and _parse_ts(start2)<_parse_ts(end1)
