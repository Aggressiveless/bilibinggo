# MCP v2 设计契约

[control-v2.tools.json](control-v2.tools.json) 是**待实施的工具设计清单**，不是运行中的 MCP `tools/list` 响应。完整设计与验收方案见 [应用控制、自动化与测试设计](../../docs/plans/11-mcp-control-automation.md)。

- 53 个标准工具，其中 4 个属于可选开发模式；现有 34 个工具通过 `x-legacyAliases` 对应迁移目标。
- 每个工具包含独立可校验的 JSON Schema 2020-12 `inputSchema`、`outputSchema`；输出 schema 内嵌它实际引用的 `$defs`，可单独交给校验器使用。
- `x-backend` 区分现有后端与待新增接口。即使有现有后端，也不表示新 MCP 工具已实现；仍需标准化响应、实例绑定和权限校验。
- `x-authorizationScopes` 是服务端必须验证的权限要求；MCP annotations 只是工具提示，不授予权限。工作流和测试模式还需应用运行时权限交集检查。
- `x-execution` 区分快速提交 Job 与有界请求。所有 `job_id` 都经过统一控制注册表映射，不能混用应用、supervisor 和测试执行器的原生数字 ID。
- 只在所有声明的跨字段和业务约束通过后调用后端；JSON Schema 不能替代规则、权限、账号、网络预算和副作用隔离检查。

本轮已通过全部 106 份 schema 校验、34 个旧工具映射、11 个已有后端绑定，以及文档/清单一致性检查。没有安装或注册新增工具。
