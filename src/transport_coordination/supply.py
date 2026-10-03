"""保供运力分配服务：登记、冻结、确认与稳定顺序再分配。

所有写操作都在 IMMEDIATE 事务内完成并写入哈希审计链，request_id 提供
跨动作的幂等回执。分配结论本身由 allocator.reconcile 纯函数计算，
每次调和的完整输入快照随 run 持久化，任何历史结论都可以离线复算。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from . import allocator
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .storage import Database


class SupplyService:
    """保供运力场景的应用服务。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    # ---- 通用辅助 ----

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create) -> WriteReceipt:
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _positive_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationError(f"{field} 必须是正整数")
        return value

    def _non_negative_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"{field} 必须是非负整数")
        return value

    # ---- 基础登记 ----

    def register_corridor(self, *, request_id: str, actor_id: str,
                          corridor_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "corridor_id": corridor_id, "name": name}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")

            def create():
                try:
                    conn.execute("INSERT INTO corridors(corridor_id,name,created_by,created_at) VALUES(?,?,?,?)",
                                 (corridor_id, name, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("通道编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="corridor.registered",
                            resource_type="corridor", resource_id=corridor_id, detail={"name": name})
                return "corridor", corridor_id, {"corridor_id": corridor_id}

            return self._idempotent(conn, request_id=request_id, action="register_corridor",
                                    payload=payload, create=create)

    def register_group(self, *, request_id: str, actor_id: str, group_id: str,
                       name: str, cap_per_run: int) -> WriteReceipt:
        cap_per_run = self._non_negative_int(cap_per_run, "cap_per_run")
        payload = {"actor_id": actor_id, "group_id": group_id, "name": name, "cap_per_run": cap_per_run}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO customer_groups(group_id,name,cap_per_run,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (group_id, name, cap_per_run, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("集团编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="group.registered",
                            resource_type="customer_group", resource_id=group_id,
                            detail={"name": name, "cap_per_run": cap_per_run})
                return "customer_group", group_id, {"group_id": group_id}

            return self._idempotent(conn, request_id=request_id, action="register_group",
                                    payload=payload, create=create)

    def register_customer(self, *, request_id: str, actor_id: str, customer_id: str,
                          group_id: str, name: str, cap_per_run: int) -> WriteReceipt:
        cap_per_run = self._non_negative_int(cap_per_run, "cap_per_run")
        payload = {"actor_id": actor_id, "customer_id": customer_id, "group_id": group_id,
                   "name": name, "cap_per_run": cap_per_run}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")
            if conn.execute("SELECT 1 FROM customer_groups WHERE group_id=?", (group_id,)).fetchone() is None:
                raise NotFoundError("集团不存在")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO customers(customer_id,group_id,name,cap_per_run,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (customer_id, group_id, name, cap_per_run, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("客户编号已经存在或集团无效") from exc
                self._audit(conn, actor_id=actor_id, action="customer.registered",
                            resource_type="customer", resource_id=customer_id,
                            detail={"group_id": group_id, "name": name, "cap_per_run": cap_per_run})
                return "customer", customer_id, {"customer_id": customer_id}

            return self._idempotent(conn, request_id=request_id, action="register_customer",
                                    payload=payload, create=create)

    def register_policy_version(self, *, request_id: str, actor_id: str,
                                policy_version_id: str, params: dict[str, Any],
                                activate: bool = False) -> WriteReceipt:
        normalized = allocator.validate_policy_params(params)
        payload = {"actor_id": actor_id, "policy_version_id": policy_version_id,
                   "params": normalized, "activate": bool(activate)}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO policy_versions(policy_version_id,params_json,active,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (policy_version_id, canonical_json(normalized), 1 if activate else 0,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("政策版本编号已经存在") from exc
                if activate:
                    conn.execute("UPDATE policy_versions SET active=0 WHERE policy_version_id<>?",
                                 (policy_version_id,))
                self._audit(conn, actor_id=actor_id, action="policy.registered",
                            resource_type="policy_version", resource_id=policy_version_id,
                            detail={"params": normalized, "activate": bool(activate)})
                return "policy_version", policy_version_id, {"policy_version_id": policy_version_id}

            return self._idempotent(conn, request_id=request_id, action="register_policy_version",
                                    payload=payload, create=create)

    def _active_policy(self, conn) -> str:
        row = conn.execute(
            "SELECT policy_version_id FROM policy_versions WHERE active=1 ORDER BY created_at, policy_version_id"
        ).fetchone()
        if row is None:
            raise ValidationError("尚未登记激活的政策版本")
        return row["policy_version_id"]

    def register_train_version(self, *, request_id: str, actor_id: str, version_id: str,
                               corridor_id: str, departure_at: str, freeze_at: str,
                               capacity: int, policy_version_id: str | None = None) -> WriteReceipt:
        capacity = self._non_negative_int(capacity, "capacity")
        departure_at = str(departure_at).strip()
        freeze_at = str(freeze_at).strip()
        if not departure_at or not freeze_at:
            raise ValidationError("departure_at 与 freeze_at 不能为空")
        payload = {"actor_id": actor_id, "version_id": version_id, "corridor_id": corridor_id,
                   "departure_at": departure_at, "freeze_at": freeze_at, "capacity": capacity,
                   "policy_version_id": policy_version_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            if conn.execute("SELECT 1 FROM corridors WHERE corridor_id=?", (corridor_id,)).fetchone() is None:
                raise NotFoundError("通道不存在")
            policy_version_id = policy_version_id or self._active_policy(conn)
            if conn.execute("SELECT 1 FROM policy_versions WHERE policy_version_id=?",
                            (policy_version_id,)).fetchone() is None:
                raise NotFoundError("政策版本不存在")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO train_versions(version_id,corridor_id,departure_at,freeze_at,capacity,"
                        "policy_version_id,status,created_by,created_at) VALUES(?,?,?,?,?,?,'open',?,?)",
                        (version_id, corridor_id, departure_at, freeze_at, capacity,
                         policy_version_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("班次版本编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="train_version.registered",
                            resource_type="train_version", resource_id=version_id,
                            detail={"corridor_id": corridor_id, "capacity": capacity,
                                    "policy_version_id": policy_version_id})
                return "train_version", version_id, {"version_id": version_id}

            return self._idempotent(conn, request_id=request_id, action="register_train_version",
                                    payload=payload, create=create)

    # ---- 需求申报 ----

    def submit_demand(self, *, request_id: str, actor_id: str, version_id: str, client_key: str,
                      customer_id: str, quantity: int, strategic_level: int,
                      cargo_deadline: str, split_root_id: str | None = None,
                      fulfillment_rate: float = 0.8) -> WriteReceipt:
        quantity = self._positive_int(quantity, "quantity")
        if isinstance(strategic_level, bool) or not isinstance(strategic_level, int) \
                or not 1 <= strategic_level <= 5:
            raise ValidationError("strategic_level 必须位于 1 到 5")
        if not isinstance(fulfillment_rate, (int, float)) or isinstance(fulfillment_rate, bool) \
                or not 0.0 <= float(fulfillment_rate) <= 1.0:
            raise ValidationError("fulfillment_rate 必须位于 0 到 1")
        cargo_deadline = str(cargo_deadline).strip()
        client_key = str(client_key).strip()
        if not cargo_deadline or not client_key:
            raise ValidationError("client_key 与 cargo_deadline 不能为空")
        payload = {"actor_id": actor_id, "version_id": version_id, "client_key": client_key,
                   "customer_id": customer_id, "quantity": quantity,
                   "strategic_level": strategic_level, "cargo_deadline": cargo_deadline,
                   "split_root_id": split_root_id, "fulfillment_rate": float(fulfillment_rate)}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            version = conn.execute("SELECT * FROM train_versions WHERE version_id=?", (version_id,)).fetchone()
            if version is None:
                raise NotFoundError("班次版本不存在")
            if version["status"] != "open":
                raise ConflictError("班次已冻结或取消，不能再申报需求")
            customer = conn.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
            if customer is None:
                raise NotFoundError("客户不存在")
            group_id = customer["group_id"]
            split_root_id = split_root_id or client_key
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(submitted_seq),0)+1 AS next_seq FROM demands WHERE version_id=?",
                (version_id,),
            ).fetchone()
            submitted_seq = seq_row["next_seq"]
            demand_id = uuid.uuid4().hex

            def create():
                try:
                    conn.execute(
                        "INSERT INTO demands(demand_id,version_id,customer_id,group_id,client_key,quantity,"
                        "strategic_level,cargo_deadline,split_root_id,submitted_seq,fulfillment_rate,"
                        "requested_qty,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (demand_id, version_id, customer_id, group_id, client_key, quantity,
                         strategic_level, cargo_deadline, split_root_id, submitted_seq,
                         float(fulfillment_rate), quantity, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同班业务键 client_key 已存在") from exc
                # 近似重复请求识别：同客户、同量、同截止时刻但业务键不同。
                duplicate = conn.execute(
                    "SELECT demand_id FROM demands WHERE version_id=? AND customer_id=? AND quantity=? "
                    "AND cargo_deadline=? AND client_key<>? LIMIT 1",
                    (version_id, customer_id, quantity, cargo_deadline, client_key),
                ).fetchone()
                if duplicate is not None:
                    self._add_flag(conn, demand_id=demand_id, code="DUPLICATE_REQUEST",
                                   detail={"similar_demand_id": duplicate["demand_id"]}, actor_id=actor_id)
                # 集团绕过限额识别：同一拆单关联号出现在同集团的其他客户名下。
                if split_root_id != client_key:
                    related_rows = conn.execute(
                        "SELECT demand_id,customer_id FROM demands WHERE version_id=? AND group_id=? "
                        "AND split_root_id=? AND customer_id<>?",
                        (version_id, group_id, split_root_id, customer_id),
                    ).fetchall()
                    if related_rows:
                        self._add_flag(conn, demand_id=demand_id, code="GROUP_CAP_CIRCUMVENTION",
                                       detail={"related_demand_id": related_rows[0]["demand_id"],
                                               "related_customer_id": related_rows[0]["customer_id"],
                                               "group_id": group_id}, actor_id=actor_id)
                        for related in related_rows:
                            self._add_flag(conn, demand_id=related["demand_id"],
                                           code="GROUP_CAP_CIRCUMVENTION",
                                           detail={"related_demand_id": demand_id,
                                                   "related_customer_id": customer_id,
                                                   "group_id": group_id}, actor_id=actor_id)
                self._audit(conn, actor_id=actor_id, action="demand.submitted",
                            resource_type="demand", resource_id=demand_id,
                            detail={"version_id": version_id, "customer_id": customer_id,
                                    "group_id": group_id, "quantity": quantity,
                                    "strategic_level": strategic_level, "split_root_id": split_root_id,
                                    "submitted_seq": submitted_seq})
                return "demand", demand_id, {"demand_id": demand_id, "submitted_seq": submitted_seq}

            return self._idempotent(conn, request_id=request_id, action="submit_demand",
                                    payload=payload, create=create)

    def _add_flag(self, conn, *, demand_id: str, code: str, detail: dict[str, Any], actor_id: str) -> None:
        conn.execute(
            "INSERT INTO demand_flags(flag_id,demand_id,flag_code,detail_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, demand_id, code, canonical_json(detail), actor_id, self._now()),
        )

    # ---- 冻结与调和 ----

    def _load_snapshot_parts(self, conn, version_id: str):
        version = conn.execute("SELECT * FROM train_versions WHERE version_id=?", (version_id,)).fetchone()
        if version is None:
            raise NotFoundError("班次版本不存在")
        rows = conn.execute("SELECT * FROM demands WHERE version_id=? AND active=1", (version_id,)).fetchall()
        demands = [allocator.DemandSnapshot(
            demand_id=row["demand_id"], customer_id=row["customer_id"], group_id=row["group_id"],
            quantity=row["quantity"], strategic_level=row["strategic_level"],
            cargo_deadline=row["cargo_deadline"], split_root_id=row["split_root_id"],
            submitted_seq=row["submitted_seq"], fulfillment_rate=row["fulfillment_rate"],
        ) for row in rows]
        customer_caps = {row["customer_id"]: row["cap_per_run"]
                         for row in conn.execute("SELECT customer_id,cap_per_run FROM customers")}
        group_caps = {row["group_id"]: row["cap_per_run"]
                      for row in conn.execute("SELECT group_id,cap_per_run FROM customer_groups")}
        policy = conn.execute("SELECT * FROM policy_versions WHERE policy_version_id=?",
                              (version["policy_version_id"],)).fetchone()
        policy_params = json.loads(policy["params_json"])
        return version, demands, customer_caps, group_caps, policy_params

    def _latest_run(self, conn, version_id: str):
        return conn.execute(
            "SELECT * FROM allocation_runs WHERE version_id=? ORDER BY sequence_no DESC LIMIT 1",
            (version_id,),
        ).fetchone()

    def _persist_run(self, conn, *, version, result, basis: str, policy_version_id: str,
                     actor_id: str, capacity_event_id: str | None, snapshot: dict[str, Any]) -> str:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence_no),0)+1 AS next_no FROM allocation_runs WHERE version_id=?",
            (version["version_id"],),
        ).fetchone()
        run_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO allocation_runs(run_id,version_id,sequence_no,basis,policy_version_id,"
            "capacity_event_id,capacity,input_json,total_offered,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, version["version_id"], row["next_no"], basis, policy_version_id,
             capacity_event_id, result.capacity, canonical_json(snapshot), result.total_offered,
             actor_id, self._now()),
        )
        for line in result.lines:
            conn.execute(
                "INSERT INTO allocation_lines(run_id,demand_id,requested_qty,offered_qty,protected_qty,"
                "granted_qty,added_qty,reduced_qty,waitlist_position,priority_rank,reasons_json,factors_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, line.demand_id, line.quantity, line.offered_qty, line.protected_qty,
                 line.granted_qty, line.added_qty, line.reduced_qty, line.waitlist_position,
                 line.priority_rank, canonical_json(list(line.reasons)),
                 canonical_json(line.factor_values)),
            )
        return run_id

    def freeze_demand_book(self, *, request_id: str, actor_id: str, version_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            version = conn.execute("SELECT * FROM train_versions WHERE version_id=?", (version_id,)).fetchone()
            if version is None:
                raise NotFoundError("班次版本不存在")
            if version["status"] != "open":
                raise ConflictError("班次不在可冻结状态")
            _, demands, customer_caps, group_caps, policy_params = self._load_snapshot_parts(conn, version_id)

            def create():
                grants = self._grants(conn, version_id)
                result = allocator.reconcile(
                    demands, capacity=version["capacity"], customer_caps=customer_caps,
                    group_caps=group_caps, policy_params=policy_params, grants=grants)
                snapshot = {"basis": "freeze", "capacity": version["capacity"],
                            "policy_version_id": version["policy_version_id"],
                            "policy_params": policy_params,
                            "customer_caps": customer_caps, "group_caps": group_caps,
                            "demands": [d.__dict__ for d in demands], "grants": grants}
                run_id = self._persist_run(conn, version=version, result=result, basis="freeze",
                                           policy_version_id=version["policy_version_id"],
                                           actor_id=actor_id, capacity_event_id=None, snapshot=snapshot)
                conn.execute("UPDATE train_versions SET status='frozen',frozen_at=? WHERE version_id=?",
                             (self._now(), version_id))
                self._audit(conn, actor_id=actor_id, action="demand_book.frozen",
                            resource_type="allocation_run", resource_id=run_id,
                            detail={"version_id": version_id, "total_offered": result.total_offered,
                                    "capacity": result.capacity})
                return "allocation_run", run_id, {"run_id": run_id}

            return self._idempotent(conn, request_id=request_id, action="freeze_demand_book",
                                    payload=payload, create=create)

    def _grants(self, conn, version_id: str) -> dict[str, int]:
        grants: dict[str, int] = {}
        for row in conn.execute(
            "SELECT demand_id,grant_qty FROM exceptions WHERE version_id=? AND status='approved'",
            (version_id,),
        ):
            grants[row["demand_id"]] = grants.get(row["demand_id"], 0) + row["grant_qty"]
        return grants

    def _reconcile_inputs(self, conn, version: dict[str, Any]) -> dict[str, Any]:
        """装配一次调和所需的全部输入（草案之后的任何再分配共用）。"""

        _, demands, customer_caps, group_caps, policy_params = self._load_snapshot_parts(
            conn, version["version_id"])
        latest = self._latest_run(conn, version["version_id"])
        offered: dict[str, int] = {}
        if latest is not None:
            for row in conn.execute("SELECT demand_id,offered_qty FROM allocation_lines WHERE run_id=?",
                                    (latest["run_id"],)):
                offered[row["demand_id"]] = row["offered_qty"]
        active_ids = {demand.demand_id for demand in demands}
        held: dict[str, int] = {}
        protected: dict[str, int] = {}
        for demand in demands:
            row = conn.execute("SELECT decision,shipped_qty FROM demands WHERE demand_id=?",
                               (demand.demand_id,)).fetchone()
            if row["decision"] == "confirmed":
                # 只有客户确认后的占用才稳定保留。
                held[demand.demand_id] = offered.get(demand.demand_id, 0)
            protected[demand.demand_id] = row["shipped_qty"]
        # 已放弃的需求退出分配；仅当已有装船时作为硬地板继续占舱。
        floor_attributes: dict[str, tuple[str, str, int]] = {}
        for row in conn.execute(
            "SELECT demand_id,customer_id,group_id,quantity,shipped_qty FROM demands "
            "WHERE version_id=? AND active=0 AND shipped_qty>0",
            (version["version_id"],),
        ):
            floor_attributes[row["demand_id"]] = (row["customer_id"], row["group_id"], row["quantity"])
            protected[row["demand_id"]] = row["shipped_qty"]
        # 例外只对仍在本轮分配中的需求生效；放弃即释放其例外表决量。
        grants = {demand_id: qty for demand_id, qty in self._grants(conn, version["version_id"]).items()
                  if demand_id in active_ids}
        return {"demands": demands, "customer_caps": customer_caps, "group_caps": group_caps,
                "policy_params": policy_params, "held": held, "prior": offered,
                "protected": protected, "grants": grants, "floor_attributes": floor_attributes}

    def reallocate(self, conn, *, version, basis: str, actor_id: str,
                   capacity_event_id: str | None = None) -> str:
        """在既有冻结状态上按稳定顺序重新调和，返回新 run_id。"""

        if version["status"] not in ("frozen", "cancelled"):
            raise ConflictError("班次尚未冻结，不能再分配")
        parts = self._reconcile_inputs(conn, version)
        capacity = version["capacity"]
        result = allocator.reconcile(
            parts["demands"], capacity=capacity, customer_caps=parts["customer_caps"],
            group_caps=parts["group_caps"], policy_params=parts["policy_params"],
            protected=parts["protected"], held=parts["held"], prior=parts["prior"],
            grants=parts["grants"], floor_attributes=parts["floor_attributes"])
        snapshot = {"basis": basis, "capacity": capacity,
                    "policy_version_id": version["policy_version_id"],
                    "policy_params": parts["policy_params"],
                    "customer_caps": parts["customer_caps"], "group_caps": parts["group_caps"],
                    "demands": [d.__dict__ for d in parts["demands"]],
                    "protected": parts["protected"], "held": parts["held"], "prior": parts["prior"],
                    "grants": parts["grants"],
                    "floor_attributes": {k: list(v) for k, v in parts["floor_attributes"].items()}}
        run_id = self._persist_run(conn, version=version, result=result, basis=basis,
                                   policy_version_id=version["policy_version_id"],
                                   actor_id=actor_id, capacity_event_id=capacity_event_id,
                                   snapshot=snapshot)
        self._audit(conn, actor_id=actor_id, action=f"allocation.{basis}",
                    resource_type="allocation_run", resource_id=run_id,
                    detail={"version_id": version["version_id"], "total_offered": result.total_offered,
                            "capacity": capacity,
                            "additions": result.additions, "reductions": result.reductions})
        return run_id

    # ---- 客户决策与执行事件 ----

    def _frozen_demand(self, conn, demand_id: str):
        row = conn.execute("SELECT * FROM demands WHERE demand_id=?", (demand_id,)).fetchone()
        if row is None:
            raise NotFoundError("需求不存在")
        version = conn.execute("SELECT * FROM train_versions WHERE version_id=?", (row["version_id"],)).fetchone()
        if version["status"] == "open":
            raise ConflictError("班次尚未冻结")
        return row, version

    def confirm_demand(self, *, request_id: str, actor_id: str, demand_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "demand_id": demand_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            row, version = self._frozen_demand(conn, demand_id)
            if version["status"] == "cancelled":
                raise ConflictError("班次已取消")
            if row["decision"] == "waived":
                raise ConflictError("需求已放弃，不能确认")

            def create():
                if row["decision"] == "confirmed":
                    raise ConflictError("需求已经确认，请勿重复提交")
                conn.execute(
                    "UPDATE demands SET decision='confirmed',decided_at=? WHERE demand_id=?",
                    (self._now(), demand_id),
                )
                self._audit(conn, actor_id=actor_id, action="demand.confirmed",
                            resource_type="demand", resource_id=demand_id,
                            detail={"version_id": row["version_id"]})
                return "demand", demand_id, {"demand_id": demand_id, "decision": "confirmed"}

            return self._idempotent(conn, request_id=request_id, action="confirm_demand",
                                    payload=payload, create=create)

    def waive_demand(self, *, request_id: str, actor_id: str, demand_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "demand_id": demand_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            row, version = self._frozen_demand(conn, demand_id)

            def create():
                if row["decision"] == "waived":
                    raise ConflictError("需求已经放弃")
                conn.execute(
                    "UPDATE demands SET decision='waived',active=0,decided_at=? WHERE demand_id=?",
                    (self._now(), demand_id),
                )
                run_id = self.reallocate(conn, version=version, basis="waiver", actor_id=actor_id)
                self._audit(conn, actor_id=actor_id, action="demand.waived",
                            resource_type="demand", resource_id=demand_id,
                            detail={"version_id": row["version_id"], "followup_run_id": run_id})
                return "demand", demand_id, {"demand_id": demand_id, "run_id": run_id}

            return self._idempotent(conn, request_id=request_id, action="waive_demand",
                                    payload=payload, create=create)

    def record_shipment(self, *, request_id: str, actor_id: str,
                        demand_id: str, shipped_qty: int) -> WriteReceipt:
        shipped_qty = self._non_negative_int(shipped_qty, "shipped_qty")
        payload = {"actor_id": actor_id, "demand_id": demand_id, "shipped_qty": shipped_qty}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            row, version = self._frozen_demand(conn, demand_id)
            if row["decision"] != "confirmed":
                raise ConflictError("只有已确认的需求才能登记装船")
            latest = self._latest_run(conn, row["version_id"])
            line = conn.execute("SELECT offered_qty FROM allocation_lines WHERE run_id=? AND demand_id=?",
                                (latest["run_id"], demand_id)).fetchone()
            if line is None or shipped_qty > line["offered_qty"]:
                raise ValidationError("装船量不能超过当前已分配舱位量")
            if shipped_qty < row["shipped_qty"]:
                raise ConflictError("已装运货物不得回收，装船量只能增加")

            def create():
                conn.execute("UPDATE demands SET shipped_qty=? WHERE demand_id=?", (shipped_qty, demand_id))
                run_id = self.reallocate(conn, version=version, basis="partial_shipment", actor_id=actor_id)
                self._audit(conn, actor_id=actor_id, action="shipment.recorded",
                            resource_type="demand", resource_id=demand_id,
                            detail={"version_id": row["version_id"], "shipped_qty": shipped_qty,
                                    "followup_run_id": run_id})
                return "demand", demand_id, {"demand_id": demand_id, "run_id": run_id}

            return self._idempotent(conn, request_id=request_id, action="record_shipment",
                                    payload=payload, create=create)

    def adjust_capacity(self, *, request_id: str, actor_id: str, version_id: str,
                        new_capacity: int, reason: str) -> WriteReceipt:
        new_capacity = self._non_negative_int(new_capacity, "new_capacity")
        reason = str(reason).strip()
        if not reason:
            raise ValidationError("reason 不能为空")
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "new_capacity": new_capacity, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            version = conn.execute("SELECT * FROM train_versions WHERE version_id=?", (version_id,)).fetchone()
            if version is None:
                raise NotFoundError("班次版本不存在")
            event_id = uuid.uuid4().hex

            def create():
                conn.execute(
                    "INSERT INTO capacity_events(event_id,version_id,previous_capacity,new_capacity,reason,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (event_id, version_id, version["capacity"], new_capacity, reason,
                     actor_id, self._now()),
                )
                conn.execute("UPDATE train_versions SET capacity=? WHERE version_id=?",
                             (new_capacity, version_id))
                updated = dict(version)
                updated["capacity"] = new_capacity
                run_id = None
                if version["status"] == "frozen":
                    run_id = self.reallocate(conn, version=updated, basis="capacity_change",
                                             actor_id=actor_id, capacity_event_id=event_id)
                self._audit(conn, actor_id=actor_id, action="capacity.adjusted",
                            resource_type="capacity_event", resource_id=event_id,
                            detail={"version_id": version_id, "previous_capacity": version["capacity"],
                                    "new_capacity": new_capacity, "reason": reason,
                                    "followup_run_id": run_id})
                return "capacity_event", event_id, {"event_id": event_id, "run_id": run_id}

            return self._idempotent(conn, request_id=request_id, action="adjust_capacity",
                                    payload=payload, create=create)

    def cancel_train(self, *, request_id: str, actor_id: str, version_id: str, reason: str) -> WriteReceipt:
        reason = str(reason).strip()
        if not reason:
            raise ValidationError("reason 不能为空")
        payload = {"actor_id": actor_id, "version_id": version_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            version = conn.execute("SELECT * FROM train_versions WHERE version_id=?", (version_id,)).fetchone()
            if version is None:
                raise NotFoundError("班次版本不存在")

            def create():
                if version["status"] == "cancelled":
                    raise ConflictError("班次已经取消")
                event_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO capacity_events(event_id,version_id,previous_capacity,new_capacity,reason,"
                    "created_by,created_at) VALUES(?,?,?,0,?,?,?)",
                    (event_id, version_id, version["capacity"], f"班次取消：{reason}", actor_id, self._now()),
                )
                conn.execute("UPDATE train_versions SET status='cancelled',capacity=0 WHERE version_id=?",
                             (version_id,))
                cancelled = dict(version)
                cancelled["status"] = "cancelled"
                cancelled["capacity"] = 0
                run_id = None
                if version["status"] == "frozen":
                    run_id = self.reallocate(conn, version=cancelled, basis="train_cancel",
                                             actor_id=actor_id, capacity_event_id=event_id)
                self._audit(conn, actor_id=actor_id, action="train.cancelled",
                            resource_type="train_version", resource_id=version_id,
                            detail={"reason": reason, "followup_run_id": run_id})
                return "train_version", version_id, {"version_id": version_id, "run_id": run_id}

            return self._idempotent(conn, request_id=request_id, action="cancel_train",
                                    payload=payload, create=create)

    # ---- 人工例外（双人审批 + 可复算影响） ----

    def propose_exception(self, *, request_id: str, actor_id: str, version_id: str,
                          demand_id: str, grant_qty: int, reason: str) -> WriteReceipt:
        grant_qty = self._positive_int(grant_qty, "grant_qty")
        reason = str(reason).strip()
        if not reason:
            raise ValidationError("reason 不能为空")
        payload = {"actor_id": actor_id, "version_id": version_id, "demand_id": demand_id,
                   "grant_qty": grant_qty, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            demand = conn.execute("SELECT * FROM demands WHERE demand_id=? AND version_id=?",
                                  (demand_id, version_id)).fetchone()
            if demand is None:
                raise NotFoundError("需求不存在或不属于该班次")
            exception_id = uuid.uuid4().hex

            def create():
                conn.execute(
                    "INSERT INTO exceptions(exception_id,version_id,demand_id,grant_qty,reason,status,"
                    "proposed_by,proposed_at) VALUES(?,?,?,?,?,'proposed',?,?)",
                    (exception_id, version_id, demand_id, grant_qty, reason, actor_id, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="exception.proposed",
                            resource_type="exception", resource_id=exception_id,
                            detail={"version_id": version_id, "demand_id": demand_id,
                                    "grant_qty": grant_qty})
                return "exception", exception_id, {"exception_id": exception_id}

            return self._idempotent(conn, request_id=request_id, action="propose_exception",
                                    payload=payload, create=create)

    def _exception_impact(self, conn, *, version, demand_id: str, grant_qty: int) -> dict[str, Any]:
        """以当前最新状态为基准，复算加入该例外后的影响。"""

        parts = self._reconcile_inputs(conn, version)
        latest = self._latest_run(conn, version["version_id"])
        before_lines: dict[str, int] = {}
        if latest is not None:
            for row in conn.execute("SELECT demand_id,offered_qty FROM allocation_lines WHERE run_id=?",
                                    (latest["run_id"],)):
                before_lines[row["demand_id"]] = row["offered_qty"]
        grants = dict(parts["grants"])
        grants[demand_id] = grants.get(demand_id, 0) + grant_qty
        result = allocator.reconcile(
            parts["demands"], capacity=version["capacity"],
            customer_caps=parts["customer_caps"], group_caps=parts["group_caps"],
            policy_params=parts["policy_params"], protected=parts["protected"],
            held=parts["held"], prior=parts["prior"], grants=grants,
            floor_attributes=parts["floor_attributes"])
        after_lines = {line.demand_id: line.offered_qty for line in result.lines}
        affected = []
        for demand in parts["demands"]:
            before = before_lines.get(demand.demand_id, 0)
            after = after_lines.get(demand.demand_id, 0)
            if before != after:
                affected.append({"demand_id": demand.demand_id, "before": before, "after": after,
                                 "delta": after - before})
        return {"before_total_offered": sum(before_lines.values()),
                "after_total_offered": result.total_offered,
                "capacity": result.capacity,
                "grant_demand_id": demand_id, "grant_qty": grant_qty,
                "affected_lines": affected,
                "additions": result.additions, "reductions": result.reductions}

    def decide_exception(self, *, request_id: str, actor_id: str,
                         exception_id: str, approve: bool) -> WriteReceipt:
        approve = bool(approve)
        payload = {"actor_id": actor_id, "exception_id": exception_id, "approve": approve}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            exc_row = conn.execute("SELECT * FROM exceptions WHERE exception_id=?", (exception_id,)).fetchone()
            if exc_row is None:
                raise NotFoundError("例外申请不存在")
            if exc_row["proposed_by"] == actor_id:
                raise PermissionDenied("双人审批要求第二位不同操作者复核")
            if exc_row["status"] != "proposed":
                raise ConflictError("该例外已经做出决定")
            version = conn.execute("SELECT * FROM train_versions WHERE version_id=?",
                                   (exc_row["version_id"],)).fetchone()

            def create():
                new_status = "approved" if approve else "rejected"
                impact = None
                run_id = None
                if approve and version["status"] == "frozen":
                    impact = self._exception_impact(conn, version=version,
                                                    demand_id=exc_row["demand_id"],
                                                    grant_qty=exc_row["grant_qty"])
                conn.execute(
                    "UPDATE exceptions SET status=?,decided_by=?,decided_at=?,impact_json=? "
                    "WHERE exception_id=?",
                    (new_status, actor_id, self._now(),
                     canonical_json(impact) if impact is not None else None, exception_id),
                )
                if approve and version["status"] == "frozen":
                    run_id = self.reallocate(conn, version=version, basis="exception", actor_id=actor_id)
                self._audit(conn, actor_id=actor_id,
                            action="exception.approved" if approve else "exception.rejected",
                            resource_type="exception", resource_id=exception_id,
                            detail={"version_id": exc_row["version_id"],
                                    "demand_id": exc_row["demand_id"],
                                    "grant_qty": exc_row["grant_qty"],
                                    "impact": impact, "followup_run_id": run_id})
                return "exception", exception_id, {"exception_id": exception_id, "status": new_status,
                                                   "run_id": run_id}

            return self._idempotent(conn, request_id=request_id, action="decide_exception",
                                    payload=payload, create=create)

    # ---- 查询视图 ----

    def get_run(self, run_id: str) -> dict[str, Any]:
        conn = self.database.connection
        run = conn.execute("SELECT * FROM allocation_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise NotFoundError("分配版本不存在")
        event = None
        if run["capacity_event_id"]:
            ev = conn.execute("SELECT * FROM capacity_events WHERE event_id=?",
                              (run["capacity_event_id"],)).fetchone()
            event = {"event_id": ev["event_id"], "previous_capacity": ev["previous_capacity"],
                     "new_capacity": ev["new_capacity"], "reason": ev["reason"],
                     "created_at": ev["created_at"]}
        lines = []
        for row in conn.execute("SELECT * FROM allocation_lines WHERE run_id=? ORDER BY priority_rank",
                                (run_id,)):
            lines.append({"demand_id": row["demand_id"], "requested_qty": row["requested_qty"],
                          "offered_qty": row["offered_qty"], "protected_qty": row["protected_qty"],
                          "granted_qty": row["granted_qty"], "added_qty": row["added_qty"],
                          "reduced_qty": row["reduced_qty"],
                          "waitlist_position": row["waitlist_position"],
                          "priority_rank": row["priority_rank"],
                          "reasons": json.loads(row["reasons_json"]),
                          "factors": json.loads(row["factors_json"])})
        return {"run_id": run["run_id"], "version_id": run["version_id"],
                "sequence_no": run["sequence_no"], "basis": run["basis"],
                "policy_version_id": run["policy_version_id"], "capacity": run["capacity"],
                "total_offered": run["total_offered"], "capacity_event": event,
                "created_by": run["created_by"], "created_at": run["created_at"],
                "input_snapshot": json.loads(run["input_json"]), "lines": lines}

    def list_runs(self, version_id: str) -> list[dict[str, Any]]:
        conn = self.database.connection
        if conn.execute("SELECT 1 FROM train_versions WHERE version_id=?", (version_id,)).fetchone() is None:
            raise NotFoundError("班次版本不存在")
        items = []
        for row in conn.execute(
            "SELECT run_id,sequence_no,basis,policy_version_id,capacity,total_offered,capacity_event_id,"
            "created_at FROM allocation_runs WHERE version_id=? ORDER BY sequence_no",
            (version_id,),
        ):
            items.append({"run_id": row["run_id"], "sequence_no": row["sequence_no"],
                          "basis": row["basis"], "policy_version_id": row["policy_version_id"],
                          "capacity": row["capacity"], "total_offered": row["total_offered"],
                          "capacity_event_id": row["capacity_event_id"], "created_at": row["created_at"]})
        return items

    def get_demand_view(self, demand_id: str) -> dict[str, Any]:
        """每票需求的获得/落选原因、候补位置与恢复后增配来源。"""

        conn = self.database.connection
        demand = conn.execute("SELECT * FROM demands WHERE demand_id=?", (demand_id,)).fetchone()
        if demand is None:
            raise NotFoundError("需求不存在")
        flags = [{"flag_code": row["flag_code"], "detail": json.loads(row["detail_json"]),
                  "created_at": row["created_at"]}
                 for row in conn.execute("SELECT * FROM demand_flags WHERE demand_id=? ORDER BY created_at",
                                         (demand_id,))]
        history = []
        for row in conn.execute(
            "SELECT r.run_id,r.sequence_no,r.basis,r.capacity_event_id,r.created_at,"
            "l.offered_qty,l.protected_qty,l.added_qty,l.reduced_qty,l.waitlist_position,l.reasons_json "
            "FROM allocation_runs r JOIN allocation_lines l ON l.run_id=r.run_id "
            "WHERE l.demand_id=? ORDER BY r.sequence_no",
            (demand_id,),
        ):
            event = None
            if row["capacity_event_id"]:
                ev = conn.execute("SELECT previous_capacity,new_capacity,reason FROM capacity_events "
                                  "WHERE event_id=?", (row["capacity_event_id"],)).fetchone()
                event = {"previous_capacity": ev["previous_capacity"],
                         "new_capacity": ev["new_capacity"], "reason": ev["reason"]}
            history.append({"run_id": row["run_id"], "sequence_no": row["sequence_no"],
                            "basis": row["basis"], "offered_qty": row["offered_qty"],
                            "protected_qty": row["protected_qty"],
                            "added_qty": row["added_qty"], "reduced_qty": row["reduced_qty"],
                            "waitlist_position": row["waitlist_position"],
                            "reasons": json.loads(row["reasons_json"]),
                            "capacity_event": event, "created_at": row["created_at"]})
        latest = history[-1] if history else None
        promotion_sources = []
        for entry in history:
            if entry["added_qty"] <= 0:
                continue
            source = {"run_id": entry["run_id"], "sequence_no": entry["sequence_no"],
                      "trigger": entry["basis"], "added_qty": entry["added_qty"]}
            if entry["capacity_event"] is not None:
                source["capacity_event"] = entry["capacity_event"]
            promotion_sources.append(source)
        return {"demand_id": demand_id, "version_id": demand["version_id"],
                "customer_id": demand["customer_id"], "group_id": demand["group_id"],
                "client_key": demand["client_key"], "quantity": demand["quantity"],
                "strategic_level": demand["strategic_level"], "cargo_deadline": demand["cargo_deadline"],
                "split_root_id": demand["split_root_id"], "submitted_seq": demand["submitted_seq"],
                "fulfillment_rate": demand["fulfillment_rate"], "decision": demand["decision"],
                "shipped_qty": demand["shipped_qty"], "active": bool(demand["active"]),
                "flags": flags, "latest": latest, "history": history,
                "promotion_sources": promotion_sources}

    def compare_policies(self, *, version_id: str, policy_version_ids: list[str]) -> dict[str, Any]:
        """以冻结时的需求快照复算不同政策版本的公平性指标（只读，不改写结果）。"""

        conn = self.database.connection
        version = conn.execute("SELECT * FROM train_versions WHERE version_id=?", (version_id,)).fetchone()
        if version is None:
            raise NotFoundError("班次版本不存在")
        if not policy_version_ids:
            raise ValidationError("policy_version_ids 不能为空")
        _, demands, customer_caps, group_caps, _ = self._load_snapshot_parts(conn, version_id)
        scenarios = []
        for policy_id in policy_version_ids:
            policy = conn.execute("SELECT * FROM policy_versions WHERE policy_version_id=?",
                                  (policy_id,)).fetchone()
            if policy is None:
                raise NotFoundError(f"政策版本不存在: {policy_id}")
            params = json.loads(policy["params_json"])
            result = allocator.reconcile(
                demands, capacity=version["capacity"], customer_caps=customer_caps,
                group_caps=group_caps, policy_params=params)
            allocated = {line.demand_id: line.offered_qty for line in result.lines}
            scenarios.append({"policy_version_id": policy_id, "params": params,
                              "metrics": allocator.fairness_metrics(demands, allocated),
                              "allocation": {line.demand_id: line.offered_qty for line in result.lines}})
        return {"version_id": version_id, "capacity": version["capacity"],
                "demand_count": len(demands), "scenarios": scenarios}

    def recompute_run(self, run_id: str) -> dict[str, Any]:
        """用持久化的输入快照离线复算某次调和，校验结果是否可重放。"""

        stored = self.get_run(run_id)
        snapshot = stored["input_snapshot"]
        demands = [allocator.DemandSnapshot(**item) for item in snapshot["demands"]]
        floors_raw = snapshot.get("floor_attributes") or {}
        floor_attributes = {key: tuple(value) for key, value in floors_raw.items()} or None
        result = allocator.reconcile(
            demands, capacity=snapshot["capacity"],
            customer_caps=snapshot["customer_caps"], group_caps=snapshot["group_caps"],
            policy_params=snapshot["policy_params"],
            protected=snapshot.get("protected") or None,
            held=snapshot.get("held") or None,
            prior=snapshot.get("prior") if snapshot.get("prior") is not None else (snapshot.get("held") or None),
            grants=snapshot.get("grants") or None,
            floor_attributes=floor_attributes,
        )
        reproduced = {line.demand_id: line.offered_qty for line in result.lines}
        stored_lines = {line["demand_id"]: line["offered_qty"] for line in stored["lines"]}
        return {"run_id": run_id, "reproduced": reproduced == stored_lines,
                "reproduced_total_offered": result.total_offered,
                "stored_total_offered": stored["total_offered"]}
