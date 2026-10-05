from __future__ import annotations
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

# ========== 车辆派车调度 ==========
from .domain import (DISPATCH_EVENT_KINDS, DISPATCH_STATES, FIRELINE_GRADES,  # noqa: E402
                     TASK_STATES, VEHICLE_TYPES, WIND_DIRECTIONS, ValidationError)

VEHICLE_MANAGE_ROLES=set(['field_commander','incident_commander','logistics'])
ZONE_MANAGE_ROLES=set(['field_commander','incident_commander'])
ENVIRONMENT_ROLES=set(['field_commander','incident_commander'])
TASK_MANAGE_ROLES=set(['field_commander','incident_commander','logistics'])
DISPATCH_ROLES=set(['field_commander','incident_commander','logistics'])
DISPATCH_VIEW_ROLES=set(['field_commander','incident_commander','logistics','viewer'])
SYNC_ROLES=DISPATCH_ROLES


def slots_overlap(start_a, end_a, start_b, end_b):
    """半开区间[start,end)重叠判定：首尾相接不算重叠，同一辆车可以背靠背出任务。"""
    return start_a < end_b and start_b < end_a


def overlap_count(intervals, start, end):
    """与给定时段重叠的区间数量（容量统计）。"""
    return sum(1 for s, e in intervals if slots_overlap(s, e, start, end))


def build_assignment_plan(waiting, dispatched, vehicles_by_id, zones_by_id):
    """贪心FIFO分配：输入当前待派单和已派单快照，输出{dispatch_id: vehicle_id}。

    waiting: [{id, zone_id, required_type, preferred_vehicle_id, slot_start, slot_end}]（按id升序）
    dispatched: 同上且带vehicle_id
    约束：
      1. 同一车辆同一时段只能接一个有效任务（半开区间，首尾相接可）。
      2. 任务区同时段在派车辆数不能超过容量。
      3. 车型必须匹配。
    指定了preferred_vehicle_id但该车不可用（被占/车型不符/不存在）时该单本轮跳过排队，
    不偷偷换车——重复指令不会多占车辆，也不会把车派给未指名的单。
    """
    plan={}
    vehicle_busy={}   # vehicle_id -> [(start,end)]
    zone_load={}      # zone_id -> [(start,end)]
    for d in dispatched:
        vid=d.get('vehicle_id')
        if vid is not None:
            vehicle_busy.setdefault(vid, []).append((d['slot_start'], d['slot_end']))
        zone_load.setdefault(d['zone_id'], []).append((d['slot_start'], d['slot_end']))
    for d in waiting:
        zone=zones_by_id.get(d['zone_id'])
        if zone is None:
            continue
        start, end=d['slot_start'], d['slot_end']
        if overlap_count(zone_load.get(d['zone_id'], []), start, end) >= int(zone['capacity']):
            continue  # 任务区容量满，继续排队
        required=d.get('required_type')
        preferred=d.get('preferred_vehicle_id')
        candidate=None
        if preferred is not None:
            vehicle=vehicles_by_id.get(preferred)
            if (vehicle is not None and vehicle['active'] == 1
                    and (required is None or vehicle['vehicle_type'] == required)
                    and not any(slots_overlap(s, e, start, end)
                                for s, e in vehicle_busy.get(preferred, []))):
                candidate=preferred
            # 点名车辆不可用 -> 本轮跳过
        else:
            for vid, vehicle in vehicles_by_id.items():
                if vehicle['active'] != 1:
                    continue
                if required is not None and vehicle['vehicle_type'] != required:
                    continue
                if any(slots_overlap(s, e, start, end) for s, e in vehicle_busy.get(vid, [])):
                    continue
                candidate=vid
                break
        if candidate is None:
            continue
        plan[d['id']]=candidate
        vehicle_busy.setdefault(candidate, []).append((start, end))
        zone_load.setdefault(d['zone_id'], []).append((start, end))
    return plan
