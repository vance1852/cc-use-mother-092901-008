"""保供运力分配的应用服务：登记、冻结、分配运行、客户响应与人工例外。

所有写操作都走 ``BEGIN IMMEDIATE`` 短事务与 request_id 幂等表，
并发确认、重复请求因此被串行化与去重；每次状态变化都以纯函数引擎
重算并留下不可改写的运行快照与哈希链审计事件。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from functools import wraps
from typing import Any

from . import allocation
from .allocation import DemandInput, allocate, fairness_summary
from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService

ISO_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
CARGO_CLASSES = ("medical", "production", "general")

TRIGGER_FREEZE = "freeze"
TRIGGER_CAPACITY = "capacity_change"
TRIGGER_CONFIRM = "confirmation"
TRIGGER_SHIP = "shipment"
TRIGGER_RELEASE = "release"
TRIGGER_CANCEL = "cancellation"
TRIGGER_EXCEPTION = "exception"


def read_locked(func):
    """让只读方法在存储锁内执行，避免读到并发写事务的中间状态。"""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        with self.database.snapshot():
            return func(self, *args, **kwargs)

    return wrapper


class SupplyService(DomainService):
    """在基础服务的权限、幂等、审计能力上实现保供运力流程。"""

    # ------------------------------------------------------------------ 登记
    def register_corridor(self, *, request_id: str, actor_id: str, corridor_id: str, name: str):
        payload = {"corridor_id": corridor_id, "name": name}

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            corridor_id = self._identifier(corridor_id, "corridor_id")
            name = self._text(name, "name")

            def create():
                try:
                    conn.execute("INSERT INTO corridors(corridor_id,name,created_at) VALUES(?,?,?)",
                                 (corridor_id, name, self._now()))
                except Exception as exc:
                    raise ConflictError("通道编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="corridor.registered",
                             resource_type="corridor", resource_id=corridor_id,
                             detail={"name": name}, occurred_at=self._now())
                return "corridor", corridor_id, {"corridor_id": corridor_id}

            return self._idempotent(conn, request_id=request_id, action="register_corridor",
                                    payload=payload, create=create)

    def register_policy(self, *, request_id: str, actor_id: str, policy_id: str,
                        params: dict[str, Any], description: str):
        if not isinstance(params, dict):
            raise ValidationError("params 必须是对象")
        payload = {"policy_id": policy_id, "params": params, "description": description}

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            policy_id = self._identifier(policy_id, "policy_id")
            description = self._text(description, "description", 500)
            # 先校验参数可被引擎接受。
            normalized = allocation.merge_params(params)

            def create():
                row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM policies WHERE policy_id=?",
                                   (policy_id,)).fetchone()
                version = row["v"] + 1
                conn.execute(
                    "INSERT INTO policies(policy_id,version,params_json,description,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (policy_id, version, canonical_json(normalized), description, actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="policy.registered",
                             resource_type="policy", resource_id=f"{policy_id}:v{version}",
                             detail={"policy_id": policy_id, "version": version,
                                     "params_hash": digest(normalized)}, occurred_at=self._now())
                return "policy", f"{policy_id}:v{version}", {"policy_id": policy_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="register_policy",
                                    payload=payload, create=create)

    def activate_policy(self, *, request_id: str, actor_id: str, corridor_id: str,
                        policy_id: str, policy_version: int | None = None):
        payload = {"corridor_id": corridor_id, "policy_id": policy_id, "policy_version": policy_version}

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            self._get_corridor(conn, corridor_id)
            version = self._resolve_policy_version(conn, policy_id, policy_version)

            def create():
                conn.execute(
                    "INSERT INTO corridor_policy(corridor_id,policy_id,policy_version,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(corridor_id) DO UPDATE SET policy_id=excluded.policy_id, "
                    "policy_version=excluded.policy_version, updated_at=excluded.updated_at",
                    (corridor_id, policy_id, version, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="policy.activated",
                             resource_type="corridor", resource_id=corridor_id,
                             detail={"policy_id": policy_id, "policy_version": version},
                             occurred_at=self._now())
                return "corridor_policy", corridor_id, {"corridor_id": corridor_id,
                                                        "policy_id": policy_id, "policy_version": version}

            return self._idempotent(conn, request_id=request_id, action="activate_policy",
                                    payload=payload, create=create)

    def register_customer(self, *, request_id: str, actor_id: str, customer_id: str, group_id: str,
                          name: str, assurance_level: str, contract_priority: int,
                          performance_score: float):
        payload = {"customer_id": customer_id, "group_id": group_id, "name": name,
                   "assurance_level": assurance_level, "contract_priority": contract_priority,
                   "performance_score": performance_score}

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            customer_id = self._identifier(customer_id, "customer_id")
            group_id = self._identifier(group_id, "group_id")
            name = self._text(name, "name")
            if assurance_level not in ("strategic", "standard"):
                raise ValidationError("assurance_level 只能是 strategic 或 standard")
            if not isinstance(contract_priority, int) or not 0 <= contract_priority <= 100:
                raise ValidationError("contract_priority 必须是 0..100 的整数")
            if not isinstance(performance_score, (int, float)) or not 0 <= float(performance_score) <= 1:
                raise ValidationError("performance_score 必须是 0..1 之间的数值")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO customers(customer_id,group_id,name,assurance_level,"
                        "contract_priority,performance_score,created_at) VALUES(?,?,?,?,?,?,?)",
                        (customer_id, group_id, name, assurance_level, contract_priority,
                         float(performance_score), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("客户编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="customer.registered",
                             resource_type="customer", resource_id=customer_id,
                             detail={"group_id": group_id, "assurance_level": assurance_level},
                             occurred_at=self._now())
                return "customer", customer_id, {"customer_id": customer_id}

            return self._idempotent(conn, request_id=request_id, action="register_customer",
                                    payload=payload, create=create)

    def register_departure(self, *, request_id: str, actor_id: str, departure_id: str,
                           corridor_id: str, sequence_no: int, cutoff_at: str, departs_at: str,
                           capacity: int):
        payload = {"departure_id": departure_id, "corridor_id": corridor_id, "sequence_no": sequence_no,
                   "cutoff_at": cutoff_at, "departs_at": departs_at, "capacity": capacity}

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            departure_id = self._identifier(departure_id, "departure_id")
            self._get_corridor(conn, corridor_id)
            if not isinstance(sequence_no, int) or sequence_no < 1:
                raise ValidationError("sequence_no 必须是正整数")
            cutoff_at = self._stamp(cutoff_at, "cutoff_at")
            departs_at = self._stamp(departs_at, "departs_at")
            if not cutoff_at < departs_at:
                raise ValidationError("申报截止必须早于发车时刻")
            if not isinstance(capacity, int) or capacity < 0:
                raise ValidationError("capacity 必须是非负整数")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO departures(departure_id,corridor_id,sequence_no,cutoff_at,departs_at,"
                        "capacity,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (departure_id, corridor_id, sequence_no, cutoff_at, departs_at,
                         capacity, "open", self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("班次编号已存在或通道内班次序号重复") from exc
                conn.execute(
                    "INSERT INTO capacity_versions(version_id,departure_id,version,capacity,delta,reason,"
                    "created_by,created_at) VALUES(?,?,1,?,?,?,?,?)",
                    (uuid.uuid4().hex, departure_id, capacity, capacity, "初始登记舱位",
                     actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="departure.registered",
                             resource_type="departure", resource_id=departure_id,
                             detail={"corridor_id": corridor_id, "sequence_no": sequence_no,
                                     "capacity": capacity, "cutoff_at": cutoff_at},
                             occurred_at=self._now())
                return "departure", departure_id, {"departure_id": departure_id, "capacity": capacity}

            return self._idempotent(conn, request_id=request_id, action="register_departure",
                                    payload=payload, create=create)

    def submit_demand(self, *, request_id: str, actor_id: str, departure_id: str, demand_id: str,
                      customer_id: str, cargo_class: str, quantity: int, latest_load_at: str,
                      split_group_id: str | None = None, submitted_at: str | None = None):
        submitted_at = submitted_at or self._now()
        payload = {"departure_id": departure_id, "demand_id": demand_id, "customer_id": customer_id,
                   "cargo_class": cargo_class, "quantity": quantity, "latest_load_at": latest_load_at,
                   "split_group_id": split_group_id, "submitted_at": submitted_at}

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            departure = self._get_departure(conn, departure_id)
            if departure["status"] != "open":
                raise ConflictError("申报已截止，需求已冻结")
            customer = self._get_customer(conn, customer_id)
            demand_id = self._identifier(demand_id, "demand_id")
            if cargo_class not in CARGO_CLASSES:
                raise ValidationError(f"cargo_class 必须是 {', '.join(CARGO_CLASSES)} 之一")
            if not isinstance(quantity, int) or quantity <= 0:
                raise ValidationError("quantity 必须是正整数")
            latest_load_at = self._stamp(latest_load_at, "latest_load_at")
            submitted_stamp = self._stamp(submitted_at, "submitted_at")
            if split_group_id is not None:
                split_group_id = self._identifier(split_group_id, "split_group_id")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO demands(demand_id,departure_id,origin_departure_id,customer_id,group_id,"
                        "cargo_class,quantity,latest_load_at,split_group_id,submitted_at,closed,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?)",
                        (demand_id, departure_id, departure_id, customer_id, customer["group_id"],
                         cargo_class, quantity, latest_load_at, split_group_id, submitted_stamp,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("重复请求：同班次同客户同内容的申报已经存在（疑似拆单/重复提交）") from exc
                append_event(conn, actor_id=actor_id, action="demand.submitted",
                             resource_type="demand", resource_id=demand_id,
                             detail={"departure_id": departure_id, "customer_id": customer_id,
                                     "group_id": customer["group_id"], "cargo_class": cargo_class,
                                     "quantity": quantity, "split_group_id": split_group_id},
                             occurred_at=self._now())
                return "demand", demand_id, {"demand_id": demand_id}

            return self._idempotent(conn, request_id=request_id, action="submit_demand",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 冻结与分配

    def freeze_and_allocate(self, *, request_id: str, actor_id: str, departure_id: str):
        payload = {"departure_id": departure_id}

        with self.database.transaction(immediate=True) as conn:
            replayed = self._replayed_receipt(request_id, "freeze_allocate", payload)
            if replayed:
                return replayed
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            departure = self._get_departure(conn, departure_id)
            if departure["status"] != "open":
                raise ConflictError("班次不在可冻结状态")
            policy_id, policy_version, params = self._active_policy(conn, departure["corridor_id"])

            def create():
                conn.execute("UPDATE departures SET status='frozen' WHERE departure_id=?", (departure_id,))
                conn.execute("UPDATE demands SET closed=1 WHERE departure_id=?", (departure_id,))
                run_id = self._run_allocation(
                    conn, departure=departure, trigger=TRIGGER_FREEZE, actor_id=actor_id,
                    policy_id=policy_id, policy_version=policy_version, params=params, cancelled=False)
                append_event(conn, actor_id=actor_id, action="departure.frozen",
                             resource_type="departure", resource_id=departure_id,
                             detail={"run_id": run_id, "policy_id": policy_id,
                                     "policy_version": policy_version}, occurred_at=self._now())
                return "allocation_run", run_id, {"run_id": run_id}

            return self._idempotent(conn, request_id=request_id, action="freeze_allocate",
                                    payload=payload, create=create)

    def confirm_demand(self, *, request_id: str, actor_id: str, departure_id: str,
                       demand_id: str, confirm_qty: int):
        payload = {"departure_id": departure_id, "demand_id": demand_id, "confirm_qty": confirm_qty}

        with self.database.transaction(immediate=True) as conn:
            replayed = self._replayed_receipt(request_id, "confirm_demand", payload)
            if replayed:
                return replayed
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            departure = self._get_departure(conn, departure_id)
            if departure["status"] not in ("frozen",):
                raise ConflictError("班次未冻结或已结束，不能确认")
            entitlement = self._get_entitlement(conn, demand_id, departure_id)
            if not isinstance(confirm_qty, int) or confirm_qty <= 0:
                raise ValidationError("confirm_qty 必须是正整数")
            # shipped 是 confirmed 的子集，待确认额度即 offered - confirmed。
            unconfirmed = entitlement["offered_qty"] - entitlement["confirmed_qty"]
            if confirm_qty > unconfirmed:
                raise ConflictError(
                    f"并发或重复确认：待确认舱位仅 {unconfirmed}，本次请求 {confirm_qty}")

            def create():
                conn.execute(
                    "UPDATE entitlements SET confirmed_qty=confirmed_qty+?, updated_at=? WHERE demand_id=?",
                    (confirm_qty, self._now(), demand_id),
                )
                run_id = self._rerun(conn, departure, TRIGGER_CONFIRM, actor_id)
                append_event(conn, actor_id=actor_id, action="demand.confirmed",
                             resource_type="demand", resource_id=demand_id,
                             detail={"departure_id": departure_id, "confirm_qty": confirm_qty,
                                     "run_id": run_id}, occurred_at=self._now())
                return "allocation_run", run_id, {"run_id": run_id, "demand_id": demand_id,
                                                  "confirmed": confirm_qty}

            return self._idempotent(conn, request_id=request_id, action="confirm_demand",
                                    payload=payload, create=create)

    def ship_demand(self, *, request_id: str, actor_id: str, departure_id: str,
                    demand_id: str, ship_qty: int):
        payload = {"departure_id": departure_id, "demand_id": demand_id, "ship_qty": ship_qty}

        with self.database.transaction(immediate=True) as conn:
            replayed = self._replayed_receipt(request_id, "ship_demand", payload)
            if replayed:
                return replayed
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            departure = self._get_departure(conn, departure_id)
            if departure["status"] not in ("frozen", "departed"):
                raise ConflictError("班次未冻结，不能装运")
            entitlement = self._get_entitlement(conn, demand_id, departure_id)
            if not isinstance(ship_qty, int) or ship_qty <= 0:
                raise ValidationError("ship_qty 必须是正整数")
            shippable = entitlement["confirmed_qty"] - entitlement["shipped_qty"]
            if ship_qty > shippable:
                raise ConflictError(f"待装运确认舱位仅 {shippable}，本次请求 {ship_qty}")

            def create():
                conn.execute(
                    "UPDATE entitlements SET shipped_qty=shipped_qty+?, updated_at=? WHERE demand_id=?",
                    (ship_qty, self._now(), demand_id),
                )
                run_id = self._rerun(conn, departure, TRIGGER_SHIP, actor_id)
                append_event(conn, actor_id=actor_id, action="demand.shipped",
                             resource_type="demand", resource_id=demand_id,
                             detail={"departure_id": departure_id, "ship_qty": ship_qty,
                                     "run_id": run_id}, occurred_at=self._now())
                return "allocation_run", run_id, {"run_id": run_id, "demand_id": demand_id,
                                                  "shipped": ship_qty}

            return self._idempotent(conn, request_id=request_id, action="ship_demand",
                                    payload=payload, create=create)

    def mark_departed(self, *, request_id: str, actor_id: str, departure_id: str):
        """显式将班次标记为已发车；发车后舱位冻结、不再接受调整或取消。"""

        payload = {"departure_id": departure_id}

        with self.database.transaction(immediate=True) as conn:
            replayed = self._replayed_receipt(request_id, "mark_departed", payload)
            if replayed:
                return replayed
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            departure = self._get_departure(conn, departure_id)
            if departure["status"] != "frozen":
                raise ConflictError("只有冻结后的班次可以标记发车")

            def create():
                conn.execute("UPDATE departures SET status='departed' WHERE departure_id=?",
                             (departure_id,))
                append_event(conn, actor_id=actor_id, action="departure.departed",
                             resource_type="departure", resource_id=departure_id,
                             detail={}, occurred_at=self._now())
                return "departure", departure_id, {"departure_id": departure_id, "status": "departed"}

            return self._idempotent(conn, request_id=request_id, action="mark_departed",
                                    payload=payload, create=create)

    def release_demand(self, *, request_id: str, actor_id: str, departure_id: str, demand_id: str):
        payload = {"departure_id": departure_id, "demand_id": demand_id}

        with self.database.transaction(immediate=True) as conn:
            replayed = self._replayed_receipt(request_id, "release_demand", payload)
            if replayed:
                return replayed
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            departure = self._get_departure(conn, departure_id)
            if departure["status"] != "frozen":
                raise ConflictError("只有冻结后的班次可以放弃草案")
            entitlement = self._get_entitlement(conn, demand_id, departure_id)
            releasable = (entitlement["offered_qty"] - entitlement["confirmed_qty"]
                          - entitlement["shipped_qty"] - entitlement["manual_extra_qty"])
            if releasable <= 0:
                raise ConflictError("该票没有可放弃的未确认舱位")

            def create():
                conn.execute("UPDATE entitlements SET status='released', updated_at=? WHERE demand_id=?",
                             (self._now(), demand_id))
                run_id = self._rerun(conn, departure, TRIGGER_RELEASE, actor_id)
                append_event(conn, actor_id=actor_id, action="demand.released",
                             resource_type="demand", resource_id=demand_id,
                             detail={"departure_id": departure_id, "released_qty": releasable,
                                     "run_id": run_id}, occurred_at=self._now())
                return "allocation_run", run_id, {"run_id": run_id, "demand_id": demand_id,
                                                  "released": releasable}

            return self._idempotent(conn, request_id=request_id, action="release_demand",
                                    payload=payload, create=create)

    def adjust_capacity(self, *, request_id: str, actor_id: str, departure_id: str,
                        new_capacity: int, reason: str):
        payload = {"departure_id": departure_id, "new_capacity": new_capacity, "reason": reason}

        with self.database.transaction(immediate=True) as conn:
            replayed = self._replayed_receipt(request_id, "adjust_capacity", payload)
            if replayed:
                return replayed
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            departure = self._get_departure(conn, departure_id)
            if departure["status"] in ("departed", "cancelled"):
                raise ConflictError("已发车或已取消的班次不能调整舱位")
            if not isinstance(new_capacity, int) or new_capacity < 0:
                raise ValidationError("new_capacity 必须是非负整数")
            reason = self._text(reason, "reason", 300)
            delta = new_capacity - departure["capacity"]
            if delta == 0:
                raise ValidationError("新舱位与当前舱位一致，无需调整")

            def create():
                row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM capacity_versions "
                                   "WHERE departure_id=?", (departure_id,)).fetchone()
                conn.execute(
                    "INSERT INTO capacity_versions(version_id,departure_id,version,capacity,delta,reason,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, departure_id, row["v"] + 1, new_capacity, delta, reason,
                     actor_id, self._now()),
                )
                conn.execute("UPDATE departures SET capacity=? WHERE departure_id=?",
                             (new_capacity, departure_id))
                departure_obj = dict(departure)
                departure_obj["capacity"] = new_capacity
                detail = {"departure_id": departure_id, "old_capacity": departure["capacity"],
                          "new_capacity": new_capacity, "delta": delta, "reason": reason}
                run_id = None
                if departure["status"] == "frozen":
                    run_id = self._rerun(conn, departure_obj, TRIGGER_CAPACITY, actor_id)
                detail["run_id"] = run_id
                append_event(conn, actor_id=actor_id, action="departure.capacity_adjusted",
                             resource_type="departure", resource_id=departure_id,
                             detail=detail, occurred_at=self._now())
                return "departure", departure_id, {"departure_id": departure_id,
                                                    "capacity": new_capacity, "run_id": run_id}

            return self._idempotent(conn, request_id=request_id, action="adjust_capacity",
                                    payload=payload, create=create)

    def cancel_departure(self, *, request_id: str, actor_id: str, departure_id: str, reason: str):
        payload = {"departure_id": departure_id, "reason": reason}

        with self.database.transaction(immediate=True) as conn:
            replayed = self._replayed_receipt(request_id, "cancel_departure", payload)
            if replayed:
                return replayed
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            departure = self._get_departure(conn, departure_id)
            if departure["status"] == "cancelled":
                raise ConflictError("班次已经取消")
            if departure["status"] == "departed":
                raise ConflictError("已发车班次不能取消")
            reason = self._text(reason, "reason", 300)

            def create():
                conn.execute("UPDATE departures SET status='cancelled' WHERE departure_id=?",
                             (departure_id,))
                policy_id, policy_version, params = self._active_policy(conn, departure["corridor_id"])
                run_id = self._run_allocation(
                    conn, departure=departure, trigger=TRIGGER_CANCEL, actor_id=actor_id,
                    policy_id=policy_id, policy_version=policy_version, params=params, cancelled=True)
                append_event(conn, actor_id=actor_id, action="departure.cancelled",
                             resource_type="departure", resource_id=departure_id,
                             detail={"reason": reason, "run_id": run_id}, occurred_at=self._now())
                return "allocation_run", run_id, {"run_id": run_id}

            return self._idempotent(conn, request_id=request_id, action="cancel_departure",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 人工例外（四眼审批）

    def request_exception(self, *, request_id: str, actor_id: str, departure_id: str,
                          demand_id: str, extra_qty: int, justification: str):
        payload = {"departure_id": departure_id, "demand_id": demand_id, "extra_qty": extra_qty,
                   "justification": justification}

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            departure = self._get_departure(conn, departure_id)
            self._get_entitlement(conn, demand_id, departure_id)
            if departure["status"] != "frozen":
                raise ConflictError("只能对已冻结并产生草案的班次申请人工例外")
            if not isinstance(extra_qty, int) or extra_qty <= 0:
                raise ValidationError("extra_qty 必须是正整数")
            justification = self._text(justification, "justification", 500)

            def create():
                exception_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO exception_cases(exception_id,departure_id,demand_id,extra_qty,"
                    "justification,status,created_by,created_at) VALUES(?,?,?,?,?,'pending',?,?)",
                    (exception_id, departure_id, demand_id, extra_qty, justification,
                     actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="exception.requested",
                             resource_type="exception", resource_id=exception_id,
                             detail={"departure_id": departure_id, "demand_id": demand_id,
                                     "extra_qty": extra_qty}, occurred_at=self._now())
                return "exception", exception_id, {"exception_id": exception_id, "status": "pending"}

            return self._idempotent(conn, request_id=request_id, action="request_exception",
                                    payload=payload, create=create)

    def decide_exception(self, *, request_id: str, actor_id: str, exception_id: str,
                         approved: bool, decision_note: str = ""):
        payload = {"exception_id": exception_id, "approved": approved, "decision_note": decision_note}

        with self.database.transaction(immediate=True) as conn:
            replayed = self._replayed_receipt(request_id, "decide_exception", payload)
            if replayed:
                return replayed
            actor = self._actor(conn, actor_id)
            # 双人审批：批准方必须是另一人，且具备审批角色。
            self._require(actor, "admin", "reviewer")
            row = conn.execute("SELECT * FROM exception_cases WHERE exception_id=?",
                               (exception_id,)).fetchone()
            if row is None:
                raise NotFoundError("例外申请不存在")
            if row["status"] != "pending":
                raise ConflictError("该例外已经审批")
            if row["created_by"] == actor_id:
                raise PermissionDenied("人工例外必须由申请人之外的第二人审批")
            decision_note = self._text(decision_note, "decision_note", 500) if decision_note else ""

            def create():
                if not approved:
                    conn.execute(
                        "UPDATE exception_cases SET status='rejected', approved_by=?, decision_note=?, "
                        "decided_at=? WHERE exception_id=?",
                        (actor_id, decision_note, self._now(), exception_id),
                    )
                    append_event(conn, actor_id=actor_id, action="exception.rejected",
                                 resource_type="exception", resource_id=exception_id,
                                 detail={"demand_id": row["demand_id"]}, occurred_at=self._now())
                    return "exception", exception_id, {"exception_id": exception_id, "status": "rejected"}

                departure = self._get_departure(conn, row["departure_id"])
                before = self._latest_result(conn, departure["departure_id"])
                before_offered = {line["demand_id"]: line["offered_qty"]
                                  for line in before["lines"]} if before else {}
                # 例外面额作为本轮增量交给引擎，随分配结果一起落库，避免双写。
                run_id = self._rerun(conn, departure, TRIGGER_EXCEPTION, actor_id,
                                     manual_deltas={row["demand_id"]: row["extra_qty"]})
                after = self._latest_result(conn, departure["departure_id"])
                changes = []
                for line in after["lines"]:
                    old = before_offered.get(line["demand_id"], 0)
                    if line["offered_qty"] != old:
                        changes.append({"demand_id": line["demand_id"], "before": old,
                                        "after": line["offered_qty"],
                                        "delta": line["offered_qty"] - old})
                impact = {"run_id": run_id, "capacity": after["capacity"],
                          "allocated": after["allocated"], "changes": changes}
                conn.execute(
                    "UPDATE exception_cases SET status='approved', approved_by=?, decision_note=?, "
                    "impact_json=?, decided_at=? WHERE exception_id=?",
                    (actor_id, decision_note, canonical_json(impact), self._now(), exception_id),
                )
                append_event(conn, actor_id=actor_id, action="exception.approved",
                             resource_type="exception", resource_id=exception_id,
                             detail={"departure_id": departure["departure_id"],
                                     "demand_id": row["demand_id"], "extra_qty": row["extra_qty"],
                                     "run_id": run_id, "impact_hash": digest(impact)},
                             occurred_at=self._now())
                return "exception", exception_id, {"exception_id": exception_id, "status": "approved",
                                                    "run_id": run_id}

            return self._idempotent(conn, request_id=request_id, action="decide_exception",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 查询

    @read_locked
    def get_exception(self, exception_id: str) -> dict[str, Any]:
        conn = self.database.connection
        row = conn.execute("SELECT * FROM exception_cases WHERE exception_id=?",
                           (exception_id,)).fetchone()
        if row is None:
            raise NotFoundError("例外申请不存在")
        result = dict(row)
        result["impact"] = json.loads(row["impact_json"]) if row["impact_json"] else None
        return result

    @read_locked
    def get_departure(self, departure_id: str) -> dict[str, Any]:
        conn = self.database.connection
        row = self._get_departure(conn, departure_id)
        result = dict(row)
        cap = conn.execute("SELECT * FROM capacity_versions WHERE departure_id=? ORDER BY version",
                           (departure_id,)).fetchall()
        result["capacity_versions"] = [dict(item) for item in cap]
        latest = self._latest_run_row(conn, departure_id)
        result["latest_run_id"] = latest["run_id"] if latest else None
        return result

    @read_locked
    def list_entitlements(self, departure_id: str) -> list[dict[str, Any]]:
        conn = self.database.connection
        self._get_departure(conn, departure_id)
        rows = conn.execute(
            "SELECT e.*, d.cargo_class, d.quantity, d.customer_id, d.group_id, d.split_group_id "
            "FROM entitlements e JOIN demands d ON e.demand_id=d.demand_id "
            "WHERE e.departure_id=? ORDER BY e.waitlist_rank IS NULL, e.waitlist_rank, d.submitted_at",
            (departure_id,)).fetchall()
        return [dict(row) for row in rows]

    @read_locked
    def explain_demand(self, demand_id: str) -> dict[str, Any]:
        """给出每票需求的获得/落选原因、候补位置与恢复后的增配来源。"""

        conn = self.database.connection
        demand = conn.execute("SELECT * FROM demands WHERE demand_id=?", (demand_id,)).fetchone()
        if demand is None:
            raise NotFoundError("需求不存在")
        departure = self._get_departure(conn, demand["departure_id"])
        entitlement = conn.execute("SELECT * FROM entitlements WHERE demand_id=?",
                                   (demand_id,)).fetchone()
        timeline = []
        rows = conn.execute(
            "SELECT r.run_id, r.trigger_type, r.parent_run_id, r.policy_id, r.policy_version, "
            "r.created_at, l.line_json FROM allocation_runs r "
            "JOIN allocation_run_lines l ON r.run_id=l.run_id "
            "WHERE r.departure_id=? AND l.demand_id=? ORDER BY r.created_at, r.rowid",
            (demand["departure_id"], demand_id)).fetchall()
        for row in rows:
            line = json.loads(row["line_json"])
            timeline.append({
                "run_id": row["run_id"], "trigger_type": row["trigger_type"],
                "parent_run_id": row["parent_run_id"],
                "policy_id": row["policy_id"], "policy_version": row["policy_version"],
                "created_at": row["created_at"],
                "offered_qty": line["offered_qty"], "new_grant_qty": line.get("new_grant_qty", 0),
                "confirmed_qty": line["confirmed_qty"], "shipped_qty": line["shipped_qty"],
                "status": line["status"], "waitlist_rank": line["waitlist_rank"],
                "change_from_parent": line["change_from_parent"],
                "score": line["score"], "score_breakdown": line["score_breakdown"],
                "reasons": line["reasons"],
            })
        # 增配来源：变化量为正的历史运行。
        increments = [
            {"run_id": item["run_id"], "trigger_type": item["trigger_type"],
             "delta": item["change_from_parent"], "created_at": item["created_at"],
             "reasons": item["reasons"]}
            for item in timeline if item["change_from_parent"] > 0
        ]
        latest_line = timeline[-1] if timeline else None
        return {
            "demand": dict(demand),
            "departure": {"departure_id": departure["departure_id"], "status": departure["status"],
                          "capacity": departure["capacity"], "cutoff_at": departure["cutoff_at"]},
            "entitlement": dict(entitlement) if entitlement else None,
            "latest": latest_line,
            "increments": increments,
            "timeline": timeline,
        }

    @read_locked
    def run_history(self, departure_id: str) -> list[dict[str, Any]]:
        conn = self.database.connection
        self._get_departure(conn, departure_id)
        rows = conn.execute(
            "SELECT run_id, trigger_type, parent_run_id, policy_id, policy_version, capacity, "
            "length(result_json) AS result_size, created_by, created_at "
            "FROM allocation_runs WHERE departure_id=? ORDER BY created_at, rowid",
            (departure_id,)).fetchall()
        return [dict(row) for row in rows]

    @read_locked
    def get_run(self, run_id: str) -> dict[str, Any]:
        conn = self.database.connection
        row = conn.execute("SELECT * FROM allocation_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError("分配运行不存在")
        result = {key: row[key] for key in row.keys()}
        result["result"] = json.loads(row["result_json"])
        result["input"] = json.loads(row["input_json"])
        return result

    @read_locked
    def compare_fairness(self, departure_id: str, policy_selectors: list[dict[str, str]] | None = None):
        """用同一冻结输入在不同政策版本下复算公平性，不写库、不改写既往结果。"""

        conn = self.database.connection
        departure = self._get_departure(conn, departure_id)
        # 公平性比较必须基于冻结那一刻的干净需求快照，而不是含后续确认/例外的最近输入。
        frozen = conn.execute(
            "SELECT * FROM allocation_runs WHERE departure_id=? AND trigger_type=? "
            "ORDER BY rowid LIMIT 1", (departure_id, TRIGGER_FREEZE)).fetchone()
        latest = self._latest_run_row(conn, departure_id)
        if latest is None:
            raise ConflictError("班次尚未冻结分配，没有可比较的运行")
        inputs = [DemandInput(**item) for item in json.loads(frozen["input_json"])]
        if policy_selectors:
            selected = [(item["policy_id"], self._resolve_policy_version(
                conn, item["policy_id"], item.get("policy_version"))) for item in policy_selectors]
        else:
            rows = conn.execute(
                "SELECT policy_id, MAX(version) AS version FROM policies GROUP BY policy_id").fetchall()
            selected = [(row["policy_id"], row["version"]) for row in rows]
        comparisons = []
        for policy_id, version in selected:
            params = self._policy_params(conn, policy_id, version)
            result = allocate(inputs, capacity=departure["capacity"], cutoff_at=departure["cutoff_at"],
                              policy_id=policy_id, policy_version=version, params=params,
                              cancelled=departure["status"] == "cancelled")
            comparisons.append(fairness_summary(result, inputs))
        return {"departure_id": departure_id, "frozen_run_id": latest["run_id"],
                "active_policy": {"policy_id": latest["policy_id"], "policy_version": latest["policy_version"]},
                "comparisons": comparisons}

    @read_locked
    def replay_run(self, run_id: str, policy_id: str, policy_version: int | None = None) -> dict[str, Any]:
        """以指定政策版本复算任意历史运行的输入快照，不落库。"""

        conn = self.database.connection
        row = conn.execute("SELECT * FROM allocation_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError("分配运行不存在")
        departure = self._get_departure(conn, row["departure_id"])
        version = self._resolve_policy_version(conn, policy_id, policy_version)
        params = self._policy_params(conn, policy_id, version)
        inputs = [DemandInput(**item) for item in json.loads(row["input_json"])]
        result = allocate(inputs, capacity=row["capacity"], cutoff_at=departure["cutoff_at"],
                          policy_id=policy_id, policy_version=version, params=params,
                          cancelled=row["trigger_type"] == TRIGGER_CANCEL)
        return {"replayed_run_id": run_id, "policy_id": policy_id, "policy_version": version,
                "fairness": fairness_summary(result, inputs), "result": result.to_dict()}

    # ------------------------------------------------------------------ 内部

    def _replayed_receipt(self, request_id: str, action: str, payload: dict[str, Any]):
        """在状态迁移守卫之前做幂等短路，避免重放被「状态已变更」误拒。

        首次请求（request_id 不存在或非法）返回 None，走正常流程；
        已存在但动作/载荷不一致则按冲突处理，与基础服务幂等语义一致。
        """

        try:
            rid = str(request_id).strip()
        except Exception:
            return None
        if not rid:
            return None
        row = self.database.connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (rid,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(rid, row["resource_type"], row["resource_id"], True)

    def _stamp(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not ISO_STAMP.fullmatch(value):
            raise ValidationError(f"{field} 必须是 YYYY-MM-DDTHH:MM:SSZ 形式的 UTC 时间")
        return value

    def _get_corridor(self, conn, corridor_id: str):
        row = conn.execute("SELECT * FROM corridors WHERE corridor_id=?", (corridor_id,)).fetchone()
        if row is None:
            raise NotFoundError("通道不存在")
        return row

    def _get_customer(self, conn, customer_id: str):
        row = conn.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
        if row is None:
            raise NotFoundError("客户不存在")
        return row

    def _get_departure(self, conn, departure_id: str):
        row = conn.execute("SELECT * FROM departures WHERE departure_id=?", (departure_id,)).fetchone()
        if row is None:
            raise NotFoundError("班次不存在")
        return row

    def _get_entitlement(self, conn, demand_id: str, departure_id: str):
        row = conn.execute("SELECT * FROM entitlements WHERE demand_id=? AND departure_id=?",
                           (demand_id, departure_id)).fetchone()
        if row is None:
            raise NotFoundError("该需求在本班次没有分配记录")
        return row

    def _resolve_policy_version(self, conn, policy_id: str, version: int | None) -> int:
        if version is None:
            row = conn.execute("SELECT MAX(version) AS v FROM policies WHERE policy_id=?",
                               (policy_id,)).fetchone()
            if row["v"] is None:
                raise NotFoundError("政策不存在")
            return row["v"]
        row = conn.execute("SELECT 1 FROM policies WHERE policy_id=? AND version=?",
                           (policy_id, version)).fetchone()
        if row is None:
            raise NotFoundError("政策版本不存在")
        return version

    def _policy_params(self, conn, policy_id: str, version: int) -> dict[str, Any]:
        row = conn.execute("SELECT params_json FROM policies WHERE policy_id=? AND version=?",
                           (policy_id, version)).fetchone()
        return json.loads(row["params_json"])

    def _active_policy(self, conn, corridor_id: str) -> tuple[str, int, dict[str, Any]]:
        row = conn.execute("SELECT * FROM corridor_policy WHERE corridor_id=?",
                           (corridor_id,)).fetchone()
        if row is None:
            raise ConflictError("通道尚未激活任何分配政策")
        params = self._policy_params(conn, row["policy_id"], row["policy_version"])
        return row["policy_id"], row["policy_version"], params

    def _latest_run_row(self, conn, departure_id: str):
        return conn.execute(
            "SELECT * FROM allocation_runs WHERE departure_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (departure_id,)).fetchone()

    def _latest_result(self, conn, departure_id: str) -> dict[str, Any] | None:
        row = self._latest_run_row(conn, departure_id)
        return json.loads(row["result_json"]) if row else None

    def _build_inputs(self, conn, departure, manual_deltas: dict[str, int] | None = None) -> list[DemandInput]:
        manual_deltas = manual_deltas or {}
        rows = conn.execute(
            "SELECT d.*, c.contract_priority, c.performance_score, c.assurance_level "
            "FROM demands d JOIN customers c ON d.customer_id=c.customer_id "
            "WHERE d.departure_id=? AND d.closed=1 ORDER BY d.submitted_at, d.demand_id",
            (departure["departure_id"],)).fetchall()
        inputs = []
        for row in rows:
            ent = conn.execute("SELECT * FROM entitlements WHERE demand_id=?",
                               (row["demand_id"],)).fetchone()
            inputs.append(DemandInput(
                demand_id=row["demand_id"], customer_id=row["customer_id"], group_id=row["group_id"],
                cargo_class=row["cargo_class"], quantity=row["quantity"],
                latest_load_at=row["latest_load_at"], submitted_at=row["submitted_at"],
                split_group_id=row["split_group_id"],
                contract_priority=row["contract_priority"],
                performance_score=row["performance_score"], assurance_level=row["assurance_level"],
                prior_status=ent["status"] if ent else allocation.WAITLISTED,
                prior_offered=ent["offered_qty"] if ent else 0,
                prior_confirmed=ent["confirmed_qty"] if ent else 0,
                prior_shipped=ent["shipped_qty"] if ent else 0,
                prior_manual=ent["manual_extra_qty"] if ent else 0,
                prior_waitlist_rank=ent["waitlist_rank"] if ent else None,
                manual_delta=manual_deltas.get(row["demand_id"], 0),
            ))
        return inputs

    def _run_allocation(self, conn, *, departure, trigger: str, actor_id: str,
                        policy_id: str, policy_version: int, params: dict[str, Any],
                        cancelled: bool, manual_deltas: dict[str, int] | None = None) -> str:
        inputs = self._build_inputs(conn, departure, manual_deltas)
        result = allocate(inputs, capacity=departure["capacity"], cutoff_at=departure["cutoff_at"],
                          policy_id=policy_id, policy_version=policy_version, params=params,
                          cancelled=cancelled)
        return self._persist_run(conn, departure=departure, trigger=trigger, actor_id=actor_id,
                                 policy_id=policy_id, policy_version=policy_version,
                                 params=params, inputs=inputs, result=result)

    def _rerun(self, conn, departure, trigger: str, actor_id: str,
               manual_deltas: dict[str, int] | None = None) -> str:
        policy_id, policy_version, params = self._active_policy(conn, departure["corridor_id"])
        return self._run_allocation(conn, departure=departure, trigger=trigger, actor_id=actor_id,
                                    policy_id=policy_id, policy_version=policy_version,
                                    params=params, cancelled=departure["status"] == "cancelled",
                                    manual_deltas=manual_deltas)

    def _persist_run(self, conn, *, departure, trigger: str, actor_id: str, policy_id: str,
                     policy_version: int, params: dict[str, Any], inputs: list[DemandInput],
                     result) -> str:
        parent = self._latest_run_row(conn, departure["departure_id"])
        run_id = uuid.uuid4().hex
        now = self._now()
        result_dict = result.to_dict()
        input_payload = [asdict(item) for item in inputs]
        conn.execute(
            "INSERT INTO allocation_runs(run_id,departure_id,trigger_type,parent_run_id,policy_id,"
            "policy_version,params_json,capacity,input_json,result_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, departure["departure_id"], trigger, parent["run_id"] if parent else None,
             policy_id, policy_version, canonical_json(params), departure["capacity"],
             canonical_json(input_payload), canonical_json(result_dict), actor_id, now),
        )
        for line in result.lines:
            conn.execute(
                "INSERT INTO allocation_run_lines(run_id,demand_id,line_json) VALUES(?,?,?)",
                (run_id, line.demand_id, canonical_json(line.to_dict())),
            )
            conn.execute(
                "INSERT INTO entitlements(demand_id,departure_id,offered_qty,confirmed_qty,"
                "shipped_qty,manual_extra_qty,waitlist_rank,status,version,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,1,?) "
                "ON CONFLICT(demand_id) DO UPDATE SET offered_qty=excluded.offered_qty, "
                "confirmed_qty=excluded.confirmed_qty, shipped_qty=excluded.shipped_qty, "
                "manual_extra_qty=excluded.manual_extra_qty, waitlist_rank=excluded.waitlist_rank, "
                "status=excluded.status, version=entitlements.version+1, updated_at=excluded.updated_at",
                (line.demand_id, departure["departure_id"], line.offered_qty, line.confirmed_qty,
                 line.shipped_qty, line.manual_extra_qty, line.waitlist_rank, line.status, now),
            )
        append_event(conn, actor_id=actor_id, action="allocation.computed",
                     resource_type="allocation_run", resource_id=run_id,
                     detail={"departure_id": departure["departure_id"], "trigger_type": trigger,
                             "parent_run_id": parent["run_id"] if parent else None,
                             "policy_id": policy_id, "policy_version": policy_version,
                             "capacity": result.capacity, "allocated": result.allocated,
                             "available": result.available,
                             "input_hash": digest(input_payload),
                             "result_hash": digest(result_dict)},
                     occurred_at=now)
        return run_id
