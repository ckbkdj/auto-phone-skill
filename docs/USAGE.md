# 0.3.1 使用与故障恢复

## 配置优先级

`--config 文件` → `AUTO_PHONE_CONFIG` → `~/.auto-phone-skill/config.json` → Skill 根目录 config.json（兼容旧误放路径）。显式文件不存在报 CONFIG_NOT_FOUND，不隐式切到别的设备。doctor 告知 config_source/config_path/local_config_ignored。不会读取当前 shell 工作目录任意同名文件。

只读 doctor 的 ok:true 表示诊断执行成功，local_prerequisites_ready 才是本地依赖检查；已有远端 Appium 不需要本机这些工具。prepare 的 appium_ready 只表示 Appium 服务可达，还不证明指定手机已连通。begin 获取成功观察后才说明手机会话已建立。

## 受信任宿主 setup

setup 的 JSON 必需 device_id，首次登记需 udid；可选 appium_url/system_port/java_home/android_home/install_jdk/replace_device。字段、类型和端点严格验证，拒绝额外字段和凭据 URL。覆盖已有设备路由需 replace_device:true，日常模型不能擅自选择/切换手机。该命令不在 MCP 工具列表。

```json
{"device_id":"cloud-1","udid":"真实已分配UDID","install_jdk":true}
```

执行前要有宿主的执行/网络许可。curl 等工具被策略拒绝不是改用另一工具下载的理由；不要绕过策略。安装 JDK 的授权也不等于修改系统、安装 SDK、同意额外许可或更改宿主策略。

JDK 安装只使用 Adoptium 官方资产元数据与官方 Temurin17 下载链接。选择 Linux/macOS/Windows 和 x64/aarch64 的 JDK，不是 JRE。压缩包先校验长度与 SHA-256，再安全解压，拒绝路径逃逸；验证 javac/Java 的运行架构后落盘。暂不自动安装 musl/不支持架构的发行包，也不自动下载 Android SDK 或 Node。显式 java_home/环境 JAVA_HOME 指向 JRE 或错误架构会报 JDK_REQUIRED。

配置持久保存 Java/SDK 路径并传给 Appium 子进程，不要求每轮临时 export PATH。完整 SDK 中 platform-tools/adb 的真实安装路径可用于推导 ANDROID_HOME；孤立的 /usr/bin/adb 不被直接当成完整 SDK。

## 有界初始化与状态

prepare、setup 不创建手机任务。setup-status.json 只记录最后阶段与错误码，是快照，不保证安装进程现在仍在运行。重复执行 prepare 先检查现有 Appium；不会自动停止其他服务。首次安装 npm/JDK 仍可能耗时，宿主应按实际进程状态处理，不能反复创建 begin 代替等待。

初始化阶段出现异常，新任务持久化为 initialization_failed，而不是 active。没有动作意图、观察或回执的旧初始化残留，会在取得设备锁后的 begin 中释放。初始化已失败且从未下发动作的同一幂等请求可以重试；已有观察/动作的任务不会自动释放。未知结果只能由用户核对后 resume，不能通过删除数据库或换请求 ID绕过。

```bash
python3 scripts/phone_agent.py tasks --json '{}'
python3 scripts/phone_agent.py status --json '{"task_id":"实际任务ID"}'
python3 scripts/phone_agent.py setup_status --json '{}'
```

tasks 最多16条，只包含任务ID、设备逻辑ID、状态、动作数。status 不输出旧页面；observe 明确获取新页面，会使旧 observation_id 和待确认动作失效。禁止把这两个接口当作静默重放动作。

## 操作闭环

begin(goal, device_id, idempotency_key, success?) → 观察 → step(task_id, observation_id, operation_id, decision) → 新观察 → 新决策。白名单动作是 launch_app、tap、type、clear、scroll、back、home、wait、finish、handoff。type 为完整替换，不接受换行、Tab或特殊控制键。目标引用来自当前观察；没有唯一 Android 元素时停止，不猜坐标。

finish 必须有正向当前页面证据，并同时满足 begin 的 success。只进入抖音的包名不能证明“浏览三个视频”；宿主需要跟踪实际不同内容及完成数量，无法辨识则交接，不用计时器变化假装换了视频。

waiting_confirmation 只允许用户明确批准后，以同一 operation_id/decision/observation_id 提交 token；页面变化或新 observe 会作废旧批准。waiting_handoff/outcome_unknown 恢复只观察不重放，需先确认旧远端命令确已结束。SQLite 保留操作意图并不等于远端恰好执行一次。

## 安装升级与宿主压缩

`python3 install.py --upgrade` 规范目录名并把已识别旧目录备份到私有状态目录；不覆盖旧配置、不删除任务。重新加载宿主技能，避免缓存的 `{baseDir}/auto-phone-skill-main/...` 重复拼接。若宿主还读旧路径，应先修正宿主加载状态，而不是改手机代码。

auto-compaction 是宿主能力。此包不能证明提高 reserveTokensFloor 能解决当前宿主版本的问题；不会自动写 openclaw.json。出现会话压缩失败，先停止新动作，再在新会话用 tasks/status 恢复已保存任务状态。首次排错限制在一次 doctor + 一次 setup/prepare + 简短错误/阶段，不循环读源码/全盘查找/搜记忆。

## 证据边界和官方参考

新测试覆盖配置优先级、初始化失败与安全回收、ARM/JDK元数据、校验与解压、故障不绕过策略、安装升级、协议和子进程。下载通过测试替身验证；没有在用户ARM云机上完成实际JDK/SDK/Appium安装与抖音业务验收。

- Appium要求：https://appium.io/docs/en/latest/quickstart/requirements/
- UiAutomator2要求：https://appium.io/docs/en/latest/quickstart/uiauto2-driver/
- JDK官方API用法：https://github.com/adoptium/api.adoptium.net/blob/main/docs/cookbook.adoc
- 宿主Skills：https://docs.openclaw.ai/tools/skills
- 宿主压缩：https://docs.openclaw.ai/concepts/compaction
