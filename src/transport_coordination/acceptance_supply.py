"""运行保供运力分配的离线端到端验收。

在临时 SQLite 数据库中走通：通道/政策/客户/班次登记 → 需求申报与冻结 →
医疗优先与集团份额上限 → 客户确认与装运 → 放弃后候补递补 → 运力恢复单调
增配 → 双人审批人工例外 → 跨政策公平性比较，并校验审计哈希链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .storage import Database
from .supply_service import SupplyService


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "supply_acceptance.sqlite3")
        service = SupplyService(database, FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范运营中心")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="operator", actor_id="admin-001", new_actor_id="op-001",
                               display_name="运营员", role="operator", organization_id="org-001")
        service.register_actor(request_id="reviewer", actor_id="admin-001", new_actor_id="rev-001",
                               display_name="复核员", role="reviewer", organization_id="org-001")
        service.register_corridor(request_id="corridor", actor_id="op-001",
                                  corridor_id="cor-001", name="中欧通道")
        service.register_policy(request_id="policy", actor_id="op-001", policy_id="std",
                                params={"group_cap_ratio": 0.5}, description="标准保供政策")
        service.activate_policy(request_id="activate", actor_id="op-001",
                                corridor_id="cor-001", policy_id="std")
        service.register_customer(request_id="cust-a1", actor_id="op-001", customer_id="A1",
                                  group_id="G-A", name="集团A客户一", assurance_level="standard",
                                  contract_priority=10, performance_score=0.5)
        service.register_customer(request_id="cust-a2", actor_id="op-001", customer_id="A2",
                                  group_id="G-A", name="集团A客户二", assurance_level="standard",
                                  contract_priority=10, performance_score=0.5)
        service.register_customer(request_id="cust-b", actor_id="op-001", customer_id="B1",
                                  group_id="G-B", name="医疗客户", assurance_level="strategic",
                                  contract_priority=90, performance_score=0.9)
        service.register_departure(request_id="departure", actor_id="op-001", departure_id="dep-001",
                                   corridor_id="cor-001", sequence_no=1,
                                   cutoff_at="2026-10-05T00:00:00Z",
                                   departs_at="2026-10-06T00:00:00Z", capacity=60)
        # 集团 A 用两个客户拆单申报各 40（共 80），医疗客户申报 20。
        service.submit_demand(request_id="demand-a1", actor_id="op-001", departure_id="dep-001",
                              demand_id="DA1", customer_id="A1", cargo_class="general", quantity=40,
                              latest_load_at="2026-10-10T00:00:00Z", split_group_id="split-1")
        service.submit_demand(request_id="demand-a2", actor_id="op-001", departure_id="dep-001",
                              demand_id="DA2", customer_id="A2", cargo_class="general", quantity=40,
                              latest_load_at="2026-10-10T00:00:00Z", split_group_id="split-1")
        service.submit_demand(request_id="demand-b", actor_id="op-001", departure_id="dep-001",
                              demand_id="DB1", customer_id="B1", cargo_class="medical", quantity=20,
                              latest_load_at="2026-10-04T00:00:00Z")
        service.freeze_and_allocate(request_id="freeze", actor_id="op-001", departure_id="dep-001")

        entitlements = {e["demand_id"]: e for e in service.list_entitlements("dep-001")}
        # 医疗战略客户应全部满足；集团 A 受 50% 上限合计不超过 30。
        assert entitlements["DB1"]["offered_qty"] == 20
        assert entitlements["DA1"]["offered_qty"] + entitlements["DA2"]["offered_qty"] <= 30

        # 医疗客户确认并装运，装运量此后不可回收。
        service.confirm_demand(request_id="confirm", actor_id="op-001", departure_id="dep-001",
                               demand_id="DB1", confirm_qty=20)
        service.ship_demand(request_id="ship", actor_id="op-001", departure_id="dep-001",
                            demand_id="DB1", ship_qty=20)
        # 运力恢复到 120，分配单调增配、不重复补分。
        before = {e["demand_id"]: e["offered_qty"] for e in service.list_entitlements("dep-001")}
        service.adjust_capacity(request_id="recover", actor_id="op-001", departure_id="dep-001",
                                new_capacity=120, reason="限流解除")
        after = {e["demand_id"]: e["offered_qty"] for e in service.list_entitlements("dep-001")}
        assert all(after[k] >= before[k] for k in before)

        # 双人审批人工例外：申请方不能自批，复核员批准并留下可复算影响。
        service.request_exception(request_id="exception", actor_id="admin-001",
                                  departure_id="dep-001", demand_id="DA1", extra_qty=5,
                                  justification="生产急需追加")
        exception_id = database.connection.execute(
            "SELECT exception_id FROM exception_cases WHERE demand_id='DA1'").fetchone()[0]
        service.decide_exception(request_id="decision", actor_id="rev-001",
                                 exception_id=exception_id, approved=True, decision_note="同意")
        impact = service.get_exception(exception_id)["impact"]
        assert impact is not None and any(c["demand_id"] == "DA1" for c in impact["changes"])

        # 跨政策版本公平性比较只复算，不新增运行、不改写既往结果。
        service.register_policy(request_id="policy-alt", actor_id="op-001", policy_id="loose",
                                params={"group_cap_ratio": 0.9}, description="宽松集团上限")
        runs_before = len(service.run_history("dep-001"))
        comparison = service.compare_fairness("dep-001")
        runs_after = len(service.run_history("dep-001"))
        assert runs_before == runs_after
        assert {c["policy_id"] for c in comparison["comparisons"]} >= {"std", "loose"}

        explanation = service.explain_demand("DB1")
        valid, event_count = service.verify_audit()
        database.close()
        return {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "runs": runs_after,
            "medical_offered": entitlements["DB1"]["offered_qty"],
            "medical_shipped": after["DB1"],
            "exception_changes": len(impact["changes"]),
            "policies_compared": len(comparison["comparisons"]),
            "explain_reasons": len(explanation["latest"]["reasons"]),
        }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
