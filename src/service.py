from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AREA_MANAGE_ROLES, AUDIT_ROLES, CREATE_ROLES, DISPATCH_ROLES,
                    ENTITY, RECORD_ROLES, TASK_MANAGE_ROLES, TITLE,
                    VEHICLE_MANAGE_ROLES, VIEW_ROLES, can_reschedule,
                    completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_capacity, validate_time_window,
                    validate_transition, valid_fire_task_status,
                    valid_wind_direction)


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
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
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
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

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

    # ---- 车辆 ----
    def create_vehicle(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, VEHICLE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        callsign = require_text(payload.get("callsign"), "callsign", 100)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        vehicle = self.repository.create_vehicle(callsign, external_ref, actor)
        self.repository.append_audit("create", "vehicle", vehicle["id"], actor,
                                     {"callsign": callsign})
        return vehicle

    def list_vehicles(self, role: str) -> list:
        self._view(role)
        return self.repository.list_vehicles()

    # ---- 任务区 ----
    def create_task_area(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, AREA_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        capacity = validate_capacity(payload.get("capacity"))
        area = self.repository.create_task_area(name, capacity, actor)
        self.repository.append_audit("create", "task_area", area["id"], actor,
                                     {"name": name, "capacity": capacity})
        return area

    def list_task_areas(self, role: str) -> list:
        self._view(role)
        return self.repository.list_task_areas()

    # ---- 火线任务 ----
    def create_fire_task(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        item_id = int(require_number(payload.get("item_id"), "item_id"))
        area_id = int(require_number(payload.get("area_id"), "area_id"))
        title = require_text(payload.get("title"), "title", 200)
        wind_direction = require_text(payload.get("wind_direction"), "wind_direction", 10)
        if not valid_wind_direction(wind_direction):
            raise ValidationError("风向必须是8方位之一")
        severity = normalize_severity(payload.get("severity"))
        start_at, end_at = validate_time_window(payload.get("start_at"), payload.get("end_at"))
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        task = self.repository.create_fire_task(item_id, area_id, title, wind_direction,
                                                severity, start_at, end_at, external_ref, actor)
        self.repository.append_audit("create", "fire_task", task["id"], actor, {
            "title": title, "area_id": area_id,
            "wind_direction": wind_direction, "severity": severity,
        })
        return task

    def list_fire_tasks(self, role: str, area_id: Optional[int] = None,
                        item_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_fire_tasks(area_id, item_id)

    def get_fire_task(self, task_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_fire_task(task_id)

    def update_fire_task(self, task_id: int, payload: Dict[str, Any],
                         expected_version: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        fields: Dict[str, Any] = {}
        for key in ("title", "wind_direction", "severity", "status", "start_at", "end_at"):
            if key in payload:
                fields[key] = payload[key]
        if "wind_direction" in fields and not valid_wind_direction(fields["wind_direction"]):
            raise ValidationError("风向必须是8方位之一")
        if "severity" in fields:
            fields["severity"] = normalize_severity(fields["severity"])
        if "status" in fields and not valid_fire_task_status(fields["status"]):
            raise ValidationError("火线任务状态不合法")
        if "start_at" in fields or "end_at" in fields:
            current = self.repository.get_fire_task(task_id)
            validate_time_window(fields.get("start_at", current["start_at"]),
                                 fields.get("end_at", current["end_at"]))
        task, changed = self.repository.update_fire_task(task_id, expected_version, fields)
        self.repository.append_audit("update", "fire_task", task["id"], actor,
                                     {"changed": changed, "fields": sorted(fields.keys())})
        if changed and can_reschedule(task["status"]):
            self._invalidate_and_reschedule(task["id"], actor)
        return self.repository.get_fire_task(task_id)

    def _invalidate_and_reschedule(self, task_id: int, actor: str) -> None:
        released = self.repository.invalidate_task_dispatches(task_id)
        if released:
            self.repository.append_audit("invalidate", "fire_task", task_id, actor,
                                         {"released_dispatches": released})
        results = self.repository.reschedule_task_dispatches(task_id)
        if results:
            self.repository.append_audit("reschedule", "fire_task", task_id, actor, {
                "rescheduled": [{"old": old["id"], "new_status": status} for old, status in results],
            })
        task = self.repository.get_fire_task(task_id)
        promoted = self.repository.drain_area(task["area_id"])
        if promoted:
            self.repository.append_audit("drain", "task_area", task["area_id"], actor,
                                         {"promoted": promoted})

    def reschedule_task(self, task_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        task = self.repository.get_fire_task(task_id)
        if not can_reschedule(task["status"]):
            raise ConflictError("任务已结束，不再重排")
        self._invalidate_and_reschedule(task_id, actor)
        return self.repository.get_fire_task(task_id)

    # ---- 派车 ----
    def dispatch_vehicle(self, task_id: int, vehicle_id: int, external_ref: Optional[str],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        task = self.repository.get_fire_task(task_id)
        if not can_reschedule(task["status"]):
            raise ConflictError("任务已结束，不再派车")
        if external_ref is None:
            external_ref = f"D-{uuid.uuid4().hex[:12]}"
        else:
            external_ref = require_text(external_ref, "external_ref", 100)
        dispatch, created = self.repository.create_dispatch(task_id, vehicle_id, external_ref)
        if created:
            self.repository.append_audit("dispatch", "fire_task", task_id, actor, {
                "vehicle_id": vehicle_id, "dispatch_id": dispatch["id"],
                "status": dispatch["status"],
            })
        return dispatch

    def list_dispatches(self, role: str, status: Optional[str] = None,
                         area_id: Optional[int] = None, task_id: Optional[int] = None,
                         vehicle_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_dispatches(status, area_id, task_id, vehicle_id)

    def dispatch_console(self, role: str) -> Dict[str, Any]:
        self._view(role)
        return {
            "waiting": self.repository.list_dispatches(status="waiting"),
            "dispatched": self.repository.list_dispatches(status="dispatched"),
            "rescheduled": self.repository.list_rescheduled_dispatches(),
        }

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
