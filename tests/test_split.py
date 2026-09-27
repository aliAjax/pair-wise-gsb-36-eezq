import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, evaluate_split, seed_demo


class AccountSplitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        ids = seed_demo(self.db)
        self.source = ids["北区水库"]   # quota=1000, used=100
        self.target = ids["河口灌区"]

    def tearDown(self):
        self.tmp.cleanup()

    def _children(self, quota, used=0.0, outgoing=None, incoming=None, name_a="北支一渠", name_b="北支二渠"):
        return [
            {"name": name_a, "quota": quota[0], "used": used[0],
             "take_outgoing": outgoing[0] if outgoing else [],
             "take_incoming": incoming[0] if incoming else []},
            {"name": name_b, "quota": quota[1], "used": used[1],
             "take_outgoing": outgoing[1] if outgoing else [],
             "take_incoming": incoming[1] if incoming else []},
        ]

    def test_draft_kept_and_differences_listed_when_totals_mismatch(self):
        children = self._children(quota=(600, 300), used=(40, 70))  # 额度差 -100，用量差 +10
        res = self.db.save_split_draft("alice", {"source_account_id": self.source, "children": children}, "editor")
        self.assertEqual(res["status"], "draft")
        self.assertFalse(res["report"]["matched"])
        self.assertAlmostEqual(res["report"]["after"]["quota_diff"], -100)
        self.assertAlmostEqual(res["report"]["after"]["used_diff"], 10)
        joined = "；".join(res["report"]["errors"])
        self.assertIn("许可额度", joined)
        self.assertIn("已用水量", joined)
        # 草稿可再次读取
        ctx = self.db.split_context(self.source)
        self.assertEqual(ctx["draft"]["id"], res["proposal_id"])
        # 对不上不能生效，草稿仍在
        with self.assertRaisesRegex(DomainError, "不一致"):
            self.db.apply_split(res["proposal_id"], "alice", "editor")
        self.assertIsNotNone(self.db.split_context(self.source)["draft"])

    def test_pending_transfers_must_be_fully_and_uniquely_assigned(self):
        t1 = self.db.create_transfer("alice", {"from_account_id": self.source, "to_account_id": self.target,
                                               "amount": 100, "effective_date": "2026-06-01"}, "editor")
        t2 = self.db.create_transfer("alice", {"from_account_id": self.source, "to_account_id": self.target,
                                               "amount": 150, "effective_date": "2026-07-01"}, "editor")
        # 未承接 t2
        children = self._children(quota=(500, 500), used=(50, 50),
                                  outgoing=([t1["id"]], []))
        rep = evaluate_split(self.db.split_context(self.source), children)
        self.assertFalse(rep["matched"])
        self.assertEqual(rep["after"]["outgoing_missing"], [t2["id"]])
        # 重复承接同一条
        children_bad = self._children(quota=(500, 500), used=(50, 50),
                                      outgoing=([t1["id"], t2["id"]], [t2["id"]]))
        rep_bad = evaluate_split(self.db.split_context(self.source), children_bad)
        self.assertFalse(rep_bad["matched"])
        self.assertTrue(any("重复承接" in e for e in rep_bad["errors"]))

    def test_apply_split_freezes_source_and_children_take_over(self):
        t_out = self.db.create_transfer("alice", {"from_account_id": self.source, "to_account_id": self.target,
                                                  "amount": 200, "effective_date": "2026-06-01"}, "editor")
        # 第三个账户向原账户转入，验证转入待审也能被承接
        extra = self.db.create_account("alice", {"name": "上游补水站", "region": "upstream", "holder": "补水公司",
                                                 "priority": 1, "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                                                 "quota": 300}, "editor")
        t_in = self.db.create_transfer("alice", {"from_account_id": extra["id"], "to_account_id": self.source,
                                                 "amount": 50, "effective_date": "2026-08-01"}, "editor")
        children = self._children(
            quota=(600, 400), used=(60, 40),
            outgoing=([t_out["id"]], []), incoming=([], [t_in["id"]]),
        )
        draft = self.db.save_split_draft("alice", {"source_account_id": self.source, "children": children}, "editor")
        self.assertTrue(draft["report"]["matched"])
        applied = self.db.apply_split(draft["proposal_id"], "alice", "editor")
        c1, c2 = applied["child_account_ids"]

        accounts = {a["id"]: a for a in self.db.list_accounts()}
        self.assertEqual(accounts[self.source]["status"], "split")
        # 原账户额度/用量快照保留，仍可查询
        self.assertEqual((accounts[self.source]["quota"], accounts[self.source]["used"]), (1000, 100))
        self.assertEqual(self.db.available(self.source)["quota"], 1000)

        for cid, q, u in ((c1, 600, 60), (c2, 400, 40)):
            self.assertEqual(accounts[cid]["status"], "active")
            self.assertEqual((accounts[cid]["quota"], accounts[cid]["used"]), (q, u))
        # 待审转让改挂到承接子账户，预占随之转移
        transfers = {t["id"]: t for t in self.db.list_transfers()}
        self.assertEqual(transfers[t_out["id"]]["from_account_id"], c1)
        self.assertEqual(transfers[t_in["id"]]["to_account_id"], c2)
        self.assertEqual(self.db.available(c1)["reserved_outgoing"], 200)
        self.assertEqual(self.db.available(c2)["reserved_outgoing"], 0)

        # 原账户只供查询：不能取水、不能发起转让
        with self.assertRaisesRegex(DomainError, "只供查询"):
            self.db.record_usage("m", {"account_id": self.source, "amount": 1,
                                       "meter_event_id": "X", "occurred_at": "2026-06-01"}, "meter")
        with self.assertRaisesRegex(DomainError, "只供查询"):
            self.db.create_transfer("alice", {"from_account_id": self.source, "to_account_id": c1,
                                              "amount": 1, "effective_date": "2026-06-01"}, "editor")
        # 新账户可继续取水、承接的转让可被审批
        usage = self.db.record_usage("m", {"account_id": c2, "amount": 30,
                                           "meter_event_id": "C2-1", "occurred_at": "2026-06-01"}, "meter")
        self.assertEqual(usage["amount"], 30)
        approved = self.db.approve_transfer(t_out["id"], "bob", "reviewer")
        self.assertEqual(approved["status"], "approved")

        # 干旱分配不再包含原账户，但包含两个新账户
        sim = self.db.simulate_drought(10000, 0)
        ids_alloc = {a["account_id"] for a in sim["allocations"]}
        self.assertNotIn(self.source, ids_alloc)
        self.assertIn(c1, ids_alloc)

        # 审计仍可追到原账户
        actions = self.db.audit()
        self.assertTrue(any(a["entity_type"] == "account" and a["entity_id"] == self.source
                            and a["action"] == "account.split" for a in actions))
        self.assertTrue(any(a["entity_id"] == draft["proposal_id"] and a["action"] == "split.applied"
                            for a in actions))

    def test_apply_revalidates_against_latest_snapshot(self):
        # 草稿核对一致后、生效前又新增一条待审转让 → 生效必须被拒绝
        children = self._children(quota=(500, 500), used=(50, 50))
        draft = self.db.save_split_draft("alice", {"source_account_id": self.source, "children": children}, "editor")
        self.assertTrue(draft["report"]["matched"])
        self.db.create_transfer("alice", {"from_account_id": self.source, "to_account_id": self.target,
                                          "amount": 80, "effective_date": "2026-06-01"}, "editor")
        with self.assertRaisesRegex(DomainError, "未全部承接"):
            self.db.apply_split(draft["proposal_id"], "alice", "editor")
        # 原账户未被冻结，草稿保留可修改
        self.assertEqual(self.db.split_context(self.source)["source"]["status"], "active")

    def test_permissions_and_repeat_apply(self):
        children = self._children(quota=(500, 500), used=(50, 50))
        with self.assertRaisesRegex(DomainError, "配额管理员"):
            self.db.save_split_draft("v", {"source_account_id": self.source, "children": children}, "viewer")
        draft = self.db.save_split_draft("alice", {"source_account_id": self.source, "children": children}, "editor")
        with self.assertRaises(DomainError):
            self.db.apply_split(draft["proposal_id"], "bob", "reviewer")
        self.db.apply_split(draft["proposal_id"], "alice", "editor")
        with self.assertRaisesRegex(DomainError, "已经生效|已拆分"):
            self.db.apply_split(draft["proposal_id"], "alice", "editor")
        with self.assertRaisesRegex(DomainError, "不能再保存"):
            self.db.save_split_draft("alice", {"source_account_id": self.source, "children": children}, "editor")


if __name__ == "__main__":
    unittest.main()
