import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service

ROOT = Path(__file__).resolve().parent.parent


class HttpSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "http.db"))
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(Service(self.repo), str(ROOT / "static")))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.repo.close()
        self.tmp.cleanup()

    def call(self, method, path, payload=None, role="field_commander", actor="d"):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json",
                     "X-Actor": actor, "X-Role": role})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_dispatch_lifecycle_over_http(self):
        st, vehicle = self.call("POST", "/api/vehicles",
                                {"request_id": "hv", "callsign": "HV-1",
                                 "vehicle_type": "fire_engine"})
        self.assertEqual(st, 201)
        st, zone = self.call("POST", "/api/zones",
                             {"request_id": "hz", "name": "南区", "capacity": 1})
        self.assertEqual(st, 201)
        task = {"request_id": "ht", "title": "南线", "zone_id": zone["id"],
                "required_type": "fire_engine", "required_vehicles": 1,
                "slot_start": "2026-10-06T08:00:00Z",
                "slot_end": "2026-10-06T10:00:00Z"}
        st, created = self.call("POST", "/api/tasks", task)
        self.assertEqual(st, 201)
        self.assertEqual(created["dispatches"][0]["status"], "dispatched")
        self.assertEqual(created["dispatches"][0]["vehicle_id"], vehicle["id"])

        # 重复指令不重复派车
        st, replay = self.call("POST", "/api/tasks", task)
        self.assertEqual(replay["id"], created["id"])
        st, board = self.call("GET", "/api/board", role="viewer")
        self.assertEqual(st, 200)
        self.assertEqual(len(board["dispatched"]), 1)

        # 环境变化失效重排
        st, env = self.call("POST", f"/api/zones/{zone['id']}/environment",
                            {"wind_direction": "W"})
        self.assertEqual(st, 200)
        self.assertEqual(env["released"], [created["dispatches"][0]["id"]])
        self.assertEqual([x["id"] for x in env["reassigned"]],
                         [created["dispatches"][0]["id"]])

        # 完成
        st, done = self.call("POST",
                             f"/api/dispatches/{created['dispatches'][0]['id']}/complete", {})
        self.assertEqual(st, 200)
        self.assertEqual(done["status"], "completed")

        # 无权限
        st, err = self.call("POST", "/api/vehicles",
                            {"request_id": "no", "callsign": "X",
                             "vehicle_type": "dozer"}, role="viewer")
        self.assertEqual(st, 403)
        self.assertEqual(err["error"], "PermissionDenied")

        # 校验错误
        st, err = self.call("POST", "/api/vehicles",
                            {"request_id": "bad", "callsign": "X", "vehicle_type": "ufo"})
        self.assertEqual(st, 422)

    def test_sync_endpoint(self):
        self.call("POST", "/api/zones",
                  {"request_id": "hz", "name": "南区", "capacity": 3})
        payload = {"commands": [
            {"op": "register_vehicle", "payload": {
                "request_id": "v1", "callsign": "V-1", "vehicle_type": "dozer"}},
            {"op": "submit_task", "payload": {
                "request_id": "t1", "title": "推隔离带", "zone_id": 1,
                "required_type": "dozer", "required_vehicles": 1,
                "slot_start": "2026-10-06T08:00:00Z",
                "slot_end": "2026-10-06T10:00:00Z"}},
        ]}
        st, resp = self.call("POST", "/api/sync", payload)
        self.assertEqual(st, 200)
        self.assertTrue(all(r["ok"] for r in resp["results"]))


if __name__ == "__main__":
    unittest.main()
