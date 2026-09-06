# Auto Phone Skill

Android 语义控制子 Agent：**公网 Docker 控制面 + 私网 Skill 会话桥梁 + Appium 云手机**。

当前版本 `0.3.0rc1`。默认执行器已改为单步闭环，不再一次生成整条流程后连续点击。源码上传不等于真实云手机生产验收；本仓库不自动创建正式 Release 或 tag。

## 单步闭环

```text
用户目标
  → 获取最新页面
  → 仅决定一个 NextAction
  → Schema、可见节点、风险与权限校验
  → 推理/用户确认后重新检查页面
  → 执行一个语义动作
  → 观察并验证结果
  → 用新页面独立决定下一步
```

每次模型请求只包含原始目标、当前页面最多 64 个节点、最多 3 条近期事实回执、有限应用目录和必要位置上下文。不会追加之前的 assistant 思考过程，也不会给模型执行多步 `steps` 数组的权力。单次输出上限为配置值与 700 token 中的较小者。

`NextAction` 严格限制动作与字段。模型只能引用当前 observation 中的节点编号，不能输出坐标、任意代码、Appium 地址或修改权限。执行器在推理和人工确认后重新读取页面，页面或目标变化则丢弃旧动作。输入框必须能在 Appium 中唯一定位；不会在指定输入框消失后偷偷改用当前焦点。

命令已发送不等于成功。动作后验证可观察条件；完成任务需要正向页面证据。未知回包、未验证结果、无进展重复操作均停止，不能让模型“解释成成功”。Appium 超时或取消后的未决命令会隔离设备，锁保持到实际调用结束；不得通过自动重连继续操作。

复杂页面仍可能判断错误；这是减少错误传播和误操作的机制，不是每轮绝对正确的保证。

## 公网与私网

```text
龙虾 / OpenClaw（内网）
  → http://127.0.0.1:8790（Skill，仅 loopback）
      ├→ 私网 Appium → 指定云手机
      └→ 主动出站 WSS → 公网域名 :443 → Caddy → Docker 控制面 :8788
                               同一连接回传受限 RPC
```

只有 Caddy 的 80/443 对公网开放。8788 不发布宿主端口；8790、4723、5555 不暴露公网。Docker 不保存 UDID、Appium URL 或设备连接凭据。龙虾只发送已由宿主选定的 `device_id`，Skill 在私网配置中解析设备。宿主与 Skill 必须共享 loopback 网络命名空间，或由宿主在同一运行环境启动 Skill；不要为了容器互通改成公网监听。

## 公网 Docker 部署

```bash
git clone https://github.com/ckbkdj/auto-phone-skill.git
cd auto-phone-skill
cp .env.example .env
chmod 600 .env
# 编辑 .env：真实域名、模型地址、MODEL，以及不同的随机 API/Bridge 密钥。
# 下面生成三个随机值：分别用于 API_TOKEN、BRIDGE_TOKEN、LOCAL_TOKEN。
python3 -c 'import secrets; [print(secrets.token_urlsafe(32)) for _ in range(3)]'
# 确保容器 UID 10001 可写诊断目录；不要使用 chmod 777。
sudo install -d -m 750 -o 10001 -g 10001 artifacts
docker compose -f deploy/docker-compose.yml config --quiet
docker compose -f deploy/docker-compose.yml up -d --build
```

`phone.example.com` 全部替换为你的真实域名，并设置 DNS 指向 Caddy 服务器。示例密钥只是占位符，不能原样部署。模型配置只在公网控制面设置；没有 `PHONE_AGENT_LLM_MODEL` 时，默认模式只支持可解析的直接打开 App 指令，复杂任务会明确报错。

三个令牌的对应关系：公网 `PHONE_AGENT_API_KEY` 对应私网 `api_token`；公网 `PHONE_AGENT_BRIDGE_TOKEN` 对应私网 `bridge_token`；私网 `local_token` 只供龙虾本机调用。三者必须不同。多桥接实例用 `PHONE_AGENT_BRIDGE_TOKENS` 给每个 bridge_id 独立令牌。

## 私网 Skill 部署

在能连接云手机的龙虾宿主上执行：

```bash
git clone https://github.com/ckbkdj/auto-phone-skill.git
cd auto-phone-skill
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[skill]'
cp skill/config.example.yaml skill/config.local.yaml
chmod 600 skill/config.local.yaml
# 编辑 server_url、三种令牌、bridge_id、devices。
# udid 由龙虾决定；例如 emulator-5554 或既有网络 ADB 设备标识。
# 同一个 Appium server 上多台手机的 system_port 必须各不相同。
bash scripts/bootstrap_appium.sh
```

Appium 和 Skill 分别作为常驻服务运行（示例用两个终端，生产请交给服务管理器）：

```bash
appium --address 127.0.0.1 --port 4723
```

```bash
. .venv/bin/activate
auto-phone-skill --config skill/config.local.yaml
```

不需要给内网做入站端口映射。Skill 主动向公网建立 WSS。不要使用 `--relaxed-security`。

将 `skills/auto-phone-skill/` 安装到龙虾支持的 Skill 目录，并确保其 Python 解释器安装了匹配版本的包。仓库名、目录名和 `SKILL.md` 的 `name` 均为 `auto-phone-skill`；兼容原有 `lobster-phone-agent` 包名及命令别名。

```bash
export PHONE_SKILL_URL=http://127.0.0.1:8790
export PHONE_SKILL_TOKEN='私网配置中的local_token'
export PHONE_SKILL_DEVICE_ID=cloud-1
python skills/auto-phone-skill/scripts/phone_agent.py submit '打开系统设置' \
  --app-package com.android.settings \
  --idempotency-key 'conversation-001:turn-001'
```

同一个用户回合的网络重试保持相同幂等键。`waiting_confirmation` 必须展示内容并得到用户明确批准；`waiting_handoff` 由用户完成登录、验证码等后再恢复。不能擅自批准付款、下单、叫车、发布或发送。动作结果未知时先核对手机和业务状态，不要重提相同交易。

## 输入输出契约

代码同时验证 LocalTaskRequest、TaskRequest、NextAction、RPC 参数/结果和 TaskRecord。拒绝未知嵌套字段、重复 JSON 键、混合解释文本、字符串布尔值、不合法数字、越界参数和超限消息。HTTP 请求体上限 262144 字节；显式 context/metadata 扩展区另有限制。输入输出约束不是通用敏感信息识别器，不要在 instruction/context 中输入凭据。

```bash
python scripts/generate_contracts.py
```

生成 `contracts/next-action.schema.json`、请求/响应 Schema、RPC 契约和两个 OpenAPI 文件。线上可通过鉴权后的 `/v1/contracts/next-action` 获取权威动作结构。CI 的 core artifacts 也包含这些文件。`ActionPlan` 在实时任务输出中只记录本轮一个动作，不代表模型提前规划整条路径。

## 测试与发布

```bash
python -m pip install -e '.[dev]'
python scripts/release_gate.py --core
# 实际部署环境同时具备 Docker、已连接的私网 Skill/Appium 和测试手机后：
export PHONE_SKILL_URL=http://127.0.0.1:8790
export PHONE_SKILL_DEVICE_ID=cloud-1
# PHONE_SKILL_TOKEN 已配置，禁止上传到日志或仓库。
python scripts/release_gate.py --release
```

GitHub Actions 验证 Python 3.11/3.12/3.13、回归测试、70% 覆盖率门槛、契约、Wheel 构建与隔离安装、Docker 构建/导入、Compose、Caddy。Core Gate 不会冒充真机认证。真实验收脚本默认仅打开系统设置，不会自动确认交易。

目前仍为单实例、进程内任务/幂等存储；跨进程重启不保证去重，不能直接扩成多副本。72 条种子 App 记录和 5 个旧配方作为兼容资源保留，不等于 Top 500 已认证；默认单步模式不会执行旧的整条配方。可选视觉/A2A/ONNX 模块不属于本次默认链路的真机验收。

200–500ms 仅是热路径动作触发目标，不是含公网、LLM、Appium 观测和手机渲染在内的端到端保证。每步都调用 LLM 通常比缓存整条计划增加网络调用，换取逐步纠偏；本版优先遵循单步正确性与停止保护。
