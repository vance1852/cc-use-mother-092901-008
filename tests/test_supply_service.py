import unittest
from datetime import datetime, timezone

from transport_coordination.clock import FixedClock
from transport_coordination.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)
from transport_coordination.supply_service import SupplyService
from transport_coordination.storage import Database


class SupplyServiceTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.svc = SupplyService(self.db, FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
        s = self.svc
        s.register_organization(request_id="ro", actor_id="bootstrap", organization_id="o1", name="运营中心")
        s.register_actor(request_id="ra", actor_id="bootstrap", new_actor_id="admin",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="rop", actor_id="admin", new_actor_id="op",
                         display_name="操作员", role="operator", organization_id="o1")
        s.register_actor(request_id="rr", actor_id="admin", new_actor_id="rev",
                         display_name="复核员", role="reviewer", organization_id="o1")
        s.register_actor(request_id="rau", actor_id="admin", new_actor_id="au",
                         display_name="审计员", role="auditor", organization_id="o1")
        s.register_corridor(request_id="rc", actor_id="op", corridor_id="cor1", name="通道一")
        s.register_policy(request_id="rp", actor_id="op", policy_id="std",
                          params={"group_cap_ratio": 0.5}, description="标准")
        s.activate_policy(request_id="rap", actor_id="op", corridor_id="cor1", policy_id="std")
        s.register_customer(request_id="rc1", actor_id="op", customer_id="cA1", group_id="gA",
                            name="A1", assurance_level="standard", contract_priority=10, performance_score=0.5)
        s.register_customer(request_id="rc2", actor_id="op", customer_id="cA2", group_id="gA",
                            name="A2", assurance_level="standard", contract_priority=10, performance_score=0.5)
        s.register_customer(request_id="rc3", actor_id="op", customer_id="cB1", group_id="gB",
                            name="B医疗", assurance_level="strategic", contract_priority=90,
                            performance_score=0.9)

    def tearDown(self):
        self.db.close()

    def _departure(self, capacity=60, dep="d1"):
        self.svc.register_departure(
            request_id=f"rd-{dep}", actor_id="op", departure_id=dep, corridor_id="cor1",
            sequence_no=1, cutoff_at="2026-10-05T00:00:00Z",
            departs_at="2026-10-06T00:00:00Z", capacity=capacity)

    def _three_demands(self, dep="d1"):
        self.svc.submit_demand(request_id="r1", actor_id="op", departure_id=dep, demand_id="dA1",
                               customer_id="cA1", cargo_class="general", quantity=40,
                               latest_load_at="2026-10-10T00:00:00Z", split_group_id="sg1")
        self.svc.submit_demand(request_id="r2", actor_id="op", departure_id=dep, demand_id="dA2",
                               customer_id="cA2", cargo_class="general", quantity=40,
                               latest_load_at="2026-10-10T00:00:00Z", split_group_id="sg1")
        self.svc.submit_demand(request_id="r3", actor_id="op", departure_id=dep, demand_id="dB1",
                               customer_id="cB1", cargo_class="medical", quantity=20,
                               latest_load_at="2026-10-04T00:00:00Z")

    def test_duplicate_submission_is_rejected(self):
        self._departure()
        self.svc.submit_demand(request_id="x1", actor_id="op", departure_id="d1", demand_id="q1",
                               customer_id="cA1", cargo_class="general", quantity=10,
                               latest_load_at="2026-10-10T00:00:00Z")
        with self.assertRaises(ConflictError):
            self.svc.submit_demand(request_id="x2", actor_id="op", departure_id="d1", demand_id="q2",
                                   customer_id="cA1", cargo_class="general", quantity=10,
                                   latest_load_at="2026-10-10T00:00:00Z")

    def test_freeze_closes_submission_and_allocates(self):
        self._departure()
        self._three_demands()
        self.svc.freeze_and_allocate(request_id="rf", actor_id="op", departure_id="d1")
        with self.assertRaises(ConflictError):
            self.svc.submit_demand(request_id="late", actor_id="op", departure_id="d1", demand_id="late1",
                                   customer_id="cA1", cargo_class="general", quantity=1,
                                   latest_load_at="2026-10-10T00:00:00Z")
        ents = {e["demand_id"]: e for e in self.svc.list_entitlements("d1")}
        # 医疗战略客户全部满足；gA 集团受 50% 上限约束合计 30。
        self.assertEqual(20, ents["dB1"]["offered_qty"])
        self.assertLessEqual(ents["dA1"]["offered_qty"] + ents["dA2"]["offered_qty"], 30)

    def test_concurrent_and_duplicate_confirmation_is_serialized(self):
        self._departure(capacity=20)
        self._three_demands()
        self.svc.freeze_and_allocate(request_id="rf", actor_id="op", departure_id="d1")
        # gB 集团受 50% 上限，医疗 20 仅获 10。
        granted = next(e for e in self.svc.list_entitlements("d1") if e["demand_id"] == "dB1")
        self.assertEqual(10, granted["offered_qty"])
        self.svc.confirm_demand(request_id="cf1", actor_id="op", departure_id="d1",
                                demand_id="dB1", confirm_qty=10)
        with self.assertRaises(ConflictError):
            self.svc.confirm_demand(request_id="cf2", actor_id="op", departure_id="d1",
                                    demand_id="dB1", confirm_qty=10)

    def test_shipped_qty_survives_capacity_cut(self):
        self._departure(capacity=200)
        self.svc.submit_demand(request_id="r1", actor_id="op", departure_id="d1", demand_id="dA1",
                               customer_id="cA1", cargo_class="general", quantity=100,
                               latest_load_at="2026-10-10T00:00:00Z")
        self.svc.freeze_and_allocate(request_id="rf", actor_id="op", departure_id="d1")
        self.svc.confirm_demand(request_id="cf", actor_id="op", departure_id="d1",
                                demand_id="dA1", confirm_qty=60)
        self.svc.ship_demand(request_id="sh", actor_id="op", departure_id="d1",
                             demand_id="dA1", ship_qty=60)
        # 限流：舱位降到 40。
        self.svc.adjust_capacity(request_id="cut", actor_id="op", departure_id="d1",
                                 new_capacity=40, reason="临时限流")
        ent = next(e for e in self.svc.list_entitlements("d1") if e["demand_id"] == "dA1")
        self.assertEqual(60, ent["shipped_qty"])
        self.assertEqual(100, ent["offered_qty"])

    def test_release_then_recovery_redistributes_in_stable_order(self):
        self._departure(capacity=40)
        self._three_demands()
        self.svc.freeze_and_allocate(request_id="rf", actor_id="op", departure_id="d1")
        ents = {e["demand_id"]: e for e in self.svc.list_entitlements("d1")}
        offered_a = next(d for d in ("dA1", "dA2") if ents[d]["status"] == "offered")
        waitlisted_a = "dA2" if offered_a == "dA1" else "dA1"
        self.svc.release_demand(request_id="rel", actor_id="op", departure_id="d1",
                                demand_id=offered_a)
        # 释放舱位流向同顺序下一张候补票。
        ents = {e["demand_id"]: e for e in self.svc.list_entitlements("d1")}
        self.assertEqual("released", ents[offered_a]["status"])
        self.assertGreater(ents[waitlisted_a]["offered_qty"], 0)
        # 运力恢复后不重复补分、总量单调。
        before = {e["demand_id"]: e["offered_qty"] for e in self.svc.list_entitlements("d1")}
        self.svc.adjust_capacity(request_id="rec", actor_id="op", departure_id="d1",
                                 new_capacity=120, reason="恢复")
        after = {e["demand_id"]: e["offered_qty"] for e in self.svc.list_entitlements("d1")}
        for demand_id, value in before.items():
            self.assertGreaterEqual(after[demand_id], value)

    def test_cancellation_revokes_unshipped_only(self):
        self._departure(capacity=40)
        self._three_demands()
        self.svc.freeze_and_allocate(request_id="rf", actor_id="op", departure_id="d1")
        self.svc.confirm_demand(request_id="cf", actor_id="op", departure_id="d1",
                                demand_id="dB1", confirm_qty=20)
        self.svc.ship_demand(request_id="sh", actor_id="op", departure_id="d1",
                             demand_id="dB1", ship_qty=10)
        self.svc.cancel_departure(request_id="cx", actor_id="op", departure_id="d1", reason="通道中断")
        ent = next(e for e in self.svc.list_entitlements("d1") if e["demand_id"] == "dB1")
        self.assertEqual(10, ent["offered_qty"])
        self.assertEqual(10, ent["shipped_qty"])

    def test_four_eyes_exception_requires_distinct_approver(self):
        self._departure()
        self._three_demands()
        self.svc.freeze_and_allocate(request_id="rf", actor_id="op", departure_id="d1")
        self.svc.request_exception(request_id="ex", actor_id="admin", departure_id="d1",
                                   demand_id="dA1", extra_qty=5, justification="急需")
        exid = self.db.connection.execute(
            "SELECT exception_id FROM exception_cases WHERE demand_id='dA1'").fetchone()[0]
        with self.assertRaises(PermissionDenied):
            self.svc.decide_exception(request_id="self", actor_id="admin", exception_id=exid,
                                      approved=True)
        # auditor 无权审批。
        with self.assertRaises(PermissionDenied):
            self.svc.decide_exception(request_id="au", actor_id="au", exception_id=exid, approved=True)
        receipt = self.svc.decide_exception(request_id="ok", actor_id="rev", exception_id=exid,
                                            approved=True, decision_note="同意")
        self.assertFalse(receipt.replayed)
        impact = self.svc.get_exception(exid)["impact"]
        self.assertTrue(any(c["demand_id"] == "dA1" for c in impact["changes"]))

    def test_idempotent_replay_returns_same_run(self):
        self._departure()
        self._three_demands()
        r1 = self.svc.freeze_and_allocate(request_id="rf", actor_id="op", departure_id="d1")
        r2 = self.svc.freeze_and_allocate(request_id="rf", actor_id="op", departure_id="d1")
        self.assertEqual(r1.resource_id, r2.resource_id)
        self.assertTrue(r2.replayed)

    def test_explain_and_fairness_comparison_do_not_rewrite_history(self):
        self._departure(capacity=40)
        self._three_demands()
        self.svc.freeze_and_allocate(request_id="rf", actor_id="op", departure_id="d1")
        self.svc.register_policy(request_id="rp2", actor_id="op", policy_id="alt",
                                 params={"group_cap_ratio": 0.9}, description="宽松集团上限")
        runs_before = len(self.svc.run_history("d1"))
        comparison = self.svc.compare_fairness("d1")
        expl = self.svc.explain_demand("dB1")
        self.assertEqual(runs_before, len(self.svc.run_history("d1")))
        self.assertGreaterEqual(len(comparison["comparisons"]), 2)
        self.assertIn("reasons", expl["latest"])
        self.assertIn("increments", expl)

    def test_policy_replay_historical_run_under_other_version(self):
        self._departure(capacity=40)
        self._three_demands()
        self.svc.freeze_and_allocate(request_id="rf", actor_id="op", departure_id="d1")
        run_id = self.svc.run_history("d1")[0]["run_id"]
        self.svc.register_policy(request_id="rp2", actor_id="op", policy_id="alt",
                                 params={"group_cap_ratio": 0.9}, description="宽松")
        replayed = self.svc.replay_run(run_id, "alt")
        self.assertEqual(40, replayed["result"]["capacity"])
        self.assertEqual("alt", replayed["policy_id"])


if __name__ == "__main__":
    unittest.main()
