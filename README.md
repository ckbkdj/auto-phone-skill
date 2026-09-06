# auto-phone-skill · 0.3.0

**下载 ZIP → 安装 Skill → 由龙虾或 MCP 宿主启动。** 不再部署 Docker、Caddy、公网控制面、WSS 桥梁或独立 Python API 服务。仓库根目录就是完整、可安装的 Skill。

这是 Python 3.11+ 的本地运行时，**Python 第三方依赖为零**。直接使用 Appium/W3C 协议调用 Android 当前页面的真实元素，不需要安装 Appium Python Client、FastAPI 或 MCP SDK。

## 安装

从 GitHub 的 **Code → Download ZIP** 下载，解压后：

```bash
python3 scripts/phone_agent.py install
```

Windows 可双击 `install.cmd`，或使用：

```powershell
py -3 scripts\phone_agent.py install
```

默认复制到 `~/.openclaw/skills/auto-phone-skill/`，让共享此目录的本机 Agent 使用。支持 `install --skills-dir /你的/skills目录`。安装器不会覆盖已有同名目录；更新时先保留旧目录再安装。配置和任务状态在独立的 `~/.auto-phone-skill/`，更新 ZIP 不会覆盖它们。

也可以直接把解压目录中的 `SKILL.md`、`auto_phone/`、`scripts/` 和其他配套文件整体放进 Skill 目录。**不能只复制 SKILL.md。** 重新加载宿主 Skill 列表或开始新会话。

## 第一次接入手机

设备选择仍交给龙虾。通过宿主环境注入分配好的真实 UDID：

```bash
export AUTO_PHONE_DEVICE_ID=cloud-1
export AUTO_PHONE_UDID=emulator-5554
# 已有云手机 Appium 时填写；否则默认本机 4723，由 Skill 按需启动。
export AUTO_PHONE_APPIUM_URL=http://127.0.0.1:4723
```

Windows 使用 `$env:AUTO_PHONE_UDID = 'emulator-5554'` 等同名环境变量。示例 UDID 不是自动发现结果；实际值由龙虾提供，不做猜测或替换。

需要持久化或多设备配置时，将 `config.example.json` 复制为 `~/.auto-phone-skill/config.json`，修改设备映射。配置路径也可通过 `AUTO_PHONE_CONFIG` 指定。

**已有可访问 Appium：Skill 侧仅需要 Python。** 没有本地 Appium 时，Skill 可使用已有 Node/npm，在用户目录安装 Appium 3 和 UiAutomator2 并自动启动；这一路径要求宿主已有 Java、Android SDK/ADB 和设备连接。ZIP 不包含 Python、Node、Java、Android SDK、模型权重或云手机账号。缺少这些基础环境会返回明确错误码，不会假装已连接。

## 两种推理方式，同一个执行核心

**默认复用龙虾模型，不需要另填模型 Key：** `begin → 当前页面 → 一个 decision → step → 新页面 → 再决定一个 action`。保持用户目标，最多带最近三个回执，不生成完整未来操作列表。

**可选独立子 Agent：** 配置 `AUTO_PHONE_LLM_BASE_URL`、`AUTO_PHONE_LLM_MODEL`、`AUTO_PHONE_LLM_API_KEY`，调用 `run`。每次模型请求只有独立的 system + 当前屏幕消息；不把先前推理全文追加到下一轮。默认输出预算 700 tokens，达到动作预算、需要用户确认或结果未知时立即停止。

```text
龙虾/支持 MCP 的宿主
    │ 自己启动 Python 子进程
    ▼
Skill CLI 或 MCP stdio
    │ 读取真实 Android 页面 → 严格单动作 Schema → 确认/幂等/观察检查
    ▼
已有 Appium 或 Skill 自动启动的本机 Appium
    ▼
指定 Android 云手机
```

## MCP 模式

把 `mcp.example.json` 中脚本路径改为安装目录绝对路径。MCP 宿主会自己启动和管理 Python 进程；**不要先手动启动服务**。

```json
{
  "mcpServers": {
    "auto-phone-skill": {
      "command": "python3",
      "args": ["/绝对路径/auto-phone-skill/scripts/phone_agent.py", "mcp"]
    }
  }
}
```

Windows 的 `command` 可用 Python 解释器绝对路径，或 `py` 并把 `-3` 放在 args 首项。实现的是 MCP stdio 工具子集，支持协议 `2025-11-25`、`2025-06-18`、`2024-11-05`；不宣称覆盖所有新协议扩展。提供 `phone_doctor`、`phone_begin`、`phone_observe`、`phone_step`、`phone_status`、`phone_resume`、`phone_cancel`。

## 精准性和安全边界

模型只能引用本轮观察中的 `target`，不能输出坐标、任意元素定位代码、Shell、ADB 脚本或多动作列表。运行时重新读取页面，再用实时 Android 元素定位和 element click / element text replacement / element scroll 执行。找不到唯一元素就停，不退回猜坐标。

每个动作执行前写入 SQLite 操作日志；相同 operation_id 不同参数拒绝，相同操作重放只返回既有回执。进程崩溃、超时、回包丢失或无法验证输入时标记 `outcome_unknown`，阻止进一步动作直到用户检查。**不宣称远端 exactly-once**；日志不会强行取消已经到达手机的命令。

实际可见按钮文字用于风险确认，模型不能自报“低风险”绕过。叫车提交、支付、下单、发送、删除、授权等保留确认门；也可在本地配置 `confirm_all:true` 对每次变更都确认。人工确认依赖宿主真实用户授权，不能防止一个拥有同一操作系统账户权限的恶意宿主伪造用户意图。

200–500ms 是热链路下动作下发的目标，不是包括 LLM、页面加载、Appium 安装和网络在内的保证。73 条 App 别名不是 500 个 App 真机认证。无法暴露可靠元素的页面将接管，而不是用固定点击坐标补齐。

## 验证

```bash
python3 scripts/phone_agent.py doctor --json '{}'
python3 -m unittest discover -s tests -v
python3 scripts/package_zip.py
```

真实 Appium 的无破坏接入检查：

```bash
python3 scripts/live_smoke.py
```

只打开系统设置并检查包名，不自动批准外部副作用。测试事实与未测边界见 [VALIDATION.md](VALIDATION.md)，请求示例与错误码见 [docs/USAGE.md](docs/USAGE.md)。旧 Docker 架构保留在 Git 历史，不再作为当前安装入口。
