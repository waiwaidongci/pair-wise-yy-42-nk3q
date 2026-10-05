from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_choice,
                     normalize_severity, normalize_slot, require_int,
                     require_number, require_text, validate_window)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DISPATCH_ROLES,
                    DISPATCH_VIEW_ROLES, ENTITY, ENVIRONMENT_ROLES,
                    FIRELINE_GRADES, RECORD_ROLES, SYNC_ROLES, TASK_MANAGE_ROLES,
                    TITLE, VEHICLE_MANAGE_ROLES, VEHICLE_TYPES, VIEW_ROLES,
                    WIND_DIRECTIONS, ZONE_MANAGE_ROLES, completion_blockers,
                    escalation_required, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


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

    # ==================== 车辆派车调度 ====================
    @staticmethod
    def _payload_hash(payload: Dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _request_id(payload: Dict[str, Any]) -> str:
        return require_text(payload.get("request_id"), "request_id", 100)

    @staticmethod
    def _check_replay(stored_hash: str, payload_hash: str) -> None:
        # 同一指令ID但载荷不同 -> 视为冲突，绝不静默覆盖
        if stored_hash is not None and stored_hash != payload_hash:
            raise ConflictError("指令ID已存在但内容不一致")

    def register_vehicle(self, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, VEHICLE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        request_id = self._request_id(payload)
        payload_hash = self._payload_hash(payload)
        existing = self.repository.find_vehicle_by_request(request_id)
        if existing is not None:
            self._check_replay(existing.get("payload_hash"), payload_hash)
            return existing
        callsign = require_text(payload.get("callsign"), "callsign", 50)
        vehicle_type = normalize_choice(payload.get("vehicle_type"),
                                        "vehicle_type", VEHICLE_TYPES)
        return self.repository.create_vehicle(callsign, vehicle_type,
                                              request_id, payload_hash, actor)

    def register_zone(self, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        request_id = self._request_id(payload)
        payload_hash = self._payload_hash(payload)
        existing = self.repository.find_zone_by_request(request_id)
        if existing is not None:
            self._check_replay(existing.get("payload_hash"), payload_hash)
            return existing
        name = require_text(payload.get("name"), "name", 100)
        capacity = require_int(payload.get("capacity"), "capacity", 1, 10000)
        wind = normalize_choice(payload.get("wind_direction"), "wind_direction",
                                WIND_DIRECTIONS, required=False)
        grade = normalize_choice(payload.get("fireline_grade"), "fireline_grade",
                                 FIRELINE_GRADES, required=False)
        return self.repository.create_zone(name, capacity, wind, grade,
                                           request_id, payload_hash, actor)

    def list_vehicles(self, role: str) -> list:
        ensure_role(role, DISPATCH_VIEW_ROLES)
        return self.repository.list_vehicles()

    def list_zones(self, role: str) -> list:
        ensure_role(role, DISPATCH_VIEW_ROLES)
        return self.repository.list_zones()

    def submit_task(self, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        """两个调度员同时提交也安全：整单在库事务内串行提交，先到先得。"""
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        request_id = self._request_id(payload)
        payload_hash = self._payload_hash(payload)
        existing = self.repository.find_task_by_request(request_id)
        if existing is not None:
            self._check_replay(existing.get("payload_hash"), payload_hash)
            return self.repository.get_task(existing["id"])
        title = require_text(payload.get("title"), "title", 200)
        zone_id = require_int(payload.get("zone_id"), "zone_id")
        required_type = normalize_choice(payload.get("required_type"),
                                         "required_type", VEHICLE_TYPES, required=False)
        required_vehicles = require_int(payload.get("required_vehicles"),
                                        "required_vehicles", 1, 1000)
        start, end = validate_window(payload.get("slot_start"), payload.get("slot_end"))
        return self.repository.submit_task(title, zone_id, required_type,
                                           required_vehicles, start, end,
                                           request_id, payload_hash, actor)

    def request_vehicle(self, task_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        task_id = require_int(task_id, "task_id")
        request_id = self._request_id(payload)
        payload_hash = self._payload_hash(payload)
        existing = self.repository.find_dispatch_by_request(request_id)
        if existing is not None:
            self._check_replay(existing.get("payload_hash"), payload_hash)
            return existing
        vehicle_id = require_int(payload.get("vehicle_id"), "vehicle_id")
        start, end = validate_window(payload.get("slot_start"), payload.get("slot_end"))
        return self.repository.request_vehicle(task_id, vehicle_id, start, end,
                                               request_id, payload_hash, actor)

    def update_environment(self, zone_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ENVIRONMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        zone_id = require_int(zone_id, "zone_id")
        wind = normalize_choice(payload.get("wind_direction"), "wind_direction",
                                WIND_DIRECTIONS, required=False)
        grade = normalize_choice(payload.get("fireline_grade"), "fireline_grade",
                                 FIRELINE_GRADES, required=False)
        if wind is None and grade is None:
            from .domain import ValidationError as _VE
            raise _VE("必须提供wind_direction或fireline_grade")
        return self.repository.update_zone_environment(zone_id, wind, grade, actor)

    def complete_dispatch(self, dispatch_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        dispatch_id = require_int(dispatch_id, "dispatch_id")
        return self.repository.complete_dispatch(dispatch_id, actor)

    def cancel_dispatch(self, dispatch_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        dispatch_id = require_int(dispatch_id, "dispatch_id")
        return self.repository.cancel_dispatch(dispatch_id, actor)

    def get_task(self, task_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_VIEW_ROLES)
        return self.repository.get_task(require_int(task_id, "task_id"))

    def list_tasks(self, role: str, status: Optional[str] = None) -> list:
        ensure_role(role, DISPATCH_VIEW_ROLES)
        if status is not None and status not in ("open", "completed", "cancelled"):
            from .domain import ValidationError as _VE
            raise _VE("status不合法")
        return self.repository.list_tasks(status)

    def complete_task(self, task_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TASK_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        return self.repository.complete_task(require_int(task_id, "task_id"), actor)

    def board(self, role: str) -> Dict[str, Any]:
        """调度台：等待队列、已派车辆、失效重排、车辆状态、事件流。"""
        ensure_role(role, DISPATCH_VIEW_ROLES)
        return self.repository.dispatch_board()

    def sync_commands(self, commands: list, actor: str, role: str) -> list:
        """断网恢复后批量补传。逐条独立提交：已完成的指令幂等返回，失败不影响其他指令。"""
        ensure_role(role, SYNC_ROLES)
        actor = require_text(actor, "actor", 100)
        if not isinstance(commands, list) or not commands:
            from .domain import ValidationError as _VE
            raise _VE("commands必须是非空数组")
        results = []
        for index, command in enumerate(commands):
            entry: Dict[str, Any] = {"index": index}
            try:
                if not isinstance(command, dict):
                    from .domain import ValidationError as _VE
                    raise _VE("每条指令必须是JSON对象")
                op = command.get("op")
                body = command.get("payload", {})
                if not isinstance(body, dict):
                    from .domain import ValidationError as _VE
                    raise _VE("payload必须是JSON对象")
                if op == "submit_task":
                    entry["result"] = self.submit_task(body, actor, role)
                elif op == "request_vehicle":
                    entry["result"] = self.request_vehicle(
                        require_int(command.get("task_id"), "task_id"), body, actor, role)
                elif op == "register_vehicle":
                    entry["result"] = self.register_vehicle(body, actor, role)
                elif op == "register_zone":
                    entry["result"] = self.register_zone(body, actor, role)
                elif op == "update_environment":
                    entry["result"] = self.update_environment(
                        require_int(command.get("zone_id"), "zone_id"), body, actor, role)
                elif op == "complete_dispatch":
                    entry["result"] = self.complete_dispatch(
                        require_int(command.get("dispatch_id"), "dispatch_id"), actor, role)
                elif op == "cancel_dispatch":
                    entry["result"] = self.cancel_dispatch(
                        require_int(command.get("dispatch_id"), "dispatch_id"), actor, role)
                else:
                    from .domain import ValidationError as _VE
                    raise _VE("未知op")
                entry["ok"] = True
            except Exception as exc:  # 单条失败隔离，其余补传继续
                entry["ok"] = False
                entry["error"] = exc.__class__.__name__
                entry["message"] = str(exc)
            results.append(entry)
        return results

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
