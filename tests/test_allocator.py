import unittest

from transport_coordination import allocator as A


def demand(demand_id, customer="c1", group="g1", quantity=10, level=2,
           deadline="2026-10-09", root=None, seq=1, fulfillment=0.8):
    return A.DemandSnapshot(demand_id, customer, group, quantity, level, deadline,
                            root or demand_id, seq, fulfillment)


class AllocatorTest(unittest.TestCase):
    def test_strategic_level_beats_submission_order(self):
        # 普通货先报，医疗急货后报，医疗仍应优先占满舱位，普通货全票候补。
        demands = [demand("normal", quantity=8, level=2, deadline="2026-10-10", seq=1),
                   demand("medical", customer="c2", quantity=8, level=5,
                          deadline="2026-10-08", seq=2)]
        result = A.reconcile(demands, capacity=8,
                            customer_caps={"c1": 20, "c2": 20}, group_caps={"g1": 40},
                            policy_params=A.DEFAULT_POLICY_PARAMS)
        lines = {line.demand_id: line for line in result.lines}
        self.assertEqual(8, lines["medical"].offered_qty)
        self.assertEqual(0, lines["normal"].offered_qty)
        self.assertEqual(1, lines["normal"].waitlist_position)
        self.assertIn(A.WAITLISTED, lines["normal"].reasons)
        self.assertIn(A.RUN_CAPACITY_EXHAUSTED, lines["normal"].reasons)

    def test_split_family_gets_contiguous_space(self):
        # 同一拆单根的两票连续分配，家庭之间按首单优先级排序。
        d1 = demand("a1", customer="c1", quantity=6, level=2, deadline="2026-10-09", root="ra", seq=1)
        d2 = demand("a2", customer="c1", quantity=6, level=2, deadline="2026-10-09", root="ra", seq=2)
        d3 = demand("m1", customer="c2", quantity=8, level=5, deadline="2026-10-07", seq=3)
        result = A.reconcile([d1, d2, d3], capacity=10,
                            customer_caps={"c1": 10, "c2": 10}, group_caps={"g1": 20},
                            policy_params=A.DEFAULT_POLICY_PARAMS)
        order = [line.demand_id for line in result.lines]
        self.assertEqual(["m1", "a1", "a2"], order)
        lines = {line.demand_id: line for line in result.lines}
        self.assertEqual([8, 2, 0], [lines[k].offered_qty for k in order])

    def test_contract_cap_blocks_split_order_stuffing(self):
        # 同一客户即使拆很多单，也不能超过合同单班上限。
        demands = [demand(f"s{i}", customer="c1", quantity=4, level=2,
                          deadline="2026-10-09", root="ra", seq=i + 1) for i in range(4)]
        result = A.reconcile(demands, capacity=100,
                            customer_caps={"c1": 10}, group_caps={"g1": 100},
                            policy_params=A.DEFAULT_POLICY_PARAMS)
        self.assertEqual(10, sum(line.offered_qty for line in result.lines))
        capped = [line for line in result.lines if line.offered_qty < line.quantity]
        self.assertTrue(capped)
        self.assertIn(A.CONTRACT_CAP_LIMIT, capped[0].reasons)

    def test_group_cap_blocks_cross_customer_circumvention(self):
        # 同集团两个客户各报很多，合计不得超过集团限额。
        demands = [demand("x1", customer="c1", quantity=20, level=2,
                          deadline="2026-10-09", seq=1),
                   demand("x2", customer="c2", quantity=20, level=2,
                          deadline="2026-10-09", seq=2)]
        result = A.reconcile(demands, capacity=100,
                            customer_caps={"c1": 30, "c2": 30},
                            group_caps={"g1": 20}, policy_params=A.DEFAULT_POLICY_PARAMS)
        self.assertEqual(20, sum(line.offered_qty for line in result.lines))

    def test_shipped_floor_never_reclaimed_on_capacity_cut(self):
        demands = [demand("a", quantity=10, level=5, deadline="2026-10-07", seq=1),
                   demand("b", customer="c2", quantity=10, level=2, deadline="2026-10-09", seq=2)]
        kwargs = dict(customer_caps={"c1": 20, "c2": 20}, group_caps={"g1": 40},
                      policy_params=A.DEFAULT_POLICY_PARAMS)
        full = A.reconcile(demands, capacity=20, **kwargs)
        offered = {line.demand_id: line.offered_qty for line in full.lines}
        # a 已装船 10，班次临时压缩到 10：a 的装船量必须保留，b 全部落空。
        cut = A.reconcile(demands, capacity=10, protected={"a": 10}, held=offered, **kwargs)
        lines = {line.demand_id: line for line in cut.lines}
        self.assertEqual(10, lines["a"].offered_qty)
        self.assertEqual(0, lines["b"].offered_qty)
        self.assertEqual(0, lines["a"].reduced_qty)
        # 恢复到 20：b 按候补顺序补回，并标记增配。
        recovered = A.reconcile(demands, capacity=20, protected={"a": 10},
                                held={k: v for k, v in offered.items()},
                                prior={line.demand_id: line.offered_qty for line in cut.lines},
                                **kwargs)
        recovered_lines = {line.demand_id: line for line in recovered.lines}
        self.assertEqual(10, recovered_lines["b"].offered_qty)
        self.assertEqual(10, recovered_lines["b"].added_qty)
        self.assertIn(A.PROMOTED_FROM_WAITLIST, recovered_lines["b"].reasons)

    def test_cancel_keeps_only_shipped_floor(self):
        demands = [demand("a", quantity=10, level=5, deadline="2026-10-07", seq=1)]
        kwargs = dict(customer_caps={"c1": 20}, group_caps={"g1": 40},
                      policy_params=A.DEFAULT_POLICY_PARAMS)
        result = A.reconcile(demands, capacity=10, **kwargs)
        offered = {line.demand_id: line.offered_qty for line in result.lines}
        cancelled = A.reconcile(demands, capacity=0, protected={"a": 4}, held=offered, **kwargs)
        line = cancelled.lines[0]
        self.assertEqual(4, line.offered_qty)
        self.assertEqual(4, line.protected_qty)
        self.assertEqual(6, line.reduced_qty)

    def test_grant_is_exempt_from_caps_but_bound_by_capacity(self):
        demands = [demand("a", quantity=10, level=1, deadline="2026-10-10", seq=1)]
        kwargs = dict(customer_caps={"c1": 2}, group_caps={"g1": 2},
                      policy_params=A.DEFAULT_POLICY_PARAMS)
        result = A.reconcile(demands, capacity=10, grants={"a": 5}, **kwargs)
        line = result.lines[0]
        # 5 个例外豁免量直接落地，剩余需求仍按合同上限 2 获得，合计 7。
        self.assertEqual(7, line.offered_qty)
        self.assertEqual(5, line.granted_qty)
        self.assertIn(A.EXCEPTION_OVERRIDE, line.reasons)
        # 物理舱位紧张时例外表决量也不能突破舱位。
        tight = A.reconcile(demands, capacity=3, grants={"a": 5}, **kwargs)
        self.assertEqual(3, tight.lines[0].offered_qty)

    def test_waitlist_positions_follow_stable_order(self):
        demands = [demand(f"d{i}", customer=f"c{i}", quantity=10, level=1,
                          deadline="2026-10-10", seq=i + 1) for i in range(3)]
        caps_customers = {f"c{i}": 10 for i in range(3)}
        result = A.reconcile(demands, capacity=10, customer_caps=caps_customers,
                            group_caps={"g1": 30}, policy_params=A.DEFAULT_POLICY_PARAMS)
        positions = {line.demand_id: line.waitlist_position for line in result.lines}
        self.assertIsNone(positions["d0"])
        self.assertEqual(1, positions["d1"])
        self.assertEqual(2, positions["d2"])

    def test_waiver_with_shipment_remains_as_floor(self):
        demands = [demand("a", quantity=10, level=2, deadline="2026-10-09", seq=1),
                   demand("b", customer="c2", quantity=6, level=2, deadline="2026-10-09", seq=2)]
        kwargs = dict(customer_caps={"c1": 20, "c2": 20}, group_caps={"g1": 40},
                      policy_params=A.DEFAULT_POLICY_PARAMS)
        # a 放弃剩余舱位但已装船 4，b 应获得剩余 6 个舱位。
        result = A.reconcile(
            [demands[1]], capacity=10, protected={"a": 4},
            floor_attributes={"a": ("c1", "g1", 10)}, **kwargs)
        self.assertEqual(6, result.lines[0].offered_qty)
        self.assertEqual(10, result.run_used)

    def test_fairness_metrics_and_gini(self):
        self.assertEqual(0.0, A.gini_coefficient([1, 1, 1]))
        self.assertGreater(A.gini_coefficient([10, 0, 0]), 0.6)
        demands = [demand("a", quantity=10, level=2, deadline="x", seq=1),
                   demand("b", customer="c2", quantity=10, level=5, deadline="x", seq=2)]
        metrics = A.fairness_metrics(demands, {"a": 10, "b": 0})
        self.assertEqual(0.5, metrics["fill_rate"])
        self.assertEqual(1, metrics["waitlisted_demands"])

    def test_deterministic_across_input_permutations(self):
        demands = [demand("a", quantity=6, level=2, deadline="2026-10-09", root="r", seq=1),
                   demand("b", customer="c2", quantity=6, level=5, deadline="2026-10-07", seq=2),
                   demand("c", quantity=6, level=2, deadline="2026-10-09", root="r", seq=3)]
        kwargs = dict(capacity=10, customer_caps={"c1": 10, "c2": 10},
                      group_caps={"g1": 20}, policy_params=A.DEFAULT_POLICY_PARAMS)
        first = A.reconcile(demands, **kwargs)
        second = A.reconcile(list(reversed(demands)), **kwargs)
        self.assertEqual([(l.demand_id, l.offered_qty) for l in first.lines],
                         [(l.demand_id, l.offered_qty) for l in second.lines])

    def test_invalid_policy_params_rejected(self):
        with self.assertRaises(ValueError):
            A.validate_policy_params({"factor_order": ["unknown_factor"]})


if __name__ == "__main__":
    unittest.main()
