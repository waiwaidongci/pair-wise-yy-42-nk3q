import tempfile
import unittest
from pathlib import Path

from src import repository as repomod
from src.domain import ConflictError
from src.repository import Repository
from src.service import Service

SLOT = {"slot_start": "2026-10-05T10:00:00+00:00",
        "slot_end": "2026-10-05T12:00:00+00:00"}
A, R = "dispatcher", "field_commander"


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "offline.db"))
        self.svc = Service(self.repo)
        self.svc.register_vehicle({"request_id": "v1", "callsign": "V-1",
                                   "vehicle_type": "fire_engine"}, A, R)
        self.zid = self.svc.register_zone(
            {"request_id": "z1", "name": "A区", "capacity": 5,
             "wind_direction": "N", "fireline_grade": "high"}, A, R)["id"]

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _task_payload(self, rid, vehicles=1, **extra):
        payload = {"request_id": rid, "title": rid, "zone_id": self.zid,
                   "required_type": "fire_engine", "required_vehicles": vehicles, **SLOT}
        payload.update(extra)
        return payload

    def test_duplicate_command_does_not_double_allocate(self):
        payload = self._task_payload("t1")
        first = self.svc.submit_task(payload, A, R)
        replay = self.svc.submit_task(payload, A, R)
        self.assertEqual(first["id"], replay["id"])
        b = self.svc.board("viewer")
        self.assertEqual(len(b["dispatched"]), 1)

    def test_same_request_id_with_different_payload_conflicts(self):
        self.svc.submit_task(self._task_payload("t1", vehicles=1), A, R)
        with self.assertRaises(ConflictError):
            self.svc.submit_task(self._task_payload("t1", vehicles=2), A, R)

    def test_write_failure_rolls_back_original_schedule_preserved(self):
        # 既有排班：一辆车已派出
        self.svc.submit_task(self._task_payload("existing"), A, R)
        before = self.svc.board("viewer")

        original = repomod.Repository._audit_locked

        def fail_once(self, *args, **kwargs):
            # 模拟断网造成事务写入失败
            raise RuntimeError("network down")

        repomod.Repository._audit_locked = fail_once
        try:
            with self.assertRaises(RuntimeError):
                self.svc.submit_task(self._task_payload("failed", vehicles=2), A, R)
        finally:
            repomod.Repository._audit_locked = original

        # 原排班保留，失败任务不留任何痕迹
        after = self.svc.board("viewer")
        self.assertEqual(len(after["dispatched"]), len(before["dispatched"]))
        self.assertEqual(len(after["waiting"]), len(before["waiting"]))
        self.assertIsNone(self.repo.find_task_by_request("failed"))

    def test_retry_after_recovery_completes_once(self):
        # 第一次写失败，恢复后同指令重试成功，且只占一次车
        original = repomod.Repository._audit_locked
        calls = {"n": 0}

        def fail_first(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("network down")
            return original(self, *args, **kwargs)

        repomod.Repository._audit_locked = fail_first
        try:
            with self.assertRaises(RuntimeError):
                self.svc.submit_task(self._task_payload("t1"), A, R)
        finally:
            repomod.Repository._audit_locked = original
        task = self.svc.submit_task(self._task_payload("t1"), A, R)
        self.assertEqual(task["dispatches"][0]["status"], "dispatched")
        b = self.svc.board("viewer")
        self.assertEqual(len(b["dispatched"]), 1)

    def test_sync_replays_only_outstanding_commands(self):
        done = self._task_payload("done")
        self.svc.submit_task(done, A, R)
        results = self.svc.sync_commands([
            {"op": "submit_task", "payload": done},               # 已完成 -> 幂等回放
            {"op": "register_vehicle", "payload": {
                "request_id": "v2", "callsign": "V-2", "vehicle_type": "dozer"}},  # 未完成 -> 补
            {"op": "submit_task", "payload": {
                "request_id": "new", "title": "new", "zone_id": self.zid,
                "required_type": "fire_engine", "required_vehicles": 1, **SLOT}},  # 未完成 -> 补
            {"op": "unknown_op", "payload": {}},                  # 坏指令隔离
        ], A, R)
        self.assertTrue(all(r["ok"] for r in results[:3]))
        self.assertFalse(results[3]["ok"])
        # 再次补传完全幂等：已完成指令不重复执行、不多占车
        before = self.svc.board("viewer")
        again = self.svc.sync_commands([
            {"op": "submit_task", "payload": done},
            {"op": "register_vehicle", "payload": {
                "request_id": "v2", "callsign": "V-2", "vehicle_type": "dozer"}},
        ], A, R)
        self.assertTrue(all(r["ok"] for r in again))
        after = self.svc.board("viewer")
        self.assertEqual(len(after["dispatched"]), len(before["dispatched"]))
        self.assertEqual(len(after["waiting"]), len(before["waiting"]))
        self.assertEqual(len(after["vehicles"]), 2)

    def test_sync_bad_command_does_not_block_others(self):
        results = self.svc.sync_commands([
            {"op": "bogus"},
            {"op": "register_vehicle", "payload": {
                "request_id": "v9", "callsign": "V-9", "vehicle_type": "support"}},
        ], A, R)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[1]["ok"])
        self.assertEqual(len(self.svc.list_vehicles("viewer")), 2)

    def test_replay_named_vehicle_request_idempotent(self):
        task = self.svc.submit_task(self._task_payload("t1", vehicles=1), A, R)
        body = {"request_id": "r1", "vehicle_id": 1, **SLOT}
        first = self.svc.request_vehicle(task["id"], body, A, R)
        again = self.svc.request_vehicle(task["id"], body, A, R)
        self.assertEqual(first["id"], again["id"])
        detail = self.svc.get_task(task["id"], "viewer")
        self.assertEqual(len(detail["dispatches"]), 2)


if __name__ == "__main__":
    unittest.main()
