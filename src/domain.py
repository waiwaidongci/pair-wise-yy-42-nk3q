from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; ROLES=['field_commander', 'incident_commander', 'logistics', 'viewer']
# 车辆派车调度域
VEHICLE_TYPES=['fire_engine', 'water_tanker', 'crew_carrier', 'dozer', 'support']
WIND_DIRECTIONS=['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
FIRELINE_GRADES=['low', 'moderate', 'high', 'extreme']
DISPATCH_STATES=['waiting', 'dispatched', 'completed', 'cancelled']
TASK_STATES=['open', 'completed', 'cancelled']
DISPATCH_EVENT_KINDS=['queued', 'assigned', 'released', 'reassigned', 'completed', 'cancelled']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def require_int(value,field,minimum=1,maximum=None):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是整数")
    if isinstance(value,float):
        if not value.is_integer(): raise ValidationError(f"{field}必须是整数")
        number=int(value)
    else:
        try: number=int(value)
        except (TypeError,ValueError): raise ValidationError(f"{field}必须是整数")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    if maximum is not None and number>maximum: raise ValidationError(f"{field}不能大于{maximum}")
    return number
def normalize_choice(value,field,choices,required=True):
    if value is None:
        if required: raise ValidationError(f"{field}不能为空")
        return None
    if value not in choices: raise ValidationError(f"{field}不在允许范围内")
    return value
def normalize_slot(value,field):
    """统一为UTC ISO字符串，保证字符串比较等价于时间比较。"""
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}必须是ISO时间")
    text=value.strip()
    if text.endswith('Z'): text=text[:-1]+'+00:00'
    try:
        dt=datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field}不是有效ISO时间") from exc
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
def validate_window(slot_start,slot_end):
    start=normalize_slot(slot_start,"slot_start")
    end=normalize_slot(slot_end,"slot_end")
    if not start<end: raise ValidationError("slot_start必须早于slot_end")
    return start,end
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
