import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, evaluate_split, normalize_split_children, seed_demo


class WaterRightsFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_transfer_approval_usage_and_drought(self):
        source, target = self.accounts["北区水库"], self.accounts["河口灌区"]
        transfer = self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 400, "effective_date": "2026-06-01"}, "editor")
        self.assertEqual(self.db.available(source)["reserved_outgoing"], 400)
        approved = self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        self.assertEqual(approved["status"], "approved")
        usage = self.db.record_usage("meter-01", {"account_id": source, "amount": 150, "meter_event_id": "UP-JUL-1", "occurred_at": "2026-07-10"}, "meter")
        self.assertEqual(usage["amount"], 150)
        simulation = self.db.simulate_drought(1000, 0.3)
        self.assertAlmostEqual(sum(x["allocation"] for x in simulation["allocations"]) + simulation["unallocated"], 700)

    def test_duplicate_meter_event_and_pending_reservation(self):
        source, target = self.accounts["北区水库"], self.accounts["河口灌区"]
        self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 500, "effective_date": "2026-06-01"}, "editor")
        with self.assertRaises(DomainError):
            self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 1, "effective_date": "2026-06-01"}, "editor")
        self.db.record_usage("meter-01", {"account_id": source, "amount": 10, "meter_event_id": "M-1", "occurred_at": "2026-08-01"}, "meter")
        with self.assertRaisesRegex(DomainError, "不能重复计水"):
            self.db.record_usage("meter-01", {"account_id": source, "amount": 10, "meter_event_id": "M-1", "occurred_at": "2026-08-01"}, "meter")

    def test_third_party_and_self_approval_conflicts(self):
        source, target = self.accounts["北区水库"], self.accounts["河口灌区"]
        with self.assertRaisesRegex(DomainError, "最小留存"):
            self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 501, "effective_date": "2026-06-01"}, "editor")
        transfer = self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 100, "effective_date": "2026-06-01"}, "editor")
        with self.assertRaisesRegex(DomainError, "不能批准自己"):
            self.db.approve_transfer(transfer["id"], "alice", "reviewer")


class AccountSplitFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        # 待拆账户：许可 500、已用 60，有一笔待审转出（100）和一笔待审转入（50）。
        self.source = self.db.create_account(
            "alice", {"name": "待拆灌区", "region": "downstream", "holder": "老合作社",
                      "priority": 2, "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                      "quota": 500}, "editor")["id"]
        self.counterparty = self.db.create_account(
            "alice", {"name": "邻区账户", "region": "upstream", "holder": "邻区水务",
                      "priority": 2, "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                      "quota": 800}, "editor")["id"]
        self.db.record_usage("meter-01", {"account_id": self.source, "amount": 60,
                                          "meter_event_id": "SRC-1", "occurred_at": "2026-03-01"}, "meter")
        self.outgoing = self.db.create_transfer(
            "alice", {"from_account_id": self.source, "to_account_id": self.counterparty,
                      "amount": 100, "effective_date": "2026-06-01"}, "editor")["id"]
        self.incoming = self.db.create_transfer(
            "alice", {"from_account_id": self.counterparty, "to_account_id": self.source,
                      "amount": 50, "effective_date": "2026-06-01"}, "editor")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _children(self, quota1=300, used1=40, quota2=200, used2=20):
        return [
            {"name": "拆分子户甲", "holder": "甲合作社", "priority": 2,
             "quota": quota1, "used": used1,
             "outgoing_transfer_ids": [self.outgoing], "incoming_transfer_ids": []},
            {"name": "拆分子户乙", "holder": "乙合作社", "priority": 2,
             "quota": quota2, "used": used2,
             "outgoing_transfer_ids": [], "incoming_transfer_ids": [self.incoming]},
        ]

    def _save(self, children=None):
        payload = {"source_account_id": self.source, "children": children or self._children(),
                   "note": "灌区改制"}
        return self.db.save_split_draft("alice", payload, "editor")

    def test_pure_validation_detects_differences(self):
        with self.db.connect() as conn:
            snapshot = self.db._split_snapshot(conn, self.source)
            source_row = conn.execute("SELECT * FROM accounts WHERE id=?", (self.source,)).fetchone()
        children = normalize_split_children(source_row, self._children(quota1=250, used1=40))
        result = evaluate_split(snapshot, children)
        self.assertFalse(result["ok"])
        codes = {d["code"] for d in result["differences"]}
        self.assertIn("quota_mismatch", codes)
        self.assertNotIn("used_mismatch", codes)

    def test_balanced_apply_carries_balances_transfers_and_blocks_old_account(self):
        detail, balanced = self._save()
        self.assertTrue(balanced)
        self.assertEqual(detail["differences"], [])
        applied = self.db.apply_split(detail["id"], "alice", "editor")
        self.assertEqual(applied["status"], "applied")

        accounts = {a["id"]: a for a in self.db.list_accounts()}
        self.assertEqual(accounts[self.source]["status"], "closed")
        child_a, child_b = [a for a in accounts.values() if a["name"].startswith("拆分子户")]
        self.assertEqual(child_a["split_from"], self.source)
        self.assertEqual(float(child_a["quota"]), 300)
        self.assertEqual(float(child_a["used"]), 40)
        self.assertEqual(float(child_b["quota"]), 200)
        self.assertEqual(float(child_b["used"]), 20)

        transfers = {t["id"]: t for t in self.db.list_transfers()}
        self.assertEqual(transfers[self.outgoing]["from_account_id"], child_a["id"])
        self.assertEqual(transfers[self.incoming]["to_account_id"], child_b["id"])
        # 承接的待审转让继续占用子账户额度，审批流程照常。
        self.assertEqual(self.db.available(child_a["id"])["reserved_outgoing"], 100)
        approved = self.db.approve_transfer(self.outgoing, "bob", "reviewer")
        self.assertEqual(approved["status"], "approved")

        with self.assertRaisesRegex(DomainError, "只供查询"):
            self.db.record_usage("meter-01", {"account_id": self.source, "amount": 1,
                                              "meter_event_id": "X", "occurred_at": "2026-06-01"}, "meter")
        with self.assertRaisesRegex(DomainError, "只供查询"):
            self.db.create_transfer("alice", {"from_account_id": self.source,
                                              "to_account_id": child_b["id"], "amount": 1,
                                              "effective_date": "2026-06-01"}, "editor")

        # 关闭账户仍可查询，且干旱分配只覆盖新账户。
        self.assertEqual(self.db.available(self.source)["quota"], 500)
        sim = self.db.simulate_drought(2000, 0)
        self.assertNotIn(self.source, {a["account_id"] for a in sim["allocations"]})
        self.assertIn(child_a["id"], {a["account_id"] for a in sim["allocations"]})

        # 历史审计仍能追到原账户和拆分动作本身。
        actions = {(e["action"], e["entity_type"]) for e in self.db.audit()}
        self.assertIn(("usage.recorded", "account"), actions)
        self.assertIn(("split.applied", "account_split"), actions)
        split_log = next(e for e in self.db.audit() if e["action"] == "split.applied")
        self.assertIn(str(self.source), split_log["details"])

    def test_mismatch_keeps_draft_with_differences_and_blocks_apply(self):
        detail, balanced = self._save(self._children(quota1=300, used1=30, quota2=200, used2=20))
        self.assertFalse(balanced)
        codes = {d["code"] for d in detail["differences"]}
        self.assertEqual(codes, {"used_mismatch"})
        self.assertEqual(detail["status"], "draft")
        with self.assertRaises(DomainError) as ctx:
            self.db.apply_split(detail["id"], "alice", "editor")
        self.assertEqual(ctx.exception.status, 409)
        self.assertTrue(ctx.exception.details)
        # 没生效：原账户仍活跃，没有子账户建出来。
        self.assertEqual(len(self.db.list_accounts()), 2)

    def test_unassigned_and_duplicate_transfer_are_reported(self):
        children = self._children()
        children[0]["outgoing_transfer_ids"] = []  # 两笔都没人承接
        detail, balanced = self._save(children)
        self.assertFalse(balanced)
        self.assertIn("transfer_unassigned", {d["code"] for d in detail["differences"]})

        children = self._children()
        children[1]["outgoing_transfer_ids"] = [self.outgoing]  # 乙重复承接甲的转出
        detail, balanced = self._save(children)
        self.assertFalse(balanced)
        self.assertIn("transfer_duplicate", {d["code"] for d in detail["differences"]})

    def test_draft_is_upserted_and_closed_source_cannot_split_again(self):
        first, _ = self._save()
        second, _ = self._save(self._children(quota1=250, quota2=250))
        self.assertEqual(first["id"], second["id"])
        self.db.apply_split(first["id"], "alice", "editor")
        with self.assertRaisesRegex(DomainError, "只供查询"):
            self._save()


if __name__ == "__main__":
    unittest.main()
