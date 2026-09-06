# 使用与接口

## 运行方式

所有运行时 Python 文件都在 Skill 目录，依赖标准库。宿主每次调用 CLI 即启动进程；SQLite 保存任务和 Appium session ID，下一次调用会验证并复用。MCP 模式由宿主启动同一入口的 `mcp` 子命令。没有 Python HTTP listener，也不需要 systemd、Docker 或公网域名。

本地 Appium 是设备驱动服务，不是额外部署的平台：已运行则复用；未运行则由 Skill 在第一次需要手机时启动，驱动安装到用户私有 `APPIUM_HOME`。没有本机服务时，可能需要 npm 联网获取驱动；不覆盖全局驱动。模型/设备环境只在主机配置中提供。

`auto_install_appium:false` 禁止联网安装；`auto_start:false` 表示仅连接现有 Appium。首次依赖初始化是引导过程，不属于热路径动作延迟。宿主应允许引导命令完成，不能把短动作超时直接套到首次安装上。

## 命令

`doctor`、`begin`、`observe`、`step`、`status`、`resume`、`cancel` 的运行结果始终是一个符合 `OUTPUT` 的 JSON 对象；失败 `ok:false`，仅输出结构化错误码，不输出原始上游错误。CLI `--help`、安装器输出与 `contracts` 是管理接口，不是任务响应。

```bash
python3 scripts/phone_agent.py doctor --json '{}'
python3 scripts/phone_agent.py begin --json '{"goal":"打开系统设置","device_id":"cloud-1","idempotency_key":"my-conversation:turn-1","success":[{"kind":"package_is","value":"com.android.settings"}]}'
```

拿到 task_id 和 observation.id 后，只发一个动作：

```json
{
  "task_id": "实际任务ID",
  "observation_id": "实际本轮观察ID",
  "operation_id": "turn-1-action-1",
  "decision": {
    "action": "launch_app",
    "package": "com.android.settings",
    "expect": [{"kind":"package_is","value":"com.android.settings"}]
  }
}
```

将上面的 JSON 通过 stdin 或 `--json` 交给 `step`。随后读取新 observation，重新决定下一动作。CLI 示例中的占位 ID 不可当作真实 ID 使用。

动作白名单：`launch_app(package)`、`tap(target)`、`type(target,text)`、`clear(target)`、`scroll(target,direction)`、`back`、`home`、`wait(milliseconds)`、`finish(expect)`、`handoff(message)`。

`target` 只引用本轮返回的 `n0` 等引用，不能指定坐标或任意 selector。`type` 使用单次替换 API，不做额外点击、不自动回退多个输入方案。禁止换行、控制键以及 Appium 会解释为 Enter 的结尾字面 `\\n`，避免输入动作夹带提交。

`expect` 支持 `text_present`、`text_absent`、`package_is`、`screen_changed`。`finish` 必须提供正向的可见成功证据，并同时满足 begin 时的 success。对裁剪过的页面不接受“没看到某文字”等于文字不存在的推断。UI 证据不能保证业务语义绝对正确，重要任务应配置更具体的目标条件。

## 确认与未知结果

`waiting_confirmation` 返回短期 token、真实可见目标说明。用户明确批准后，重发完全相同的 step 请求，增加 confirmation_token；operation_id、decision、observation_id 都保持不变。重新 observe、页面变化、参数变化、过期都使旧批准失效。

`waiting_handoff` 或 `outcome_unknown` 只接受用户完成检查之后的 `resume {task_id,token}`。恢复只重新观察，不执行旧动作。远端命令超时不代表它已取消；必须先确认手机端动作已结束。`cancel` 不会声称撤销未知的远端动作。

本地日志在磁盘中记录 dispatch 前后状态。若进程在动作执行期间被宿主杀死，下次调用会检测 `executing` 并进入未知结果状态，不自动重放。单机文件锁避免两个进程同时控制同一已登记手机；SQLite 使成功/未知回执跨进程保留。它不是分布式多用户服务，也不保证远端 exactly-once。

## 常见错误码

| 错误码 | 处理 |
|---|---|
| UNKNOWN_DEVICE / DEVICE_TARGET_REQUIRED | 由龙虾提供实际设备映射或 AUTO_PHONE_UDID；不要猜测。 |
| NODE_NPM_REQUIRED | 本机自动启动路径缺少 Node/npm；或者连接已有云机 Appium。 |
| ANDROID_SDK_JAVA_REQUIRED | 本机设备驱动需要 Java、Android SDK/ADB。 |
| APPIUM_INSTALL_REQUIRED / UIAUTOMATOR2_INSTALL_REQUIRED | 自动安装被本地配置禁止；准备受信任的 Appium/驱动。 |
| APPIUM_INSTALL_FAILED / UIAUTOMATOR2_INSTALL_FAILED | 检查用户目录写入权限、npm 出站网络与工具链版本。 |
| APPIUM_NOT_READY | 已配置服务不可用，或远端服务未启动；begin 失败仍返回可恢复任务 ID。 |
| DEVICE_HAS_ACTIVE_TASK / DEVICE_BUSY | 等待或处理同一设备现有任务，不并发抢占。 |
| STALE_OBSERVATION | 调用 observe 并基于新页面再做一个决策。 |
| SCREEN_CHANGED_REPLAN | 旧动作未下发，按返回的新 observation 决策。 |
| AMBIGUOUS_OR_MISSING_TARGET | Android 当前元素不唯一/不存在；不猜坐标。 |
| OUTCOME_UNKNOWN | 人工检查远端动作结果，不自动重试。 |
| REPEATED_ACTION_BLOCKED | 同页面同动作被阻止，人工检查而不是换 operation_id 绕过。 |
| INVALID_LLM_OUTPUT | 独立模型没返回严格单动作 JSON；没有执行该输出。 |

## 隐私与限制

设备 URL、UDID、模型密钥不进入公共调用参数。模型 API key 只从配置指定的环境变量读取。状态文件包含任务目标、非密码界面文字和动作相关信息，必须保持私有；不要提交到 GitHub。密码标记和可识别敏感字段被隐藏，但这不是通用个人信息脱敏系统。

没有可靠 Android 可访问元素的 Canvas、自绘、某些 WebView 或验证码页面不会以猜测坐标补齐。没有旧版固定多步配方自动运行。App 别名只是辅助识别，不是成功率认证。真机检查脚本 `scripts/live_smoke.py` 仅打开系统设置，不自动叫车、消费或发消息。

## 官方协议参考

- OpenClaw Skills: https://docs.openclaw.ai/tools/skills
- MCP stdio 2025-11-25: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- MCP tools: https://modelcontextprotocol.io/specification/2025-11-25/server/tools
- Appium requirements: https://appium.io/docs/en/latest/quickstart/requirements/
- UiAutomator2: https://github.com/appium/appium-uiautomator2-driver

MCP 实现限定为文档所列协议的工具子集，不宣称支持所有后续版本、Streamable HTTP、sampling 或所有通知扩展。
