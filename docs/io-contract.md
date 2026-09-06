# 输入输出契约 — 0.2.0rc2 候选

输入输出必须在代码中验证，不能只依靠提示词。

## 边界

- 龙虾 → 私网 Skill：LocalTaskRequest，只接受 instruction、device_id 和文档化可选字段。
- Skill → 公网 Docker：TaskRequest，设备只包含 id、bridge_id 和有界非敏感 metadata。
- LLM → 规划器：ActionPlan，必须是完整的单个 JSON completion，通过本地 JSON Schema 和 Pydantic 校验。
- Docker ↔ Skill：固定 RPC 方法，各自独立验证参数与返回值。
- API 返回：TaskRecord 或 version=1.0 的 ErrorResponse。

## 强制规则

嵌套字段 extra=forbid，拒绝未知参数、字符串布尔值、字符串整数、非有限数值、重复 JSON 键、混合解释文本及异常 Unicode。HTTP 变更请求最大 262144 字节，仅接受未压缩 application/json 对象。

context / metadata 仅作为显式有界扩展区：限制 16384 字节、8 层、2048 节点，递归拒绝设备连接与凭据字段。字段过滤不是通用敏感文本检测；不得在 instruction 放入密码或验证码。

动作类型、目标、参数、等待时长、滑动方向和坐标页面指纹分别校验。未知 RPC 方法和不一致的 ok/error 返回被拒绝。performed 必须是真实布尔值，不能将字符串 false 当作成功。

同一 idempotency_key 仅能绑定相同规范化请求。相同 operation_id 不同参数被拒绝。当前短期去重与任务存储在单进程内，不宣称持久 exactly-once。

## 发布边界

候选源码本地 176 项测试通过；真实 Docker/WSS/Appium/云手机测试未完成，不创建正式 Release 或 tag。此文档不能代替完整源码和目标环境验收。
