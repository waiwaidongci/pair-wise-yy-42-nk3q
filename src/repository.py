from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


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
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','inactive')),
                    external_ref TEXT UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_areas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    capacity INTEGER NOT NULL CHECK(capacity>=1),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fire_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    area_id INTEGER NOT NULL REFERENCES task_areas(id),
                    title TEXT NOT NULL,
                    wind_direction TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled'
                        CHECK(status IN ('scheduled','active','done','cancelled')),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES fire_tasks(id) ON DELETE CASCADE,
                    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
                    area_id INTEGER NOT NULL REFERENCES task_areas(id),
                    status TEXT NOT NULL
                        CHECK(status IN ('waiting','dispatched','invalid')),
                    external_ref TEXT NOT NULL UNIQUE,
                    rescheduled_from INTEGER REFERENCES dispatches(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_dispatches_area_status
                    ON dispatches(area_id, status);
                CREATE INDEX IF NOT EXISTS ix_dispatches_vehicle_status
                    ON dispatches(vehicle_id, status);
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

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---- 车辆 ----
    def create_vehicle(self, callsign: str, external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO vehicles(callsign, status, external_ref, created_by, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (callsign, "active", external_ref, actor, now),
                )
                vehicle_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("车辆呼号或唯一标识已存在") from exc
        return self.get_vehicle(vehicle_id)

    def get_vehicle(self, vehicle_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if row is None:
            raise NotFoundError("车辆不存在")
        return dict(row)

    def list_vehicles(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM vehicles ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    # ---- 任务区 ----
    def create_task_area(self, name: str, capacity: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO task_areas(name, capacity, created_by, created_at) VALUES(?,?,?,?)",
                    (name, capacity, actor, now),
                )
                area_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("任务区名称已存在") from exc
        return self.get_task_area(area_id)

    def get_task_area(self, area_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM task_areas WHERE id=?", (area_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务区不存在")
        return dict(row)

    def list_task_areas(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM task_areas ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    # ---- 火线任务 ----
    def create_fire_task(self, item_id: int, area_id: int, title: str, wind_direction: str,
                          severity: str, start_at: str, end_at: str,
                          external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        self.get_task_area(area_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO fire_tasks(item_id, area_id, title, wind_direction, severity,
                       start_at, end_at, status, version, external_ref, created_by, created_at,
                       updated_at) VALUES(?,?,?,?,?,?,?, 'scheduled', 1, ?, ?, ?, ?)""",
                    (item_id, area_id, title, wind_direction, severity, start_at, end_at,
                     external_ref, actor, now, now),
                )
                task_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("火线任务唯一标识已存在") from exc
        return self.get_fire_task(task_id)

    def get_fire_task(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM fire_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("火线任务不存在")
        return dict(row)

    def list_fire_tasks(self, area_id: Optional[int] = None,
                         item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM fire_tasks WHERE 1=1"
        params: list = []
        if area_id is not None:
            sql += " AND area_id=?"; params.append(area_id)
        if item_id is not None:
            sql += " AND item_id=?"; params.append(item_id)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def update_fire_task(self, task_id: int, expected_version: int,
                          fields: Dict[str, Any]) -> tuple:
        now = utc_now()
        sensitive = set(["wind_direction", "severity"])
        with self._lock, self.conn:
            task = self.get_fire_task(task_id)
            sets: list = []
            params: list = []
            changed = False
            for key, value in fields.items():
                if key in task and value is not None and value != task[key]:
                    sets.append(f"{key}=?"); params.append(value)
                    if key in sensitive:
                        changed = True
            if not sets:
                return task, False
            sets.append("version=version+1"); sets.append("updated_at=?")
            params.append(now)
            params.append(task_id); params.append(expected_version)
            cur = self.conn.execute(
                f"UPDATE fire_tasks SET {', '.join(sets)} WHERE id=? AND version=?", params)
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM fire_tasks WHERE id=?", (task_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("火线任务不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_fire_task(task_id), changed

    # ---- 派车 ----
    def _overlapping_dispatched(self, vehicle_id: int, start_at: str, end_at: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            """SELECT d.* FROM dispatches d JOIN fire_tasks t ON d.task_id=t.id
               WHERE d.vehicle_id=? AND d.status='dispatched'
                 AND t.start_at < ? AND ? < t.end_at LIMIT 1""",
            (vehicle_id, end_at, start_at),
        ).fetchone()
        return dict(row) if row else None

    def _dispatched_count(self, area_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM dispatches WHERE area_id=? AND status='dispatched'",
            (area_id,),
        ).fetchone()
        return int(row["n"])

    def create_dispatch(self, task_id: int, vehicle_id: int,
                        external_ref: str) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            task = self.get_fire_task(task_id)
            self.get_vehicle(vehicle_id)
            existing = self.conn.execute(
                "SELECT * FROM dispatches WHERE external_ref=?", (external_ref,)).fetchone()
            if existing is not None:
                return dict(existing), False
            if self._overlapping_dispatched(vehicle_id, task["start_at"], task["end_at"]) is not None:
                raise ConflictError("该车此时段已有有效任务，不能重复派车")
            area = self.get_task_area(task["area_id"])
            status = "dispatched" if self._dispatched_count(area["id"]) < area["capacity"] else "waiting"
            cur = self.conn.execute(
                """INSERT INTO dispatches(task_id, vehicle_id, area_id, status, external_ref,
                   rescheduled_from, created_at, updated_at)
                   VALUES(?,?,?,?,?,NULL,?,?)""",
                (task_id, vehicle_id, area["id"], status, external_ref, now, now),
            )
            dispatch_id = int(cur.lastrowid)
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
            return dict(row), True

    def invalidate_task_dispatches(self, task_id: int) -> List[int]:
        now = utc_now()
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT id FROM dispatches WHERE task_id=? AND status='dispatched'",
                (task_id,),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE dispatches SET status='invalid', updated_at=? WHERE id IN ({placeholders})",
                    [now] + ids,
                )
        return ids

    def reschedule_task_dispatches(self, task_id: int) -> List[tuple]:
        now = utc_now()
        with self._lock, self.conn:
            task = self.get_fire_task(task_id)
            area = self.get_task_area(task["area_id"])
            invalid = self.conn.execute(
                """SELECT d.* FROM dispatches d
                   WHERE d.task_id=? AND d.status='invalid'
                     AND NOT EXISTS (
                       SELECT 1 FROM dispatches r WHERE r.rescheduled_from=d.id)
                   ORDER BY d.id""",
                (task_id,),
            ).fetchall()
            results: List[tuple] = []
            for old in invalid:
                overlap = self._overlapping_dispatched(
                    old["vehicle_id"], task["start_at"], task["end_at"])
                if overlap is None and self._dispatched_count(area["id"]) < area["capacity"]:
                    new_status = "dispatched"
                else:
                    new_status = "waiting"
                new_ref = f"RS-{old['id']}"
                cur = self.conn.execute(
                    """INSERT INTO dispatches(task_id, vehicle_id, area_id, status, external_ref,
                       rescheduled_from, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (task_id, old["vehicle_id"], area["id"], new_status, new_ref,
                     old["id"], now, now),
                )
                results.append((dict(old), new_status))
            return results

    def drain_area(self, area_id: int) -> List[int]:
        now = utc_now()
        promoted: List[int] = []
        with self._lock, self.conn:
            area = self.get_task_area(area_id)
            while self._dispatched_count(area["id"]) < area["capacity"]:
                row = self.conn.execute(
                    """SELECT d.* FROM dispatches d JOIN fire_tasks t ON d.task_id=t.id
                       WHERE d.area_id=? AND d.status='waiting'
                       ORDER BY d.id LIMIT 1""",
                    (area_id,),
                ).fetchone()
                if row is None:
                    break
                task = self.get_fire_task(row["task_id"])
                if self._overlapping_dispatched(
                        row["vehicle_id"], task["start_at"], task["end_at"]) is not None:
                    break
                self.conn.execute(
                    "UPDATE dispatches SET status='dispatched', updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                promoted.append(int(row["id"]))
        return promoted

    def list_dispatches(self, status: Optional[str] = None, area_id: Optional[int] = None,
                         task_id: Optional[int] = None,
                         vehicle_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = """SELECT d.*, v.callsign AS callsign, t.title AS task_title,
                        a.name AS area_name
                 FROM dispatches d
                 JOIN vehicles v ON d.vehicle_id=v.id
                 JOIN fire_tasks t ON d.task_id=t.id
                 JOIN task_areas a ON d.area_id=a.id
                 WHERE 1=1"""
        params: list = []
        if status is not None:
            sql += " AND d.status=?"; params.append(status)
        if area_id is not None:
            sql += " AND d.area_id=?"; params.append(area_id)
        if task_id is not None:
            sql += " AND d.task_id=?"; params.append(task_id)
        if vehicle_id is not None:
            sql += " AND d.vehicle_id=?"; params.append(vehicle_id)
        sql += " ORDER BY d.id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_rescheduled_dispatches(self) -> List[Dict[str, Any]]:
        sql = """SELECT d.*, v.callsign AS callsign, t.title AS task_title,
                        a.name AS area_name
                 FROM dispatches d
                 JOIN vehicles v ON d.vehicle_id=v.id
                 JOIN fire_tasks t ON d.task_id=t.id
                 JOIN task_areas a ON d.area_id=a.id
                 WHERE d.rescheduled_from IS NOT NULL
                 ORDER BY d.id"""
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
        return [dict(row) for row in rows]
