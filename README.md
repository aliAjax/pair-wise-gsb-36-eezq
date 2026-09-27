# 跨区域水资源使用权分配与转让

一个仅使用 Python 标准库实现的水权账户、计量、转让审批和干旱情景服务。SQLite 保存账户额度、取水记录、季节规则、上下游影响规则和完整审计日志。

## 运行

```bash
python app.py --init
python app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 会创建北区水库和河口灌区两个示例账户，并添加一条 7 月季节上限和一条最小留存规则。数据库默认是 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。

## API

请求头 `X-User` 和 `X-Role` 用来模拟身份。角色包括 `editor`、`reviewer`、`meter`、`viewer`。

- `POST /api/accounts`：建立账户（额度、优先级、有效期）。
- `POST /api/rules/season`：设置某地区某月份的用水比例上限。
- `POST /api/rules/impact`：设置上下游转让的最小留存比例。
- `POST /api/transfers`：发起转让；待审批金额立即预占，避免同一额度被重复转卖。
- `POST /api/transfers/{id}/approve|reject`：审核；发起人不能审批自己的记录。
- `POST /api/usage`：按计量事件登记实际取水，同一账户同一事件编号只会入账一次。
- `GET /api/accounts/{id}/available`：查看扣减实际用量和待审批预占后的可用额度。
- `GET /api/drought/simulate?supply=1000&reduction=0.3`：按高优先级先行分配，同级账户按剩余额度比例分配。
- `GET /api/accounts/{id}/split`：拆分前明细（额度、已用水量、待审转出/转入、未生效草稿）。
- `POST /api/splits/draft`：保存拆分草稿。申请人按子账户填 `quota`、`used` 和承接的 `take_outgoing`/`take_incoming` 转让编号；地区、持有人、优先级、有效期留空则继承原账户。核对不通过也保存，响应里逐项列出差额。
- `POST /api/splits/{id}/apply`：生效拆分。生效前用最新快照重新核对，对不上则拒绝并保留草稿。
- `GET /api/audit`：完整操作审计。

灌区改制账户拆分不用"先销户再建账"。核对规则：子账户许可额度合计、已用水量合计必须与原账户逐项相等（容差 1e-6），每条待审转让（转出和转入）必须恰好被一个子账户承接。生效在同一事务内完成：新建子账户并按申请落账，待审转让改挂到承接子账户（预占随之转移），原账户置为 `split` 保留额度/用量快照只供查询——不能再取水或发起转让，也不参与干旱分配；新账户可正常取水、转让、审批和干旱分配。审计写入 `split.draft_saved`、`split.applied` 和原账户的 `account.split`，历史记录仍追到原账户。数据读取（`split_context` 快照）、判断（纯函数 `evaluate_split`）、保存（草稿事务/生效事务）和 HTTP 路由分层，前端同口径实现判断层用于实时预览。

余额计算和审批使用 `BEGIN IMMEDIATE`，把余额判断与写入放在同一事务中；因此并发提交不会绕过额度检查。最小留存比例按转出账户的当前许可额度计算。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖转让审批与实际计量、重复计量事件、季节/最小留存规则、预占导致余额不足和发起人自审冲突，以及账户拆分的差额核对、草稿保留、待审转让承接、生效后原账户只读与审计追踪。
