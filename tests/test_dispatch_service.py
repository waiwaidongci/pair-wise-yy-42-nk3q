import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service

SLOT = {"slot_start": "2026-10-05T10:00:00+00:00",
        "slot_end": "2026-10-05T12:00:00+00:00"}
A, R = "dispatcher", "field_commander"


class DispatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "dispatch.db"))
        self.svc = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def vehicle(self, rid, callsign=None, vtype="fire_engine", actor=A, role=R):
        return self.svc.register_vehicle(
            {"request_id": rid, "callsign": callsign or f"V-{rid}",
             "vehicle_type": vtype}, actor, role)

    def zone(self, rid="z1", name="A区", capacity=2, wind="N", grade="high"):
        return self.svc.register_zone(
            {"request_id": rid, "name": name, "capacity": capacity,
             "wind_direction": wind, "fireline_grade": grade}, A, R)

    def task(self, rid, zone_id, vehicles=1, vtype="fire_engine", **slot_extra):
        payload = {"request_id": rid, "title": rid, "zone_id": zone_id,
                   "required_type": vtype, "required_vehicles": vehicles, **SLOT}
        payload.update(slot_extra)
        return self.svc.submit_task(payload, A, R)

    def board(self):
        return self.svc.board("viewer")


class CapacityQueueTest(DispatchBase):
    def test_capacity_bounds_dispatches_rest_queue(self):
        self.vehicle("v1"); self.vehicle("v2"); self.vehicle("v3")
        zid = self.zone(capacity=2)["id"]
        t = self.task("t1", zid, vehicles=3)
        statuses = sorted(d["status"] for d in t["dispatches"])
        self.assertEqual(statuses, ["dispatched", "dispatched", "waiting"])
        b = self.board()
        self.assertEqual(len(b["dispatched"]), 2)
        self.assertEqual(len(b["waiting"]), 1)

    def test_completing_dispatch_pumps_queue_within_capacity(self):
        self.vehicle("v1"); self.vehicle("v2"); self.vehicle("v3")
        zid = self.zone(capacity=2)["id"]
        t = self.task("t1", zid, vehicles=3)
        first_done = next(d["id"] for d in t["dispatches"] if d["status"] == "dispatched")
        self.svc.complete_dispatch(first_done, A, R)
        detail = self.svc.get_task(t["id"], "viewer")
        self.assertEqual(sorted(d["status"] for d in detail["dispatches"]),
                         ["completed", "dispatched", "dispatched"])

    def test_back_to_back_slots_share_vehicle(self):
        vid = self.vehicle("v1")["id"]
        zid = self.zone(capacity=1)["id"]
        self.task("t1", zid, vehicles=1)
        t2 = self.task("t2", zid, vehicles=1,
                       slot_start="2026-10-05T12:00:00+00:00",
                       slot_end="2026-10-05T13:00:00+00:00")
        self.assertEqual(t2["dispatches"][0]["status"], "dispatched")
        self.assertEqual(t2["dispatches"][0]["vehicle_id"], vid)


class MutualExclusionTest(DispatchBase):
    def test_concurrent_submissions_assign_vehicle_once(self):
        self.vehicle("v1")
        zid = self.zone(capacity=10)["id"]
        outcomes = []
        barrier = threading.Barrier(2)

        def submit(rid):
            barrier.wait()
            t = self.task(rid, zid, vehicles=1)
            outcomes.append(t["dispatches"][0]["status"])

        threads = [threading.Thread(target=submit, args=("c1",)),
                   threading.Thread(target=submit, args=("c2",))]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(outcomes), ["dispatched", "waiting"])
        b = self.board()
        self.assertEqual(len(b["dispatched"]), 1)
        self.assertEqual(b["dispatched"][0]["vehicle_id"], 1)
        self.assertEqual(len(b["waiting"]), 1)

    def test_named_vehicle_busy_queues_without_swapping(self):
        self.vehicle("v1"); self.vehicle("v2")
        zid = self.zone(capacity=10)["id"]
        t = self.task("t1", zid, vehicles=1)
        req = self.svc.request_vehicle(t["id"], {
            "request_id": "r1", "vehicle_id": 1, **SLOT}, A, R)
        self.assertEqual(req["status"], "waiting")
        self.assertEqual(req["preferred_vehicle_id"], 1)
        self.assertIsNone(req["vehicle_id"])
        # 空出后点名单拿到它要的车
        first = t["dispatches"][0]["id"]
        self.svc.complete_dispatch(first, A, R)
        got = self.repo.get_dispatch(req["id"])
        self.assertEqual(got["status"], "dispatched")
        self.assertEqual(got["vehicle_id"], 1)

    def test_wrong_vehicle_type_rejected(self):
        self.vehicle("v1", vtype="dozer")
        zid = self.zone(capacity=10)["id"]
        t = self.task("t1", zid, vehicles=1, vtype="water_tanker")
        with self.assertRaises(ConflictError):
            self.svc.request_vehicle(t["id"], {
                "request_id": "r1", "vehicle_id": 1, **SLOT}, A, R)


class EnvironmentInvalidationTest(DispatchBase):
    def test_wind_change_releases_then_fifo_rearranges(self):
        self.vehicle("v1")
        zid = self.zone(capacity=10)["id"]
        t1 = self.task("t1", zid)
        t2 = self.task("t2", zid)
        d1, d2 = t1["dispatches"][0], t2["dispatches"][0]
        self.assertEqual(d1["status"], "dispatched")
        self.assertEqual(d2["status"], "waiting")

        result = self.svc.update_environment(zid, {"wind_direction": "S"}, A, R)
        self.assertTrue(result["changed"])
        self.assertEqual(result["released"], [d1["id"]])
        self.assertEqual([x["id"] for x in result["reassigned"]], [d1["id"]])

        refreshed = {d["id"]: d for d in self.svc.get_task(t1["id"], "viewer")["dispatches"]}
        self.assertEqual(refreshed[d1["id"]]["status"], "dispatched")
        self.assertEqual(refreshed[d1["id"]]["vehicle_id"], 1)
        self.assertEqual(refreshed[d1["id"]]["invalidated"], 1)
        self.assertEqual(refreshed[d1["id"]]["requeue_count"], 1)
        # 后来排队的单不能抢在失效重排单前面
        self.assertEqual(self.repo.get_dispatch(d2["id"])["status"], "waiting")

    def test_fireline_grade_change_also_invalidates(self):
        self.vehicle("v1")
        zid = self.zone(capacity=10)["id"]
        self.task("t1", zid)
        result = self.svc.update_environment(zid, {"fireline_grade": "extreme"}, A, R)
        self.assertEqual(len(result["released"]), 1)

    def test_same_environment_report_is_noop(self):
        self.vehicle("v1")
        zid = self.zone(wind="N", grade="high")["id"]
        self.task("t1", zid)
        result = self.svc.update_environment(zid, {"wind_direction": "N",
                                                   "fireline_grade": "high"}, A, R)
        self.assertFalse(result["changed"])
        self.assertEqual(result["released"], [])

    def test_environment_change_scoped_to_zone(self):
        self.vehicle("v1")
        za = self.zone(rid="za", name="A区", capacity=10)["id"]
        zb = self.zone(rid="zb", name="B区", capacity=10)["id"]
        self.task("t1", za)
        result = self.svc.update_environment(zb, {"wind_direction": "W"}, A, R)
        self.assertEqual(result["released"], [])
        b = self.board()
        self.assertFalse(any(d["zone_id"] == za for d in b["invalidated_rearranged"]))

    def test_no_available_vehicle_after_release_keeps_queue(self):
        # 容量1单车：释放瞬间重排必然重新派回（无其他等待者抢车）。
        # 这里验证两单一车时，先释放再重排不会改变FIFO归属。
        self.vehicle("v1")
        zid = self.zone(capacity=10)["id"]
        t1 = self.task("t1", zid)
        t2 = self.task("t2", zid)
        d1 = t1["dispatches"][0]["id"]
        self.svc.update_environment(zid, {"wind_direction": "E"}, A, R)
        self.assertEqual(self.repo.get_dispatch(d1)["status"], "dispatched")
        self.assertEqual(self.repo.get_dispatch(t2["dispatches"][0]["id"])["status"], "waiting")


class BoardTest(DispatchBase):
    def test_board_shows_waiting_dispatched_and_rearranged(self):
        self.vehicle("v1"); self.vehicle("v2"); self.vehicle("v3")
        zid = self.zone(capacity=2)["id"]
        self.task("t1", zid, vehicles=3)
        self.svc.update_environment(zid, {"wind_direction": "S"}, A, R)
        b = self.board()
        self.assertIn("waiting", b); self.assertIn("dispatched", b)
        self.assertIn("invalidated_rearranged", b)
        self.assertEqual(len(b["waiting"]), 1)
        self.assertEqual(len(b["dispatched"]), 2)
        self.assertTrue(all(d["invalidated"] == 1 for d in b["invalidated_rearranged"]))
        idle = [v for v in b["vehicles"] if v["state"] == "idle"]
        busy = [v for v in b["vehicles"] if v["state"] == "dispatched"]
        self.assertEqual(len(idle), 1)
        self.assertEqual(len(busy), 2)
        kinds = {e["kind"] for e in b["events"]}
        self.assertIn("released", kinds)
        self.assertIn("reassigned", kinds)

    def test_viewer_can_read_but_not_dispatch(self):
        with self.assertRaises(PermissionDenied):
            self.vehicle("vx", role="viewer")


if __name__ == "__main__":
    unittest.main()
