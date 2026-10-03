import json
import unittest
from datetime import datetime, timezone

from transport_coordination.api import route
from transport_coordination.clock import FixedClock
from transport_coordination.storage import Database
from transport_coordination.supply_service import SupplyService


class SupplyApiTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.svc = SupplyService(self.db, FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
        self.call("POST", "/organizations", "bootstrap",
                  {"request_id": "ro", "organization_id": "o1", "name": "运营中心"})
        self.call("POST", "/actors", "bootstrap",
                  {"request_id": "ra", "new_actor_id": "admin", "display_name": "管理员",
                   "role": "admin", "organization_id": "o1"})
        self.call("POST", "/actors", "admin",
                  {"request_id": "rop", "new_actor_id": "op", "display_name": "操作员",
                   "role": "operator", "organization_id": "o1"})
        self.call("POST", "/actors", "admin",
                  {"request_id": "rr", "new_actor_id": "rev", "display_name": "复核员",
                   "role": "reviewer", "organization_id": "o1"})

    def tearDown(self):
        self.db.close()

    def call(self, method, path, actor, body=None):
        status, payload = route(self.svc, method, path, body or {},
                                {"X-Actor-Id": actor})
        return status, payload

    def _ready(self):
        self.call("POST", "/corridors", "op",
                  {"request_id": "rc", "corridor_id": "cor1", "name": "通道"})
        self.call("POST", "/policies", "op",
                  {"request_id": "rp", "policy_id": "std", "params": {"group_cap_ratio": 0.5},
                   "description": "标准"})
        self.call("POST", "/policy-activations", "op",
                  {"request_id": "rap", "corridor_id": "cor1", "policy_id": "std"})
        self.call("POST", "/customers", "op",
                  {"request_id": "rc1", "customer_id": "c1", "group_id": "g1", "name": "客户一",
                   "assurance_level": "strategic", "contract_priority": 80, "performance_score": 0.9})
        self.call("POST", "/departures", "op",
                  {"request_id": "rd", "departure_id": "d1", "corridor_id": "cor1",
                   "sequence_no": 1, "cutoff_at": "2026-10-05T00:00:00Z",
                   "departs_at": "2026-10-06T00:00:00Z", "capacity": 20})
        self.call("POST", "/demands", "op",
                  {"request_id": "r1", "departure_id": "d1", "demand_id": "q1", "customer_id": "c1",
                   "cargo_class": "medical", "quantity": 10,
                   "latest_load_at": "2026-10-04T00:00:00Z"})

    def test_full_flow_over_http(self):
        self._ready()
        status, payload = self.call("POST", "/departures/d1/freeze", "op", {"request_id": "rf"})
        self.assertEqual(200, status)
        run_id = payload["resource_id"]

        status, payload = self.call("GET", "/departures/d1/entitlements", "op")
        self.assertEqual(200, status)
        self.assertEqual(10, payload["items"][0]["offered_qty"])

        status, payload = self.call("POST", "/demands/q1/confirm", "op",
                                    {"request_id": "cf", "departure_id": "d1", "confirm_qty": 10})
        self.assertEqual(200, status)

        status, payload = self.call("GET", "/demands/q1/explain", "op")
        self.assertEqual(200, status)
        self.assertEqual("confirmed", payload["latest"]["status"])

        status, payload = self.call("GET", f"/runs/{run_id}", "op")
        self.assertEqual(200, status)
        self.assertIn("lines", payload["result"])

    def test_four_eyes_and_impact_via_http(self):
        self._ready()
        self.call("POST", "/departures/d1/freeze", "op", {"request_id": "rf"})
        status, payload = self.call("POST", "/exceptions", "admin",
                                    {"request_id": "ex", "departure_id": "d1", "demand_id": "q1",
                                     "extra_qty": 3, "justification": "追加"})
        self.assertEqual(201, status)
        exid = payload["resource_id"]
        status, payload = self.call("POST", f"/exceptions/{exid}/decision", "admin",
                                    {"request_id": "self", "approved": True})
        self.assertEqual(403, status)
        status, payload = self.call("POST", f"/exceptions/{exid}/decision", "rev",
                                    {"request_id": "ok", "approved": True, "decision_note": "同意"})
        self.assertEqual(200, status)
        self.assertIn("impact", payload["exception"])

    def test_fairness_and_replay_endpoints(self):
        self._ready()
        self.call("POST", "/departures/d1/freeze", "op", {"request_id": "rf"})
        self.call("POST", "/policies", "op",
                  {"request_id": "rp2", "policy_id": "alt", "params": {"group_cap_ratio": 0.9},
                   "description": "宽松"})
        status, payload = self.call("GET", "/departures/d1/fairness", "op")
        self.assertEqual(200, status)
        policies = {c["policy_id"] for c in payload["comparisons"]}
        self.assertIn("std", policies)
        self.assertIn("alt", policies)

    def test_supply_routes_absent_on_base_service(self):
        from transport_coordination.service import DomainService
        base = DomainService(self.db)
        status, payload = route(base, "GET", "/departures/d1/entitlements", None, {})
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
