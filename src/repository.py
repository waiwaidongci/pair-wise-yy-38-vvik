from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, ORDER_STATES

ORDER_COLUMNS = [
    "order_no", "level", "flood_limit", "inflow", "rise", "downstream",
    "construction_limit", "gates_json",
    "planned_flow", "gate_count", "selected_gates_json", "chief_review_required",
    "level_rising", "uses_restricted_gates", "severe_cap_applied",
    "capacity_shortfall", "plan_notes_json", "severity",
    "actual_flow", "deviation_ratio", "deviation_reason",
    "status", "version", "created_by", "created_at", "updated_at",
]


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s + "'" for s in ORDER_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT NOT NULL UNIQUE,
                    level REAL NOT NULL,
                    flood_limit REAL NOT NULL,
                    inflow REAL NOT NULL,
                    rise REAL NOT NULL,
                    downstream TEXT NOT NULL,
                    construction_limit TEXT NOT NULL DEFAULT '',
                    gates_json TEXT NOT NULL,
                    planned_flow INTEGER NOT NULL,
                    gate_count INTEGER NOT NULL,
                    selected_gates_json TEXT NOT NULL,
                    chief_review_required INTEGER NOT NULL,
                    level_rising INTEGER NOT NULL,
                    uses_restricted_gates INTEGER NOT NULL,
                    severe_cap_applied INTEGER NOT NULL,
                    capacity_shortfall INTEGER NOT NULL,
                    plan_notes_json TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    actual_flow REAL,
                    deviation_ratio REAL,
                    deviation_reason TEXT,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('review','note')),
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _order(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["gates"] = json.loads(item.pop("gates_json"))
        item["selected_gates"] = json.loads(item.pop("selected_gates_json"))
        item["plan_notes"] = json.loads(item.pop("plan_notes_json"))
        for flag in ("chief_review_required", "level_rising",
                     "uses_restricted_gates", "severe_cap_applied",
                     "capacity_shortfall"):
            item[flag] = bool(item[flag])
        return item

    def create_order(self, data: Dict[str, Any], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(id),0)+1 AS n FROM orders").fetchone()["n"]
            order_no = f"{ID_PREFIX}-{seq:04d}"
            self.conn.execute(
                """INSERT INTO orders(order_no, level, flood_limit, inflow, rise,
                   downstream, construction_limit, gates_json, planned_flow,
                   gate_count, selected_gates_json, chief_review_required,
                   level_rising, uses_restricted_gates, severe_cap_applied,
                   capacity_shortfall, plan_notes_json, severity, status, version,
                   created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order_no, data["level"], data["flood_limit"], data["inflow"],
                 data["rise"], data["downstream"], data["construction_limit"],
                 json.dumps(data["gates"], ensure_ascii=False),
                 data["planned_flow"], data["gate_count"],
                 json.dumps(data["selected_gates"], ensure_ascii=False),
                 int(data["chief_review_required"]), int(data["level_rising"]),
                 int(data["uses_restricted_gates"]), int(data["severe_cap_applied"]),
                 int(data["capacity_shortfall"]),
                 json.dumps(data["plan_notes"], ensure_ascii=False),
                 data["severity"], ORDER_STATES[0], 1, actor, now, now))
            order_id = int(self.conn.execute("SELECT last_insert_rowid() AS n").fetchone()["n"])
        return self.get_order(order_id)

    def get_order(self, order_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFoundError("调度单不存在")
        return self._order(row)

    def get_order_by_no(self, order_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        if row is None:
            raise NotFoundError("调度单不存在")
        return self._order(row)

    def list_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM orders"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._order(row) for row in rows]

    def transition_order(self, order_id: int, target: str, expected_version: int,
                         actor: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = utc_now()
        sets = ["status=?", "version=version+1", "updated_at=?"]
        params: List[Any] = [target, now]
        if extra:
            for key in ("actual_flow", "deviation_ratio", "deviation_reason"):
                if key in extra:
                    sets.append(f"{key}=?")
                    params.append(extra[key])
        params.extend([order_id, expected_version])
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"UPDATE orders SET {', '.join(sets)} WHERE id=? AND version=?",
                params)
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("调度单不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_order(order_id)

    def add_record(self, order_id: int, kind: str, detail: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_order(order_id)
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO records(order_id, kind, detail, status, created_by, created_at)
                   VALUES(?,?,?,'open',?,?)""",
                (order_id, kind, detail, actor, now))
            record_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def close_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE records SET status='closed' WHERE id=? AND status='open'",
                (record_id,))
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM records WHERE id=?", (record_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("记录不存在")
                raise ConflictError("记录已关闭")
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, order_id: int) -> List[Dict[str, Any]]:
        self.get_order(order_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, order_id: int, kind: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM records WHERE order_id=? AND status='open'"
        params: list = [order_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]))
            event_id = int(self.conn.execute("SELECT last_insert_rowid() AS n").fetchone()["n"])
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
