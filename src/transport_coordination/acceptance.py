"""运行基础服务与保供运力分配的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .service import DomainService
from .storage import Database
from .supply import SupplyService


def run() -> dict[str, object]:
    """执行完整登记链与保供运力分配链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        supply = SupplyService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范运营机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="运营负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="operator-002",
                               display_name="复核运营员", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号交通节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        supply_result = _run_supply(supply)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, **supply_result}
        database.close()
        return result


def _run_supply(supply: SupplyService) -> dict[str, object]:
    supply.register_corridor(request_id="req-corridor", actor_id="operator-001",
                             corridor_id="corridor-001", name="中欧保供通道")
    supply.register_group(request_id="req-group", actor_id="operator-001", group_id="group-001",
                          name="示范集团", cap_per_run=20)
    supply.register_customer(request_id="req-customer-1", actor_id="operator-001",
                             customer_id="customer-001", group_id="group-001",
                             name="生产企业", cap_per_run=10)
    supply.register_customer(request_id="req-customer-2", actor_id="operator-001",
                             customer_id="customer-002", group_id="group-001",
                             name="医疗物资企业", cap_per_run=10)
    supply.register_policy_version(request_id="req-policy", actor_id="admin-001",
                                   policy_version_id="policy-default", params={}, activate=True)
    supply.register_policy_version(
        request_id="req-policy-fifo", actor_id="admin-001", policy_version_id="policy-fifo",
        params={"factor_order": ["submitted_seq", "strategic_level", "cargo_deadline",
                                 "fulfillment"]}, activate=False)
    supply.register_train_version(request_id="req-train", actor_id="operator-001",
                                  version_id="train-001", corridor_id="corridor-001",
                                  departure_at="2026-10-10T00:00:00Z",
                                  freeze_at="2026-10-05T00:00:00Z", capacity=10)

    # 同一客户拆两单（合计 12，超过合同上限 10），医疗急货后报。
    supply.submit_demand(request_id="req-demand-1", actor_id="operator-001", version_id="train-001",
                         client_key="split-1", customer_id="customer-001", quantity=6,
                         strategic_level=2, cargo_deadline="2026-10-09", split_root_id="root-factory")
    supply.submit_demand(request_id="req-demand-2", actor_id="operator-001", version_id="train-001",
                         client_key="split-2", customer_id="customer-001", quantity=6,
                         strategic_level=2, cargo_deadline="2026-10-09", split_root_id="root-factory")
    medical = supply.submit_demand(request_id="req-demand-3", actor_id="operator-001",
                                   version_id="train-001", client_key="medical-1",
                                   customer_id="customer-002", quantity=8, strategic_level=5,
                                   cargo_deadline="2026-10-07")
    supply.freeze_demand_book(request_id="req-freeze", actor_id="operator-001",
                              version_id="train-001")
    freeze_run = supply.get_run(supply.list_runs("train-001")[0]["run_id"])
    medical_freeze = next(line for line in freeze_run["lines"]
                          if line["demand_id"] == medical.resource_id)
    factory_lines = [line for line in freeze_run["lines"]
                     if line["demand_id"] != medical.resource_id]

    # 医疗客户确认并装船 8（硬地板）。
    supply.confirm_demand(request_id="req-confirm-medical", actor_id="operator-001",
                          demand_id=medical.resource_id)
    supply.record_shipment(request_id="req-shipment", actor_id="operator-001",
                           demand_id=medical.resource_id, shipped_qty=8)

    # 限流解除，舱位恢复到 20，候补按稳定顺序增配且不重复补分。
    supply.adjust_capacity(request_id="req-capacity", actor_id="operator-001",
                           version_id="train-001", new_capacity=20, reason="临时限流解除")
    recovered_run = supply.get_run(supply.list_runs("train-001")[-1]["run_id"])
    recompute = supply.recompute_run(recovered_run["run_id"])
    medical_view = supply.get_demand_view(medical.resource_id)

    # 人工例外：一人提议、另一人审批，影响可复算。恢复后第二票拆单仍被
    # 合同上限卡在 4，例外追加 2 个豁免舱位使其满足。
    proposed = supply.propose_exception(request_id="req-exception", actor_id="operator-001",
                                        version_id="train-001",
                                        demand_id=factory_lines[1]["demand_id"],
                                        grant_qty=2, reason="灾后重建急用钢材")
    supply.decide_exception(request_id="req-exception-decision", actor_id="operator-002",
                            exception_id=proposed.resource_id, approve=True)
    exception_run = supply.get_run(supply.list_runs("train-001")[-1]["run_id"])
    exception_line = next(line for line in exception_run["lines"]
                          if line["demand_id"] == factory_lines[1]["demand_id"])

    comparison = supply.compare_policies(
        version_id="train-001", policy_version_ids=["policy-default", "policy-fifo"])
    runs = supply.list_runs("train-001")

    return {
        "freeze_medical_offered": medical_freeze["offered_qty"],
        "freeze_medical_rank": medical_freeze["priority_rank"],
        "freeze_factory_offered": [line["offered_qty"] for line in factory_lines],
        "recovered_total_offered": recovered_run["total_offered"],
        "recompute_reproduced": recompute["reproduced"],
        "medical_shipped": medical_view["shipped_qty"],
        "promotion_source_count": len(medical_view["promotion_sources"]),
        "exception_granted": exception_line["granted_qty"],
        "exception_offered": exception_line["offered_qty"],
        "policy_scenarios": len(comparison["scenarios"]),
        "run_bases": [run["basis"] for run in runs],
    }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
