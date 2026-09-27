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
- `GET /api/audit`：完整操作审计。

### 账户拆分（灌区改制）

拆分不采用“先销户再建账”的做法，避免已用水量和待审转让在中间丢失：

1. `GET /api/accounts/{id}/split-preview`：取拆分前明细（许可额度、已用水量、待审转出/转入）。
2. `POST /api/splits`：申请人为每个子账户提交许可额度 `quota`、已用水量 `used`、承接的待审转出 `outgoing_transfer_ids` 和待审转入 `incoming_transfer_ids`（地区、优先级、有效期、持有人可覆盖，默认继承原账户）。
   - 三类合计必须与拆分前**逐项一致**：许可额度、已用水量、每一笔待审转让恰好由一个子账户承接。
   - 对得上返回 `200`（平衡草稿，可生效）；对不上**不报错、不丢数据**，返回 `202` 并保留草稿，`differences` 逐条列出差额（`quota_mismatch`、`used_mismatch`、`transfer_unassigned`、`transfer_duplicate`、`transfer_unknown`、子账户超额/优先级冲突等）。同一原账户的草稿重复提交会更新而非新增。
3. `POST /api/splits/{id}/apply`：生效在同一事务内完成——重跑核对（防止草稿期间数据变化）、按草稿创建子账户（继承相应额度与已用水量）、把待审转让改挂到承接子账户、原账户置为 `closed`。核对仍不过则 `409` 并带回差额。
4. `GET /api/splits` / `GET /api/splits/{id}`：拆分记录列表与拆分前后明细（生效后还会回填每个子账户的新账户 ID 和当前余额）。

生效后原账户**只供查询**（余额、明细、审计都保留），不能再取水或发起转让，也不参与干旱分配；新账户是正常活跃账户，继续取水、转让审批（承接的待审转让照常预占和批准）和干旱分配。子账户以 `split_from` 指向原账户，原账户的历史取水、转让和审计记录不受影响，审计中另有 `split.draft_saved`、`split.draft_updated`、`split.applied` 三条轨迹。

代码按数据、判断、保存和接口分层：`_split_snapshot` 只取数，`normalize_split_children` 只做输入整理，`evaluate_split` 是不碰数据库的纯核对函数，`save_split_draft`/`apply_split` 负责事务保存与生效，HTTP 层只做路由和身份校验。

余额计算和审批使用 `BEGIN IMMEDIATE`，把余额判断与写入放在同一事务中；因此并发提交不会绕过额度检查。最小留存比例按转出账户的当前许可额度计算。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖转让审批与实际计量、重复计量事件、季节/最小留存规则、预占导致余额不足和发起人自审冲突，以及账户拆分的纯核对差额、草稿保留、生效后转让承接与原账户只读。
