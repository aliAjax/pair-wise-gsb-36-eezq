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


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400, details: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.status = status
        # 拆分核对差额等结构化问题，随错误响应一并返回给调用方。
        self.details = details


EPS = 1e-9


def normalize_split_children(source: sqlite3.Row | dict[str, Any], raw_children: Any) -> list[dict[str, Any]]:
    """把申请人提交的子账户草稿整理成统一结构（不访问数据库、不做业务判断）。"""
    if not isinstance(raw_children, list) or not raw_children:
        raise DomainError("子账户列表不能为空")
    children: list[dict[str, Any]] = []
    for index, item in enumerate(raw_children):
        if not isinstance(item, dict):
            raise DomainError(f"第{index + 1}个子账户格式不正确")

        def number(key: str) -> float:
            try:
                return float(item[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise DomainError(f"第{index + 1}个子账户的{key}必须是数值") from exc

        try:
            priority = int(item.get("priority", source["priority"]))
        except (TypeError, ValueError) as exc:
            raise DomainError(f"第{index + 1}个子账户的优先级必须是整数") from exc
        valid_from = parse_date(str(item.get("valid_from") or source["valid_from"]), "生效日期")
        valid_to = parse_date(str(item.get("valid_to") or source["valid_to"]), "失效日期")

        def id_list(key: str) -> list[int]:
            value = item.get(key, [])
            if not isinstance(value, list):
                raise DomainError(f"第{index + 1}个子账户承接的转让必须是编号列表")
            try:
                return [int(v) for v in value]
            except (TypeError, ValueError) as exc:
                raise DomainError(f"第{index + 1}个子账户承接的转让编号必须是整数") from exc

        children.append({
            "name": str(item.get("name", "")).strip(),
            "holder": str(item.get("holder", "")).strip(),
            "region": str(item.get("region") or source["region"]).strip(),
            "priority": priority,
            "valid_from": valid_from.isoformat(),
            "valid_to": valid_to.isoformat(),
            "quota": number("quota"),
            "used": number("used"),
            "outgoing_transfer_ids": id_list("outgoing_transfer_ids"),
            "incoming_transfer_ids": id_list("incoming_transfer_ids"),
        })
    return children


def evaluate_split(snapshot: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any]:
    """纯判断：逐项核对许可额度、已用水量、待审转让承接是否与拆分前一致。"""
    source = snapshot["source"]
    outgoing = {int(t["id"]): t for t in snapshot["pending_outgoing"]}
    incoming = {int(t["id"]): t for t in snapshot["pending_incoming"]}
    pending = {**outgoing, **incoming}
    existing_names = set(snapshot["existing_names"])
    differences: list[dict[str, Any]] = []
    assigned: dict[int, dict[str, Any]] = {}
    seen_names: set[str] = set()
    total_quota = total_used = 0.0

    for idx, child in enumerate(children):
        quota, used = float(child["quota"]), float(child["used"])
        total_quota += quota
        total_used += used
        name = child["name"]
        if not name:
            differences.append({"code": "child_name_missing", "child_index": idx,
                                "message": f"第{idx + 1}个子账户缺少名称"})
        elif name in seen_names:
            differences.append({"code": "child_name_duplicate", "child_index": idx, "name": name,
                                "message": f"子账户名称“{name}”重复"})
        else:
            seen_names.add(name)
            if name in existing_names:
                differences.append({"code": "child_name_exists", "child_index": idx, "name": name,
                                    "message": f"账户名称“{name}”已存在"})
        if not child["holder"]:
            differences.append({"code": "child_holder_missing", "child_index": idx,
                                "message": f"子账户“{name or idx + 1}”缺少持有人"})
        if not 1 <= int(child["priority"]) <= 5:
            differences.append({"code": "child_priority_invalid", "child_index": idx,
                                "message": f"子账户“{name or idx + 1}”的优先级应在 1 到 5 之间"})
        if child["valid_from"] > child["valid_to"]:
            differences.append({"code": "child_validity_invalid", "child_index": idx,
                                "message": f"子账户“{name or idx + 1}”的生效日期不能晚于失效日期"})
        if quota < 0 or used < 0:
            differences.append({"code": "child_negative", "child_index": idx, "name": name,
                                "message": f"子账户“{name or idx + 1}”的额度和已用水量不能为负"})
        if used > quota + EPS:
            differences.append({"code": "child_used_over_quota", "child_index": idx, "name": name,
                                "quota": quota, "used": used,
                                "message": f"子账户“{name or idx + 1}”已用水量 {used:g} 超过许可额度 {quota:g}"})
        reserved_outgoing = 0.0
        for direction, ids in (("outgoing", child["outgoing_transfer_ids"]),
                               ("incoming", child["incoming_transfer_ids"])):
            for transfer_id in ids:
                transfer = pending.get(int(transfer_id))
                if transfer is None:
                    differences.append({"code": "transfer_unknown", "child_index": idx,
                                        "transfer_id": transfer_id,
                                        "message": f"转让 {transfer_id} 不是该账户的待审转让"})
                    continue
                if int(transfer_id) in assigned:
                    differences.append({"code": "transfer_duplicate", "child_index": idx,
                                        "transfer_id": transfer_id,
                                        "message": f"待审转让 {transfer_id} 被重复承接"})
                    continue
                assigned[int(transfer_id)] = {"child_index": idx, "direction": direction}
                if direction == "outgoing":
                    reserved_outgoing += float(transfer["amount"])
                    if int(child["priority"]) > int(transfer["other_priority"]):
                        differences.append({"code": "transfer_priority_conflict", "child_index": idx,
                                            "transfer_id": transfer_id,
                                            "message": f"承接转出 {transfer_id} 后会违反优先级保护"})
                elif int(transfer["other_priority"]) > int(child["priority"]):
                    differences.append({"code": "transfer_priority_conflict", "child_index": idx,
                                        "transfer_id": transfer_id,
                                        "message": f"承接转入 {transfer_id} 后会违反优先级保护"})
        if used + reserved_outgoing > quota + EPS:
            differences.append({"code": "child_reserved_over_quota", "child_index": idx, "name": name,
                                "quota": quota, "used": used, "reserved_outgoing": reserved_outgoing,
                                "message": f"子账户“{name or idx + 1}”已用水量加承接的待审转出超过许可额度"})

    quota_diff = float(source["quota"]) - total_quota
    if abs(quota_diff) > EPS:
        differences.append({"code": "quota_mismatch", "expected": float(source["quota"]),
                            "actual": total_quota, "diff": quota_diff,
                            "message": f"许可额度合计 {total_quota:g}，与拆分前 {float(source['quota']):g} 相差 {quota_diff:g}"})
    used_diff = float(source["used"]) - total_used
    if abs(used_diff) > EPS:
        differences.append({"code": "used_mismatch", "expected": float(source["used"]),
                            "actual": total_used, "diff": used_diff,
                            "message": f"已用水量合计 {total_used:g}，与拆分前 {float(source['used']):g} 相差 {used_diff:g}"})
    for transfer_id, transfer in pending.items():
        if transfer_id not in assigned:
            direction = "转出" if transfer_id in outgoing else "转入"
            differences.append({"code": "transfer_unassigned", "transfer_id": transfer_id,
                                "amount": float(transfer["amount"]), "direction": direction,
                                "message": f"待审{direction}转让 {transfer_id}（{float(transfer['amount']):g}）没有子账户承接"})

    return {"ok": not differences, "differences": differences,
            "totals": {"quota": total_quota, "used": total_used,
                       "expected_quota": float(source["quota"]),
                       "expected_used": float(source["used"]),
                       "assigned_transfers": len(assigned),
                       "pending_transfers": len(pending)}}


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
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed')),
                    split_from INTEGER REFERENCES accounts(id),
                    created_at TEXT NOT NULL,
                    CHECK(valid_from <= valid_to)
                );
                CREATE TABLE IF NOT EXISTS account_splits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','applied')),
                    children TEXT NOT NULL,
                    differences TEXT NOT NULL DEFAULT '[]',
                    totals TEXT NOT NULL DEFAULT '{}',
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    applied_by TEXT,
                    created_at TEXT NOT NULL,
                    applied_at TEXT,
                    UNIQUE(source_account_id, status)
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
                """
            )
            # 旧版本数据库没有账户状态列，按顺序补齐（SQLite 不支持 IF NOT EXISTS ADD COLUMN）。
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
            if "status" not in columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            if "split_from" not in columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN split_from INTEGER REFERENCES accounts(id)")

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

    def _require_active(self, row: sqlite3.Row, label: str = "账户") -> None:
        if row["status"] != "active":
            raise DomainError(f"{label}已随拆分关闭，只供查询，不能再办理业务", 409)

    def _split_snapshot(self, conn: sqlite3.Connection, account_id: int) -> dict[str, Any]:
        """取数层：汇总拆分前的账户、待审转让（含对手方优先级）和现有账户名。"""
        source = self._account_row(conn, account_id)
        pending_out = [
            dict(r) for r in conn.execute(
                """SELECT t.id,t.from_account_id,t.to_account_id,t.amount,t.effective_date,
                          a.priority AS other_priority,a.name AS other_name
                   FROM transfers t JOIN accounts a ON a.id=t.to_account_id
                   WHERE t.from_account_id=? AND t.status='pending'""",
                (account_id,),
            )
        ]
        pending_in = [
            dict(r) for r in conn.execute(
                """SELECT t.id,t.from_account_id,t.to_account_id,t.amount,t.effective_date,
                          a.priority AS other_priority,a.name AS other_name
                   FROM transfers t JOIN accounts a ON a.id=t.from_account_id
                   WHERE t.to_account_id=? AND t.status='pending'""",
                (account_id,),
            )
        ]
        existing_names = [r["name"] for r in conn.execute("SELECT name FROM accounts")]
        return {"source": dict(source), "pending_outgoing": pending_out,
                "pending_incoming": pending_in, "existing_names": existing_names}

    def split_preview(self, account_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            snapshot = self._split_snapshot(conn, account_id)
        source = snapshot["source"]
        return {
            "source": source,
            "quota": source["quota"],
            "used": source["used"],
            "pending_outgoing": snapshot["pending_outgoing"],
            "pending_incoming": snapshot["pending_incoming"],
        }

    def _split_detail(self, conn: sqlite3.Connection, split_id: int) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM account_splits WHERE id=?", (split_id,)).fetchone()
        if not row:
            raise DomainError("拆分记录不存在", 404)
        source = self._account_row(conn, row["source_account_id"])
        children = json.loads(row["children"])
        if row["status"] == "applied":
            new_rows = conn.execute(
                "SELECT id,name,quota,used,status FROM accounts WHERE split_from=? ORDER BY id",
                (row["source_account_id"],),
            ).fetchall()
            new_by_name = {r["name"]: dict(r) for r in new_rows}
            for child in children:
                created = new_by_name.get(child["name"])
                child["new_account_id"] = int(created["id"]) if created else None
                if created:
                    child["current_quota"] = created["quota"]
                    child["current_used"] = created["used"]
                    child["current_status"] = created["status"]
        return {
            "id": row["id"],
            "source_account_id": row["source_account_id"],
            "status": row["status"],
            "note": row["note"],
            "children": children,
            "differences": json.loads(row["differences"]),
            "totals": json.loads(row["totals"]),
            "created_by": row["created_by"],
            "applied_by": row["applied_by"],
            "created_at": row["created_at"],
            "applied_at": row["applied_at"],
            "source": dict(source),
        }

    def save_split_draft(self, actor: str, payload: dict[str, Any], role: str = "editor") -> tuple[dict[str, Any], bool]:
        if role != "editor":
            raise DomainError("只有配额管理员可以办理账户拆分", 403)
        try:
            source_id = int(payload.get("source_account_id"))
        except (TypeError, ValueError) as exc:
            raise DomainError("原账户编号必须是整数") from exc
        note = str(payload.get("note", "")).strip()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._account_row(conn, source_id)
            self._require_active(source, "原账户")
            children = normalize_split_children(source, payload.get("children"))
            result = evaluate_split(self._split_snapshot(conn, source_id), children)
            existing = conn.execute(
                "SELECT id FROM account_splits WHERE source_account_id=? AND status='draft'",
                (source_id,),
            ).fetchone()
            now = utcnow()
            serialized = (
                json.dumps(children, ensure_ascii=False),
                json.dumps(result["differences"], ensure_ascii=False),
                json.dumps(result["totals"], ensure_ascii=False),
            )
            if existing:
                conn.execute(
                    "UPDATE account_splits SET children=?,differences=?,totals=?,note=?,created_by=?,created_at=? WHERE id=?",
                    (*serialized, note, actor, now, existing["id"]),
                )
                split_id, action = int(existing["id"]), "split.draft_updated"
            else:
                cur = conn.execute(
                    """INSERT INTO account_splits(source_account_id,children,differences,totals,note,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (source_id, *serialized, note, actor, now),
                )
                split_id, action = int(cur.lastrowid), "split.draft_saved"
            self._audit(conn, actor, action, "account_split", split_id,
                        {"source": source_id, "balanced": result["ok"],
                         "difference_count": len(result["differences"])})
            detail = self._split_detail(conn, split_id)
        return detail, result["ok"]

    def apply_split(self, split_id: int, actor: str, role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以生效拆分", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM account_splits WHERE id=?", (split_id,)).fetchone()
            if not row:
                raise DomainError("拆分记录不存在", 404)
            if row["status"] != "draft":
                raise DomainError("该拆分已经生效，不能重复执行", 409)
            source = self._account_row(conn, row["source_account_id"])
            self._require_active(source, "原账户")
            children = json.loads(row["children"])
            # 生效前用最新余额和待审转让重跑一次纯判断，防止草稿期间数据变化。
            result = evaluate_split(self._split_snapshot(conn, source["id"]), children)
            if not result["ok"]:
                raise DomainError("拆分差额未补齐，草稿已保留并列出差额", 409, result["differences"])
            new_ids: list[int] = []
            for child in children:
                try:
                    cur = conn.execute(
                        """INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,
                                                quota,used,status,split_from,created_at)
                           VALUES(?,?,?,?,?,?,?,?,'active',?,?)""",
                        (child["name"], child["region"], child["holder"], child["priority"],
                         child["valid_from"], child["valid_to"], child["quota"], child["used"],
                         source["id"], utcnow()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DomainError(f"账户名称“{child['name']}”已存在", 409) from exc
                new_ids.append(int(cur.lastrowid))
            reassigned: list[int] = []
            for child, new_id in zip(children, new_ids):
                for transfer_id in child["outgoing_transfer_ids"]:
                    conn.execute(
                        "UPDATE transfers SET from_account_id=? WHERE id=? AND status='pending'",
                        (new_id, transfer_id),
                    )
                    reassigned.append(int(transfer_id))
                for transfer_id in child["incoming_transfer_ids"]:
                    conn.execute(
                        "UPDATE transfers SET to_account_id=? WHERE id=? AND status='pending'",
                        (new_id, transfer_id),
                    )
                    reassigned.append(int(transfer_id))
            conn.execute("UPDATE accounts SET status='closed' WHERE id=?", (source["id"],))
            conn.execute(
                "UPDATE account_splits SET status='applied',applied_by=?,applied_at=? WHERE id=?",
                (actor, utcnow(), split_id),
            )
            self._audit(conn, actor, "split.applied", "account_split", split_id,
                        {"source": source["id"], "new_accounts": new_ids,
                         "reassigned_transfers": reassigned, "totals": result["totals"]})
            detail = self._split_detail(conn, split_id)
        return detail

    def split_detail(self, split_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            return self._split_detail(conn, split_id)

    def list_splits(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM account_splits ORDER BY id DESC").fetchall()
            result = []
            for row in rows:
                item = {k: row[k] for k in row.keys() if k not in {"children", "differences", "totals"}}
                item["children_count"] = len(json.loads(row["children"]))
                item["difference_count"] = len(json.loads(row["differences"]))
                item["totals"] = json.loads(row["totals"])
                result.append(item)
        return result

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
            self._require_active(source, "转出账户")
            self._require_active(target, "转入账户")
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
            self._require_active(account)
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

    def simulate_drought(self, total_supply: float, reduction: float = 0.0, role: str = "viewer") -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        with self.connect() as conn:
            # 拆分关闭后的账户只供查询，不再参与干旱分配。
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
            result = []
            for row in rows:
                item = dict(row)
                item["available"] = max(0.0, float(row["quota"]) - float(row["used"]) - self._reserved_outgoing(conn, int(row["id"])))
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
            if parsed.path == "/api/splits":
                return self._send({"splits": self.db.list_splits()})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/available"):
                account_id = int(parsed.path.split("/")[3])
                return self._send(self.db.available(account_id))
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/split-preview"):
                account_id = int(parsed.path.split("/")[3])
                return self._send(self.db.split_preview(account_id))
            if parsed.path.startswith("/api/splits/"):
                split_id = int(parsed.path.split("/")[3])
                return self._send(self.db.split_detail(split_id))
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
            if parts == ["api", "splits"]:
                # 对不上时不报错拒绝：草稿照常落库，差额随 202 响应返回。
                detail, balanced = self.db.save_split_draft(actor, body, role)
                return self._send(detail, 200 if balanced else 202)
            if len(parts) == 4 and parts[:2] == ["api", "splits"] and parts[3] == "apply":
                return self._send(self.db.apply_split(int(parts[2]), actor, role), 201)
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc), **({"differences": exc.details} if getattr(exc, "details", None) else {})},
                       getattr(exc, "status", 400))

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
