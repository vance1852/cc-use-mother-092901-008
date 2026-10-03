import unittest

from transport_coordination.allocation import (
    CONFIRMED, OFFERED, RELEASED, SHIPPED, WAITLISTED, DemandInput, allocate,
    fairness_summary,
)


def demand(demand_id, *, group_id="g1", customer_id=None, cargo_class="general", quantity=10,
           latest="2026-10-10T00:00:00Z", submitted=None, split=None, priority=10,
           performance=0.5, assurance="standard", **prior):
    return DemandInput(
        demand_id=demand_id, customer_id=customer_id or demand_id, group_id=group_id,
        cargo_class=cargo_class, quantity=quantity, latest_load_at=latest,
        submitted_at=submitted or f"2026-10-01T00:00:{int(demand_id[-1]):02d}Z",
        split_group_id=split, contract_priority=priority, performance_score=performance,
        assurance_level=assurance, **prior)


class AllocationEngineTest(unittest.TestCase):
    def test_medical_outranks_general_regardless_of_submission_order(self):
        # 通用货提交更早，但医疗急需应优先。
        general = demand("d1", cargo_class="general", submitted="2026-10-01T00:00:01Z")
        medical = demand("d2", group_id="g2", customer_id="d2", cargo_class="medical",
                         submitted="2026-10-01T00:00:09Z", latest="2026-10-04T00:00:00Z")
        result = allocate([general, medical], capacity=10, cutoff_at="2026-10-05T00:00:00Z",
                          policy_id="p", policy_version=1, params={"group_cap_ratio": 1})
        self.assertEqual(10, result.line("d2").offered_qty)
        self.assertEqual(0, result.line("d1").offered_qty)
        self.assertEqual(WAITLISTED, result.line("d1").status)

    def test_group_cap_blocks_split_order_hogging(self):
        # 同集团两个客户各申报 40（拆单），集团上限 50% 阻止其占满 60 舱。
        a1 = demand("a1", group_id="gA", cargo_class="general", quantity=40,
                    submitted="2026-10-01T00:00:01Z", split="sg1")
        a2 = demand("a2", group_id="gA", cargo_class="general", quantity=40,
                    submitted="2026-10-01T00:00:02Z", split="sg1")
        b1 = demand("b1", group_id="gB", cargo_class="general", quantity=40,
                    submitted="2026-10-01T00:00:03Z")
        result = allocate([a1, a2, b1], capacity=60, cutoff_at="2026-10-05T00:00:00Z",
                          policy_id="p", policy_version=1, params={"group_cap_ratio": 0.5})
        self.assertEqual(30, result.line("a1").offered_qty + result.line("a2").offered_qty)
        self.assertEqual(30, result.line("b1").offered_qty)
        self.assertTrue(any("split_order" in r for r in result.line("a1").reasons))

    def test_confirmed_and_shipped_are_never_reclaimed(self):
        first = allocate([demand("d1", quantity=100)], capacity=100,
                         cutoff_at="2026-10-05T00:00:00Z", policy_id="p", policy_version=1,
                         params={"group_cap_ratio": 1})
        self.assertEqual(100, first.line("d1").offered_qty)
        # 限流把舱位砍到 40，但 d1 已确认+装运 60，不得回收。
        d = demand("d1", quantity=100, prior_status=CONFIRMED, prior_offered=100,
                   prior_confirmed=60, prior_shipped=60)
        second = allocate([d], capacity=40, cutoff_at="2026-10-05T00:00:00Z",
                          policy_id="p", policy_version=1, params={"group_cap_ratio": 1})
        line = second.line("d1")
        self.assertEqual(100, line.offered_qty)
        self.assertEqual(60, line.shipped_qty)
        self.assertTrue(any("超过当前舱位" in w for w in second.warnings))

    def test_capacity_recovery_fills_waitlist_in_stable_order_without_double_grant(self):
        d1 = demand("d1", group_id="gA", quantity=30, submitted="2026-10-01T00:00:01Z")
        d2 = demand("d2", group_id="gB", quantity=30, submitted="2026-10-01T00:00:02Z")
        first = allocate([d1, d2], capacity=40, cutoff_at="2026-10-05T00:00:00Z",
                         policy_id="p", policy_version=1, params={"group_cap_ratio": 1})
        self.assertEqual(30, first.line("d1").offered_qty)
        self.assertEqual(10, first.line("d2").offered_qty)
        self.assertEqual(1, first.line("d2").waitlist_rank)
        # d1 确认 30，运力恢复到 60。
        p1 = demand("d1", group_id="gA", quantity=30, submitted="2026-10-01T00:00:01Z",
                    prior_status=CONFIRMED, prior_offered=30, prior_confirmed=30)
        p2 = demand("d2", group_id="gB", quantity=30, submitted="2026-10-01T00:00:02Z",
                    prior_status=OFFERED, prior_offered=10, prior_waitlist_rank=1)
        second = allocate([p1, p2], capacity=60, cutoff_at="2026-10-05T00:00:00Z",
                          policy_id="p", policy_version=1, params={"group_cap_ratio": 1})
        self.assertEqual(30, second.line("d1").offered_qty)
        self.assertEqual(30, second.line("d2").offered_qty)
        self.assertEqual(20, second.line("d2").new_grant_qty)
        self.assertEqual(20, second.line("d2").change_from_parent)

    def test_released_demand_keeps_core_and_does_not_retake_freed_space(self):
        d1 = demand("d1", group_id="gA", quantity=50, submitted="2026-10-01T00:00:01Z")
        d2 = demand("d2", group_id="gB", quantity=50, submitted="2026-10-01T00:00:02Z")
        first = allocate([d1, d2], capacity=80, cutoff_at="2026-10-05T00:00:00Z",
                         policy_id="p", policy_version=1, params={"group_cap_ratio": 1})
        self.assertEqual(50, first.line("d1").offered_qty)
        self.assertEqual(30, first.line("d2").offered_qty)
        # d1 放弃全部未确认（无确认核心），释放 50 全归候补 d2。
        r1 = demand("d1", group_id="gA", quantity=50, submitted="2026-10-01T00:00:01Z",
                    prior_status=OFFERED, prior_offered=50)
        # 手动把 prior_status 标记为 released
        r1.prior_status = RELEASED
        r2 = demand("d2", group_id="gB", quantity=50, submitted="2026-10-01T00:00:02Z",
                    prior_status=OFFERED, prior_offered=30, prior_waitlist_rank=1)
        second = allocate([r1, r2], capacity=80, cutoff_at="2026-10-05T00:00:00Z",
                          policy_id="p", policy_version=1, params={"group_cap_ratio": 1})
        self.assertEqual(0, second.line("d1").offered_qty)
        self.assertEqual(RELEASED, second.line("d1").status)
        self.assertEqual(50, second.line("d2").offered_qty)

    def test_cancellation_keeps_only_shipped(self):
        d = demand("d1", quantity=100, prior_status=CONFIRMED, prior_offered=80,
                   prior_confirmed=80, prior_shipped=30)
        result = allocate([d], capacity=100, cutoff_at="2026-10-05T00:00:00Z",
                          policy_id="p", policy_version=1, cancelled=True)
        line = result.line("d1")
        self.assertEqual(30, line.offered_qty)
        self.assertEqual(30, line.shipped_qty)
        self.assertEqual(-50, line.change_from_parent)

    def test_deterministic_and_replayable(self):
        demands = [demand(f"d{i}", group_id=f"g{i % 2}", quantity=20,
                          submitted=f"2026-10-01T00:00:{i:02d}Z") for i in range(1, 6)]
        r1 = allocate(demands, capacity=50, cutoff_at="2026-10-05T00:00:00Z",
                      policy_id="p", policy_version=1)
        r2 = allocate(demands, capacity=50, cutoff_at="2026-10-05T00:00:00Z",
                      policy_id="p", policy_version=1)
        self.assertEqual(r1.to_dict(), r2.to_dict())
        summary = fairness_summary(r1, demands)
        self.assertIn("by_group", summary)
        self.assertIn("by_cargo_class", summary)


if __name__ == "__main__":
    unittest.main()
