# 0.3.0rc2：最终代码交付与部署验收

本版基于已经合并的单步执行器继续完善，没有切换成第二套协议。`NextAction` 是唯一默认模型输出；旧多步规划只留作兼容代码，不在默认任务链路运行。

## 已实现的链路

龙虾只向同一主机/网络命名空间的 loopback Skill 提交目标与 device_id。Skill 主动建立到公网 Docker 的认证 WSS；真实 Appium 连接信息保留在私网。模型每轮只收到最新页面、有界最近回执和固定目标，只输出一个动作。每步验证后才重新决策。

公网先核验新页面、节点和风险，私网收到 guarded_action 后在设备锁内再次读页面。指纹绑定全部节点索引、路径、原始大小写/标点、可用性与尺寸，不能把 1.00 与 100、或交换后的节点当作同一页。输入框只能唯一定位；密码节点内容在私网解析时清除。

## 持久防重复执行

在私网 `skill/config.local.yaml` 配置：

```yaml
runtime:
  operation_journal_path: /var/lib/auto-phone-skill/private-operations.sqlite3
```

目录由运行 Skill 的用户持有，必须可写且持久化；不要公开或提交该数据库。日志只存设备/操作标识、请求摘要及执行状态，不存文字、截图或凭据。

动作前先持久写入 intent，再调用 Appium；成功后记录 executed。相同 operation_id 不同参数被拒绝，已执行的相同操作返回原回执。超时、进程中断或未确认异常会留下 unresolved intent：该设备的新操作 ID 同样被阻止。重启不能把不确定动作变成可自动重试。

这是保守的至多一次语义下发，不是业务“恰好执行一次”。`type` 内部可能包含聚焦、清空、替换等已校验的输入子操作。用户取消也不等于手机系统已经撤销正在执行的操作。已收到执行回执但业务结果未确认时也应人工核对，不能把新任务当作重试交易。

## 本地管理员恢复

先停止 Skill，确认旧 Appium 命令结束，再查看手机/订单等真实状态。查最近操作：

```bash
sqlite3 /var/lib/auto-phone-skill/private-operations.sqlite3 \
 'SELECT device, operation, outcome FROM operations ORDER BY rowid DESC LIMIT 20;'
```

核实是否执行后，选择 executed 或 not-executed：

```bash
python scripts/reconcile_operation.py \
  --journal /var/lib/auto-phone-skill/private-operations.sqlite3 \
  --device cloud-1 --operation '<记录中的operation_id>' \
  --outcome executed --operator-verified
```

该脚本不重放动作，未通过 HTTP/MCP/LLM 暴露。恢复后重启 Skill，读取新页面后启动真正的新任务。不要删除 journal “解锁”，也不要让模型替管理员选核查结论。

## 安装与测试

部署按根 README 分别安装 Docker 与私网 Skill；二者必须升级到同一版，旧无页面绑定的变更 RPC 在私网网络入口被禁用。仓库名、安装目录和 skill frontmatter 均保留 `auto-phone-skill`。

```bash
python -m pip install -e '.[dev,skill]'
python scripts/release_gate.py --core
```

Core 包含 schema/输入输出负例、单步闭环、页面变化、超时/取消/重启去重、真正 HTTP/WSS 协议栈的本机回归和真实 Appium Python SDK 对 W3C HTTP 测试服务的检查。合成手机不是 Android 真机。

```bash
export PHONE_SKILL_URL=http://127.0.0.1:8790
export PHONE_SKILL_DEVICE_ID=cloud-1
# PHONE_SKILL_TOKEN 使用私网 local_token，不要打印/上传它。
export PHONE_AGENT_LIVE_INSTRUCTION='打开系统设置'
export PHONE_AGENT_LIVE_APP_PACKAGE=com.android.settings
python scripts/release_gate.py --release
```

Strict Gate 需要 Docker、你的已连接 Skill/Appium/云手机。打开设置 smoke test 只验证这一个真实目标和链路，不能代替美团、滴滴、支付、不同 App 版本与 Top 500 回归。最终叫车、付款、下单、发消息继续要求相应授权和用户确认。

控制面任务状态和任务幂等仍在进程内；**只有私网操作意图持久化**，不能宣称整个系统具有跨重启任务恢复或直接多副本能力。部署范围为单控制面、每台手机唯一私网 Skill 所有者。

200–500ms 是热会话动作触发目标，没有端到端实测就不作保证；模型推理、私网核验、网络和页面渲染分别计时。每步重新调用 LLM 会增加调用次数，本版限制输入/输出规模而不宣称一定减少整个任务费用。
