# Auto Phone Skill

面向龙虾 / OpenClaw 的 Android 语义控制子 Agent。**默认执行路径是单步闭环，不是一次生成整条操作流程。**

当前版本：`0.3.0rc1`。本版代码可部署验收；候选代码上传不等于线上 App / 真机认证，不自动创建正式 Release 或 tag。

## 执行方式

```text
固定用户目标 + 本轮新页面 + 最近 4 条执行摘要
    → 本轮只决策一个动作
    → 严格 JSON / 节点归属 / 风险检查
    → 私网 Skill 重新观察并核对页面指纹
    → 写入持久化操作意图 → 执行一次语义操作
    → 获取新页面、验证效果
    → 再决定下一步，或明确完成 / 请求人工接管
```

模型不能返回动作数组、未来步骤、代码、ADB、XPath 或坐标。目标只能引用本轮观测中真实显示的 `node_id`；私网执行前再核对指纹。页面变化时旧决策作废，**不是换坐标继续点击**。

简单“打开某 App”可用本地规则，并验证前台包名。复杂任务每个动作前调用配置的 LLM；旧配方/规划代码保留兼容性测试，**默认服务不会执行旧的整段计划**。不承诺单步模式必然节省总 token 或每一步都正确：输入上下文受限、错误可发现且禁止盲目继续，才是本版的约束。

## Docker 与 Skill 分开部署

```text
公网：Caddy HTTPS :443 → Docker 控制面 :8788（仅容器网络）
                            ↕ 已建立的 WSS 连接，受限 RPC
私网：龙虾 → http://127.0.0.1:8790 Skill → Appium → 指定云手机
                            └── 主动向公网建立 WSS，私网无入站映射
```

设备发现/分配由龙虾完成。Skill 的本地配置保存 `udid`、`appium_url`、`system_port`；龙虾调用时仅传 `device_id`。Docker 不直接连接 Appium/ADB，不需要知道内网设备地址。仓库名和安装目录 `skills/auto-phone-skill/` 无需改名，Python 包名 `lobster-phone-agent` 保留兼容。

### 公网服务器

```bash
git clone https://github.com/ckbkdj/auto-phone-skill.git
cd auto-phone-skill
cp .env.example .env
# 修改域名、公网 URL、两枚不同的随机令牌、模型 URL/key/model。
# 域名 DNS 指向本机；80/443 交给 Caddy。
mkdir -p artifacts
# 容器以 UID 10001 运行；只给此诊断目录写权限。
sudo chown 10001:10001 artifacts
docker compose -f deploy/docker-compose.yml up -d --build
```

`.env` 至少填写：

```dotenv
PHONE_AGENT_DOMAIN=phone.example.com
PHONE_AGENT_PUBLIC_URL=https://phone.example.com
PHONE_AGENT_PRODUCTION_MODE=true
PHONE_AGENT_API_KEY=<独立随机令牌，至少24字符>
PHONE_AGENT_BRIDGE_TOKEN=<另一个独立随机令牌，至少24字符>
PHONE_AGENT_LLM_BASE_URL=https://your-model-endpoint.example/v1
PHONE_AGENT_LLM_API_KEY=<模型密钥>
PHONE_AGENT_LLM_MODEL=<实际支持JSON输出的模型名>
```

令牌可使用 `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` 分别生成，不要使用模板占位符。服务不把模型的长篇推理/历史对话传入下一轮，只传有界当前状态。不同模型的 `reasoning_effort` / `enable_thinking` 参数并不通用，需在模型部署侧配置，本项目不伪造其支持。

### 私网龙虾 / Skill 主机

```bash
git clone https://github.com/ckbkdj/auto-phone-skill.git
cd auto-phone-skill
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[skill]'
bash scripts/bootstrap_appium.sh
appium --address 127.0.0.1 --port 4723
```

另一个终端配置 Skill：

```bash
cd auto-phone-skill
. .venv/bin/activate
cp skill/config.example.yaml skill/config.local.yaml
chmod 600 skill/config.local.yaml
# 编辑公网 HTTPS 域名、bridge/API/local 三枚不同令牌、设备映射。
# runtime.operation_journal_path 指向持久目录，不能放临时文件系统。
auto-phone-skill --config skill/config.local.yaml
```

`api_token` 对应公网 `PHONE_AGENT_API_KEY`，`bridge_token` 对应公网桥接令牌；`local_token` 只供龙虾访问 loopback Skill。共享 Appium 的不同设备需唯一 `system_port`。不要公开 8790 / 4723 / ADB 5555，不使用 `--relaxed-security`。

将 `skills/auto-phone-skill/` 安装到宿主 Skill 目录；运行 helper 的 Python 必须安装本项目：

```bash
export PHONE_SKILL_URL=http://127.0.0.1:8790
export PHONE_SKILL_TOKEN='<local_token>'
export PHONE_SKILL_DEVICE_ID=cloud-1
python skills/auto-phone-skill/scripts/phone_agent.py submit '打开系统设置' \
  --app-package com.android.settings --idempotency-key 'conversation-001:turn-001'
```

## 强制输入输出与恢复边界

JSON 字段白名单、严格类型、重复键拒绝、有限数值、UTF-8、长度/深度/节点数限制同时作用于 HTTP、LLM 和 RPC。LLM 仅允许一个 `Decision`；合法 JSON 仍需通过实际节点、包名、页面证据和风险检查。

私网 SQLite 在动作前持久写入意图。相同操作 ID 只能对应相同请求；重连/重启后返回已记录结果，不重复执行。执行超时、取消或回包不明时为 `unknown`；设备上新的操作 ID 也被挡住，防止换 ID 绕过重试保护。这是**保守的至多一次下发与人工核对，不是业务 exactly-once 保证**。

恢复流程见 [单步协议与恢复](docs/stepwise.md)。**不得删除 journal 文件“解决”问题**。叫车、下单、发送、删除等需要相应权限及当次用户确认。登录、验证码、生物识别和系统权限弹窗交给用户处理。

## 测试

```bash
python -m pip install -e '.[dev,skill]'
python scripts/release_gate.py --core
# 生成实际运行模型对应的全部契约：
python scripts/generate_contracts.py
```

生成目录 `contracts/` 包含 `next-decision.schema.json`、`atomic-request.schema.json`、`atomic-result.schema.json`、任务输入/输出及 OpenAPI；GitHub CI 的 `core-*` artifact 同时携带这些文件。`GET /v1/contracts/next-decision` 可在鉴权后获取当前模型契约。

CI 检查 Python 3.11 / 3.12 / 3.13、覆盖率门槛、Wheel 隔离安装、Docker 构建/运行导入、Compose/Caddy 配置。测试包含**真实 Appium Python SDK 对合成 W3C HTTP 服务**的协议测试，不把它冒充真手机。

目标环境的真实云手机验收：

```bash
export PHONE_SKILL_URL=http://127.0.0.1:8790
export PHONE_SKILL_TOKEN='<local_token>'
export PHONE_SKILL_DEVICE_ID=cloud-1
export PHONE_AGENT_LIVE_INSTRUCTION='打开系统设置'
export PHONE_AGENT_LIVE_APP_PACKAGE=com.android.settings
python scripts/release_gate.py --release
```

该 smoke test 只证明选定链路和测试目标，不证明美团/滴滴全流程或 Top 500。200–500ms 是热会话动作触发目标，远程 LLM、网络往返、页面加载与观察验证另计；事件/trace 分别记录 `decision_ms`、`dispatch_ms`、`observation_ms`。

**部署范围：单控制面进程、每设备唯一私网 Skill 所有者。**任务状态目前在控制面进程内；进程重启不会自动恢复历史任务。私网操作日志会保留并阻止不确定重复执行。多租户/多副本需要另加持久任务存储、分布式设备租约和完整授权体系，不能仅增加 Uvicorn worker 数。
