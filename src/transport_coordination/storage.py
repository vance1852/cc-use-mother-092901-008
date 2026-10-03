"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corridors (
    corridor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    params_json TEXT NOT NULL,
    description TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, version)
);
CREATE TABLE IF NOT EXISTS corridor_policy (
    corridor_id TEXT PRIMARY KEY REFERENCES corridors(corridor_id),
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    name TEXT NOT NULL,
    assurance_level TEXT NOT NULL CHECK(assurance_level IN ('strategic', 'standard')),
    contract_priority INTEGER NOT NULL CHECK(contract_priority BETWEEN 0 AND 100),
    performance_score REAL NOT NULL CHECK(performance_score BETWEEN 0 AND 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS departures (
    departure_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES corridors(corridor_id),
    sequence_no INTEGER NOT NULL,
    cutoff_at TEXT NOT NULL,
    departs_at TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    status TEXT NOT NULL CHECK(status IN ('open', 'frozen', 'departed', 'cancelled')),
    created_at TEXT NOT NULL,
    UNIQUE(corridor_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS capacity_versions (
    version_id TEXT PRIMARY KEY,
    departure_id TEXT NOT NULL REFERENCES departures(departure_id),
    version INTEGER NOT NULL,
    capacity INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(departure_id, version)
);
CREATE TABLE IF NOT EXISTS demands (
    demand_id TEXT PRIMARY KEY,
    departure_id TEXT NOT NULL REFERENCES departures(departure_id),
    origin_departure_id TEXT NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    group_id TEXT NOT NULL,
    cargo_class TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    latest_load_at TEXT NOT NULL,
    split_group_id TEXT,
    submitted_at TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0 CHECK(closed IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_demands_dedup ON demands(
    departure_id, customer_id, cargo_class, quantity, latest_load_at, COALESCE(split_group_id, '')
);
CREATE INDEX IF NOT EXISTS idx_demands_departure ON demands(departure_id);
CREATE TABLE IF NOT EXISTS entitlements (
    demand_id TEXT PRIMARY KEY REFERENCES demands(demand_id),
    departure_id TEXT NOT NULL REFERENCES departures(departure_id),
    offered_qty INTEGER NOT NULL DEFAULT 0,
    confirmed_qty INTEGER NOT NULL DEFAULT 0,
    shipped_qty INTEGER NOT NULL DEFAULT 0,
    manual_extra_qty INTEGER NOT NULL DEFAULT 0,
    waitlist_rank INTEGER,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocation_runs (
    run_id TEXT PRIMARY KEY,
    departure_id TEXT NOT NULL REFERENCES departures(departure_id),
    trigger_type TEXT NOT NULL,
    parent_run_id TEXT,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    input_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocation_run_lines (
    run_id TEXT NOT NULL REFERENCES allocation_runs(run_id),
    demand_id TEXT NOT NULL,
    line_json TEXT NOT NULL,
    PRIMARY KEY (run_id, demand_id)
);
CREATE TABLE IF NOT EXISTS exception_cases (
    exception_id TEXT PRIMARY KEY,
    departure_id TEXT NOT NULL REFERENCES departures(departure_id),
    demand_id TEXT NOT NULL REFERENCES demands(demand_id),
    extra_qty INTEGER NOT NULL CHECK(extra_qty > 0),
    justification TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
    created_by TEXT NOT NULL,
    approved_by TEXT,
    decision_note TEXT,
    impact_json TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # 单一连接被多线程（ThreadingHTTPServer）共享：用可重入锁把写事务串行化，
        # 读路径也取同一把锁，避免读到另一线程写一半的中间状态。
        self.lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。整段事务持锁，串行化并发写。"""

        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        """取同一把锁执行只读查询，避免与写事务交错。"""

        with self.lock:
            yield self.connection

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
