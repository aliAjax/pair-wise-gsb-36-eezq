"""Cross-region water-right allocation and transfer service (standard library only)."""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "water_rights.db"

# 拆分核对允许的浮点误差
SPLIT_EPS = 1e-6


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _as_float(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_split(snapshot: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any]:
    """纯判断：把申请人填的子账户与拆分前快照逐项核对。

    核对项：许可额度合计、已用水量合计、待审承接转让（出/入）逐条唯一承接，
    以及每个子账户自身字段的合法性。不读库、不写库，前端预览和后端保存共用。
    """
    errors: list[str] = []
    if not isinstance(children, list) or not children:
        errors.append("至少需要填写一个子账户")
        children = []

    # split_context() 的账户字段在 source 下；直接传账户行时字段在顶层。
    source = snapshot.get("source", snapshot)
    source_name = str(source.get("name", ""))
    source_quota = float(snapshot.get("quota", source.get("quota", 0.0)))
    source_used = float(snapshot.get("used", source.get("used", 0.0)))
    outgoing = {int(t["id"]): float(t["amount"]) for t in snapshot.get("pending_outgoing", [])}
    incoming = {int(t["id"]): float(t["amount"]) for t in snapshot.get("pending_incoming", [])}

    seen_names: set[str] = set()
    out_seen: dict[int, str] = {}
    in_seen: dict[int, str] = {}
    reps: list[dict[str, Any]] = []
    total_quota = total_used = 0.0

    for index, raw in enumerate(children):
        label = f"第{index + 1}个子账户"
        rep: dict[str, Any] = {"index": index}
        if not isinstance(raw, dict):
            errors.append(f"{label}格式不合法")
            continue

        name = str(raw.get("name", "")).strip()
        rep["name"] = name
        if not name:
            errors.append(f"{label}缺少名称")
        elif name == source_name:
            errors.append(f"{label}名称不能与原账户相同")
        elif name in seen_names:
            errors.append(f"子账户名称重复：{name}")
        else:
            seen_names.add(name)

        priority = raw.get("priority")
        if priority is None:
            priority = source.get("priority")
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = None
            errors.append(f"{label}优先级必须是 1-5 的整数")
        rep["priority"] = priority
        if priority is not None and not 1 <= priority <= 5:
            errors.append(f"{label}优先级必须在 1 到 5 之间")

        def inherit(key: str) -> str:
            value = raw.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                value = source.get(key, "")
            return str(value).strip()

        region, holder = inherit("region"), inherit("holder")
        rep["region"], rep["holder"] = region, holder
        if not region or not holder:
            errors.append(f"{label}地区和持有人不能为空")

        valid_from = raw.get("valid_from") or source.get("valid_from", "")
        valid_to = raw.get("valid_to") or source.get("valid_to", "")
        rep["valid_from"], rep["valid_to"] = valid_from, valid_to
        try:
            vf, vt = date.fromisoformat(valid_from), date.fromisoformat(valid_to)
            if vf > vt:
                errors.append(f"{label}生效日期不能晚于失效日期")
        except (TypeError, ValueError):
            errors.append(f"{label}日期必须是 YYYY-MM-DD")

        quota = _as_float(raw.get("quota"))
        used = _as_float(raw.get("used"))
        if quota is None:
            errors.append(f"{label}许可额度必须是数值")
            quota = 0.0
        if used is None:
            errors.append(f"{label}已用水量必须是数值")
            used = 0.0
        if quota < 0 or used < 0:
            errors.append(f"{label}额度和已用水量不能为负")
        if used > quota + SPLIT_EPS:
            errors.append(f"{label}已用水量不能超过许可额度")
        rep["quota"], rep["used"] = quota, used
        total_quota += quota
        total_used += used

        def parse_ids(key: str) -> list[int]:
            ids: list[int] = []
            for value in raw.get(key, []) or []:
                try:
                    ids.append(int(value))
                except (TypeError, ValueError):
                    errors.append(f"{label}承接的转让编号必须是整数：{value!r}")
            return ids

        out_ids = parse_ids("take_outgoing")
        in_ids = parse_ids("take_incoming")
        if len(out_ids) != len(set(out_ids)):
            errors.append(f"{label}承接的转出转让有重复编号")
        if len(in_ids) != len(set(in_ids)):
            errors.append(f"{label}承接的转入转让有重复编号")
        rep["take_outgoing"], rep["take_incoming"] = out_ids, in_ids
        rep["reserved_outgoing"] = sum(outgoing.get(i, 0.0) for i in out_ids)
        rep["available_after"] = max(0.0, quota - used - rep["reserved_outgoing"])

        for tid, seen, direction in (
            (out_ids, out_seen, "转出"),
            (in_ids, in_seen, "转入"),
        ):
            for t in tid:
                if t in seen:
                    errors.append(f"{direction}转让 {t} 被多个子账户重复承接")
                else:
                    seen[t] = name

        reps.append(rep)

    quota_diff = total_quota - source_quota
    used_diff = total_used - source_used

    for tid in out_seen:
        if tid not in outgoing:
            errors.append(f"转出转让 {tid} 不存在或已不是待审批状态")
    for tid in in_seen:
        if tid not in incoming:
            errors.append(f"转入转让 {tid} 不存在或已不是待审批状态")
    missing_out = sorted(set(outgoing) - set(out_seen))
    missing_in = sorted(set(incoming) - set(in_seen))
    if missing_out:
        errors.append("待审批转出转让未全部承接：" + ", ".join(map(str, missing_out)))
    if missing_in:
        errors.append("待审批转入转让未全部承接：" + ", ".join(map(str, missing_in)))

    if abs(quota_diff) > SPLIT_EPS:
        errors.append(f"许可额度合计 {total_quota:g} 与拆分前 {source_quota:g} 不一致")
    if abs(used_diff) > SPLIT_EPS:
        errors.append(f"已用水量合计 {total_used:g} 与拆分前 {source_used:g} 不一致")

    matched = not errors
    return {
        "matched": matched,
        "errors": errors,
        "before": {"name": source_name, "quota": source_quota, "used": source_used,
                   "pending_outgoing": sorted(outgoing), "pending_incoming": sorted(incoming)},
        "after": {"quota_total": total_quota, "used_total": total_used,
                  "quota_diff": quota_diff, "used_diff": used_diff,
                  "outgoing_assigned": sorted(out_seen), "incoming_assigned": sorted(in_seen),
                  "outgoing_missing": missing_out, "incoming_missing": missing_in,
                  "children": reps},
    }


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    region TEXT NOT NULL,
                    holder TEXT NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 5),
                    valid_from TEXT NOT NULL,
                    valid_to TEXT NOT NULL,
                    quota REAL NOT NULL CHECK(quota >= 0),
                    used REAL NOT NULL DEFAULT 0 CHECK(used >= 0),
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','split')),
                    created_at TEXT NOT NULL,
                    CHECK(valid_from <= valid_to)
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    to_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    amount REAL NOT NULL CHECK(amount > 0),
                    effective_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by TEXT NOT NULL,
                    approved_by TEXT,
                    created_at TEXT NOT NULL,
                    approved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    meter_event_id TEXT NOT NULL,
                    amount REAL NOT NULL CHECK(amount > 0),
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, meter_event_id)
                );
                CREATE TABLE IF NOT EXISTS season_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    region TEXT NOT NULL,
                    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
                    max_fraction REAL NOT NULL CHECK(max_fraction > 0 AND max_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(region, month)
                );
                CREATE TABLE IF NOT EXISTS impact_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_region TEXT NOT NULL,
                    target_region TEXT NOT NULL,
                    min_source_fraction REAL NOT NULL CHECK(min_source_fraction >= 0 AND min_source_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_region, target_region)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS split_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','applied')),
                    children_json TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    applied_at TEXT
                );
                """
            )
            # 旧库升级：账户表补拆分状态列（幂等）
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
            if "status" not in existing:
                conn.execute("ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")

    def split_context(self, account_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """拆分前快照：账户本身、待审转让（出/入）、已用水量、关联提案。

        可传入已有连接（如 BEGIN IMMEDIATE 事务内），避免再开连接被写锁阻塞。
        """
        def _query(c: sqlite3.Connection) -> dict[str, Any]:
            account = self._account_row(c, account_id)
            outgoing = [dict(r) for r in c.execute(
                "SELECT * FROM transfers WHERE from_account_id=? AND status='pending' ORDER BY id", (account_id,))]
            incoming = [dict(r) for r in c.execute(
                "SELECT * FROM transfers WHERE to_account_id=? AND status='pending' ORDER BY id", (account_id,))]
            proposal = c.execute(
                "SELECT * FROM split_proposals WHERE source_account_id=? AND status='draft' ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            return {
                "source": dict(account),
                "quota": float(account["quota"]),
                "used": float(account["used"]),
                "pending_outgoing": outgoing,
                "pending_incoming": incoming,
                "draft": None if not proposal else {
                    "id": proposal["id"],
                    "children": json.loads(proposal["children_json"]),
                    "report": json.loads(proposal["report_json"]),
                    "created_by": proposal["created_by"],
                    "created_at": proposal["created_at"],
                },
            }

        if conn is not None:
            return _query(conn)
        with self.connect() as new_conn:
            return _query(new_conn)

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def create_account(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以创建账户", 403)
        name = str(payload.get("name", "")).strip()
        region = str(payload.get("region", "")).strip()
        holder = str(payload.get("holder", "")).strip()
        if not name or not region or not holder:
            raise DomainError("账户名称、地区和持有人不能为空")
        try:
            priority = int(payload.get("priority"))
            quota = float(payload.get("quota"))
        except (TypeError, ValueError) as exc:
            raise DomainError("优先级和额度必须是数值") from exc
        if not 1 <= priority <= 5 or quota < 0:
            raise DomainError("优先级应在 1 到 5 之间，额度不能为负")
        valid_from = parse_date(str(payload.get("valid_from", "")), "生效日期")
        valid_to = parse_date(str(payload.get("valid_to", "")), "失效日期")
        if valid_from > valid_to:
            raise DomainError("生效日期不能晚于失效日期")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,quota,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (name, region, holder, priority, valid_from.isoformat(), valid_to.isoformat(), quota, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("账户名称已存在", 409) from exc
            self._audit(conn, actor, "account.created", "account", cur.lastrowid, {"name": name, "quota": quota})
            return dict(conn.execute("SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_season_rule(self, actor: str, region: str, month: int, max_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置季节规则", 403)
        if not 1 <= int(month) <= 12 or not 0 < float(max_fraction) <= 1:
            raise DomainError("月份或季节比例不合法")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO season_rules(region,month,max_fraction,note) VALUES(?,?,?,?)
                   ON CONFLICT(region,month) DO UPDATE SET max_fraction=excluded.max_fraction,note=excluded.note""",
                (region.strip(), int(month), float(max_fraction), note),
            )
            self._audit(conn, actor, "season_rule.saved", "region", None, {"region": region, "month": month, "max_fraction": max_fraction})
        return {"region": region, "month": month, "max_fraction": max_fraction, "note": note}

    def set_impact_rule(self, actor: str, source_region: str, target_region: str, min_source_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置第三方影响规则", 403)
        if not 0 <= float(min_source_fraction) <= 1:
            raise DomainError("最小留存比例必须在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO impact_rules(source_region,target_region,min_source_fraction,note) VALUES(?,?,?,?)
                   ON CONFLICT(source_region,target_region) DO UPDATE SET min_source_fraction=excluded.min_source_fraction,note=excluded.note""",
                (source_region, target_region, float(min_source_fraction), note),
            )
            self._audit(conn, actor, "impact_rule.saved", "region", None, {"source": source_region, "target": target_region, "min_fraction": min_source_fraction})
        return {"source_region": source_region, "target_region": target_region, "min_source_fraction": min_source_fraction, "note": note}

    def _account_row(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise DomainError("水权账户不存在", 404)
        return row

    def _reserved_outgoing(self, conn: sqlite3.Connection, account_id: int) -> float:
        row = conn.execute("SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending'", (account_id,)).fetchone()
        return float(row["total"])

    def available(self, account_id: int, as_of: str | None = None) -> dict[str, Any]:
        if as_of:
            parse_date(as_of, "查询日期")
        with self.connect() as conn:
            account = self._account_row(conn, account_id)
            reserved = self._reserved_outgoing(conn, account_id)
            value = max(0.0, float(account["quota"]) - float(account["used"]) - reserved)
        return {"account_id": account_id, "available": value, "reserved_outgoing": reserved, "quota": account["quota"], "used": account["used"]}

    def create_transfer(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有水权编辑人员可以发起转让", 403)
        try:
            source_id = int(payload.get("from_account_id"))
            target_id = int(payload.get("to_account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和转让量必须是数值") from exc
        if source_id == target_id or amount <= 0:
            raise DomainError("转让账户不能相同，转让量必须大于 0")
        effective = parse_date(str(payload.get("effective_date", "")), "生效日期")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._account_row(conn, source_id)
            target = self._account_row(conn, target_id)
            if source["status"] != "active" or target["status"] != "active":
                raise DomainError("已拆分账户只供查询，不能再发起转让", 409)
            if not (source["valid_from"] <= effective.isoformat() <= source["valid_to"]):
                raise DomainError("转出账户在生效日无效", 409)
            if not (target["valid_from"] <= effective.isoformat() <= target["valid_to"]):
                raise DomainError("转入账户在生效日无效", 409)
            reserved = self._reserved_outgoing(conn, source_id)
            available = float(source["quota"]) - float(source["used"]) - reserved
            if amount > available + 1e-9:
                raise DomainError("可用额度不足，待审批转让会预占额度", 409)
            # More critical users (smaller priority number) cannot transfer their
            # protected allocation to a less critical user.
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            impact = conn.execute(
                "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
                (source["region"], target["region"]),
            ).fetchone()
            if impact:
                remaining = available - amount
                minimum = float(source["quota"]) * float(impact["min_source_fraction"])
                if remaining + 1e-9 < minimum:
                    raise DomainError("转让会违反下游第三方最小留存约束", 409)
            cur = conn.execute(
                "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (source_id, target_id, amount, effective.isoformat(), actor, utcnow()),
            )
            self._audit(conn, actor, "transfer.created", "transfer", cur.lastrowid,
                        {"source": source_id, "target": target_id, "amount": amount, "effective_date": effective.isoformat()})
            return dict(conn.execute("SELECT * FROM transfers WHERE id=?", (cur.lastrowid,)).fetchone())

    def approve_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以批准转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            transfer = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not transfer:
                raise DomainError("转让记录不存在", 404)
            if transfer["status"] != "pending":
                raise DomainError("该转让已处理，不能重复批准", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能批准自己的转让", 403)
            source = self._account_row(conn, transfer["from_account_id"])
            target = self._account_row(conn, transfer["to_account_id"])
            amount = float(transfer["amount"])
            # Compute against pending reservations other than this transfer.
            other_reserved = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending' AND id<>?",
                (source["id"], transfer_id),
            ).fetchone()["total"]
            available = float(source["quota"]) - float(source["used"]) - float(other_reserved)
            if amount > available + 1e-9:
                raise DomainError("审批时额度已被其他记录占用，不能批准", 409)
            impact = conn.execute(
                "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
                (source["region"], target["region"]),
            ).fetchone()
            if impact:
                minimum = float(source["quota"]) * float(impact["min_source_fraction"])
                if available - amount + 1e-9 < minimum:
                    raise DomainError("审批时下游最小留存约束不再满足", 409)
            # The approved amount moves between quota balances. Keeping the
            # movement in the quota column preserves the original allocation
            # while making every downstream availability calculation consistent.
            conn.execute("UPDATE accounts SET quota=quota-? WHERE id=?", (amount, source["id"]))
            conn.execute("UPDATE accounts SET quota=quota+? WHERE id=?", (amount, target["id"]))
            conn.execute("UPDATE transfers SET status='approved',approved_by=?,approved_at=? WHERE id=?", (actor, utcnow(), transfer_id))
            self._audit(conn, actor, "transfer.approved", "transfer", transfer_id, {"amount": amount})
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        return dict(row)

    def reject_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以退回转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise DomainError("转让不存在或已经处理", 409)
            if actor == row["created_by"]:
                raise DomainError("发起人不能自行退回", 403)
            conn.execute("UPDATE transfers SET status='rejected',approved_by=?,approved_at=? WHERE id=?", (actor, utcnow(), transfer_id))
            self._audit(conn, actor, "transfer.rejected", "transfer", transfer_id, {})
        return {"id": transfer_id, "status": "rejected"}

    def record_usage(self, actor: str, payload: dict[str, Any], role: str = "meter") -> dict[str, Any]:
        if role not in {"meter", "editor"}:
            raise DomainError("只有计量员可以登记取水", 403)
        try:
            account_id = int(payload.get("account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和取水量必须是数值") from exc
        meter_event_id = str(payload.get("meter_event_id", "")).strip()
        occurred = parse_date(str(payload.get("occurred_at", "")), "计量日期")
        if amount <= 0 or not meter_event_id:
            raise DomainError("取水量必须大于 0，计量事件编号不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self._account_row(conn, account_id)
            if account["status"] != "active":
                raise DomainError("原账户已拆分，只供查询，不能再登记取水", 409)
            if not (account["valid_from"] <= occurred.isoformat() <= account["valid_to"]):
                raise DomainError("取水日期不在许可有效期内", 409)
            reserved = self._reserved_outgoing(conn, account_id)
            available = float(account["quota"]) - float(account["used"]) - reserved
            if amount > available + 1e-9:
                raise DomainError("取水超过可用额度", 409)
            season = conn.execute("SELECT max_fraction FROM season_rules WHERE region=? AND month=?", (account["region"], occurred.month)).fetchone()
            month_total = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM usage_records WHERE account_id=? AND substr(occurred_at,1,7)=?",
                (account_id, occurred.strftime("%Y-%m")),
            ).fetchone()["total"]
            if season:
                cap = float(account["quota"]) * float(season["max_fraction"])
                if float(month_total) + amount > cap + 1e-9:
                    raise DomainError("本次取水超过该月份的季节配额", 409)
            try:
                cur = conn.execute(
                    "INSERT INTO usage_records(account_id,meter_event_id,amount,occurred_at,actor,created_at) VALUES(?,?,?,?,?,?)",
                    (account_id, meter_event_id, amount, occurred.isoformat(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("计量事件已登记，不能重复计水", 409) from exc
            conn.execute("UPDATE accounts SET used=used+? WHERE id=?", (amount, account_id))
            self._audit(conn, actor, "usage.recorded", "account", account_id,
                        {"amount": amount, "occurred_at": occurred.isoformat(), "meter_event_id": meter_event_id})
            row = conn.execute("SELECT * FROM usage_records WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def _split_children_payload(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            raise DomainError("子账户列表必须是数组")
        children: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise DomainError("每个子账户必须是对象")

            def text(key: str) -> str | None:
                value = item.get(key)
                if value is None:
                    return None
                value = str(value).strip()
                return value or None

            children.append({
                "name": text("name") or "",
                "region": text("region"),
                "holder": text("holder"),
                "priority": item.get("priority") if str(item.get("priority", "")).strip() else None,
                "valid_from": text("valid_from"),
                "valid_to": text("valid_to"),
                "quota": item.get("quota") if str(item.get("quota", "")).strip() else None,
                "used": item.get("used") if str(item.get("used", "")).strip() else None,
                "take_outgoing": item.get("take_outgoing") or [],
                "take_incoming": item.get("take_incoming") or [],
            })
        return children

    def save_split_draft(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        """保存层（草稿）：对不上也允许保留，但返回逐项差额；只允许每个原账户一份未生效草稿。"""
        if role != "editor":
            raise DomainError("只有配额管理员可以保存拆分草稿", 403)
        try:
            source_id = int(payload.get("source_account_id"))
        except (TypeError, ValueError) as exc:
            raise DomainError("原账户编号必须是数值") from exc
        children = self._split_children_payload(payload.get("children"))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._account_row(conn, source_id)
            if source["status"] != "active":
                raise DomainError("该账户已拆分，不能再保存拆分草稿", 409)
            snapshot = self.split_context(source_id, conn)
            report = evaluate_split(snapshot, children)
            # 名称冲突由账户表唯一约束兜底，这里提前给出清晰提示（含其他已存在账户）。
            names = {r["name"] for r in conn.execute("SELECT name FROM accounts")}
            name_clash = [c["name"] for c in report["after"]["children"] if c["name"] and c["name"] in names]
            if name_clash:
                report["matched"] = False
                report["errors"].append("子账户名称已被占用：" + ", ".join(name_clash))
            existing = conn.execute(
                "SELECT id FROM split_proposals WHERE source_account_id=? AND status='draft'", (source_id,)
            ).fetchone()
            now, children_json, report_json = utcnow(), json.dumps(children, ensure_ascii=False), json.dumps(report, ensure_ascii=False)
            if existing:
                proposal_id = int(existing["id"])
                conn.execute(
                    "UPDATE split_proposals SET children_json=?,report_json=?,created_by=?,created_at=? WHERE id=?",
                    (children_json, report_json, actor, now, proposal_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO split_proposals(source_account_id,status,children_json,report_json,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (source_id, "draft", children_json, report_json, actor, now),
                )
                proposal_id = int(cur.lastrowid)
            self._audit(conn, actor, "split.draft_saved", "split_proposal", proposal_id,
                        {"source_account_id": source_id, "matched": report["matched"], "errors": report["errors"]})
        return {"proposal_id": proposal_id, "source_account_id": source_id,
                "status": "draft", "report": report}

    def apply_split(self, proposal_id: int, actor: str, role: str = "editor") -> dict[str, Any]:
        """保存层（生效）：在同一事务内重建账户、承接待审转让、冻结原账户。

        生效前用最新快照重新核对；对不上则拒绝，草稿原样保留并列出差额。
        不删任何数据：原账户保留额度/用量快照，status=split 只供查询。
        """
        if role != "editor":
            raise DomainError("只有配额管理员可以生效拆分", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            proposal = conn.execute("SELECT * FROM split_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not proposal:
                raise DomainError("拆分提案不存在", 404)
            if proposal["status"] != "draft":
                raise DomainError("该拆分提案已经生效", 409)
            source_id = int(proposal["source_account_id"])
            source = self._account_row(conn, source_id)
            if source["status"] != "active":
                raise DomainError("原账户已经拆分，不能重复生效", 409)
            children = json.loads(proposal["children_json"])
            snapshot = self.split_context(source_id, conn)
            report = evaluate_split(snapshot, children)
            if not report["matched"]:
                raise DomainError("拆分合计与原账户不一致，草稿已保留并列出差额：" + "；".join(report["errors"]), 409)
            names = {r["name"] for r in conn.execute("SELECT name FROM accounts")}
            clash = [c["name"] for c in report["after"]["children"] if c["name"] in names]
            if clash:
                raise DomainError("子账户名称已被占用：" + "，".join(clash), 409)

            child_ids: list[int] = []
            # 子账户继承原账户的地区/持有人/优先级/有效期，额度和已用水量按申请落账。
            for child in report["after"]["children"]:
                cur = conn.execute(
                    """INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,quota,used,status,created_at)
                       VALUES(?,?,?,?,?,?,?,?,'active',?)""",
                    (child["name"], child["region"], child["holder"], child["priority"],
                     child["valid_from"], child["valid_to"], child["quota"], child["used"], utcnow()),
                )
                child_ids.append(int(cur.lastrowid))

            moved_outgoing: list[dict[str, Any]] = []
            for child, cid in zip(report["after"]["children"], child_ids):
                for tid in child["take_outgoing"]:
                    conn.execute("UPDATE transfers SET from_account_id=? WHERE id=? AND status='pending'", (cid, tid))
                    moved_outgoing.append({"transfer_id": tid, "from_child": cid})
                for tid in child["take_incoming"]:
                    conn.execute("UPDATE transfers SET to_account_id=? WHERE id=? AND status='pending'", (cid, tid))
            # 原账户额度与已用水量保留为查询快照；只冻结，不销户。
            conn.execute("UPDATE accounts SET status='split' WHERE id=?", (source_id,))
            conn.execute(
                "UPDATE split_proposals SET status='applied',applied_at=? WHERE id=?",
                (utcnow(), proposal_id),
            )
            self._audit(conn, actor, "split.applied", "split_proposal", proposal_id,
                        {"source_account_id": source_id, "child_account_ids": child_ids,
                         "moved_outgoing": moved_outgoing, "moved_incoming_count":
                             sum(len(c["take_incoming"]) for c in report["after"]["children"])})
            self._audit(conn, actor, "account.split", "account", source_id,
                        {"child_account_ids": child_ids, "proposal_id": proposal_id})
        return {"proposal_id": proposal_id, "source_account_id": source_id,
                "child_account_ids": child_ids, "status": "applied", "report": report}

    def simulate_drought(self, total_supply: float, reduction: float = 0.0, role: str = "viewer") -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts WHERE status='active' ORDER BY priority,name").fetchall()
        supply = total_supply * (1 - reduction)
        allocation: dict[int, float] = {}
        deficit: dict[int, float] = {}
        remaining = supply
        for priority in range(1, 6):
            group = [r for r in rows if int(r["priority"]) == priority]
            if not group:
                continue
            # During shortage, more critical rights receive their remaining
            # allocation first; only then does water flow to lower priorities.
            requested = sum(max(0.0, float(r["quota"]) - float(r["used"])) for r in group)
            take = min(remaining, requested)
            if requested <= 0:
                continue
            for row in group:
                quota_left = max(0.0, float(row["quota"]) - float(row["used"]))
                share = take * quota_left / requested
                allocation[int(row["id"])] = share
                deficit[int(row["id"])] = quota_left - share
            remaining -= take
            if remaining <= 1e-9:
                for lower in rows:
                    if int(lower["priority"]) > priority:
                        left = max(0.0, float(lower["quota"]) - float(lower["used"]))
                        allocation[int(lower["id"])] = 0.0
                        deficit[int(lower["id"])] = left
                break
        return {"total_supply": total_supply, "reduction": reduction, "effective_supply": supply,
                "unallocated": remaining, "allocations": [
                    {"account_id": int(r["id"]), "name": r["name"], "priority": r["priority"],
                     "allocation": allocation.get(int(r["id"]), 0.0), "deficit": deficit.get(int(r["id"]), 0.0)}
                    for r in rows
                ]}

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            source_of = {int(r["source_account_id"]): int(r["id"])
                         for r in conn.execute(
                             "SELECT source_account_id,id FROM split_proposals WHERE status='applied'")}
            result = []
            for row in rows:
                item = dict(row)
                item["available"] = max(0.0, float(row["quota"]) - float(row["used"]) - self._reserved_outgoing(conn, int(row["id"])))
                if row["status"] == "split":
                    item["split_proposal_id"] = source_of.get(int(row["id"]))
                result.append(item)
        return result

    def list_transfers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM transfers ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_accounts():
        return {str(a["name"]): int(a["id"]) for a in db.list_accounts()}
    upstream = db.create_account("alice", {"name": "北区水库", "region": "upstream", "holder": "北区水务公司", "priority": 1, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 1000}, "editor")
    downstream = db.create_account("alice", {"name": "河口灌区", "region": "downstream", "holder": "河口合作社", "priority": 2, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 500}, "editor")
    db.set_season_rule("alice", "upstream", 7, 0.35, "夏季上限", "editor")
    db.set_impact_rule("alice", "upstream", "downstream", 0.4, "保障河口最小生态流量", "editor")
    db.record_usage("meter-01", {"account_id": upstream["id"], "amount": 100, "meter_event_id": "UP-2026-0001", "occurred_at": "2026-03-01"}, "meter")
    return {"北区水库": int(upstream["id"]), "河口灌区": int(downstream["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "WaterRights/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/accounts":
                return self._send({"accounts": self.db.list_accounts()})
            if parsed.path == "/api/transfers":
                return self._send({"transfers": self.db.list_transfers()})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/available"):
                account_id = int(parsed.path.split("/")[3])
                return self._send(self.db.available(account_id))
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/split"):
                account_id = int(parsed.path.split("/")[3])
                return self._send(self.db.split_context(account_id))
            if parsed.path == "/api/drought/simulate":
                q = parse_qs(parsed.query)
                return self._send(self.db.simulate_drought(float(q.get("supply", ["0"])[0]), float(q.get("reduction", ["0"])[0])))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "accounts"]:
                return self._send(self.db.create_account(actor, body, role), 201)
            if parts == ["api", "rules", "season"]:
                return self._send(self.db.set_season_rule(actor, str(body.get("region", "")), int(body.get("month", 0)), body.get("max_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "rules", "impact"]:
                return self._send(self.db.set_impact_rule(actor, str(body.get("source_region", "")), str(body.get("target_region", "")), body.get("min_source_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "transfers"]:
                return self._send(self.db.create_transfer(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "approve":
                return self._send(self.db.approve_transfer(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "reject":
                return self._send(self.db.reject_transfer(int(parts[2]), actor, role))
            if parts == ["api", "usage"]:
                return self._send(self.db.record_usage(actor, body, role), 201)
            if parts == ["api", "splits", "draft"]:
                return self._send(self.db.save_split_draft(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "splits"] and parts[3] == "apply":
                payload = {"proposal_id": body.get("proposal_id", parts[2])}
                return self._send(self.db.apply_split(int(payload["proposal_id"]), actor, role), 201)
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[water] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="跨区域水资源使用权分配与转让服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8007")))
    parser.add_argument("--db", default=os.getenv("WATER_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库并写入示例账户")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed_demo(db)
        print(f"initialized database at {args.db}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"water-rights listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
