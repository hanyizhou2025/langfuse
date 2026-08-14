# AgentDebugX File Not Found 生产部署

本文描述如何只复制并安装 AgentDebugX 目录，在不依赖 Langfuse Web、Worker、
Prisma 或 TypeScript 源码的情况下，直接读取 ClickHouse 中的 `traces`、
`observations` 和 `tool_error` 表完成 File Not Found 失败归因。

## 1. 可迁移边界

运行时只需要：

- Python 3.9–3.13；
- AgentDebugX 目录及其 Python 依赖；
- 对三张表的只读 ClickHouse HTTP 访问；
- 一个明确的 `project_id`；
- 可选的人工 `annotations.jsonl`；
- 可选的 OpenAI-compatible 模型 API，仅允许用于已经脱敏的数据源。

代码不会导入或读取 AgentDebugX 目录之外的 Langfuse 文件。ClickHouse 连接
使用 AgentDebugX 的 `httpx` 核心依赖，不需要安装 Langfuse、Node.js、pnpm、
Prisma Client 或 ClickHouse Python SDK。

## 2. 安装

```bash
cd /path/to/AgentDebugX-main
python3 -m venv .venv
.venv/bin/pip install .
```

安装后验证入口：

```bash
.venv/bin/agentdebug langfuse-fnf --help
```

也可以不使用 console script：

```bash
PYTHONPATH=src .venv/bin/python -m \
  agentdebug.integrations.langfuse_attribution.production --help
```

## 3. 数据库连接

连接信息只从环境变量读取，不写入 JSON 配置、数据集、预测结果或日志。

```bash
export AGENTDEBUG_CLICKHOUSE_URL="https://clickhouse.example.internal:8123"
export AGENTDEBUG_CLICKHOUSE_USER="agentdebug_readonly"
export AGENTDEBUG_CLICKHOUSE_PASSWORD="..."
export AGENTDEBUG_CLICKHOUSE_DATABASE="default"
export AGENTDEBUG_PROJECT_ID="your-project-id"
```

可选变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENTDEBUG_CLICKHOUSE_TIMEOUT_SECONDS` | `60` | 单次查询超时 |
| `AGENTDEBUG_CLICKHOUSE_VERIFY_TLS` | `true` | 是否验证 TLS；生产环境不应关闭 |
| `AGENTDEBUG_CLICKHOUSE_CA_FILE` | 空 | 企业内部 CA 文件；设置后优先于布尔开关 |

URL 禁止携带用户名、密码、query 或 fragment，HTTP 客户端不跟随重定向。
所有查询强制携带 ClickHouse `readonly=2`，代码没有 INSERT、ALTER、DELETE 或
任意 SQL 输入入口。数据库账号仍应只授予三张表或视图的 `SELECT` 权限。

## 4. 表契约

### traces

必须存在：`id`、`project_id`。生产 Langfuse 通常还具有 `timestamp`、
`name`、`input`、`output`、`metadata`、`event_ts` 和 `is_deleted`。

### observations

必须存在：`id`、`trace_id`、`project_id`、`type`、`start_time`。可选字段包括
`parent_observation_id`、`end_time`、`name`、`input`、`output`、`metadata`、
`level`、`status_message`、`event_ts` 和 `is_deleted`。

### tool_error

必须能够定位错误 Observation。以下字段会自动识别：

| 规范字段 | 自动识别别名 |
| --- | --- |
| `failure_observation_id` | `observation_id`、`tool_result_observation_id`、`matched_observation_id`、`observationId`、`id` |
| `trace_id` | `trace_id`、`traceId` |
| `project_id` | `project_id`、`projectId` |
| `case_id` | `case_id`、`candidate_id`、`caseId`、`candidateId` |
| `rule_id` | `rule_id`、`ruleId`、`error_type`、`errorType` |
| `matched_field` | `matched_field`、`matchedField` |
| `matched_text` | `matched_text_redacted`、`matched_text`、`error_message_redacted`、`error_message` |
| `timestamp` | `timestamp`、`created_at`、`createdAt`、`start_time` |

如果实际字段名不同，使用
[`docs/examples/langfuse-fnf-source.json`](./examples/langfuse-fnf-source.json)
显式映射。SQL 表名和列名只允许普通标识符，不能传入 SQL 表达式。

`tool_error` 默认必须有 `project_id`，候选查询会先按项目过滤。只有当它是
物理隔离的单项目表或视图时，才可以显式设置
`allow_unscoped_tool_errors=true`。`traces` 和 `observations` 查询始终按
`project_id` 过滤，不能关闭。

## 5. 上线前 Doctor

```bash
agentdebug langfuse-fnf doctor \
  --config /path/to/langfuse-fnf-source.json
```

Doctor 执行三次 `DESCRIBE TABLE`，输出：

- 三张表是否可访问；
- 自动识别或显式配置的 `tool_error` 字段映射；
- 表中可用字段；
- 候选表是否具备项目隔离；
- 当前 I/O 契约是 `raw` 还是 `redacted`。

输出不包含 URL、用户名或密码。Doctor 不会读取实际 Trace 内容。

## 6. 生成标注数据集

从最新的 50 个候选冻结标准四文件数据集：

```bash
agentdebug langfuse-fnf snapshot /data/fnf-v1 \
  --config /path/to/langfuse-fnf-source.json \
  --limit 50 \
  --rule-id file-not-found-v1
```

输出：

```text
/data/fnf-v1/
├── manifest.json
├── traces.jsonl
├── observations.jsonl
└── cases.jsonl
```

人工完成标注后增加 `annotations.jsonl`。如果执行 snapshot 时传入
`--annotations /path/to/annotations.jsonl`，它会复制到冻结目录；未传入时会
删除输出目录中可能存在的旧 Annotation，防止标签版本串用。

`payload_contract=raw` 时，snapshot 包含生产原始 I/O，manifest 会明确写入
`contains_raw_payloads=true`。这类数据集必须保存在内部受控目录，不能发送给
公共模型。只想直接运行归因而不落原始快照时，应使用下一节的 `run`。

## 7. 直接运行归因并评测人工标签

```bash
agentdebug langfuse-fnf run /data/fnf-results \
  --config /path/to/langfuse-fnf-source.json \
  --limit 50 \
  --rule-id file-not-found-v1 \
  --annotations /data/fnf-v1/annotations.jsonl
```

输出：

```text
/data/fnf-results/
├── predictions.jsonl
├── trajectories.jsonl
└── report.json
```

`predictions.jsonl` 只包含 Observation ID、类别、原因码、置信度和计数；
`trajectories.jsonl` 使用 detector-safe 转换，不复制原始 input、output 或任意
metadata。没有 Annotation 时不会生成 `report.json`，并会移除旧报告。

人工标签同时兼容两种格式：

- 当前嵌套格式：`technical_error_review.human_label`、
  `semantic_outcome.is_agent_failure`、`attribution.*`；
- 已有扁平格式：`technical_error_review`、`is_agent_failure`、
  `root_cause_label`、`primary_root_cause_observation_id`。

Case ID 优先使用 `tool_error.case_id/candidate_id`。没有该字段时，系统使用
`project_id + trace_id + observation_id + rule_id` 生成稳定 ID，保证下一次抽取
仍能与人工 Annotation 对齐。

## 8. 公共 LLM 模式

原始 Langfuse 表不能直接启用公共模型。AgentDebugX 不会把 `input`、`output`
改个字段名就宣称已经脱敏，也不会在后台自动上传原始生产数据。

如需公共模型增强，先在工作环境提供经过组织隐私策略验证的脱敏 ClickHouse
视图，将配置中的 `traces`、`observations`、`tool_errors` 指向这些视图，并设置：

```json
{
  "payload_contract": "redacted",
  "matched_text_is_redacted": true
}
```

然后配置模型并运行：

```bash
export AGENTDEBUG_LLM_BASE_URL="https://api.example.com/v1"
export AGENTDEBUG_LLM_API_KEY="..."
export AGENTDEBUG_LLM_MODEL="your-model"

agentdebug langfuse-fnf run /data/fnf-results-llm \
  --config /path/to/redacted-source.json \
  --annotations /data/fnf-v1/annotations.jsonl \
  --llm
```

`payload_contract=raw` 与 `--llm` 同时出现时，程序会在查询业务数据和调用模型
之前终止。`redacted` 是运维方对数据源的显式契约，不是 AgentDebugX 的自动
脱敏声明。

## 9. 查询规模与 ClickHouse 注意事项

一次运行包含：一条受 `project_id`、可选 `rule_id` 和 `LIMIT` 约束的候选
查询；每个不同 Trace 各一条 Trace 查询和一条 Observation 查询；候选缺少
`trace_id` 时增加一次 Observation ID 查询。默认 50 条，适合作为当前人工
标注 MVP 的规模。

实现不做三张大表 JOIN，避免把大表装入 JOIN 哈希表；不使用 `FINAL`，而是按
`event_ts` 读取最新版本并过滤 `is_deleted`；Observation 查询同时使用
`project_id` 和 `trace_id`，可利用 Langfuse 表上的 Trace ID skipping index。
上线前应针对实际 `tool_error` 表执行 `EXPLAIN indexes = 1`，确认
`project_id/rule_id` 的筛选不会扫描不可接受的数据量。

## 10. 当前明确边界

- 直接表连接器目前支持 ClickHouse HTTP；其他数据库先导出标准
  `traces.jsonl`、`observations.jsonl`、`cases.jsonl`，再运行现有离线入口；
- `tool_error` 负责提供技术错误候选，不被当作人工真值；
- `annotations.jsonl` 只进入评测器，不进入确定性归因或 LLM prompt；
- 缺失 Trace、错误 Observation 不属于 Trace、候选重复等情况不会崩溃，
  会计入运行摘要的 `skipped_candidates`；
- Trace 入口若已包含带角色的历史 `assistant/system` 上下文，系统不会把它
  误标为当前 Trace 内的用户或模型根因。输出将保持 `decision=unknown`、
  `root_cause_observation_id=null`，同时以 `root_cause_scope`、
  `earliest_local_evidence_observation_id`、`local_trigger_observation_id` 和
  `propagation_observation_ids` 提供当前 Trace 内可核查的参考链；
- 上述参考链不做跨 Trace 查询，也不声称找到了上一条 Trace 的真正责任节点；
- 当前归因目标仍限定为 File Not Found，其他 Tool 错误后续扩展。
