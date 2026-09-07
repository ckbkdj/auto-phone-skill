# auto-phone-skill · 0.3.1

**下载 ZIP → 安装 Skill → 龙虾/MCP 按需启动 Python。** 不需要 Docker、git pull、公网域名、独立 API 服务或 Python 第三方依赖。要求 Python 3.11+；仍通过 Appium 控制宿主分配的 Android 云手机。

## 本版针对启动日志的修复

配置放错位置、依赖失败仍占设备、JRE 被误当 JDK、ARM/x64 安装包混淆、首次初始化时反复打印/读取源码，均增加了代码检查或专用入口。不是只提高模型重试次数。**日志没有显示抖音操作成功，宿主的 auto-compaction 报错也不能当作 Appium 回执。**

- `doctor` 返回全部缺失项、CPU 架构、Node/npm 版本、JDK/SDK 状态和实际配置来源。诊断成功不等于设备就绪。
- `setup` 接收宿主分配的设备，原子写入正确的私有配置。显式错误配置路径拒绝，不偷偷换另一个文件。旧 Skill 根目录 config.json 在没有私有配置时兼容读取。
- `initialization_failed` 释放设备占用；同一幂等键可在修复环境后重试。旧版无观察、无回执、无操作意图的初始化残留可安全回收；未知动作绝不清除。
- 显式 `install_jdk:true` 按实际平台选择 Temurin **JDK 17**，验证官方元数据、下载 SHA-256 和运行架构，保存到用户目录。不是硬编码 x64/JRE，不用 sudo。
- 启动私有 Appium 前验证 Node/npm、JDK、真实 SDK 根目录。可从已安装 SDK 的原生 adb 路径识别 SDK；不会创建一个空目录冒充 SDK。
- `prepare` 不创建任务；`setup_status` 只读取最后阶段；`tasks` 与 `status` 不打印整页 UI。SKILL 明确禁止反复 grep/读源码、搜全盘或绕过宿主执行策略。

## 安装或升级

从 GitHub **Code → Download ZIP** 下载，解压后进入实际含 `install.py` 的目录：

```bash
python3 install.py --upgrade
```

Windows 使用 `py -3 install.py --upgrade`。没有旧版时也可运行同一命令。默认规范安装到 `~/.openclaw/skills/auto-phone-skill/`；指定宿主目录用 `--skills-dir /实际/skills目录`。

`--upgrade` 先把现有规范目录备份到 `~/.auto-phone-skill/skill-backups/`，再换新。已识别的旧 `auto-phone-skill-main` 也移到扫描目录外备份；旧本地配置仅在没有私有配置时迁移，绝不覆盖现有配置或删除任务数据库。新会话重新加载 Skill，不能继续使用旧会话缓存的双重目录路径。

**整个目录一起安装，不能只复制 SKILL.md。** 默认仍不需要 pip 或单独启动 Python 服务。

## 首次接入：一个配置入口

在宿主已经分配好手机、并允许用户级依赖安装的前提下，通过 stdin/宿主参数数组调用：

```bash
python3 scripts/phone_agent.py setup --json '{"device_id":"cloud-1","udid":"宿主分配的真实UDID","install_jdk":true}'
```

示例 UDID 必须替换，程序不会选手机。已有云机 Appium 时在同一对象提供 `appium_url`；此时不需要 Skill 主机具备本地 Android 工具链。可提供 `java_home`、`android_home` 指向已有路径；`install_jdk` 不传时不下载 JDK。Appium 安装仍受本地 `auto_install_appium` 控制。

以后配置已存在时：

```bash
python3 scripts/phone_agent.py doctor --json '{}'
python3 scripts/phone_agent.py prepare --json '{"device_id":"cloud-1"}'
```

标准配置路径为 `~/.auto-phone-skill/config.json`。环境变量 `AUTO_PHONE_UDID` / `AUTO_PHONE_DEVICE_ID` 仍可由宿主注入；显式 `--config` 或 `AUTO_PHONE_CONFIG` 优先。密钥和设备连接信息不要放进指令或公开日志。

**真实环境限制：**JDK 安装不是 Android SDK 安装。只有 adb 和一个 JRE 的精简容器，可能仍缺完整 SDK 或 Node 版本不匹配。新版一次指出这些缺项，不会在 ARM 上盲下 x64 SDK、不接受未知许可、不提升权限，也不把启动失败记成活跃手机任务。没有合适本地工具链时可使用宿主已有 Appium；这不是要求部署新的公网平台。

## 单步语义控制与 MCP

```text
龙虾当前模型 / MCP 宿主
  → 当前页面 → 一个严格 decision → 新页面 → 再决定下一步
  → 本地 Skill Python → 已有/按需启动的 Appium → 已分配云手机
```

默认复用宿主模型，无第二份 LLM Key。可选 `run` 每次只发送 system + 当前页面/最多三条回执，输出预算默认700 tokens。MCP 宿主启动 `python3 /绝对路径/scripts/phone_agent.py mcp`，参考 `mcp.example.json`；无监听端口。增加 prepare/tasks/setup_status 工具，setup 和安装器仍只给受信任宿主/管理员使用。

动作只接受当前观察中的 ref，不接受坐标、任意代码、Shell 或 XPath。输入使用单次文本替换。相同操作 ID 对应相同参数，结果未知停止，确认与密码/验证码接管规则不变。200–500ms 是热链路目标，不包含首次安装/LLM/页面加载，也没有真机达标声明。

## 测试和诊断

```bash
python3 -m unittest discover -s tests -v
python3 scripts/package_zip.py
python3 scripts/phone_agent.py setup_status --json '{}'
python3 scripts/phone_agent.py tasks --json '{}'
```

反馈排错时提供新版 doctor、setup_status、任务状态与错误码即可；不必先把所有源码读进会话。原始屏幕、配置、数据库和 Appium 原始日志可能包含隐私，不自动上传。`scripts/live_smoke.py` 只在真实接入后打开系统设置检查，不自动下单或叫车。

见 [使用文档](docs/USAGE.md) 与 [验证边界](VALIDATION.md)。宿主的自动压缩失败只能通过宿主本身的诊断确认；本版仅减少诱发无谓上下文膨胀的排错流程，不会擅改宿主配置。73 条 App 别名不等于 Top500 认证。
