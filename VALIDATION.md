# 0.3.0 验证记录

日期：2026-09-06。状态：已完成本地代码/协议/安装包测试，待目标云手机验收。

## 已实际执行

执行环境 Linux、Python 3.13.5。`python -m unittest discover -s tests -v`：**55 项测试通过**。失败数 0、错误数 0。测试包含多个参数子用例，55 是 unittest 报告的测试方法数，不是伪造的 App 覆盖数。

- 严格 JSON 与动作 Schema：重复键、未知字段、非法类型、超限结构、多动作列表和模型输出拒绝。
- 本轮 observation_id / 页面指纹绑定、唯一元素匹配、同页面同动作阻止、确认令牌与人工接管。
- SQLite 操作日志持久化、同 ID 不同参数冲突、进程退出/回包丢失后不重放、设备独占。
- 真实本地 HTTP 连接到 **Appium 兼容测试替身**，验证 W3C 请求、中文文本单次替换和 scrollGesture false 的正确含义。这不是真实 Appium/Android 测试。
- 真实 CLI 子进程、跨 CLI 进程复用已记录 session、真实 MCP stdio 子进程初始化/工具调用。
- 复制整个 Skill 安装目录，并用 `python -I -S` 运行，证明入口不依赖 pip、项目工作目录或 site-packages。
- 自动启动 Appium 的参数、安全绑定和缺少依赖错误分支通过模拟子进程测试；未实际联网下载 Appium。

另外执行 Python 编译检查、生成 ZIP、ZIP 完整性检查、解压目录的独立入口检查和同一套测试。

## 尚未执行，不作完成声明

没有连接用户的 Android 云手机或真实账号。未完成真实 Appium/UiAutomator2、npm 安装下载、Android SDK/设备授权联调、实际 OpenClaw 宿主导入、Windows/macOS、远程 LLM、各 App 线上灰度页面测试。没有 Top 500 真实覆盖认证，没有 200–500ms 真机时延保证。

MCP 实现为文档列明的 stdio lifecycle + tools 子集，经过本项目协议及子进程回归，不是所有 MCP SDK 或协议扩展的认证。GitHub CI 为 Python 3.11/3.12/3.13 配置；本记录不等于远端三环境已经全部运行完成。

## 可复现

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q auto_phone scripts tests
python3 -I -S scripts/phone_agent.py doctor --json '{}'
python3 scripts/package_zip.py
```

设备映射已配置且有可访问真实 Appium 后，执行 `python3 scripts/live_smoke.py`。该检查只打开系统设置并验证包名，不提交叫车、订单、付款、消息或授权，不自动越过确认。

## 发布边界

本次把新架构源码与安装 ZIP 交付到仓库，替代当前分支的 Docker 方案；旧版本仍在 Git 历史。没有因为本地替身测试通过就创建“真机全通过”的 Release 证明。使用者应先用测试手机验收再用于实际业务。
