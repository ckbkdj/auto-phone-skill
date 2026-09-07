# 0.3.1 验证记录

2026-09-07。基于主线 f9b7c501 的纯 Python ZIP Skill/MCP 版本修复，未恢复 Docker 架构。

## 已执行的本地验证

Python 3.13：85 个 unittest 测试方法通过，无失败/错误/跳过。旧59项保留，并按有意更改的错误语义和新增工具更新断言；新增26项初始化、配置、JDK与升级回归，不把参数子用例伪装成 App覆盖数。

测试范围：双层安装路径规避/规范目录升级；私有与Skill本地配置优先级；显式错误路径拒绝；配置原子写入/备份；JRE与JDK区分、SDK根目录；环境路径跨启动保留；首次失败释放任务；同键重试；旧无动作残留回收；未知动作不清除；setup无任务；策略拒绝不重试；ARM资产选择；JDK长度/SHA-256与安全解压；重复安装复用；现有HTTP、CLI、MCP、单步动作和SQLite回执测试。

测试中的JDK下载使用可校验的合成压缩包，不是实际下载或实际CPU执行认证。Appium HTTP测试仍是协议替身，不是真手机。CI配置增加Linux ARM64与x64各Python3.11/3.12/3.13，实际执行状态以对应提交的GitHub Actions为准。

## 可复现

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q auto_phone scripts tests
python3 -I -S scripts/phone_agent.py doctor --json '{}'
python3 scripts/package_zip.py
```

ZIP重新解压后的独立入口与全量测试也应通过，打包按白名单排除用户配置、任务DB、日志及其他临时文件。doctor诊断成功不表示当前环境已准备好手机；请检查issues或使用prepare。

## 未验证/不能据此声明

没有连接用户真实ARM云手机、Java/SDK、Appium服务或抖音账户。没有实际完成三个视频、Top500、真机时延认证；宿主auto-compaction错误没有被这个仓库修复。本次修改使故障正确分层、初始化可恢复和诊断简短，不捏造业务执行成功。

不创建声称真机已通过的正式Release/tag。发布源代码与可安装ZIP供目标环境验收；旧任务数据库和不确定动作必须保留。
