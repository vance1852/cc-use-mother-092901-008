"""保供运力分配的纯函数引擎。

引擎不访问数据库、不读取时间，只对一份完整的输入快照做确定性计算，
因此同一份输入在任意政策版本下都可以无损复算，审计方可以比较不同
政策版本的公平性而不改变既往结果。

单调稳定模型
------------
每次运行都从每票需求的「既有持有量」出发：

- 已装运（shipped）、已确认（confirmed）、人工例外（manual）是受保护核心，
  任何后续运行都不得回收；
- 已经给出且客户未放弃的草案（offered）同样粘住，保证客户能获得连续舱位，
  也保证运力恢复后不会重复补分；
- 放弃（released）与班次取消（cancelled）的票回落到受保护核心；
- 释放出的舱位按全序（政策分数、提交先后、需求编号）补给候补队首，
  同集团受份额上限约束（上限只限制新增授予，既往持有不追溯削减）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 保障级别权重：医疗与生产急需物资优先。
CARGO_CLASS_WEIGHT = {"medical": 100, "production": 80, "general": 0}
# 客户战略保障级别权重。
ASSURANCE_WEIGHT = {"strategic": 60, "standard": 0}

DEFAULT_PARAMS: dict[str, Any] = {
    "cargo_class_weights": dict(CARGO_CLASS_WEIGHT),
    "assurance_weights": dict(ASSURANCE_WEIGHT),
    "contract_priority_weight": 1.0,
    "performance_weight": 40.0,
    "time_critical_boost": 25.0,
    "group_cap_ratio": 0.35,
}

# 票需求状态机取值。
OFFERED = "offered"          # 草案获得舱位（可能含待确认增量），等待客户确认
CONFIRMED = "confirmed"      # 已全部确认占用，等待装运
SHIPPED = "shipped"          # 已（全部）装运，不可回收
PARTIAL_SHIPPED = "partial_shipped"  # 部分装运，其余仍持有
WAITLISTED = "waitlisted"    # 落候补
UNSATISFIED = "unsatisfied"  # 无舱位
RELEASED = "released"        # 客户放弃未确认部分
CANCELLED = "cancelled"      # 班次取消导致未兑现分配失效

# 重分时持有量粘住的状态。
STICKY_STATUS = (OFFERED, CONFIRMED, SHIPPED, PARTIAL_SHIPPED)


@dataclass
class DemandInput:
    """一票需求在某次分配时刻的完整视图。"""

    demand_id: str
    customer_id: str
    group_id: str
    cargo_class: str
    quantity: int
    latest_load_at: str
    submitted_at: str
    split_group_id: str | None
    contract_priority: int
    performance_score: float
    assurance_level: str
    # 上一轮状态与数量（首次冻结时全部为零值）。
    prior_status: str = WAITLISTED
    prior_offered: int = 0
    prior_confirmed: int = 0
    prior_shipped: int = 0
    prior_manual: int = 0
    prior_waitlist_rank: int | None = None
    # 本次运行新批准的人工例外面额（尚未计入 prior_offered/prior_manual）。
    manual_delta: int = 0
    flags: list[str] = field(default_factory=list)


@dataclass
class AllocationLine:
    """一票需求在一次分配运行中的结果。"""

    demand_id: str
    customer_id: str
    group_id: str
    cargo_class: str
    quantity: int
    offered_qty: int
    new_grant_qty: int
    confirmed_qty: int
    shipped_qty: int
    manual_extra_qty: int
    protected_qty: int
    waitlist_rank: int | None
    status: str
    score: float
    score_breakdown: dict[str, float]
    reasons: list[str]
    change_from_parent: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "demand_id": self.demand_id,
            "customer_id": self.customer_id,
            "group_id": self.group_id,
            "cargo_class": self.cargo_class,
            "quantity": self.quantity,
            "offered_qty": self.offered_qty,
            "new_grant_qty": self.new_grant_qty,
            "confirmed_qty": self.confirmed_qty,
            "shipped_qty": self.shipped_qty,
            "manual_extra_qty": self.manual_extra_qty,
            "protected_qty": self.protected_qty,
            "waitlist_rank": self.waitlist_rank,
            "status": self.status,
            "score": round(self.score, 4),
            "score_breakdown": {key: round(value, 4) for key, value in self.score_breakdown.items()},
            "reasons": self.reasons,
            "change_from_parent": self.change_from_parent,
        }


@dataclass
class AllocationResult:
    """一次完整分配运行的输出。"""

    lines: list[AllocationLine]
    capacity: int
    allocated: int
    available: int
    policy_id: str
    policy_version: int
    params: dict[str, Any]
    group_usage: dict[str, dict[str, Any]]
    warnings: list[str]

    def line(self, demand_id: str) -> AllocationLine:
        for line in self.lines:
            if line.demand_id == demand_id:
                return line
        raise KeyError(demand_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "allocated": self.allocated,
            "available": self.available,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "params": self.params,
            "group_usage": self.group_usage,
            "warnings": self.warnings,
            "lines": [line.to_dict() for line in self.lines],
        }


def merge_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """以默认参数为底，用政策参数覆盖（允许只声明差异项）。"""

    merged = {key: (dict(value) if isinstance(value, dict) else value) for key, value in DEFAULT_PARAMS.items()}
    if params:
        for key, value in params.items():
            merged[key] = value
    return merged


def score_demand(demand: DemandInput, params: dict[str, Any], cutoff_at: str) -> tuple[float, dict[str, float]]:
    """按政策参数计算优先级分数。全部成分都记录下来供逐票解释。"""

    cargo = params["cargo_class_weights"].get(demand.cargo_class, 0)
    assurance = params["assurance_weights"].get(demand.assurance_level, 0)
    contract = demand.contract_priority * params["contract_priority_weight"]
    performance = demand.performance_score * params["performance_weight"]
    # 最晚装运时限不晚于申报截止：时限紧迫，给予加权。
    urgency = params["time_critical_boost"] if demand.latest_load_at <= cutoff_at else 0
    breakdown = {
        "cargo_class": float(cargo),
        "assurance": float(assurance),
        "contract_priority": float(contract),
        "performance": float(performance),
        "urgency": float(urgency),
    }
    return sum(breakdown.values()), breakdown


def detect_flags(demands: list[DemandInput]) -> None:
    """就地标记拆单占配额与同集团重复提交。"""

    split_groups: dict[str, list[DemandInput]] = {}
    group_cargo: dict[tuple[str, str, str, int], list[DemandInput]] = {}
    for demand in demands:
        if demand.split_group_id:
            split_groups.setdefault(demand.split_group_id, []).append(demand)
        key = (demand.group_id, demand.cargo_class, demand.latest_load_at, demand.quantity)
        group_cargo.setdefault(key, []).append(demand)
    for members in split_groups.values():
        if len(members) > 1:
            for member in members:
                if "split_order" not in member.flags:
                    member.flags.append("split_order")
    for members in group_cargo.values():
        if len({member.customer_id for member in members}) > 1:
            for member in members:
                if "group_duplicate" not in member.flags:
                    member.flags.append("group_duplicate")


def _rank(scored: list[tuple[float, str, str, DemandInput, dict[str, float]]],
          demand_id: str) -> int | None:
    for index, entry in enumerate(scored):
        if entry[3].demand_id == demand_id:
            return index + 1
    return None


def allocate(demands: list[DemandInput], *, capacity: int, cutoff_at: str,
             policy_id: str, policy_version: int, params: dict[str, Any] | None = None,
             cancelled: bool = False) -> AllocationResult:
    """执行一次确定性分配。

    同一份输入必然得到同一份输出；重分时既有持有量单调不减，
    只有放弃/取消的票回落，释放舱位按全序补到候补队首。
    """

    params = merge_params(params)
    detect_flags(demands)

    if cancelled:
        lines = []
        for d in demands:
            # 已装运货物不可回收；确认未装运与人工例外随班次取消而失效。
            protected_qty = d.prior_shipped
            released = max(0, d.prior_offered - protected_qty)
            lines.append(AllocationLine(
                demand_id=d.demand_id, customer_id=d.customer_id, group_id=d.group_id,
                cargo_class=d.cargo_class, quantity=d.quantity,
                offered_qty=protected_qty, new_grant_qty=0,
                confirmed_qty=d.prior_confirmed, shipped_qty=d.prior_shipped,
                manual_extra_qty=d.prior_manual,
                protected_qty=protected_qty, waitlist_rank=None, status=CANCELLED,
                score=0.0, score_breakdown={},
                reasons=["班次取消：未兑现分配失效，已装运部分不可回收"],
                change_from_parent=-released,
            ))
        return AllocationResult(lines, capacity, sum(line.offered_qty for line in lines),
                                capacity, policy_id, policy_version, params, {}, ["班次已取消"])

    # 第一阶段：确定每票的起点持有量（受保护核心 + 粘性草案）。
    # shipped 是 confirmed 的子集（先确认后装运），故核心 = confirmed + manual。
    held: dict[str, int] = {}
    protected: dict[str, int] = {}
    for d in demands:
        # 本轮新批准的人工例外面额与确认额度一样，批准即占用、受保护。
        core = d.prior_confirmed + d.prior_manual + d.manual_delta
        protected[d.demand_id] = core
        if d.prior_status in STICKY_STATUS:
            held[d.demand_id] = max(core, d.prior_offered + d.manual_delta)
        else:
            # RELEASED / CANCELLED / 初次参与：只保留不可回收的核心与本轮新批例外。
            held[d.demand_id] = core

    total_held = sum(held.values())
    remaining = capacity - total_held
    group_used: dict[str, int] = {}
    for d in demands:
        group_used[d.group_id] = group_used.get(d.group_id, 0) + held[d.demand_id]

    # 第二阶段：政策评分与全序（分数降序 → 提交先后 → 需求编号）。
    scored: list[tuple[float, str, str, DemandInput, dict[str, float]]] = []
    for d in demands:
        score, breakdown = score_demand(d, params, cutoff_at)
        scored.append((score, d.submitted_at, d.demand_id, d, breakdown))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))

    ratio = params["group_cap_ratio"]
    group_cap = round(capacity * ratio) if ratio < 1 else capacity
    waitlist: list[str] = []
    new_grants: dict[str, int] = {d.demand_id: 0 for d in demands}
    reasons: dict[str, list[str]] = {d.demand_id: [] for d in demands}

    # 第三阶段：只分配增量舱位，按全序逐票授予。
    for score, _, _, d, _ in scored:
        want = d.quantity - held[d.demand_id]
        if want <= 0:
            reasons[d.demand_id].append("既有持有量已覆盖全部申报数量")
            continue
        if d.prior_status == RELEASED:
            # 放弃是客户终态决定：未确认部分永久让出，不参与后续增量分配。
            reasons[d.demand_id].append("客户已放弃未确认舱位，仅保留不可回收核心额度")
            continue
        if remaining <= 0:
            reasons[d.demand_id].append("舱位已分尽，进入候补")
            waitlist.append(d.demand_id)
            continue
        group_headroom = group_cap - group_used.get(d.group_id, 0)
        grant = min(want, remaining, max(0, group_headroom))
        if grant > 0:
            held[d.demand_id] += grant
            new_grants[d.demand_id] += grant
            remaining -= grant
            group_used[d.group_id] = group_used.get(d.group_id, 0) + grant
        if held[d.demand_id] < d.quantity:
            if grant == 0 or group_headroom <= remaining:
                reasons[d.demand_id].append(
                    f"同集团份额上限 {group_cap}（{ratio:.0%}）约束，持有 {held[d.demand_id]}/{d.quantity}")
            else:
                reasons[d.demand_id].append(f"班次总舱位不足，持有 {held[d.demand_id]}/{d.quantity}")
            waitlist.append(d.demand_id)

    rank_by_id = {demand_id: index + 1 for index, demand_id in enumerate(waitlist)}

    # 第四阶段：生成逐票结果与可解释状态。
    lines: list[AllocationLine] = []
    for score, _, _, d, breakdown in scored:
        total = held[d.demand_id]
        # shipped 已包含在 confirmed 中，未确认持有 = 总持有 - 确认核心（含例外）。
        unconfirmed = total - d.prior_confirmed - d.prior_manual
        if d.prior_status == RELEASED and unconfirmed <= 0 and total == protected[d.demand_id]:
            status = RELEASED
        elif unconfirmed > 0:
            status = OFFERED
        elif d.prior_shipped > 0:
            status = SHIPPED if d.prior_shipped >= d.quantity else PARTIAL_SHIPPED
        elif d.prior_confirmed > 0:
            status = CONFIRMED
        elif total > 0:
            status = OFFERED
        elif rank_by_id.get(d.demand_id):
            status = WAITLISTED
        else:
            status = UNSATISFIED

        line_reasons = list(reasons[d.demand_id])
        if total > protected[d.demand_id]:
            line_reasons.insert(0, f"政策 {policy_id}@v{policy_version} 评分 {round(score, 2)}，按全序获得舱位")
        if new_grants[d.demand_id] > 0 and d.prior_waitlist_rank:
            line_reasons.append(f"运力恢复/释放后由候补第 {d.prior_waitlist_rank} 位递补")
        for flag in d.flags:
            line_reasons.append(f"请求标记：{flag}")
        lines.append(AllocationLine(
            demand_id=d.demand_id, customer_id=d.customer_id, group_id=d.group_id,
            cargo_class=d.cargo_class, quantity=d.quantity,
            offered_qty=total, new_grant_qty=new_grants[d.demand_id],
            confirmed_qty=d.prior_confirmed, shipped_qty=d.prior_shipped,
            manual_extra_qty=d.prior_manual + d.manual_delta, protected_qty=protected[d.demand_id],
            waitlist_rank=rank_by_id.get(d.demand_id),
            status=status, score=score, score_breakdown=breakdown,
            reasons=line_reasons, change_from_parent=total - d.prior_offered,
        ))

    group_summary = {
        group_id: {"used": used, "cap": group_cap,
                   "ratio": round(used / capacity, 4) if capacity else 0.0}
        for group_id, used in sorted(group_used.items())
    }
    allocated = capacity - max(0, remaining)
    warnings = []
    if total_held > capacity:
        warnings.append(
            f"既有持有量合计 {total_held} 超过当前舱位 {capacity}：限流不追溯削减，"
            "超出部分在释放/发运后自然消化")
    return AllocationResult(lines, capacity, allocated, max(0, remaining), policy_id,
                            policy_version, params, group_summary, warnings)


def fairness_summary(result: AllocationResult, demands: list[DemandInput]) -> dict[str, Any]:
    """汇总公平性指标，供跨政策版本比较。结果不写库，不影响既往分配。"""

    quantities = {d.demand_id: d.quantity for d in demands}
    groups: dict[str, dict[str, int]] = {}
    classes: dict[str, dict[str, int]] = {}
    for line in result.lines:
        quantity = quantities[line.demand_id]
        bucket = groups.setdefault(line.group_id, {"requested": 0, "offered": 0, "demands": 0})
        bucket["requested"] += quantity
        bucket["offered"] += line.offered_qty
        bucket["demands"] += 1
        cbucket = classes.setdefault(line.cargo_class, {"requested": 0, "offered": 0, "demands": 0})
        cbucket["requested"] += quantity
        cbucket["offered"] += line.offered_qty
        cbucket["demands"] += 1
    for bucket in (*groups.values(), *classes.values()):
        bucket["fill_rate"] = round(bucket["offered"] / bucket["requested"], 4) if bucket["requested"] else 0.0
    return {
        "policy_id": result.policy_id,
        "policy_version": result.policy_version,
        "capacity": result.capacity,
        "allocated": result.allocated,
        "utilization": round(result.allocated / result.capacity, 4) if result.capacity else 0.0,
        "satisfied": sum(1 for line in result.lines if line.offered_qty >= line.quantity),
        "partially_satisfied": sum(1 for line in result.lines if 0 < line.offered_qty < line.quantity),
        "waitlisted": sum(1 for line in result.lines if line.status == WAITLISTED),
        "by_group": groups,
        "by_cargo_class": classes,
    }
