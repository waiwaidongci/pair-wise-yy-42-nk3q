import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError
from src.repository import Repository
from src.service import Service


ACTOR = "commander"
ROLE = "field_commander"
START = "2026-10-05T08:00:00+00:00"
END = "2026-10-05T12:00:00+00:00"
START2 = "2026-10-05T09:00:00+00:00"
END2 = "2026-10-05T11:00:00+00:00"
START_NEXT = "2026-10-06T08:00:00+00:00"
END_NEXT = "2026-10-06T12:00:00+00:00"


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "火线事件", "description": "派车测试", "severity": "high",
             "quantity": 5, "threshold": 10, "external_ref": "ITEM-1"},
            ACTOR, ROLE)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _area(self, name="东线一区", capacity=1):
        return self.service.create_task_area(
            {"name": name, "capacity": capacity}, ACTOR, "incident_commander")

    def _vehicle(self, callsign):
        return self.service.create_vehicle({"callsign": callsign}, ACTOR, ROLE)

    def _task(self, area, title="堵截", wind="N", severity="high",
              start=START, end=END, ref=None):
        payload = {"item_id": self.item["id"], "area_id": area["id"], "title": title,
                   "wind_direction": wind, "severity": severity,
                   "start_at": start, "end_at": end}
        if ref:
            payload["external_ref"] = ref
        return self.service.create_fire_task(payload, ACTOR, ROLE)

    def _set_wind(self, task, wind, expected_version=None):
        version = expected_version if expected_version is not None else task["version"]
        return self.service.update_fire_task(
            task["id"], {"wind_direction": wind}, version, ACTOR, ROLE)

    def test_dispatch_then_queue_when_capacity_full(self):
        area = self._area(capacity=1)
        task = self._task(area)
        v1 = self._vehicle("川A·1001")
        v2 = self._vehicle("川A·1002")
        d1 = self.service.dispatch_vehicle(task["id"], v1["id"], "D-1", ACTOR, ROLE)
        self.assertEqual(d1["status"], "dispatched")
        d2 = self.service.dispatch_vehicle(task["id"], v2["id"], "D-2", ACTOR, ROLE)
        self.assertEqual(d2["status"], "waiting")
        console = self.service.dispatch_console("viewer")
        self.assertEqual(len(console["dispatched"]), 1)
        self.assertEqual(len(console["waiting"]), 1)
        self.assertEqual(console["dispatched"][0]["callsign"], "川A·1001")

    def test_same_vehicle_one_valid_task_per_period(self):
        area = self._area(capacity=2)
        t1 = self._task(area, title="东线")
        t2 = self._task(area, title="西线", start=START2, end=END2)
        v1 = self._vehicle("川A·2001")
        self.service.dispatch_vehicle(t1["id"], v1["id"], "D-1", ACTOR, ROLE)
        with self.assertRaises(ConflictError):
            self.service.dispatch_vehicle(t2["id"], v1["id"], "D-2", ACTOR, ROLE)
        # 非重叠时段可以接第二个任务
        t3 = self._task(area, title="次日", start=START_NEXT, end=END_NEXT)
        d3 = self.service.dispatch_vehicle(t3["id"], v1["id"], "D-3", ACTOR, ROLE)
        self.assertEqual(d3["status"], "dispatched")

    def test_wind_change_invalidates_then_reschedules(self):
        area = self._area(capacity=1)
        task = self._task(area)
        v1 = self._vehicle("川A·3001")
        v2 = self._vehicle("川A·3002")
        self.service.dispatch_vehicle(task["id"], v1["id"], "D-1", ACTOR, ROLE)
        self.service.dispatch_vehicle(task["id"], v2["id"], "D-2", ACTOR, ROLE)
        updated = self._set_wind(task, "S")
        self.assertEqual(updated["version"], 2)
        dispatches = self.service.list_dispatches(ROLE, task_id=task["id"])
        by_ref = {d["external_ref"]: d for d in dispatches}
        self.assertEqual(by_ref["D-1"]["status"], "invalid")
        self.assertEqual(by_ref["D-2"]["status"], "waiting")
        rs = [d for d in dispatches if d["rescheduled_from"] is not None]
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["status"], "dispatched")
        self.assertEqual(rs[0]["vehicle_id"], v1["id"])
        console = self.service.dispatch_console("viewer")
        self.assertEqual(len(console["dispatched"]), 1)
        self.assertEqual(len(console["waiting"]), 1)
        self.assertEqual(len(console["rescheduled"]), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_severity_change_invalidates_then_reschedules(self):
        area = self._area(capacity=1)
        task = self._task(area, severity="high")
        v1 = self._vehicle("川A·4001")
        self.service.dispatch_vehicle(task["id"], v1["id"], "D-1", ACTOR, ROLE)
        updated = self.service.update_fire_task(
            task["id"], {"severity": "extreme"}, task["version"], ACTOR, ROLE)
        self.assertEqual(updated["version"], 2)
        dispatches = self.service.list_dispatches(ROLE, task_id=task["id"])
        self.assertEqual(dispatches[0]["status"], "invalid")
        rs = [d for d in dispatches if d["rescheduled_from"] is not None]
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["status"], "dispatched")

    def test_duplicate_command_does_not_occupy_more_vehicles(self):
        area = self._area(capacity=2)
        task = self._task(area)
        v1 = self._vehicle("川A·5001")
        d1 = self.service.dispatch_vehicle(task["id"], v1["id"], "DUP-1", ACTOR, ROLE)
        d2 = self.service.dispatch_vehicle(task["id"], v1["id"], "DUP-1", ACTOR, ROLE)
        self.assertEqual(d1["id"], d2["id"])
        self.assertEqual(d2["status"], "dispatched")
        # 容量仍只被占用1个，重复指令没有多占车
        dispatched = self.service.list_dispatches(ROLE, status="dispatched", area_id=area["id"])
        self.assertEqual(len(dispatched), 1)
        total = self.service.list_dispatches(ROLE, area_id=area["id"])
        self.assertEqual(len(total), 1)

    def test_failed_write_keeps_original_schedule(self):
        area = self._area(capacity=2)
        t1 = self._task(area, title="东线")
        t2 = self._task(area, title="西线", start=START2, end=END2)
        v1 = self._vehicle("川A·6001")
        original = self.service.dispatch_vehicle(t1["id"], v1["id"], "D-1", ACTOR, ROLE)
        with self.assertRaises(ConflictError):
            self.service.dispatch_vehicle(t2["id"], v1["id"], "D-2", ACTOR, ROLE)
        # 写入失败后原排班保留
        current = self.service.list_dispatches(ROLE, task_id=t1["id"])
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["id"], original["id"])
        self.assertEqual(current[0]["status"], "dispatched")

    def test_reschedule_only_incomplete_tasks(self):
        area = self._area(capacity=1)
        task = self._task(area)
        v1 = self._vehicle("川A·7001")
        self.service.dispatch_vehicle(task["id"], v1["id"], "D-1", ACTOR, ROLE)
        # 已完成任务不再重排
        done = self.service.update_fire_task(
            task["id"], {"status": "done"}, task["version"], ACTOR, ROLE)
        with self.assertRaises(ConflictError):
            self.service.reschedule_task(done["id"], ACTOR, ROLE)
        dispatches = self.service.list_dispatches(ROLE, task_id=task["id"])
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]["status"], "dispatched")

    def test_drain_promotes_waiting_after_release(self):
        area = self._area(capacity=1)
        task = self._task(area)
        v1 = self._vehicle("川A·8001")
        v2 = self._vehicle("川A·8002")
        self.service.dispatch_vehicle(task["id"], v1["id"], "D-1", ACTOR, ROLE)
        self.service.dispatch_vehicle(task["id"], v2["id"], "D-2", ACTOR, ROLE)
        self.repo.invalidate_task_dispatches(task["id"])
        promoted = self.repo.drain_area(area["id"])
        self.assertEqual(len(promoted), 1)
        after = self.service.list_dispatches(ROLE, area_id=area["id"])
        by_vehicle = {d["vehicle_id"]: d for d in after}
        self.assertEqual(by_vehicle[v1["id"]]["status"], "invalid")
        self.assertEqual(by_vehicle[v2["id"]]["status"], "dispatched")

    def test_concurrent_dispatch_no_overbooking(self):
        area = self._area(capacity=1)
        task = self._task(area)
        vehicles = [self._vehicle(f"川A·{9000 + i}") for i in range(10)]
        results = []
        errors = []

        def do(vehicle):
            try:
                results.append(self.service.dispatch_vehicle(
                    task["id"], vehicle["id"], f"REF-{vehicle['id']}", ACTOR, ROLE))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do, args=(v,)) for v in vehicles]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(errors), 0)
        dispatched = [d for d in results if d["status"] == "dispatched"]
        waiting = [d for d in results if d["status"] == "waiting"]
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(len(waiting), 9)
        count = self.service.list_dispatches(ROLE, status="dispatched", area_id=area["id"])
        self.assertEqual(len(count), 1)

    def test_concurrent_same_vehicle_rejected(self):
        area = self._area(capacity=2)
        t1 = self._task(area, title="东线")
        t2 = self._task(area, title="西线", start=START2, end=END2)
        v1 = self._vehicle("川A·9100")
        results = []
        errors = []

        def do(task, ref):
            try:
                results.append(self.service.dispatch_vehicle(
                    task["id"], v1["id"], ref, ACTOR, ROLE))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=do, args=(t1, "R-1")),
            threading.Thread(target=do, args=(t2, "R-2")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)


if __name__ == "__main__":
    unittest.main()
