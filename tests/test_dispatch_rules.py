import unittest

from src.rules import build_assignment_plan, slots_overlap


def d(dispatch_id, zone_id=1, vehicle_id=None, required_type=None,
      start="2026-10-05T10:00:00+00:00", end="2026-10-05T12:00:00+00:00",
      preferred_vehicle_id=None):
    return {"id": dispatch_id, "task_id": dispatch_id, "zone_id": zone_id,
            "vehicle_id": vehicle_id, "required_type": required_type,
            "preferred_vehicle_id": preferred_vehicle_id,
            "slot_start": start, "slot_end": end}


def vehicle(vid, vtype="fire_engine", active=1):
    return {"id": vid, "vehicle_type": vtype, "active": active}


def zone(zid=1, capacity=2):
    return {"id": zid, "capacity": capacity}


class DispatchPlanTest(unittest.TestCase):
    def test_back_to_back_slots_are_not_overlapping(self):
        self.assertFalse(slots_overlap("2026-10-05T10:00:00+00:00", "2026-10-05T12:00:00+00:00",
                                       "2026-10-05T12:00:00+00:00", "2026-10-05T13:00:00+00:00"))
        self.assertTrue(slots_overlap("2026-10-05T10:00:00+00:00", "2026-10-05T12:00:00+00:00",
                                      "2026-10-05T11:00:00+00:00", "2026-10-05T13:00:00+00:00"))

    def test_capacity_full_queues_and_existing_assignments_hold(self):
        # 两辆车、容量2，已有两个在派；第三个单必须排队
        existing = [d(1, vehicle_id=1), d(2, vehicle_id=2)]
        plan = build_assignment_plan([d(3)], existing,
                                     {1: vehicle(1), 2: vehicle(2)}, {1: zone(capacity=2)})
        self.assertEqual(plan, {})

    def test_capacity_frees_after_completion(self):
        plan = build_assignment_plan([d(3)], [d(1, vehicle_id=1)],
                                     {1: vehicle(1), 2: vehicle(2)}, {1: zone(capacity=2)})
        self.assertEqual(plan, {3: 2})

    def test_same_vehicle_same_slot_never_double_booked(self):
        # 容量给得很大，瓶颈是车辆：一辆车同一时段只能给一个单
        plan = build_assignment_plan([d(1), d(2)], [], {1: vehicle(1)},
                                     {1: zone(capacity=10)})
        self.assertEqual(plan, {1: 1})

    def test_vehicle_type_must_match(self):
        plan = build_assignment_plan([d(1, required_type="dozer")], [],
                                     {1: vehicle(1, "fire_engine")}, {1: zone()})
        self.assertEqual(plan, {})
        plan = build_assignment_plan([d(1, required_type="dozer")], [],
                                     {1: vehicle(1, "fire_engine"), 2: vehicle(2, "dozer")},
                                     {1: zone()})
        self.assertEqual(plan, {1: 2})

    def test_preferred_vehicle_waits_instead_of_swap(self):
        # 点名2号车，但2号车正忙；不能偷偷换1号车
        busy = [d(9, vehicle_id=2)]
        plan = build_assignment_plan([d(1, preferred_vehicle_id=2)], busy,
                                     {1: vehicle(1), 2: vehicle(2)}, {1: zone(capacity=10)})
        self.assertEqual(plan, {})
        # 点名车空闲且车型匹配 -> 派给它
        plan = build_assignment_plan([d(1, preferred_vehicle_id=2)], [],
                                     {1: vehicle(1), 2: vehicle(2)}, {1: zone(capacity=10)})
        self.assertEqual(plan, {1: 2})

    def test_fifo_order(self):
        plan = build_assignment_plan([d(1), d(2)], [],
                                     {7: vehicle(7)}, {1: zone(capacity=10)})
        self.assertEqual(list(plan), [1])


if __name__ == "__main__":
    unittest.main()
