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
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cap_per_run INTEGER NOT NULL CHECK(cap_per_run >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES customer_groups(group_id),
    name TEXT NOT NULL,
    cap_per_run INTEGER NOT NULL CHECK(cap_per_run >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_versions (
    policy_version_id TEXT PRIMARY KEY,
    params_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS train_versions (
    version_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES corridors(corridor_id),
    departure_at TEXT NOT NULL,
    freeze_at TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    policy_version_id TEXT REFERENCES policy_versions(policy_version_id),
    status TEXT NOT NULL CHECK(status IN ('open', 'frozen', 'cancelled')),
    frozen_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capacity_events (
    event_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES train_versions(version_id),
    previous_capacity INTEGER NOT NULL,
    new_capacity INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demands (
    demand_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES train_versions(version_id),
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    group_id TEXT NOT NULL REFERENCES customer_groups(group_id),
    client_key TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    strategic_level INTEGER NOT NULL CHECK(strategic_level BETWEEN 1 AND 5),
    cargo_deadline TEXT NOT NULL,
    split_root_id TEXT NOT NULL,
    submitted_seq INTEGER NOT NULL,
    fulfillment_rate REAL NOT NULL CHECK(fulfillment_rate BETWEEN 0 AND 1),
    requested_qty INTEGER NOT NULL,
    shipped_qty INTEGER NOT NULL DEFAULT 0 CHECK(shipped_qty >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending', 'confirmed', 'waived')),
    decided_run_id TEXT,
    decided_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, client_key)
);
CREATE TABLE IF NOT EXISTS demand_flags (
    flag_id TEXT PRIMARY KEY,
    demand_id TEXT NOT NULL REFERENCES demands(demand_id),
    flag_code TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocation_runs (
    run_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES train_versions(version_id),
    sequence_no INTEGER NOT NULL,
    basis TEXT NOT NULL CHECK(basis IN ('freeze', 'waiver', 'partial_shipment',
                                       'capacity_change', 'train_cancel', 'exception')),
    policy_version_id TEXT NOT NULL,
    capacity_event_id TEXT REFERENCES capacity_events(event_id),
    capacity INTEGER NOT NULL,
    input_json TEXT NOT NULL,
    total_offered INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS allocation_lines (
    run_id TEXT NOT NULL REFERENCES allocation_runs(run_id),
    demand_id TEXT NOT NULL,
    requested_qty INTEGER NOT NULL,
    offered_qty INTEGER NOT NULL,
    protected_qty INTEGER NOT NULL,
    granted_qty INTEGER NOT NULL,
    added_qty INTEGER NOT NULL,
    reduced_qty INTEGER NOT NULL,
    waitlist_position INTEGER,
    priority_rank INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    factors_json TEXT NOT NULL,
    PRIMARY KEY(run_id, demand_id)
);
CREATE TABLE IF NOT EXISTS exceptions (
    exception_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES train_versions(version_id),
    demand_id TEXT NOT NULL REFERENCES demands(demand_id),
    grant_qty INTEGER NOT NULL CHECK(grant_qty > 0),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'approved', 'rejected')),
    proposed_by TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    impact_json TEXT
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        # 进程内所有写操作共用一个 SQLite 连接，用锁串行化写事务，
        # 避免并发确认/再分配在同一连接上交错。
        self._write_lock = threading.RLock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；写事务整体串行执行。"""

        with self._write_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
