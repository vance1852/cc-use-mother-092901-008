"""保供运力分配的纯函数核心。

该模块不访问数据库、不读取时钟，所有结论都由显式传入的快照决定，
因此冻结草案、运力恢复后的增量分配、人工例外影响复算和不同政策版本的
公平性比较都可以重复执行并得到完全一致的结果。

核心设计：
- 同一拆单家庭（split_root_id 相同）连续获得舱位，保障医疗/生产急货
  不被"先到先得"式的拆单插队挤出连续舱位；
- 客户合同单班上限、集团单班总限额在占用时逐票扣减，拆单绕限无效；
- 已装船数量是硬地板（protected），任何重分配都不得回收；
- 草案、放弃后的再分配、班次取消后的压缩、运力恢复后的增配、
  人工例外影响，全部由同一个 reconcile 函数按稳定顺序算出。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# 战略保障级别 1（普通）~ 5（最高，例如医疗急救）；货物时限越早越优先；
# 历史履约率越高越优先；申报序号作为最终稳定次序。家庭连续分配始终生效，
# factor_order 只决定各个"拆单家庭"之间的排序。
SUPPORTED_FACTORS = frozenset({
    "strategic_level",
    "cargo_deadline",
    "fulfillment",
    "submitted_seq",
})

DEFAULT_POLICY_VERSION = "default-v1"
DEFAULT_POLICY_PARAMS: dict[str, Any] = {
    "factor_order": ["strategic_level", "cargo_deadline", "fulfillment", "submitted_seq"],
    "enforce_contract_cap": True,
    "enforce_group_cap": True,
}

# 结论原因代码
RUN_CAPACITY_EXHAUSTED = "RUN_CAPACITY_EXHAUSTED"   # 班次舱位耗尽
CONTRACT_CAP_LIMIT = "CONTRACT_CAP_LIMIT"         # 客户合同单班上限
GROUP_CAP_LIMIT = "GROUP_CAP_LIMIT"               # 集团单班总限额
WAITLISTED = "WAITLISTED"                         # 全部进入候补
PARTIALLY_ALLOCATED = "PARTIALLY_ALLOCATED"       # 部分获得
ALLOCATED = "ALLOCATED"                           # 全部满足
PROMOTED_FROM_WAITLIST = "PROMOTED_FROM_WAITLIST"  # 候补增配
REALLOCATED_DOWN = "REALLOCATED_DOWN"             # 未装船部分被稳定顺序压缩
EXCEPTION_OVERRIDE = "EXCEPTION_OVERRIDE"         # 含双人审批例外表决量


@dataclass(frozen=True)
class DemandSnapshot:
    """一次冻结时参与分配的单票需求快照。"""

    demand_id: str
    customer_id: str
    group_id: str
    quantity: int
    strategic_level: int
    cargo_deadline: str
    split_root_id: str
    submitted_seq: int
    fulfillment_rate: float


@dataclass(frozen=True)
class Line:
    """一票需求在某个分配版本下的结论。"""

    demand_id: str
    quantity: int
    offered_qty: int
    protected_qty: int
    granted_qty: int
    added_qty: int
    reduced_qty: int
    waitlist_position: int | None
    priority_rank: int
    reasons: tuple[str, ...]
    factor_values: dict[str, Any]


@dataclass(frozen=True)
class AllocationResult:
    """整班一次调和的完整结果。"""

    lines: tuple[Line, ...]
    capacity: int
    total_offered: int
    run_used: int
    additions: dict[str, int]
    reductions: dict[str, int]


def validate_policy_params(params: dict[str, Any]) -> dict[str, Any]:
    """校验政策参数并返回规范化副本。"""

    if not isinstance(params, dict):
        raise ValueError("政策参数必须是对象")
    order = params.get("factor_order", DEFAULT_POLICY_PARAMS["factor_order"])
    if not isinstance(order, list) or not order:
        raise ValueError("factor_order 必须是非空数组")
    for factor in order:
        if factor not in SUPPORTED_FACTORS:
            raise ValueError(f"不支持的排序因子: {factor}")
    normalized = {
        "factor_order": list(order),
        "enforce_contract_cap": bool(params.get("enforce_contract_cap", True)),
        "enforce_group_cap": bool(params.get("enforce_group_cap", True)),
    }
    return normalized


def _priority_key(demand: DemandSnapshot, factor_order: list[str]) -> tuple[Any, ...]:
    """构造升序排序键（越靠前优先级越高），末位用 demand_id 保证确定性。"""

    parts: list[Any] = []
    for factor in factor_order:
        if factor == "strategic_level":
            parts.append(-demand.strategic_level)
        elif factor == "cargo_deadline":
            parts.append(demand.cargo_deadline)
        elif factor == "fulfillment":
            parts.append(-round(demand.fulfillment_rate, 6))
        elif factor == "submitted_seq":
            parts.append(demand.submitted_seq)
    parts.append(demand.demand_id)
    return tuple(parts)


def _ordered_demands(demands: list[DemandSnapshot], factor_order: list[str]) -> list[DemandSnapshot]:
    """拆单家庭内按申报顺序连续排列，家庭间按家庭首单的政策优先级排列。"""

    grouped: dict[str, list[DemandSnapshot]] = {}
    for demand in demands:
        grouped.setdefault(demand.split_root_id, []).append(demand)
    families = list(grouped.values())
    for family in families:
        family.sort(key=lambda item: (item.submitted_seq, item.demand_id))
    families.sort(key=lambda family: _priority_key(family[0], factor_order))
    return [demand for family in families for demand in family]


def reconcile(
    demands: list[DemandSnapshot],
    *,
    capacity: int,
    customer_caps: dict[str, int],
    group_caps: dict[str, int],
    policy_params: dict[str, Any],
    protected: dict[str, int] | None = None,
    held: dict[str, int] | None = None,
    grants: dict[str, int] | None = None,
    prior: dict[str, int] | None = None,
    floor_attributes: dict[str, tuple[str, str, int]] | None = None,
) -> AllocationResult:
    """按稳定顺序计算整班分配。

    参数语义：
    - protected：已装船量，硬地板，计入舱位与合同/集团占用，永不回收；
    - held：上一版本已确认占用（含装船部分）。调和时先在稳定顺序内
      保住其未装船部分，再用剩余舱位按候补顺序补充分配；
    - grants：双人审批通过的人工例外量，计入舱位占用但豁免合同/集团上限；
    - prior：上一版本对每票需求的 offered 量，仅用于计算 added/reduced
      与增配来源，缺省与 held 相同；
    - floor_attributes：已退出本轮分配（放弃/取消）但仍有装船或例外
      占用的需求，键为 demand_id，值为 (customer_id, group_id, quantity)，
      其 protected/grants 量只计入占用、不再出现在分配行中。

    草案（冻结）等价于 protected/held/grants 全部为空的一次调和。
    """

    params = validate_policy_params(policy_params)
    order = params["factor_order"]
    protected = protected or {}
    held = held or {}
    grants = grants or {}
    prior = prior if prior is not None else held
    floor_attributes = floor_attributes or {}
    ordered = _ordered_demands(demands, order)

    attributes: dict[str, tuple[str, str, int]] = {
        demand.demand_id: (demand.customer_id, demand.group_id, demand.quantity)
        for demand in demands
    }
    for demand_id, triple in floor_attributes.items():
        attributes.setdefault(demand_id, triple)

    offered: dict[str, int] = {}
    grants_applied: dict[str, int] = {}
    customer_used: dict[str, int] = {}
    group_used: dict[str, int] = {}
    run_used = 0

    # 第一阶段：铺硬地板。装船量计入合同/集团占用；例外量豁免合同/集团
    # 上限，但仍受物理舱位约束。按 demand_id 确定顺序保证可复算。
    for demand_id, (customer_id, group_id, max_qty) in sorted(attributes.items()):
        floor_ship = min(int(protected.get(demand_id, 0)), max_qty)
        floor_grant = min(int(grants.get(demand_id, 0)), max_qty - floor_ship,
                          max(0, capacity - run_used))
        grants_applied[demand_id] = floor_grant
        offered[demand_id] = floor_ship + floor_grant
        run_used += floor_ship + floor_grant
        customer_used[customer_id] = customer_used.get(customer_id, 0) + floor_ship
        group_used[group_id] = group_used.get(group_id, 0) + floor_ship

    def _rooms(demand: DemandSnapshot) -> tuple[int, float, float]:
        room_run = capacity - run_used
        room_customer: float = math.inf
        room_group: float = math.inf
        if params["enforce_contract_cap"]:
            room_customer = int(customer_caps.get(demand.customer_id, 0)) - customer_used.get(demand.customer_id, 0)
        if params["enforce_group_cap"]:
            room_group = int(group_caps.get(demand.group_id, 0)) - group_used.get(demand.group_id, 0)
        return room_run, room_customer, room_group

    def _take(demand: DemandSnapshot, want: int) -> int:
        nonlocal run_used
        if want <= 0:
            return 0
        room_run, room_customer, room_group = _rooms(demand)
        take = max(0, min(want, room_run, room_customer, room_group))
        offered[demand.demand_id] += take
        run_used += take
        customer_used[demand.customer_id] = customer_used.get(demand.customer_id, 0) + take
        group_used[demand.group_id] = group_used.get(demand.group_id, 0) + take
        return take

    # 第二阶段：在稳定顺序内保住上一版本已确认的未装船占用，
    # 顺序靠后的需求若已无空间，其未装船部分被压缩（装船地板不动）。
    for demand in ordered:
        preserve = min(int(held.get(demand.demand_id, 0)), demand.quantity) - offered[demand.demand_id]
        _take(demand, preserve)

    # 第三阶段：剩余未满足需求按同一稳定顺序（即候补顺序）增配。
    for demand in ordered:
        want = demand.quantity - offered[demand.demand_id]
        _take(demand, want)

    lines: list[Line] = []
    additions: dict[str, int] = {}
    reductions: dict[str, int] = {}
    waitlist_counter = 0
    for rank, demand in enumerate(ordered, start=1):
        qty_offered = offered[demand.demand_id]
        prior_qty = min(int(prior.get(demand.demand_id, 0)), demand.quantity)
        granted_qty = grants_applied.get(demand.demand_id, 0)
        protected_qty = min(int(protected.get(demand.demand_id, 0)), demand.quantity)
        added = max(0, qty_offered - prior_qty)
        reduced = max(0, prior_qty - qty_offered)
        if added:
            additions[demand.demand_id] = added
        if reduced:
            reductions[demand.demand_id] = reduced

        shortfall = demand.quantity - qty_offered
        position = None
        binding: list[str] = []
        if shortfall > 0:
            waitlist_counter += 1
            position = waitlist_counter
            room_run, room_customer, room_group = _rooms(demand)
            if room_run < shortfall:
                binding.append(RUN_CAPACITY_EXHAUSTED)
            if room_customer < shortfall:
                binding.append(CONTRACT_CAP_LIMIT)
            if room_group < shortfall:
                binding.append(GROUP_CAP_LIMIT)
            if qty_offered == 0:
                binding.append(WAITLISTED)
            else:
                binding.append(PARTIALLY_ALLOCATED)
        else:
            binding.append(ALLOCATED)
        if added and (prior_qty > 0 or demand_id in prior):
            binding.append(PROMOTED_FROM_WAITLIST)
        if reduced:
            binding.append(REALLOCATED_DOWN)
        if granted_qty:
            binding.append(EXCEPTION_OVERRIDE)

        lines.append(Line(
            demand_id=demand.demand_id,
            quantity=demand.quantity,
            offered_qty=qty_offered,
            protected_qty=protected_qty,
            granted_qty=granted_qty,
            added_qty=added,
            reduced_qty=reduced,
            waitlist_position=position,
            priority_rank=rank,
            reasons=tuple(binding),
            factor_values={
                "strategic_level": demand.strategic_level,
                "cargo_deadline": demand.cargo_deadline,
                "fulfillment_rate": round(demand.fulfillment_rate, 6),
                "submitted_seq": demand.submitted_seq,
                "split_root_id": demand.split_root_id,
            },
        ))

    return AllocationResult(
        lines=tuple(lines),
        capacity=capacity,
        total_offered=sum(offered.values()),
        run_used=run_used,
        additions=additions,
        reductions=reductions,
    )


def gini_coefficient(values: list[float]) -> float:
    """计算标准 Gini 系数，0 为绝对平均，1 为最大集中。"""

    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    total = sum(ordered)
    if total == 0:
        return 0.0
    weighted = sum((index + 1) * value for index, value in enumerate(ordered))
    return 2 * weighted / (len(ordered) * total) - (len(ordered) + 1) / len(ordered)


def fairness_metrics(demands: list[DemandSnapshot], allocated: dict[str, int]) -> dict[str, Any]:
    """汇总一个分配方案的公平性指标，供政策版本横向比较。"""

    per_customer_requested: dict[str, int] = {}
    per_customer_allocated: dict[str, int] = {}
    per_group_allocated: dict[str, int] = {}
    tier_requested: dict[int, int] = {level: 0 for level in range(1, 6)}
    tier_allocated: dict[int, int] = {level: 0 for level in range(1, 6)}
    total_requested = 0
    total_allocated = 0
    waitlisted = 0

    for demand in demands:
        qty = allocated.get(demand.demand_id, 0)
        total_requested += demand.quantity
        total_allocated += qty
        per_customer_requested[demand.customer_id] = per_customer_requested.get(demand.customer_id, 0) + demand.quantity
        per_customer_allocated[demand.customer_id] = per_customer_allocated.get(demand.customer_id, 0) + qty
        per_group_allocated[demand.group_id] = per_group_allocated.get(demand.group_id, 0) + qty
        tier_requested[demand.strategic_level] += demand.quantity
        tier_allocated[demand.strategic_level] += qty
        if qty < demand.quantity:
            waitlisted += 1

    ratios = [per_customer_allocated[cid] / per_customer_requested[cid]
              for cid in per_customer_requested]
    group_total = sum(per_group_allocated.values())
    return {
        "total_requested": total_requested,
        "total_allocated": total_allocated,
        "fill_rate": round(total_allocated / total_requested, 6) if total_requested else 0.0,
        "waitlisted_demands": waitlisted,
        "customer_fill_gini": round(gini_coefficient(ratios), 6),
        "group_shares": {gid: round(qty / group_total, 6) for gid, qty in per_group_allocated.items()}
        if group_total else {},
        "tier_fill_rates": {
            str(level): (round(tier_allocated[level] / tier_requested[level], 6)
                         if tier_requested[level] else None)
            for level in range(1, 6)
        },
    }
