# Binggo MCP v2

本地 stdio 适配器通过鉴权的 Control API 操作现有 Binggo，复用应用保存的登录与 LLM 配置。标准工具 53 个（含 4 个可选开发工具），覆盖应用启停、账号、活动、主动发现、参与计划与核验、任务、调度、设置、诊断、隔离测试、构建与回滚。

## 安装与连接

应用使用 `requirements.txt`，适配器使用独立环境与 `mcp/requirements-lock.txt`。本机已准备好 `D:/A_Bonus/work/binggo-mcp-venv`。不要把应用 HTTP 页面地址当 MCP 服务地址。

用 `scripts/install_mcp_control.py` 生成本机 profile 与通用宿主配置。所有本机路径保留在 profile 中；不复制 Cookie、LLM key 或令牌。

```powershell
python scripts/install_mcp_control.py --home D:/A_Bonus/Binggo-RepostFix/user-data --runtime D:/A_Bonus/work/binggo-mcp-venv/Scripts/python.exe --app-python D:/A_Bonus/work/venv/Scripts/python.exe --work D:/A_Bonus/work --application D:/A_Bonus/Binggo-RepostFix/Binggo-RepostFix.exe
```

在有权写入宿主配置的普通 PowerShell 中连接 Codex：

```powershell
& D:/A_Bonus/Binggo-RepostFix-source/scripts/register_codex_mcp.ps1
```

随后重新加载 MCP 或重启宿主。生成配置、协议测试成功和当前聊天出现工具是三个独立状态。当前任务无权写入 `C:/Users/jingl/.codex/config.toml` 时，脚本应在普通 PowerShell 运行，不需要管理员权限。

## 使用

先调用 `binggo_instances_list`、`binggo_instance_get`，后续明确携带 `instance_id`。变更传入当前 `expected_boot_id` 和新的 `idempotency_key`；同键重试返回原结果。长任务立即返回控制 `job_id`，通过 `binggo_job_get/wait/events` 追踪；等待上限 20 秒。

参与流程为 plan → execute → verify。计划不代表已经参与；API 接受不代表平台核验通过。无须在已有用户授权内重复请求确认。收藏规则、账号、配置或应用身份改变后必须重新计划。未知的发帖结果不会自动重放；已成功转发/评论的本地回执可用于防止重试重复发帖，平台核验仍执行新读取。

任务与网页共享 JobRunner 和互斥。离线套件使用独立数据根并阻断外网；`network-read` 只读真实平台，`live-participation` 只执行既有计划。开发模式可用登记构建和包级验收后切换/回滚程序，用户数据保持独立。

## 主动检索（修复版 5）

`binggo_discovery_start` → `binggo_job_wait` → `binggo_discovery_get` → `binggo_discovery_import`。不需要手工提供活动 ID。标准模式默认检查最多 6 个视频，预算 60 次平台请求，找到 3 条经过核验的新动态后结束；综合单页模式仍为最多 36 个视频、96 次请求。已在本地活动库的目标不会再次计为新活动。

检索会沿视频简介、UP 主置顶评论、专栏和主页动态查找入口，保留来源链。动态详情没有正文时会回退 Opus；模型只能摘录来源原文。开奖日期与报名截止分开处理，已结束、付费或规则证据不足的候选不会直接导入。只在主页公布中奖结果的文字不会触发主页参与查找。

运行中的候选和请求/模型计数持续更新。预算停止会保留已经核验的候选，允许导入；持续工作流随后暂停并报告原因。导入与平台参与分开，检索不会自动点赞、关注、转发、评论或完成晒图等附加条件。

实机验收命令（导入本地活动库，不参与活动）：

```powershell
D:/A_Bonus/work/binggo-mcp-venv/Scripts/python.exe scripts/check_discovery_control.py --profile D:/A_Bonus/work/binggo-control-profile.json --keyword 转发抽奖 --rounds 2 --output D:/A_Bonus/work/discovery-live.json
```

## 验证

```powershell
D:/A_Bonus/work/binggo-mcp-venv/Scripts/python.exe scripts/check_mcp_control.py --profile D:/A_Bonus/work/binggo-control-profile.json --run-tests
```

`--mode legacy` 验证旧协议兼容；`--lifecycle` 会重启登记的应用，隔离测试时使用专门 profile。测试报告分别记录离线、真实读取、真实写入，不能互相替代。

工具 schema：[control-v2.tools.json](contracts/control-v2.tools.json)。设计背景：[完整设计](../docs/plans/11-mcp-control-automation.md)。旧接口：[README-v1.md](README-v1.md)。适配器默认不公开旧工具，profile 设置 `expose_legacy=true` 才启用 34 个兼容名称。
