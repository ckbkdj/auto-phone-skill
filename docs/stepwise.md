# 单步协议、验证及故障恢复

## 单轮输入

每轮请求仅包含固定目标、locale/location、本轮 observation_id、有界页面节点和最多 4 条操作回执摘要。不追加模型上轮长篇推理，不让模型修改用户权限或设备路由。节点最多 80 个；包名候选最多 100 个。复杂任务每次动作需新 LLM 决策；新鲜决策不等于清空用户目标和最小执行记忆。

## 单轮输出示例

```json
{
  "version": "2.0",
  "observation_id": "0123456789abcdef0123456789abcdef",
  "status": "act",
  "action": {
    "kind": "tap",
    "node_id": 12,
    "text": null,
    "package": null,
    "direction": null,
    "wait_ms": null
  },
  "checks": [{"kind": "text_present", "value": "输入目的地"}],
  "summary": "打开当前页面的目的地输入框"
}
```

可执行动作只有 launch_app / tap / type / clear / swipe / back / home / wait。`type` 是单次**语义替换操作**，底层可能包含聚焦、清空和输入，不宣称一次物理触摸。wait 最长 1500ms。每个动作字段严格对应类型，不使用的字段必须为 null；节点 ID 只对本轮有效。done 必须没有 action，且有正面的当前页面证据；handoff 不能假称完成。

模型没有 steps、thoughts、risk、approval、shell 或 coordinates 字段。协议解析、JSON Schema、Pydantic 语义校验和运行时页面证据共同校验。结构正确和局部证据成立并不能证明任意自然语言业务目标绝对完成，重要任务仍需业务/人工验收。

## 执行边界

控制面先根据真实控件标签评估风险，然后请求确认。确认后页面变化则丢弃旧批准。私网执行器再次采集页面，核验 fingerprint/node_id 后才写日志并下发。指纹包括全部节点的顺序、索引、路径、文本、可用性和控件属性；禁止只按排序文本或前 300 个节点计算指纹。

不自动在找不到元素时点父容器、不暗中批量滑动或关闭弹窗、不在失败后执行多步 repair。需要这些操作时必须新一轮显式决策。单步结果验证后再观察下一屏；对于无进展重复动作、未确认的高风险结果或连接不明，暂停/失败而非盲目推进。

## 不确定结果

Skill 的 SQLite `runtime.operation_journal_path` 持久保存 device + operation_id + 请求摘要 + receipt，不保存输入文字/截图/密钥。效果前写入 intent；执行中断、线程超时、进程崩溃会保留 unresolved/unknown。新请求不能通过更换 ID 继续操作同一设备。

应用层取消无法保证系统调用已被撤销。Appium Python 的后台线程超时后可能仍运行；本版隔离该会话并拒绝后续命令。不是“超时=没有执行”。未知结果需停下核查。

本地管理员恢复：

```bash
# 1. 停止该 Skill 进程，确认旧 Appium 命令已经结束。
# 2. 查看真实手机/业务订单状态，定位 operation_id。
# 3. 根据核查结果明确选择 executed 或 not-executed；不允许模型替选。
python scripts/reconcile_operation.py \
  --journal /持久目录/private-operations.sqlite3 \
  --device cloud-1 --operation '<task_id>:<turn>' \
  --outcome executed --operator-verified
# 4. 重启 Skill，从新页面开始新任务；不要重放旧指令。
```

需要检查本地日志但不泄露用户内容时，可在停机后通过 SQLite 只读查询：

```bash
sqlite3 /持久目录/private-operations.sqlite3 \
  'SELECT device, operation, result FROM operations ORDER BY rowid DESC LIMIT 20;'
```

不要删除或替换 journal；保持目录私有、备份和持久化。恢复命令仅本地管理员可调用，没有网络 RPC / LLM tool 路由。

## 测试与发布的区别

自动化测试：类型约束、过期观测、伪完成、单步闭环、多轮新页面、风险门禁、操作去重/重启围栏、Appium 超时隔离、真正 SDK 的 HTTP 协议测试。

仍需环境验收：你的域名 TLS/WSS、私网网络、Android/UiAutomator2、App 版本、登录态、灰度 UI、位置/目的地消歧、真实订单结果以及 P95 延迟。仓库内 72 个 App 别名和 5 个历史配方不是 Top 500 认证。

本版不自动发布 tag/Release；GitHub 绿色 CI 不替代这些现场验收。控制面任务状态仍是进程内；只有私网操作意图和回执是持久存储。
