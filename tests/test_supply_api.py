import unittest
from datetime import datetime, timezone

from transport_coordination.api import route
from transport_coordination.clock import FixedClock
from transport_coordination.service import DomainService
from transport_coordination.storage import Database
from transport_coordination.supply import SupplyService


class SupplyApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.service.supply = SupplyService(self.database, clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="运营中心")
        self.service.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                   display_name="运营员", role="operator", organization_id="o1")
        self.headers = {"X-Actor-Id": "op1"}
        self.admin_headers = {"X-Actor-Id": "a1"}
        route(self.service, "POST", "/corridors", {"request_id": "cor", "corridor_id": "cor1",
                                                    "name": "中欧通道"}, self.headers)
        route(self.service, "POST", "/customer-groups",
              {"request_id": "grp", "group_id": "grp1", "name": "集团甲", "cap_per_run": 20},
              self.headers)
        route(self.service, "POST", "/customers",
              {"request_id": "cu1", "customer_id": "cust1", "group_id": "grp1",
               "name": "客户一", "cap_per_run": 10}, self.headers)
        route(self.service, "POST", "/customers",
              {"request_id": "cu2", "customer_id": "cust2", "group_id": "grp1",
               "name": "客户二", "cap_per_run": 10}, self.headers)
        route(self.service, "POST", "/policy-versions",
              {"request_id": "pol", "policy_version_id": "pol1", "params": {}, "activate": True},
              self.admin_headers)
        route(self.service, "POST", "/train-versions",
              {"request_id": "trn", "version_id": "v1", "corridor_id": "cor1",
               "departure_at": "2026-10-10T00:00:00Z", "freeze_at": "2026-10-05T00:00:00Z",
               "capacity": 10}, self.headers)

    def tearDown(self):
        self.database.close()

    def test_full_lifecycle_routes(self):
        status, body = route(self.service, "POST", "/demands",
                             {"request_id": "d1", "version_id": "v1", "client_key": "normal",
                              "customer_id": "cust1", "quantity": 8, "strategic_level": 2,
                              "cargo_deadline": "2026-10-10"}, self.headers)
        self.assertEqual(201, status)
        normal_id = body["resource_id"]
        status, body = route(self.service, "POST", "/demands",
                             {"request_id": "d2", "version_id": "v1", "client_key": "medical",
                              "customer_id": "cust2", "quantity": 8, "strategic_level": 5,
                              "cargo_deadline": "2026-10-07"}, self.headers)
        self.assertEqual(201, status)
        medical_id = body["resource_id"]

        status, body = route(self.service, "POST", "/train-versions/v1/freeze",
                             {"request_id": "frz"}, self.headers)
        self.assertEqual(201, status)
        run_id = body["resource_id"]

        status, run = route(self.service, "GET", f"/runs/{run_id}", None, self.headers)
        self.assertEqual(200, status)
        self.assertEqual(medical_id, run["lines"][0]["demand_id"])
        self.assertEqual(8, run["lines"][0]["offered_qty"])

        status, body = route(self.service, "POST", f"/demands/{medical_id}/confirm",
                             {"request_id": "cf1"}, self.headers)
        self.assertEqual(201, status)

        status, view = route(self.service, "GET", f"/demands/{normal_id}", None, self.headers)
        self.assertEqual(200, status)
        self.assertEqual(1, view["latest"]["waitlist_position"])
        self.assertIn("RUN_CAPACITY_EXHAUSTED", view["latest"]["reasons"])

        # 重复确认返回 409。
        status, body = route(self.service, "POST", f"/demands/{medical_id}/confirm",
                             {"request_id": "cf2"}, self.headers)
        self.assertEqual(409, status)

    def test_capacity_recovery_and_recompute_routes(self):
        route(self.service, "POST", "/demands",
              {"request_id": "d1", "version_id": "v1", "client_key": "k1", "customer_id": "cust1",
               "quantity": 20, "strategic_level": 1, "cargo_deadline": "2026-10-12"}, self.headers)
        route(self.service, "POST", "/train-versions/v1/freeze", {"request_id": "frz"}, self.headers)
        status, body = route(self.service, "POST", "/train-versions/v1/capacity",
                             {"request_id": "cap", "new_capacity": 20, "reason": "限流解除"},
                             self.headers)
        self.assertEqual(201, status)
        status, body = route(self.service, "GET", "/runs?version_id=v1", None, self.headers)
        self.assertEqual(200, status)
        run_id = body["items"][-1]["run_id"]
        status, check = route(self.service, "GET", f"/runs/{run_id}/recompute", None, self.headers)
        self.assertEqual(200, status)
        self.assertTrue(check["reproduced"])

    def test_policy_comparison_route(self):
        route(self.service, "POST", "/policy-versions",
              {"request_id": "pol2", "policy_version_id": "pol-fifo",
               "params": {"factor_order": ["submitted_seq"]}, "activate": False},
              self.admin_headers)
        route(self.service, "POST", "/demands",
              {"request_id": "d1", "version_id": "v1", "client_key": "k1", "customer_id": "cust1",
               "quantity": 10, "strategic_level": 5, "cargo_deadline": "2026-10-07"}, self.headers)
        route(self.service, "POST", "/train-versions/v1/freeze", {"request_id": "frz"}, self.headers)
        status, body = route(
            self.service, "GET",
            "/policy-comparison?version_id=v1&policy_version_ids=pol1,pol-fifo", None, self.headers)
        self.assertEqual(200, status)
        self.assertEqual(2, len(body["scenarios"]))

    def test_write_without_actor_is_rejected(self):
        status, body = route(self.service, "POST", "/corridors",
                             {"request_id": "x", "corridor_id": "c2", "name": "无操作者"})
        self.assertEqual(404, status)

    def test_exception_two_person_route(self):
        _, body = route(self.service, "POST", "/demands",
                        {"request_id": "d1", "version_id": "v1", "client_key": "k1",
                         "customer_id": "cust1", "quantity": 10, "strategic_level": 1,
                         "cargo_deadline": "2026-10-12"}, self.headers)
        demand_id = body["resource_id"]
        route(self.service, "POST", "/train-versions/v1/freeze", {"request_id": "frz"}, self.headers)
        status, body = route(self.service, "POST", "/exceptions",
                             {"request_id": "ex1", "version_id": "v1", "demand_id": demand_id,
                              "grant_qty": 2, "reason": "应急医疗"}, self.headers)
        self.assertEqual(201, status)
        exception_id = body["resource_id"]
        # 提议人不能自己审批。
        status, body = route(self.service, "POST", f"/exceptions/{exception_id}/decision",
                             {"request_id": "d-self", "approve": True}, self.headers)
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
