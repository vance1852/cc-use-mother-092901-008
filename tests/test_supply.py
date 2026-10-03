import threading
import unittest
from datetime import datetime, timezone

from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, PermissionDenied, ValidationError
from transport_coordination.service import DomainService
from transport_coordination.storage import Database
from transport_coordination.supply import SupplyService


class SupplyServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, clock)
        self.service = SupplyService(self.database, clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="运营中心")
        self.domain.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="a-op1", actor_id="a1", new_actor_id="op1",
                                   display_name="运营员甲", role="operator", organization_id="o1")
        self.domain.register_actor(request_id="a-op2", actor_id="a1", new_actor_id="op2",
                                   display_name="运营员乙", role="operator", organization_id="o1")
        self.domain.register_actor(request_id="a-auditor", actor_id="a1", new_actor_id="au1",
                                   display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_corridor(request_id="cor", actor_id="op1",
                                       corridor_id="cor1", name="中欧通道")
        self.service.register_group(request_id="grp", actor_id="op1", group_id="grp1",
                                    name="集团甲", cap_per_run=20)
        self.service.register_customer(request_id="cust1", actor_id="op1", customer_id="cust1",
                                       group_id="grp1", name="客户一", cap_per_run=10)
        self.service.register_customer(request_id="cust2", actor_id="op1", customer_id="cust2",
                                       group_id="grp1", name="客户二", cap_per_run=10)
        self.service.register_policy_version(request_id="pol", actor_id="a1",
                                             policy_version_id="pol1", params={}, activate=True)
        self.service.register_policy_version(
            request_id="pol2", actor_id="a1", policy_version_id="pol-fifo",
            params={"factor_order": ["submitted_seq", "strategic_level", "cargo_deadline",
                                     "fulfillment"]}, activate=False)

    def tearDown(self):
        self.database.close()

    def _open_train(self, version_id="v1", capacity=10):
        self.service.register_train_version(
            request_id=f"train-{version_id}", actor_id="op1", version_id=version_id,
            corridor_id="cor1", departure_at="2026-10-10T00:00:00Z",
            freeze_at="2026-10-05T00:00:00Z", capacity=capacity)

    def _freeze(self, version_id="v1"):
        self.service.freeze_demand_book(request_id=f"freeze-{version_id}",
                                        actor_id="op1", version_id=version_id)
        run_id = self.service.list_runs(version_id)[0]["run_id"]
        return self.service.get_run(run_id)

    def _line_by_client_key(self, run, client_key):
        for line in run["lines"]:
            view = self.service.get_demand_view(line["demand_id"])
            if view["client_key"] == client_key:
                return line
        self.fail(f"client_key {client_key} 不在草案中")

    def test_freeze_prefers_medical_over_submission_order(self):
        self._open_train()
        self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                   client_key="normal", customer_id="cust1", quantity=8,
                                   strategic_level=2, cargo_deadline="2026-10-10")
        self.service.submit_demand(request_id="d2", actor_id="op1", version_id="v1",
                                   client_key="medical", customer_id="cust2", quantity=8,
                                   strategic_level=5, cargo_deadline="2026-10-07")
        run = self._freeze()
        normal = self._line_by_client_key(run, "normal")
        medical = self._line_by_client_key(run, "medical")
        self.assertEqual(8, medical["offered_qty"])
        self.assertEqual(2, normal["offered_qty"])
        self.assertIn("RUN_CAPACITY_EXHAUSTED", normal["reasons"])
        self.assertEqual(1, normal["waitlist_position"])

    def test_split_family_and_contract_cap(self):
        self._open_train()
        self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                   client_key="s1", customer_id="cust1", quantity=6,
                                   strategic_level=2, cargo_deadline="2026-10-09",
                                   split_root_id="root-a")
        self.service.submit_demand(request_id="d2", actor_id="op1", version_id="v1",
                                   client_key="s2", customer_id="cust1", quantity=6,
                                   strategic_level=2, cargo_deadline="2026-10-09",
                                   split_root_id="root-a")
        self.service.submit_demand(request_id="d3", actor_id="op1", version_id="v1",
                                   client_key="med", customer_id="cust2", quantity=8,
                                   strategic_level=5, cargo_deadline="2026-10-07")
        run = self._freeze()
        order = [line["priority_rank"] for line in run["lines"]]
        self.assertEqual([1, 2, 3], order)
        offered = [line["offered_qty"] for line in run["lines"]]
        self.assertEqual([8, 2, 0], offered)

    def test_duplicate_request_is_flagged(self):
        self._open_train()
        self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                   client_key="k1", customer_id="cust1", quantity=5,
                                   strategic_level=2, cargo_deadline="2026-10-09")
        receipt = self.service.submit_demand(request_id="d2", actor_id="op1", version_id="v1",
                                             client_key="k2", customer_id="cust1", quantity=5,
                                             strategic_level=2, cargo_deadline="2026-10-09")
        view = self.service.get_demand_view(receipt.resource_id)
        self.assertIn("DUPLICATE_REQUEST", [flag["flag_code"] for flag in view["flags"]])

    def test_group_cap_circumvention_flagged_across_customers(self):
        self._open_train()
        self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                   client_key="k1", customer_id="cust1", quantity=5,
                                   strategic_level=2, cargo_deadline="2026-10-09",
                                   split_root_id="shared-root")
        receipt = self.service.submit_demand(request_id="d2", actor_id="op1", version_id="v1",
                                             client_key="k2", customer_id="cust2", quantity=5,
                                             strategic_level=2, cargo_deadline="2026-10-09",
                                             split_root_id="shared-root")
        view = self.service.get_demand_view(receipt.resource_id)
        self.assertIn("GROUP_CAP_CIRCUMVENTION", [flag["flag_code"] for flag in view["flags"]])

    def test_confirm_then_reject_duplicate_confirm(self):
        self._open_train()
        receipt = self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                             client_key="k1", customer_id="cust1", quantity=5,
                                             strategic_level=5, cargo_deadline="2026-10-07")
        self._freeze()
        first = self.service.confirm_demand(request_id="cf1", actor_id="op1",
                                            demand_id=receipt.resource_id)
        self.assertFalse(first.replayed)
        replay = self.service.confirm_demand(request_id="cf1", actor_id="op1",
                                             demand_id=receipt.resource_id)
        self.assertTrue(replay.replayed)
        with self.assertRaises(ConflictError):
            self.service.confirm_demand(request_id="cf2", actor_id="op1",
                                        demand_id=receipt.resource_id)

    def test_concurrent_confirms_are_serialized(self):
        self._open_train()
        receipt = self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                             client_key="k1", customer_id="cust1", quantity=5,
                                             strategic_level=5, cargo_deadline="2026-10-07")
        self._freeze()
        outcomes = []

        def confirm(tag):
            try:
                self.service.confirm_demand(request_id=f"cf-{tag}", actor_id="op1",
                                            demand_id=receipt.resource_id)
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=confirm, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])

    def test_waiver_reallocates_to_waitlist(self):
        self._open_train(capacity=10)
        self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                   client_key="high", customer_id="cust1", quantity=10,
                                   strategic_level=5, cargo_deadline="2026-10-07")
        low_receipt = self.service.submit_demand(request_id="d2", actor_id="op1", version_id="v1",
                                                 client_key="low", customer_id="cust2", quantity=6,
                                                 strategic_level=2, cargo_deadline="2026-10-10")
        run = self._freeze()
        high_line = self._line_by_client_key(run, "high")
        self.service.confirm_demand(request_id="cf-high", actor_id="op1",
                                    demand_id=high_line["demand_id"])
        self.service.waive_demand(request_id="wv", actor_id="op1",
                                  demand_id=high_line["demand_id"])
        latest = self.service.get_run(self.service.list_runs("v1")[-1]["run_id"])
        low_line = self._line_by_client_key(latest, "low")
        self.assertEqual(6, low_line["offered_qty"])
        self.assertEqual(6, low_line["added_qty"])
        # 放弃的需求退出最新版本。
        self.assertNotIn(high_line["demand_id"], [line["demand_id"] for line in latest["lines"]])
        view = self.service.get_demand_view(low_receipt.resource_id)
        self.assertEqual("waiver", view["promotion_sources"][-1]["trigger"])

    def test_shipped_goods_are_never_reclaimed_on_cancel(self):
        self._open_train(capacity=20)
        receipt = self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                             client_key="k1", customer_id="cust1", quantity=10,
                                             strategic_level=5, cargo_deadline="2026-10-07")
        self._freeze()
        self.service.confirm_demand(request_id="cf", actor_id="op1",
                                    demand_id=receipt.resource_id)
        self.service.record_shipment(request_id="sh", actor_id="op1",
                                     demand_id=receipt.resource_id, shipped_qty=6)
        self.service.cancel_train(request_id="cancel", actor_id="op1", version_id="v1",
                                  reason="口岸关闭")
        latest = self.service.get_run(self.service.list_runs("v1")[-1]["run_id"])
        line = self._line_by_client_key(latest, "k1")
        self.assertEqual(6, line["offered_qty"])
        self.assertEqual(6, line["protected_qty"])
        self.assertEqual(4, line["reduced_qty"])
        with self.assertRaises(ConflictError):
            self.service.record_shipment(request_id="sh-lower", actor_id="op1",
                                         demand_id=receipt.resource_id, shipped_qty=3)

    def test_capacity_recovery_promotes_waitlist_without_double_grant(self):
        self._open_train(capacity=10)
        self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                   client_key="s1", customer_id="cust1", quantity=6,
                                   strategic_level=2, cargo_deadline="2026-10-09",
                                   split_root_id="root-a")
        self.service.submit_demand(request_id="d2", actor_id="op1", version_id="v1",
                                   client_key="s2", customer_id="cust1", quantity=6,
                                   strategic_level=2, cargo_deadline="2026-10-09",
                                   split_root_id="root-a")
        med_receipt = self.service.submit_demand(request_id="d3", actor_id="op1", version_id="v1",
                                                 client_key="med", customer_id="cust2", quantity=8,
                                                 strategic_level=5, cargo_deadline="2026-10-07")
        self._freeze()
        self.service.confirm_demand(request_id="cf", actor_id="op1",
                                    demand_id=med_receipt.resource_id)
        self.service.record_shipment(request_id="sh", actor_id="op1",
                                     demand_id=med_receipt.resource_id, shipped_qty=8)
        self.service.adjust_capacity(request_id="recover", actor_id="op1", version_id="v1",
                                     new_capacity=20, reason="限流解除")
        runs = self.service.list_runs("v1")
        latest = self.service.get_run(runs[-1]["run_id"])
        total = sum(line["offered_qty"] for line in latest["lines"])
        self.assertEqual(18, total)
        self.assertEqual(18, latest["total_offered"])
        check = self.service.recompute_run(latest["run_id"])
        self.assertTrue(check["reproduced"])
        view = self.service.get_demand_view(med_receipt.resource_id)
        self.assertEqual(8, view["latest"]["offered_qty"])
        promoted = [line for line in latest["lines"] if line["added_qty"] > 0]
        self.assertEqual(2, len(promoted))

    def test_exception_requires_second_approver_and_records_impact(self):
        self._open_train(capacity=10)
        receipt = self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                             client_key="k1", customer_id="cust1", quantity=10,
                                             strategic_level=1, cargo_deadline="2026-10-12")
        self._freeze()
        proposed = self.service.propose_exception(request_id="ex1", actor_id="op1",
                                                  version_id="v1", demand_id=receipt.resource_id,
                                                  grant_qty=3, reason="客户追加医疗耗材")
        with self.assertRaises(PermissionDenied):
            self.service.decide_exception(request_id="exd1", actor_id="op1",
                                          exception_id=proposed.resource_id, approve=True)
        decided = self.service.decide_exception(request_id="exd2", actor_id="op2",
                                                exception_id=proposed.resource_id, approve=True)
        self.assertFalse(decided.replayed)
        self.assertTrue(decided.resource_id)
        conn = self.database.connection
        row = conn.execute("SELECT impact_json,status FROM exceptions WHERE exception_id=?",
                           (proposed.resource_id,)).fetchone()
        self.assertEqual("approved", row["status"])
        self.assertIsNotNone(row["impact_json"])
        latest = self.service.get_run(self.service.list_runs("v1")[-1]["run_id"])
        line = self._line_by_client_key(latest, "k1")
        self.assertIn("EXCEPTION_OVERRIDE", line["reasons"])

    def test_reject_exception_does_not_change_allocation(self):
        self._open_train(capacity=10)
        receipt = self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                             client_key="k1", customer_id="cust1", quantity=10,
                                             strategic_level=1, cargo_deadline="2026-10-12")
        self._freeze()
        proposed = self.service.propose_exception(request_id="ex1", actor_id="op1",
                                                  version_id="v1", demand_id=receipt.resource_id,
                                                  grant_qty=3, reason="测试拒绝")
        self.service.decide_exception(request_id="exd", actor_id="op2",
                                      exception_id=proposed.resource_id, approve=False)
        bases = [run["basis"] for run in self.service.list_runs("v1")]
        self.assertEqual(["freeze"], bases)

    def test_policy_comparison_is_read_only(self):
        self._open_train(capacity=10)
        self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                   client_key="normal", customer_id="cust1", quantity=8,
                                   strategic_level=2, cargo_deadline="2026-10-10")
        self.service.submit_demand(request_id="d2", actor_id="op1", version_id="v1",
                                   client_key="medical", customer_id="cust2", quantity=8,
                                   strategic_level=5, cargo_deadline="2026-10-07")
        self._freeze()
        comparison = self.service.compare_policies(
            version_id="v1", policy_version_ids=["pol1", "pol-fifo"])
        self.assertEqual(2, len(comparison["scenarios"]))
        default_scenario = next(s for s in comparison["scenarios"]
                                if s["policy_version_id"] == "pol1")
        fifo_scenario = next(s for s in comparison["scenarios"]
                             if s["policy_version_id"] == "pol-fifo")
        # 两种政策给出不同的医疗满足率。
        self.assertNotEqual(default_scenario["metrics"]["tier_fill_rates"],
                            fifo_scenario["metrics"]["tier_fill_rates"])
        # 对比不改写历史：仍然只有一次 freeze run。
        self.assertEqual(1, len(self.service.list_runs("v1")))

    def test_auditor_cannot_register_supply_data(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_corridor(request_id="x", actor_id="au1",
                                           corridor_id="c2", name="禁止")

    def test_frozen_train_rejects_new_demand(self):
        self._open_train()
        self._freeze()
        with self.assertRaises(ConflictError):
            self.service.submit_demand(request_id="d-late", actor_id="op1", version_id="v1",
                                       client_key="late", customer_id="cust1", quantity=1,
                                       strategic_level=2, cargo_deadline="2026-10-09")

    def test_idempotent_replays_same_resource(self):
        self._open_train()
        first = self.service.submit_demand(request_id="same", actor_id="op1", version_id="v1",
                                           client_key="k1", customer_id="cust1", quantity=5,
                                           strategic_level=2, cargo_deadline="2026-10-09")
        second = self.service.submit_demand(request_id="same", actor_id="op1", version_id="v1",
                                            client_key="k1", customer_id="cust1", quantity=5,
                                            strategic_level=2, cargo_deadline="2026-10-09")
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)

    def test_shipment_cannot_exceed_offered(self):
        self._open_train(capacity=10)
        receipt = self.service.submit_demand(request_id="d1", actor_id="op1", version_id="v1",
                                             client_key="k1", customer_id="cust1", quantity=20,
                                             strategic_level=5, cargo_deadline="2026-10-07")
        self._freeze()
        self.service.confirm_demand(request_id="cf", actor_id="op1",
                                    demand_id=receipt.resource_id)
        with self.assertRaises(ValidationError):
            self.service.record_shipment(request_id="sh", actor_id="op1",
                                         demand_id=receipt.resource_id, shipped_qty=11)


if __name__ == "__main__":
    unittest.main()
