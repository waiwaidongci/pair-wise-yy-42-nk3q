from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (DISPATCH_EVENT_KINDS, FIRELINE_GRADES, ID_PREFIX, STATES,
                    VEHICLE_TYPES, WIND_DIRECTIONS, build_assignment_plan)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vehicles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    callsign TEXT NOT NULL UNIQUE,
                    vehicle_type TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    request_id TEXT,
                    payload_hash TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_vehicles_request
                    ON vehicles(request_id) WHERE request_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS zones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    capacity INTEGER NOT NULL,
                    wind_direction TEXT,
                    fireline_grade TEXT,
                    env_version INTEGER NOT NULL DEFAULT 0,
                    request_id TEXT,
                    payload_hash TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_zones_request
                    ON zones(request_id) WHERE request_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS fire_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    zone_id INTEGER NOT NULL REFERENCES zones(id),
                    required_type TEXT,
                    required_vehicles INTEGER NOT NULL,
                    slot_start TEXT NOT NULL,
                    slot_end TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','completed','cancelled')),
                    request_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_request ON fire_tasks(request_id);
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES fire_tasks(id),
                    zone_id INTEGER NOT NULL REFERENCES zones(id),
                    required_type TEXT,
                    preferred_vehicle_id INTEGER REFERENCES vehicles(id),
                    vehicle_id INTEGER REFERENCES vehicles(id),
                    slot_start TEXT NOT NULL,
                    slot_end TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('waiting','dispatched','completed','cancelled')),
                    dispatch_seq INTEGER NOT NULL,
                    invalidated INTEGER NOT NULL DEFAULT 0,
                    requeue_count INTEGER NOT NULL DEFAULT 0,
                    request_id TEXT,
                    payload_hash TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_dispatches_status ON dispatches(status);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_dispatches_request
                    ON dispatches(request_id) WHERE request_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS dispatch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_id INTEGER,
                    task_id INTEGER,
                    zone_id INTEGER NOT NULL,
                    vehicle_id INTEGER,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{{}}',
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ==================== 车辆派车调度 ====================
    def _audit_locked(self, action, entity_type, entity_id, actor, detail):
        """在已持锁的事务内追加审计事件，保证与业务写入同生共死。"""
        row = self.conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        self.conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]),
        )

    def _dispatch_event_locked(self, dispatch_id, task_id, zone_id, vehicle_id,
                               kind, actor, detail=None):
        if kind not in DISPATCH_EVENT_KINDS:
            raise ValidationError("未知调度事件类型")
        self.conn.execute(
            """INSERT INTO dispatch_events(dispatch_id, task_id, zone_id, vehicle_id,
               kind, detail, actor, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (dispatch_id, task_id, zone_id, vehicle_id, kind,
             json.dumps(detail or {}, ensure_ascii=False, sort_keys=True), actor, utc_now()),
        )

    # ---- 车辆 ----
    def create_vehicle(self, callsign, vehicle_type, request_id, payload_hash, actor):
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO vehicles(callsign, vehicle_type, request_id,
                       payload_hash, created_by, created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (callsign, vehicle_type, request_id, payload_hash, actor, now),
                )
                vehicle_id = int(cur.lastrowid)
                self._audit_locked("dispatch.vehicle.create", "vehicle", vehicle_id, actor,
                                   {"callsign": callsign, "vehicle_type": vehicle_type,
                                    "request_id": request_id})
        except sqlite3.IntegrityError as exc:
            raise ConflictError("车辆呼号或指令ID重复") from exc
        return self.get_vehicle(vehicle_id)

    def find_vehicle_by_request(self, request_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM vehicles WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_vehicle(self, vehicle_id):
        with self._lock:
            row = self.conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if row is None:
            raise NotFoundError("车辆不存在")
        return dict(row)

    def list_vehicles(self):
        with self._lock:
            rows = self.conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ---- 任务区 ----
    def create_zone(self, name, capacity, wind_direction, fireline_grade,
                    request_id, payload_hash, actor):
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO zones(name, capacity, wind_direction, fireline_grade,
                       env_version, request_id, payload_hash, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,0,?,?,?,?,?)""",
                    (name, capacity, wind_direction, fireline_grade, request_id,
                     payload_hash, actor, now, now),
                )
                zone_id = int(cur.lastrowid)
                self._audit_locked("dispatch.zone.create", "zone", zone_id, actor,
                                   {"name": name, "capacity": capacity})
        except sqlite3.IntegrityError as exc:
            raise ConflictError("任务区名称或指令ID重复") from exc
        return self.get_zone(zone_id)

    def find_zone_by_request(self, request_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM zones WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_zone(self, zone_id):
        with self._lock:
            row = self.conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务区不存在")
        return dict(row)

    def list_zones(self):
        with self._lock:
            rows = self.conn.execute("SELECT * FROM zones ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def update_zone_environment(self, zone_id, wind_direction, fireline_grade, actor):
        """风向/火线等级变化：先释放该区域全部已派单（车辆归还），再触发FIFO重排。

        失效只影响环境变化时已经在执行(dispatched)的单子；原本排队的单本来就没占车，
        保持其排队顺序参与重排。返回受影响并重新派上的派车单列表。
        """
        with self._lock, self.conn:
            zone = self.conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
            if zone is None:
                raise NotFoundError("任务区不存在")
            old_wind, old_grade = zone["wind_direction"], zone["fireline_grade"]
            wind = old_wind if wind_direction is None else wind_direction
            grade = old_grade if fireline_grade is None else fireline_grade
            changed = wind != old_wind or grade != old_grade
            self.conn.execute(
                "UPDATE zones SET wind_direction=?, fireline_grade=?, env_version=env_version+1, updated_at=? WHERE id=?",
                (wind, grade, utc_now(), zone_id),
            )
            released_ids = []
            if changed:
                active = self.conn.execute(
                    "SELECT * FROM dispatches WHERE zone_id=? AND status='dispatched'",
                    (zone_id,),
                ).fetchall()
                # 1) 先释放：所有受影响车辆归还
                for d in active:
                    released_ids.append(int(d["id"]))
                    self.conn.execute(
                        """UPDATE dispatches SET status='waiting', vehicle_id=NULL,
                           invalidated=1, requeue_count=requeue_count+1, updated_at=? WHERE id=?""",
                        (utc_now(), d["id"]),
                    )
                    self._dispatch_event_locked(
                        d["id"], d["task_id"], zone_id, d["vehicle_id"], "released", actor,
                        {"reason": "environment_changed", "old_wind": old_wind,
                         "new_wind": wind, "old_grade": old_grade, "new_grade": grade})
                self._audit_locked("dispatch.environment.change", "zone", zone_id, actor,
                                   {"wind_direction": wind, "fireline_grade": grade,
                                    "released": released_ids})
            else:
                self._audit_locked("dispatch.environment.report", "zone", zone_id, actor,
                                   {"wind_direction": wind, "fireline_grade": grade})
            reassigned = self._pump_locked(actor, reassign_ids=set(released_ids))
        return {"zone": self.get_zone(zone_id), "released": released_ids,
                "reassigned": reassigned, "changed": changed}

    # ---- 火线任务与派车单 ----
    def find_task_by_request(self, request_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM fire_tasks WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_task(self, task_id):
        with self._lock:
            row = self.conn.execute("SELECT * FROM fire_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("火线任务不存在")
        task = dict(row)
        with self._lock:
            task["dispatches"] = [dict(r) for r in self.conn.execute(
                "SELECT * FROM dispatches WHERE task_id=? ORDER BY dispatch_seq", (task_id,)
            ).fetchall()]
        return task

    def list_tasks(self, status=None):
        sql = "SELECT * FROM fire_tasks"
        params = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def submit_task(self, title, zone_id, required_type, required_vehicles,
                    slot_start, slot_end, request_id, payload_hash, actor):
        """提交火线任务并创建派车单：容量/车辆不足的单立即排队，随后FIFO重排。

        与审计在同一事务：写失败（断网）时整单回滚，既有排班原样保留。
        request_id唯一约束兜底：重复指令绝不会多占车辆。
        """
        now = utc_now()
        with self._lock, self.conn:
            if self.conn.execute("SELECT 1 FROM zones WHERE id=?", (zone_id,)).fetchone() is None:
                raise NotFoundError("任务区不存在")
            try:
                cur = self.conn.execute(
                    """INSERT INTO fire_tasks(title, zone_id, required_type, required_vehicles,
                       slot_start, slot_end, status, request_id, payload_hash,
                       created_by, created_at)
                       VALUES(?,?,?,?,?,?, 'open', ?,?,?,?)""",
                    (title, zone_id, required_type, required_vehicles, slot_start, slot_end,
                     request_id, payload_hash, actor, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("指令ID重复") from exc
            task_id = int(cur.lastrowid)
            for seq in range(1, required_vehicles + 1):
                self.conn.execute(
                    """INSERT INTO dispatches(task_id, zone_id, required_type,
                       preferred_vehicle_id, vehicle_id, slot_start, slot_end, status,
                       dispatch_seq, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,NULL,?,?, 'waiting', ?,?,?,?)""",
                    (task_id, zone_id, required_type, None, slot_start, slot_end, seq,
                     actor, now, now),
                )
            self._dispatch_event_locked(
                None, task_id, zone_id, None, "queued", actor,
                {"request_id": request_id, "required_vehicles": required_vehicles})
            self._audit_locked("dispatch.task.submit", "fire_task", task_id, actor,
                               {"zone_id": zone_id, "required_type": required_type,
                                "required_vehicles": required_vehicles,
                                "slot_start": slot_start, "slot_end": slot_end,
                                "request_id": request_id})
            self._pump_locked(actor)
        return self.get_task(task_id)

    def find_dispatch_by_request(self, request_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def request_vehicle(self, task_id, vehicle_id, slot_start, slot_end,
                        request_id, payload_hash, actor):
        """指定车辆的派车指令（点名派车）。车辆被占或车型不符时排队等它，不换车。"""
        now = utc_now()
        with self._lock, self.conn:
            task = self.conn.execute(
                "SELECT * FROM fire_tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("火线任务不存在")
            if task["status"] != "open":
                raise ConflictError("火线任务已结束，不能再派车")
            vehicle = self.conn.execute(
                "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if vehicle is None:
                raise NotFoundError("车辆不存在")
            if task["required_type"] is not None and vehicle["vehicle_type"] != task["required_type"]:
                raise ConflictError("车辆车型与任务要求不符")
            next_seq = self.conn.execute(
                "SELECT COALESCE(MAX(dispatch_seq),0)+1 AS s FROM dispatches WHERE task_id=?",
                (task_id,)).fetchone()["s"]
            try:
                cur = self.conn.execute(
                    """INSERT INTO dispatches(task_id, zone_id, required_type,
                       preferred_vehicle_id, vehicle_id, slot_start, slot_end, status,
                       dispatch_seq, request_id, payload_hash, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?, ?,?, 'waiting', ?,?,?,?,?,?)""",
                    (task_id, task["zone_id"], task["required_type"], vehicle_id,
                     None, slot_start, slot_end, next_seq, request_id, payload_hash,
                     actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("指令ID重复") from exc
            dispatch_id = int(cur.lastrowid)
            self._dispatch_event_locked(dispatch_id, task_id, task["zone_id"], None,
                                        "queued", actor, {"request_id": request_id,
                                                          "vehicle_id": vehicle_id})
            self._audit_locked("dispatch.vehicle.request", "dispatch", dispatch_id, actor,
                               {"task_id": task_id, "vehicle_id": vehicle_id,
                                "request_id": request_id})
            self._pump_locked(actor)
            row = self.conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
            return dict(row)

    def _snapshot_for_pump(self):
        waiting = [dict(r) for r in self.conn.execute(
            """SELECT * FROM dispatches WHERE status='waiting' ORDER BY id""").fetchall()]
        dispatched = [dict(r) for r in self.conn.execute(
            """SELECT * FROM dispatches WHERE status='dispatched'""").fetchall()]
        vehicles = {int(r["id"]): dict(r) for r in self.conn.execute(
            "SELECT * FROM vehicles").fetchall()}
        zones = {int(r["id"]): dict(r) for r in self.conn.execute(
            "SELECT * FROM zones").fetchall()}
        return waiting, dispatched, vehicles, zones

    def _pump_locked(self, actor, reassign_ids=None):
        """FIFO重排核心：按当前快照贪心分配waiting单。

        已dispatched的单保持不动（重排不抢已派出的车）；只有环境变化释放出来的单
        （reassign_ids）重新派上时记reassigned，其余首次派上记assigned。
        返回 [{id, vehicle_id, reassigned: bool}]。
        """
        reassign_ids = reassign_ids or set()
        waiting, dispatched, vehicles, zones = self._snapshot_for_pump()
        plan = build_assignment_plan(waiting, dispatched, vehicles, zones)
        now = utc_now()
        result = []
        for d in waiting:
            vid = plan.get(d["id"])
            if vid is None:
                continue
            self.conn.execute(
                "UPDATE dispatches SET status='dispatched', vehicle_id=?, updated_at=? WHERE id=?",
                (vid, now, d["id"]),
            )
            reassigned = d["id"] in reassign_ids
            kind = "reassigned" if reassigned else "assigned"
            self._dispatch_event_locked(d["id"], d["task_id"], d["zone_id"], vid,
                                        kind, actor,
                                        {"requeue_count": d["requeue_count"]})
            if reassigned:
                self._audit_locked("dispatch.reassigned", "dispatch", d["id"], actor,
                                   {"task_id": d["task_id"], "vehicle_id": vid})
            result.append({"id": d["id"], "task_id": d["task_id"],
                           "vehicle_id": vid, "reassigned": reassigned})
        return result

    def complete_dispatch(self, dispatch_id, actor):
        with self._lock, self.conn:
            d = self.conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
            if d is None:
                raise NotFoundError("派车单不存在")
            if d["status"] != "dispatched":
                raise ConflictError("只有已派出的派车单可以完成")
            self.conn.execute(
                "UPDATE dispatches SET status='completed', updated_at=? WHERE id=?",
                (utc_now(), dispatch_id))
            self._dispatch_event_locked(dispatch_id, d["task_id"], d["zone_id"],
                                        d["vehicle_id"], "completed", actor)
            self._audit_locked("dispatch.complete", "dispatch", dispatch_id, actor,
                               {"task_id": d["task_id"], "vehicle_id": d["vehicle_id"]})
            # 车辆腾出后，用剩余容量给排队单补派
            self._pump_locked(actor)
        return self.get_dispatch(dispatch_id)

    def cancel_dispatch(self, dispatch_id, actor):
        with self._lock, self.conn:
            d = self.conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
            if d is None:
                raise NotFoundError("派车单不存在")
            if d["status"] in ("completed", "cancelled"):
                raise ConflictError("派车单已结束")
            self.conn.execute(
                "UPDATE dispatches SET status='cancelled', updated_at=? WHERE id=?",
                (utc_now(), dispatch_id))
            self._dispatch_event_locked(dispatch_id, d["task_id"], d["zone_id"],
                                        d["vehicle_id"], "cancelled", actor)
            self._audit_locked("dispatch.cancel", "dispatch", dispatch_id, actor, {})
            self._pump_locked(actor)
        return self.get_dispatch(dispatch_id)

    def get_dispatch(self, dispatch_id):
        with self._lock:
            row = self.conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise NotFoundError("派车单不存在")
        return dict(row)

    def complete_task(self, task_id, actor):
        with self._lock, self.conn:
            task = self.conn.execute("SELECT * FROM fire_tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("火线任务不存在")
            if task["status"] != "open":
                raise ConflictError("火线任务已结束")
            active = self.conn.execute(
                "SELECT COUNT(*) AS n FROM dispatches WHERE task_id=? AND status='dispatched'",
                (task_id,)).fetchone()["n"]
            waiting = self.conn.execute(
                "SELECT COUNT(*) AS n FROM dispatches WHERE task_id=? AND status='waiting'",
                (task_id,)).fetchone()["n"]
            if active or waiting:
                raise ConflictError("仍有在途或排队派车单，不能关闭任务")
            self.conn.execute("UPDATE fire_tasks SET status='completed' WHERE id=?", (task_id,))
            self._audit_locked("dispatch.task.complete", "fire_task", task_id, actor, {})
        return self.get_task(task_id)

    def dispatch_board(self):
        """调度台视图：等待 / 已派 / 失效重排 + 车辆状态 + 事件流。"""
        with self._lock:
            waiting = [dict(r) for r in self.conn.execute(
                """SELECT d.*, v.callsign AS vehicle_callsign
                   FROM dispatches d LEFT JOIN vehicles v ON v.id=d.preferred_vehicle_id
                   WHERE d.status='waiting' ORDER BY d.id""").fetchall()]
            dispatched = [dict(r) for r in self.conn.execute(
                """SELECT d.*, v.callsign AS vehicle_callsign
                   FROM dispatches d LEFT JOIN vehicles v ON v.id=d.vehicle_id
                   WHERE d.status='dispatched' ORDER BY d.id""").fetchall()]
            rearranged = [dict(r) for r in self.conn.execute(
                """SELECT d.*, v.callsign AS vehicle_callsign
                   FROM dispatches d LEFT JOIN vehicles v ON v.id=d.vehicle_id
                   WHERE d.invalidated=1 ORDER BY d.id""").fetchall()]
            events = [dict(r, detail=json.loads(r["detail"])) for r in self.conn.execute(
                "SELECT * FROM dispatch_events ORDER BY id DESC LIMIT 200").fetchall()]
            vehicles = [dict(r) for r in self.conn.execute(
                "SELECT * FROM vehicles ORDER BY id").fetchall()]
            zones = [dict(r) for r in self.conn.execute(
                "SELECT * FROM zones ORDER BY id").fetchall()]
        for v in vehicles:
            active = [d for d in dispatched if d["vehicle_id"] == v["id"]]
            v["state"] = "dispatched" if active else "idle"
            v["dispatch_ids"] = [d["id"] for d in active]
        return {"waiting": waiting, "dispatched": dispatched,
                "invalidated_rearranged": rearranged, "events": events,
                "vehicles": vehicles, "zones": zones}

    def close(self) -> None:
        with self._lock:
            self.conn.close()
