# AgentDebugX File Not Found 标注工具

这是一个可独立移植的前后端标注工作台，用于从 Langfuse
`traces`、`observations` 和 `tool_error` 候选表构建 File Not Found
失败归因 Annotation v2。

工具不依赖 Langfuse 应用源码、Node.js、React、npm、Redis、PostgreSQL、对象存储、
LLM 或其他外部服务。前端是原生 HTML/CSS/JavaScript，后端使用 Python 标准库
HTTP Server，Annotation revision 保存在本地 SQLite。生产数据通过 AgentDebugX
已有的只读 ClickHouse HTTP 连接器加载；移植后只需提供 ClickHouse 连接和项目配置。

## 1. 能力边界

- 只处理 Tool `file not found` 候选；关键字命中不是人工真值。
- 分别保存技术错误、任务语义、错误显现、根因范围、当前 Trace 参考链和审核结论。
- `is_agent_failure` 与 `is_task_failure` 支持 `true / false / null`。
- 支持 `current_trace / outside_current_trace / unknown` 根因范围。
- 所有 Observation 引用都由服务端验证属于当前 `project_id + trace_id` 快照。
- 初次标注不读取或展示 DebugX/LLM 预测，避免锚定偏差。
- 当前不跨 Trace 查询或追踪真正根因。
- 不修改 ClickHouse 的 `traces`、`observations`、`tool_error` 原表。

## 2. 运行要求

- Python 3.9–3.13。
- AgentDebugX 核心依赖已安装。ClickHouse 客户端复用核心依赖 `httpx`，无需安装新的包。
- ClickHouse HTTP endpoint 能读取三张表。

先在任意环境验证页面：

```bash
cd AgentDebugX-main
python dataset/run_annotation_tool.py --demo --port 8790
```

打开 `http://127.0.0.1:8790`。演示模式包含当前 Trace 根因、Trace 外根因、
失败后恢复、规则误报和 unknown 五条脱敏 Fixture。

需要在工作环境按 anchor Trace、用户、时间和数量加载固定批次，同时生成 DebugX 输出时，
使用独立脚本及其[工作环境使用说明](./WORK_ENVIRONMENT_TRACE_BATCH.md)。该入口不会改变
本页下方普通 `--limit/--rule-id` 加载方式。

## 3. 生产配置

连接凭证只允许放在环境变量中：

```bash
export AGENTDEBUG_CLICKHOUSE_URL="https://clickhouse.example.internal:8443"
export AGENTDEBUG_CLICKHOUSE_USER="readonly_user"
export AGENTDEBUG_CLICKHOUSE_PASSWORD="replace-me"
export AGENTDEBUG_CLICKHOUSE_DATABASE="default"
export AGENTDEBUG_PROJECT_ID="replace-with-langfuse-project-id"
export AGENTDEBUG_ANNOTATOR_ID="your-internal-user-id"
```

可选变量：

```text
AGENTDEBUG_CLICKHOUSE_TIMEOUT_SECONDS=60
AGENTDEBUG_CLICKHOUSE_VERIFY_TLS=true
AGENTDEBUG_CLICKHOUSE_CA_FILE=/path/to/company-ca.pem
AGENTDEBUG_ANNOTATION_TOKEN=<remote-access-token>
```

默认表名和字段与现有生产 Pipeline 一致，通常无需配置文件：

```bash
python dataset/run_annotation_tool.py --limit 200 --port 8790
```

如果工作环境表名或 `tool_error` 字段不同，复制
`dataset/source.example.json` 并调整非敏感映射：

```bash
python dataset/run_annotation_tool.py \
  --config dataset/source.work.json \
  --limit 200 \
  --rule-id file-not-found-v1 \
  --port 8790
```

配置文件禁止包含 URL、用户名、密码、database 或 API Key；这些值必须使用环境变量。

默认只监听 `127.0.0.1`。如果需要监听工作环境网卡，必须设置
`AGENTDEBUG_ANNOTATION_TOKEN`：

```bash
export AGENTDEBUG_ANNOTATION_TOKEN="generate-a-random-internal-token"
python dataset/run_annotation_tool.py --host 0.0.0.0 --port 8790
```

页面顶部的“访问令牌”只保存在浏览器 `sessionStorage`，不会写入 SQLite。

### 3.1 环境变量输入

| 输入 | 是否必填 | 含义 | 默认值 | 示例 |
| --- | --- | --- | --- | --- |
| `AGENTDEBUG_CLICKHOUSE_URL` | 生产模式必填 | ClickHouse HTTP(S) endpoint | 无 | `https://clickhouse.internal:8443` |
| `AGENTDEBUG_CLICKHOUSE_USER` | 可选 | 只读数据库用户名 | `default` | `agentdebug_readonly` |
| `AGENTDEBUG_CLICKHOUSE_PASSWORD` | 可选 | 数据库密码；应由密钥管理注入 | 空字符串 | `***` |
| `AGENTDEBUG_CLICKHOUSE_DATABASE` | 可选 | 三张源表所在 database | `default` | `langfuse` |
| `AGENTDEBUG_PROJECT_ID` | 生产模式必填，或在 source JSON 中填写 | 只加载这个 Langfuse project 的数据 | 无 | `cm123project` |
| `AGENTDEBUG_CLICKHOUSE_TIMEOUT_SECONDS` | 可选 | 单次 HTTP 查询超时秒数 | `60` | `120` |
| `AGENTDEBUG_CLICKHOUSE_VERIFY_TLS` | 可选 | 是否校验 HTTPS 证书 | `true` | `true` |
| `AGENTDEBUG_CLICKHOUSE_CA_FILE` | 私有 CA 时条件必填 | 自定义 CA 文件路径 | 无 | `/etc/company/ca.pem` |
| `AGENTDEBUG_ANNOTATOR_ID` | 建议必填 | 写入每个本地revision的标注人；不是登录身份 | `local-annotator` | `zhangsan` |
| `AGENTDEBUG_ANNOTATION_TOKEN` | 非 loopback 监听时必填 | 标注 API 的共享 Bearer Token | 无 | 随机生成的内部令牌 |

Demo 模式不需要任何 ClickHouse 环境变量。不要把密码、Token 或 API Key 写进
`source.work.json`、SQLite 旁的说明文件或 Git。

### 3.2 启动参数输入

| 参数 | 是否必填 | 含义 | 默认值 | 示例 |
| --- | --- | --- | --- | --- |
| `--host` | 可选 | HTTP 监听地址；非 loopback 会强制检查 Token | `127.0.0.1` | `0.0.0.0` |
| `--port` | 可选 | 页面和 API 端口 | `8790` | `8791` |
| `--db` | 可选 | 本地 SQLite 文件 | `dataset/data/annotations.sqlite` | `/data/labels/fnf.sqlite` |
| `--config` | 表名/字段非默认时条件必填 | 非敏感 ClickHouse source JSON | 无 | `dataset/source.work.json` |
| `--limit` | 可选 | 每次同步最多加载的候选数 | `200` | `50` |
| `--rule-id` | 可选 | 从 `tool_error` 召回的规则 ID | `file-not-found-v1` | `file-not-found-v1` |
| `--max-observations` | 可选 | 单 Trace 最多保留的 Observation 数 | `10000` | `5000` |
| `--demo` | 可选开关 | 不连接 ClickHouse，加载五条脱敏 Fixture | false | `--demo` |
| `--log-level` | 可选 | Python 日志级别 | `INFO` | `DEBUG` |

`--limit` 限制的是候选加载量，不表示最终真值数量；`--max-observations` 触发时会保留
failure 节点并设置 `context_truncated=true`，但根因、父节点或恢复节点仍可能不在快照内。

### 3.3 Source JSON 输入

最小示例：

```json
{
  "project_id": "cm123project",
  "payload_contract": "redacted",
  "tables": {
    "traces": "traces",
    "observations": "observations",
    "tool_errors": "tool_error"
  },
  "candidate_columns": {
    "failure_observation_id": "observation_id"
  },
  "allow_unscoped_tool_errors": false,
  "matched_text_is_redacted": true
}
```

| 字段 | 是否必填 | 含义 | 示例 |
| --- | --- | --- | --- |
| `project_id` | 与环境变量二选一必填 | 项目隔离键；环境变量优先 | `cm123project` |
| `payload_contract` | 可选 | `raw` 展示/保存原字段，`redacted` 读取脱敏字段 | `redacted` |
| `tables.traces` | 可选 | Trace 表名 | `traces` |
| `tables.observations` | 可选 | Observation 表名 | `observations` |
| `tables.tool_errors` | 可选 | 候选表名 | `tool_error` |
| `candidate_columns` | 自动识别失败时条件必填 | 规范字段到实际候选表列名的映射 | `{"failure_observation_id":"obs_id"}` |
| `allow_unscoped_tool_errors` | 可选，通常必须为 false | 是否允许候选表缺少 project 列；只可用于物理隔离的专用表 | `false` |
| `matched_text_is_redacted` | 候选错误文本已脱敏时设 true | 声明 matched text 是否可以安全进入脱敏输出 | `true` |

`candidate_columns` 支持的规范 key 为：`failure_observation_id`、`trace_id`、
`project_id`、`case_id`、`rule_id`、`matched_field`、`matched_text`、`timestamp`。
表名和列名必须是普通 SQL identifier；配置文件中的 `url/user/password/database/api_key`
等连接字段会被拒绝。

## 4. 数据与迁移

默认 Annotation 数据库：

```text
dataset/data/annotations.sqlite
```

也可以显式指定：

```bash
python dataset/run_annotation_tool.py --db /data/agentdebugx/annotations.sqlite
```

SQLite schema 在启动时幂等创建：

- `cases`：只读源快照；同步时按 `case_id` upsert。
- `annotations`：以 `case_id + revision` 为主键的不可变revision。

每次保存草稿或提交都会创建新revision。`submitted` 默认只读，点击“创建新修订”后
才能继续修改；历史列表可打开任意revision并查看相对当前revision的字段差异。
`submitted/needs_review` 可经带变更原因的裁决生成 `adjudicated` revision，Case 冻结
或数据集冻结会生成 `frozen` revision；`frozen` 后不允许再修改。重新同步 ClickHouse
不会删除历史 Annotation。

旧标注迁移应先转换为 `failure-attribution-annotation-v2` JSONL，再通过页面“导入
JSONL”执行 dry-run 和确认导入。导入要求 Case 已存在、source hash 完全匹配并通过
严格校验，整批有错误时不会写入；成功导入始终创建本地新revision。无法确定的新字段
必须使用 `null/unknown`，不能猜测。原工作环境工具源码和数据结构不在本仓库，因此
本工具不会自动读取或覆盖旧工具数据库。

## 5. 备份和回滚

停服后备份 SQLite 主文件；如果启用了 WAL，同时备份 `-wal` 和 `-shm`：

```bash
cp /data/agentdebugx/annotations.sqlite /backup/annotations-YYYYMMDD.sqlite
```

恢复时停止服务，用备份文件替换指定 `--db` 文件再启动。工具不执行破坏性数据库
迁移；回滚代码不会影响已有 JSON payload revision。

## 6. 标注前必须理解的提交规则

README 下文所说“必填”默认指点击“提交并下一条”以及创建
`needs_review/adjudicated/frozen` 等严格revision时。保存 `draft` 或排除 Case 使用宽松
校验，允许内容未完成，但仍会检查枚举、Observation 是否属于当前 Case、root scope
互斥关系、传播链顺序等结构约束。

字段要求分为：

- **必填**：严格提交时必须给出；
- **条件必填**：只有满足表中条件时必须给出；
- **三态必填**：字段必须明确考虑，但允许选择“未知”，保存值为 `null`；
- **可选**：可以为空，但有证据时建议填写；
- **系统生成**：页面从当前 Case 或服务端生成，标注者不应手工伪造。

以下情况不会被强制猜测，提交后会自动进入 `needs_review`：

- `technical_error_review.human_label=insufficient_evidence`；
- 已确认技术错误，但 `is_agent_failure` 或 `is_task_failure` 为 `null`；
- 任一主要层级置信度为 `low`；
- 当前 Trace 上下文被截断。

### 6.1 三个判断层次不能合并

| 层次 | 要回答的问题 | 典型结果 |
| --- | --- | --- |
| 技术错误 | Tool 是否真的返回了 File Not Found？ | `confirmed` / `rule_false_positive` / `insufficient_evidence` |
| 任务语义 | 这个 Tool 结果是否导致 Agent 或最终任务失败？ | `true` / `false` / `null` |
| 根因归因 | 真正原因能否在当前 Trace 的 Observation 中被证实？ | `current_trace` / `outside_current_trace` / `unknown` |

例如：一次探测不存在文件可以是“真实 File Not Found”，但
`is_agent_failure=false`；Worker 5 案例是真实错误，但路径来源早于当前 Trace，所以应填
`outside_current_trace`，不能把当前 Trace 内转发路径的 LLM 节点伪造为真正 root。

## 7. 六步表单字段说明

### 7.1 第一步：技术错误审核

| 页面输入 / JSON 字段 | 是否必填 | 含义 | 示例 |
| --- | --- | --- | --- |
| 人工结论 `technical_error_review.human_label` | 必填 | 审核规则召回是否是真实技术错误 | `confirmed` |
| 纠错原因 `technical_error_review.correction_reason` | `rule_false_positive` 时必填 | 说明候选为什么是规则误报 | `log_or_documentation_only` |
| 技术审核置信度 `technical_error_review.confidence` | 必填 | 对“是否真实技术错误”的把握 | `high` |
| 审核说明 `technical_error_review.comment` | 可选 | 记录直接支持技术判断的简短事实 | `read Tool 明确返回 ENOENT。` |

人工结论枚举：

| 值 | 使用条件 | 示例 |
| --- | --- | --- |
| `confirmed` | Tool 确实返回找不到文件/路径 | output 为 `File not found: /workspace/a.md` |
| `rule_false_positive` | 关键词出现在成功正文、日志、文档或重复候选中，并非 Tool 失败 | 文档正文解释“file not found” |
| `insufficient_evidence` | 当前快照无法确认是否真的报错 | 只有截断文本，没有状态或完整 output |

纠错原因枚举：

| 值 | 含义 | 示例 |
| --- | --- | --- |
| `successful_content_mentions_error` | 成功返回内容中提到错误词 | 读取 FAQ 成功，正文含错误消息 |
| `expected_text_assertion` | 测试/断言期望出现该文本 | 单测验证 ENOENT 文案 |
| `log_or_documentation_only` | 只是日志或文档内容 | README 中列举错误处理方式 |
| `wrong_observation_type` | 命中了不应作为 Tool 结果的节点 | 普通 LLM 文本被候选规则命中 |
| `duplicate_candidate` | 同一次错误被重复召回 | 同 Observation 产生两条候选 |
| `other` | 以上均不适用 | 在审核说明中补充原因 |

规则误报不能保留 failure、root 或 recovery Observation；页面切换为规则误报时会清空
这些字段。规则误报和“真实错误但不是 Agent 失败”不是同一概念。

### 7.2 第二步：Tool 结果和任务语义

| 页面输入 / JSON 字段 | 是否必填 | 含义 | 示例 |
| --- | --- | --- | --- |
| 结果语义 `semantic_outcome.tool_result_semantics` | `confirmed` 时必填 | 该 Tool 结果在任务流程中的语义 | `unexpected_failure` |
| Agent 是否失败 `semantic_outcome.is_agent_failure` | 三态必填 | Agent 是否因该错误未能正常完成其职责 | `true` |
| 最终任务是否失败 `semantic_outcome.is_task_failure` | 三态必填 | 用户最终目标是否失败；与局部 Agent 错误分开 | `null` |
| 恢复状态 `semantic_outcome.recovery_status` | `confirmed` 时必填 | 当前 Trace 是否显示从失败中恢复 | `recovered` |
| 恢复 Observation `semantic_outcome.recovery_observation_id` | `recovered` 时必填 | 证明恢复的当前 Trace 节点，通过 `Y` 设置 | `recovered-success` |
| 语义置信度 `semantic_outcome.confidence` | `confirmed` 时必填 | 对 Agent/任务结果和恢复判断的把握 | `medium` |
| 结果判断原因 `semantic_outcome.outcome_reason` | 可选，建议填写 | 简述后续是否继续、恢复或终止 | `随后创建文件并再次读取成功。` |

`is_agent_failure/is_task_failure` 的页面“未知”会保存为 JSON `null`，不是字符串
`"unknown"`。证据不足时应保留 `null`，不要为了通过提交强填 `false`。

结果语义枚举：

| 值 | 含义 | 示例 |
| --- | --- | --- |
| `unexpected_failure` | 非预期错误，且没有被设计为控制流 | 读取模型臆造路径失败 |
| `expected_negative_result` | 预期可能不存在，并把负结果作为正常结果 | 检查可选配置是否存在 |
| `validation_probe` | 主动探测存在性/能力 | 先 read 探测缓存文件 |
| `control_flow_signal` | 失败被用作流程分支信号 | 不存在则进入创建分支 |
| `recovered_failure` | 先真实失败，后续动作恢复 | create 后再次 read 成功 |
| `tolerated_failure` | 错误未恢复，但业务容忍并继续完成 | 可选附件缺失但报告仍生成 |
| `user_requested_negative_test` | 用户明确要求触发/验证失败 | 用户要求测试缺失文件处理 |
| `external_failure` | 失败来自外部依赖或外部状态 | 挂载卷/远端存储不可用 |
| `unknown` | 当前 Trace 不足以确定语义 | 只有 ENOENT，缺少上下文 |

### 7.3 第三步：错误显现节点

| 页面输入 / JSON 字段 | 是否必填 | 含义 | 设置方式与示例 |
| --- | --- | --- | --- |
| 首次显现 `failure_manifestation.failure_onset_observation_id` | `confirmed` 时必填 | Trace 时间上最早出现该失败的节点 | 对 `outside-failure` 标 `F` 后自动取最早值 |
| 主要失败节点 `failure_manifestation.primary_failure_observation_id` | `confirmed` 时必填 | 本 Case 主要评测目标的失败节点 | `current-failure` |
| 全部失败节点 `failure_manifestation.failure_observation_ids` | `confirmed` 时至少一个 | 同一 Case 中所有相关失败显现 | `["read-fail-1","read-fail-2"]` |
| 下游症状 `failure_manifestation.downstream_symptom_observation_ids` | 可选 | 由该错误引发、但不是根因或原始 failure 的后续异常 | `assistant-final-error` |

在 Observation 树中选择节点后使用 `F` 设置 failure，使用 `S` 设置下游症状。错误显现
回答“错误在哪里出现”，不是“谁造成了错误”；因此 failure 和 root 可以是不同节点。

### 7.4 第四步：归因适用性和根因范围

| 页面输入 / JSON 字段 | 是否必填 | 含义 | 示例 |
| --- | --- | --- | --- |
| 根因范围 `attribution.root_cause_scope` | `confirmed` 时必填 | 真正根因在当前 Trace、Trace 外，或无法判断 | `outside_current_trace` |
| 归因适用 `attribution.applicable` | 条件必填 | `current_trace` 必须为 `true`；另两种 scope 必须为 `false` | `false` |
| 归因置信度 `attribution.confidence` | `confirmed` 时必填 | 对 scope、domain、label 和 root 判断的把握 | `high` |
| 主要根因 `attribution.primary_root_cause_observation_id` | `current_trace` 时必填 | 当前 Trace 内首要根因节点，通过 `R` 设置 | `current-model` |
| 根因节点列表 `attribution.root_cause_observation_ids` | `current_trace` 时至少一个 | 所有当前 Trace 根因节点 | `["current-model"]` |
| 责任域 `attribution.root_cause_domain` | `current_trace` 时必填 | 根因责任来源 | `model` |
| 原因标签 `attribution.root_cause_label` | `current_trace` 时必填 | 可评测的根因类型 | `model_path_hallucination` |
| 路径来源类型 `attribution.path_source_kind` | 可选 | 当前证据显示路径从何处进入 | `assistant_history` |
| 路径来源节点 `attribution.path_source_observation_id` | 可选 | 当前 Trace 内最早携带路径来源证据的节点，通过 `C` 设置 | `outside-entry` |

scope 规则：

- `current_trace`：当前 Trace 中有可验证 root；必须选择 root、domain、label 和总体证据；
- `outside_current_trace`：当前 Trace 入口已经携带错误路径或状态；禁止选择本地 root，
  但应填写第五步参考链；
- `unknown`：既不能证明当前 Trace root，也不能证明根因在 Trace 外；禁止选择本地 root。

File Not Found 的产品层 F1–F5 建议映射：

| 产品层 | 典型 Annotation | 使用要求 |
| --- | --- | --- |
| F1 非 Agent 失败 | `is_agent_failure=false`，通常 `applicable=false` | 真实 probe/tolerated failure；规则误报另用 `rule_false_positive` |
| F2 用户无效路径 | `domain=user`, `label=user_path_invalid` | 当前 Trace 有明确 user 路径证据 |
| F3 模型路径幻觉 | `domain=model`, `label=model_path_hallucination` | 模型在当前 Trace 生成了无来源路径 |
| F4 上游/Skill/System 路径失效 | `domain=skill/upstream`, `label=upstream_path_invalid` | 必须有显式 provenance；复杂 System 上下文不足时用 unknown/outside |
| F5 运行环境路径不可用 | `domain=runtime/environment`, `label=runtime_path_unavailable` | 需要 `resource_existence=true` 等运行时证据；历史 File Not Found 文本本身不能证明 F5 |

`tool_contract_mismatch` 和 `external_dependency_failure` 虽在 schema 枚举中，但只在当前
File Not Found Case 有直接证据时使用；否则保守选择 `unknown`。路径来源节点是证据，
不自动等于 root。例如 Worker 5 的 `outside-entry` 是入口参考，不是当前 Trace 根因。

责任域枚举：

| 值 | 含义 |
| --- | --- |
| `user` | 路径直接来自当前 Trace 中的用户输入 |
| `model` | 模型在当前 Trace 自行生成或改写路径 |
| `skill` | 路径来自有明确证据的 Skill 内容 |
| `upstream` | 路径来自当前 Agent 之前的上游组件/步骤 |
| `runtime` | 执行器、容器或运行时状态导致路径不可用 |
| `environment` | 挂载、工作目录、配置等环境状态导致路径不可用 |
| `tool` | Tool 参数契约或实现行为与调用方不匹配 |
| `external_dependency` | 外部存储、远程文件服务等依赖失败 |
| `unknown` | 当前证据无法可靠确定责任域 |

原因标签枚举：

| 值 | 含义 |
| --- | --- |
| `user_path_invalid` | 用户给出的路径无效或不存在 |
| `model_path_hallucination` | 模型臆造了没有来源依据的路径 |
| `upstream_path_invalid` | Skill/System/上游提供了失效路径 |
| `runtime_path_unavailable` | 资源应存在，但在当前运行环境不可访问 |
| `tool_contract_mismatch` | 路径字段、格式或 Tool 契约不一致 |
| `external_dependency_failure` | 外部依赖使文件不可访问 |
| `unknown` | 能确认 current-trace root，但证据不足以细分标签；使用时应降低置信度并复核 |

路径来源类型枚举：

| 值 | 含义 | 示例 |
| --- | --- | --- |
| `user` | 当前用户消息 | 用户明确输入 `/data/a.csv` |
| `assistant_history` | Trace 入口携带的历史 assistant 内容 | Worker 5 报告已经在历史中 |
| `model` | 当前模型输出/Tool Call | 模型生成 `/workspace/invented.md` |
| `skill` | Skill 指令或模板 | Skill 引用固定脚本路径 |
| `system` | System prompt/上下文 | 系统消息给出默认目录 |
| `environment` | 环境变量、cwd、挂载等运行配置 | metadata 中记录工作目录 |
| `unknown` | 无法确定路径来源 | 只有 Tool error 文本 |

### 7.5 第五步：当前 Trace 参考链

| 页面输入 / JSON 字段 | 是否必填 | 含义 | 示例 |
| --- | --- | --- | --- |
| 最早本地证据 `current_trace_reference.earliest_local_evidence_observation_id` | 可选，outside 时强烈建议 | 当前 Trace 内最早能看到路径/状态的节点，通过 `C` 设置 | `outside-entry` |
| 本地触发节点 `current_trace_reference.local_trigger_observation_id` | 可选，outside 时强烈建议 | 当前 Trace 内直接触发失败 Tool Call 的节点，通过 `T` 设置 | `outside-trigger` |
| 有序传播链 `current_trace_reference.propagation_observation_ids` | 可选；填写时必须含主要 failure | 从本地证据到 failure/症状的时间有序链 | `outside-entry → outside-trigger → outside-failure` |
| 参考证据 `current_trace_reference.evidence_observation_ids` | 可选 | 支持参考链判断的节点，通过 `C/T/E` 设置 | 三个上述节点 |
| 边界原因 `current_trace_reference.boundary_reason` | 可选，outside 时建议填写 | 为什么无法在当前 Trace 给出真正 root | `assistant_context_at_trace_entry` |
| 参考摘要 `current_trace_reference.reference_summary` | 可选，outside 时建议填写 | 只描述当前 Trace 可证实的传播事实 | `路径在入口历史中已存在，当前 LLM 转发给 read。` |
| 参考置信度 `current_trace_reference.confidence` | 页面始终提供 | 对当前 Trace 参考链的把握；`low` 会进入复核 | `high` |

边界原因枚举：

| 值 | 含义 |
| --- | --- |
| `assistant_context_at_trace_entry` | 路径已存在于 Trace 入口 assistant 历史 |
| `system_context_at_trace_entry` | 路径已存在于 Trace 入口 system 上下文 |
| `upstream_reference_without_source` | 只看到上游引用，看不到产生来源 |
| `trace_context_truncated` | 可能的来源被上下文截断 |
| `other` | 其他边界；在摘要或备注中说明 |

传播链所有 ID 都必须属于当前 Case 快照，按 Observation `start_time` 非递减，并包含
主要失败节点。当前工具不做跨 Trace 查询；Trace 外 root 不应被写进任何 Observation ID。

### 7.6 第六步：审核、置信度和revision

| 页面输入 / JSON 字段 | 是否必填 | 含义 | 示例 |
| --- | --- | --- | --- |
| 总体证据节点 `review.evidence_observation_ids` | `current_trace` 时至少一个 | 支持最终归因结论的当前 Trace 节点，通过 `E/C/T` 设置 | `["current-model","current-failure"]` |
| 推理摘要 `review.reasoning_summary` | `confirmed` 时必填 | 解释技术、语义和根因结论，不复制整段 Trace | `模型生成了用户未提供的路径，随后 read 失败。` |
| 已知限制 `review.limitations` | 可选多选 | 标出可能影响标签可靠性的观测限制 | `["root_outside_trace"]` |
| 总体置信度 `review.overall_confidence` | 必填 | 对整条 Annotation 的综合把握 | `medium` |
| 修改原因 `audit.change_reason` | 保存/提交时可选，新revision建议填写 | 说明为什么创建本次revision；服务端写入 audit | `补充恢复节点证据` |
| 备注 `review.notes` | 可选 | 记录不适合放进推理摘要的审核信息 | `等待任务负责人确认最终结果。` |

已知限制：

| 值 | 何时勾选 |
| --- | --- |
| `root_outside_trace` | 真正根因早于或位于当前 Trace 外 |
| `downstream_outcome_unclear` | 看不到 Agent/任务最终结果 |
| `context_truncated` | 当前 Case 明确标记上下文截断 |
| `tool_call_pairing_ambiguous` | Tool Call 与 Result 配对不稳定 |
| `runtime_state_unavailable` | 缺少文件存在性、挂载、权限等运行时事实 |

置信度建议统一使用：

- `high`：直接字段、明确调用参数和相邻节点共同支持，基本无竞争解释；
- `medium`：主要结论有证据，但任务结果、路径 provenance 或局部上下文仍有缺口；
- `low`：只有间接文本、上下文截断或存在多个同样合理解释；会自动进入复核。

## 8. Observation 树和页面辅助输入

### 8.1 节点选择与角色

先勾选一个或多个 Observation；未勾选时，角色按钮作用于当前详情节点。

| 输入 | 含义 | 会更新的字段 | 示例 |
| --- | --- | --- | --- |
| `F` 错误 | 标记 failure | failure 列表、首次/主要 failure、传播链 | `current-failure` |
| `R` 根因 | 标记当前 Trace root；仅 current scope 可用 | root 列表、主要 root、applicable | `current-model` |
| `E` 证据 | 标记总体/参考证据 | 两组 evidence IDs | `current-model` |
| `C` 最早证据 | 标记当前 Trace 最早路径证据 | earliest、path source、参考链 | `outside-entry` |
| `T` 触发 | 标记直接触发 Tool 的本地节点 | local trigger、参考链 | `outside-trigger` |
| `S` 症状 | 标记 failure 后的下游症状 | downstream symptom IDs | `assistant-final-error` |
| `Y` 恢复 | 标记恢复节点并设置 recovered | recovery status/Observation | `recovered-success` |
| 清除角色 | 从所有相关角色中移除节点 | 对应 scalar/list 字段 | 移除误选 root |

`F/R/E/C/T` 可直接使用键盘快捷键。传播链的上移/下移只是编辑顺序，服务端仍要求
最终顺序符合 Trace 时间。

### 8.2 搜索、筛选和显示输入

这些输入不写入 Annotation：

| 输入 | 是否必填 | 作用 | 示例 |
| --- | --- | --- | --- |
| 访问令牌 | 远程 Token 部署时必填 | 保存到浏览器 sessionStorage 并用于 API | 内部共享 Token |
| Case 状态 | 可选 | 只显示某个工作流状态 | `needs_review` |
| 搜索 Case | 可选 | 匹配 Case ID、Trace ID、scenario | `worker-5` |
| 标签筛选 | 可选 | 按技术标签、scope、root label、总体置信度筛选 | `outside_current_trace` |
| Observation 搜索 | 可选 | 匹配 ID、name、错误文本 | `ENOENT` |
| Observation 类型 | 可选 | 只显示一种节点 type | `GENERATION` |
| 仅错误 | 可选 | 只显示 ERROR/含错误文本节点 | 勾选 |
| 自动换行 | 可选 | 控制详情长文本显示 | 勾选 |
| 自动保存草稿 | 可选 | 修改约 1.4 秒后创建一个新的 draft revision | 勾选；大量试填时可暂不启用 |

## 9. 完整填写示例

示例只展示人工字段；`schema_version/case_id/project_id/trace_id/source_snapshot` 由当前
Case 自动生成。Observation ID 必须替换为当前页面中的实际 ID。

### 9.1 当前 Trace 内模型路径幻觉

Demo 节点：`current-model → current-failure`。

```json
{
  "annotation_status": "submitted",
  "technical_error_review": {
    "human_label": "confirmed",
    "correction_reason": null,
    "confidence": "high",
    "comment": "read 明确返回 File not found。"
  },
  "semantic_outcome": {
    "tool_result_semantics": "unexpected_failure",
    "is_agent_failure": true,
    "is_task_failure": true,
    "recovery_status": "not_recovered",
    "recovery_observation_id": null,
    "outcome_reason": "没有看到恢复或替代路径。",
    "confidence": "high"
  },
  "failure_manifestation": {
    "failure_onset_observation_id": "current-failure",
    "primary_failure_observation_id": "current-failure",
    "failure_observation_ids": ["current-failure"],
    "downstream_symptom_observation_ids": []
  },
  "attribution": {
    "applicable": true,
    "root_cause_scope": "current_trace",
    "primary_root_cause_observation_id": "current-model",
    "root_cause_observation_ids": ["current-model"],
    "root_cause_domain": "model",
    "root_cause_label": "model_path_hallucination",
    "path_source_kind": "model",
    "path_source_observation_id": "current-model",
    "confidence": "high"
  },
  "current_trace_reference": {
    "earliest_local_evidence_observation_id": "current-model",
    "local_trigger_observation_id": "current-model",
    "propagation_observation_ids": ["current-model", "current-failure"],
    "evidence_observation_ids": ["current-model", "current-failure"],
    "boundary_reason": null,
    "reference_summary": "模型生成路径后立即调用 read。",
    "confidence": "high"
  },
  "review": {
    "evidence_observation_ids": ["current-model", "current-failure"],
    "reasoning_summary": "用户未提供该路径；模型在当前 Trace 生成路径，随后 read 失败。",
    "limitations": [],
    "overall_confidence": "high",
    "notes": ""
  }
}
```

### 9.2 Worker 5：根因位于 Trace 外

Demo 节点：`outside-entry → outside-trigger → outside-failure`。`outside-entry` 的输入已经
含有“Worker 5 reported ... missing”，所以它是当前 Trace 最早参考证据；它没有展示
Worker 5 如何创建或产生该路径，因此不是当前 Trace root。

```json
{
  "annotation_status": "needs_review",
  "technical_error_review": {
    "human_label": "confirmed",
    "correction_reason": null,
    "confidence": "high",
    "comment": "read 明确返回 File Not Found。"
  },
  "semantic_outcome": {
    "tool_result_semantics": "unexpected_failure",
    "is_agent_failure": null,
    "is_task_failure": null,
    "recovery_status": "unknown",
    "recovery_observation_id": null,
    "outcome_reason": "当前 Trace 仍有后续节点，无法确认最终任务结果。",
    "confidence": "low"
  },
  "failure_manifestation": {
    "failure_onset_observation_id": "outside-failure",
    "primary_failure_observation_id": "outside-failure",
    "failure_observation_ids": ["outside-failure"],
    "downstream_symptom_observation_ids": []
  },
  "attribution": {
    "applicable": false,
    "root_cause_scope": "outside_current_trace",
    "primary_root_cause_observation_id": null,
    "root_cause_observation_ids": [],
    "root_cause_domain": null,
    "root_cause_label": null,
    "path_source_kind": "assistant_history",
    "path_source_observation_id": "outside-entry",
    "confidence": "high"
  },
  "current_trace_reference": {
    "earliest_local_evidence_observation_id": "outside-entry",
    "local_trigger_observation_id": "outside-trigger",
    "propagation_observation_ids": [
      "outside-entry",
      "outside-trigger",
      "outside-failure"
    ],
    "evidence_observation_ids": [
      "outside-entry",
      "outside-trigger",
      "outside-failure"
    ],
    "boundary_reason": "assistant_context_at_trace_entry",
    "reference_summary": "路径在 Trace 入口历史中已存在，当前 LLM 只把它转发给 read。",
    "confidence": "high"
  },
  "review": {
    "evidence_observation_ids": [
      "outside-entry",
      "outside-trigger",
      "outside-failure"
    ],
    "reasoning_summary": "确认技术错误，但真正路径来源早于当前 Trace，不把入口上下文或本地触发节点伪造成 root。",
    "limitations": [
      "root_outside_trace",
      "downstream_outcome_unclear",
      "runtime_state_unavailable"
    ],
    "overall_confidence": "medium",
    "notes": "当前不做跨 Trace 自动根因追踪。"
  }
}
```

### 9.3 其他常见案例速查

| 场景 | 关键填写 | 不应填写 |
| --- | --- | --- |
| 规则误报 | `human_label=rule_false_positive`，选择 correction reason，overall confidence | failure/root/recovery 节点 |
| 真实探测负例 | `confirmed + validation_probe + is_agent_failure=false + is_task_failure=false` | 不要仅因有 ENOENT 就标 Agent 失败 |
| 失败后恢复 | `recovered_failure`、两项失败结果按最终事实填写、`recovery_status=recovered`、用 `Y` 选择成功节点 | 不要把恢复节点当 root |
| 根因证据不足 | `root_scope=unknown`、`applicable=false`、root 列表为空、低/中置信度 | 不要从错误文本猜 user/model/runtime |
| F5 运行时不可用 | 明确运行证据支持时填 `runtime_path_unavailable` | 只有历史 File Not Found 文本时禁止填 F5 |

## 10. 标签管理操作的输入

| 操作输入 | 是否必填 | 含义 | 示例 |
| --- | --- | --- | --- |
| 创建新revision后的“修改原因” | 建议填写 | 解释本次人工修订 | `补充任务最终结果` |
| 裁决对话框“变更原因” | 必填 | 从 submitted/needs_review 创建 adjudicated revision 的审计原因 | `两位审核人确认 root scope` |
| Case 冻结“变更原因” | 必填 | 从 adjudicated 创建不可修改的 frozen revision | `纳入 fnf-20260817-v1` |
| 导入 JSONL 文件 | 导入时必填 | 每行一个完整 Annotation v2 | `annotations.jsonl` |
| 数据集版本 | 冻结时必填 | 1–64 位字母、数字、点、横线或下划线，以字母/数字开头 | `fnf-20260817-v1` |
| 测试集比例 | 冻结时必填 | 按 Trace 确定性分入 test 的比例，必须在 0 和 1 之间 | `0.4` |

导入文件每一行都必须：Case 已存在、`source_snapshot.source_hash` 与当前 Case 完全匹配、
status 属于 `submitted/needs_review/adjudicated/frozen`、通过严格校验，且当前 Case 没有
冻结。页面总是先 dry-run；任何一行失败都会阻止整批应用。

最安全的导入模板是本工具“导出 JSONL”生成的一整行，再针对目标 Case 修改人工字段。
第 9 节示例为了可读性省略了身份和 source snapshot，因此不能直接作为导入文件。导入
时不能把一个 Case 的 `case_id/trace_id/source_hash` 复制给另一个 Case，也不能保留旧
工具自增 revision 作为本地事实；服务端会创建新的本地revision和audit。

数据集冻结要求所有非排除 Case 已经 `adjudicated/frozen`。预检中的 blocker 应逐条处理，
不能通过提高置信度或强制填写 root 来绕过。test ratio 是 Trace 级哈希阈值，不保证标签
分层均衡。

## 11. 系统生成字段和状态流转

| JSON 字段 | 来源 | 标注者是否编辑 | 示例 |
| --- | --- | --- | --- |
| `schema_version` | 页面固定 | 否 | `failure-attribution-annotation-v2` |
| `case_id/project_id/trace_id` | 当前 Case envelope | 否 | `demo-current-root` |
| `annotation_status` | 保存/提交/裁决/冻结操作 | 不直接编辑 | `needs_review` |
| `source_snapshot` | 服务端以当前 Case 覆盖写入 | 否 | 含 `source_hash/context_truncated` |
| `annotation_revision` | SQLite 按 Case 递增 | 否 | `3` |
| `audit.annotator_id` | 服务端环境变量 | 否 | `zhangsan` |
| `audit.created_at/updated_at` | 服务端 UTC 时间 | 否 | ISO-8601 时间 |
| `audit.change_reason` | 页面操作输入后由服务端写入 | 间接输入 | `补充恢复证据` |

状态流转：

```text
candidate → draft → submitted ─┐
                     └→ needs_review → adjudicated → frozen
candidate/draft ────────────────────────────────→ excluded
```

实际提交可能因 `null`、低置信度或截断自动从 requested `submitted/adjudicated` 变为
`needs_review`。submitted/needs_review/adjudicated 默认只读，可创建新revision；frozen
不能再修改。裁决只表示当前单机流程状态，不代表已经实现双人盲标或组织审批。

## 12. 流程与服务端校验摘要

### 12.1 六步操作顺序

1. 技术错误：确认、规则误报或证据不足。
2. 任务语义：Tool语义、Agent/任务三态结果和恢复状态。
3. 错误显现：首次、主要、全部失败节点及下游症状。
4. 根因范围：当前Trace、Trace外或未知；只在当前Trace分支选择根因。
5. 参考链：最早本地证据、本地触发、传播链和边界原因。
6. 审核：证据、推理摘要、限制、分层置信度、revision和提交。

### 12.2 页面能力

Observation 树支持：

- ID、name、type、错误文本搜索；类型和错误过滤；
- 定位规则命中 Tool、父节点和后续节点；
- 多选后批量标记 `F/R/E/C/T/S/Y`；
- 键盘快捷键 `F/R/E/C/T`；
- 传播链上移、下移和删除；
- input/output/status/metadata 分页详情。

标签管理支持：

- 队列直接展示并筛选技术标签、根因 scope/label 和置信度；
- 当前标签摘要不读取或展示 DebugX/LLM 预测；
- 打开任意历史revision的完整 payload，并与当前revision比较；
- 带必填变更原因的 Case 裁决和 Case 冻结；
- 标签质量视图，展示状态、标签分布和待处理问题计数；
- JSONL 导入 dry-run、整批校验和显式确认；
- 数据集冻结预检、按 Trace 确定性分组、文件 hash 和 ZIP 下载。

### 12.3 提交校验

- 规则误报不能包含失败、根因或恢复节点。
- `recovered` 必须选择恢复Observation。
- `current_trace` 必须有primary root、根因列表、责任域、原因标签和证据。
- `outside_current_trace/unknown` 禁止当前Trace根因ID。
- 传播链必须包含主要失败节点并按Trace时间非递减。
- 所有节点必须属于当前Case快照。
- `null`任务结果、低置信度或上下文截断自动进入 `needs_review`。
- 源快照hash变化时拒绝基于旧快照提交。
- 冻结revision不可原地修改。

API 会返回字段级 `field_errors`，前端自动定位到对应步骤和字段。

## 13. 导入、导出与数据集冻结

页面“导出 JSONL”下载 `annotations.jsonl`。每个Case只导出最新的
`submitted / needs_review / adjudicated / frozen` revision，忽略草稿和排除记录。
原始Trace/Observation数据不会混入这个文件。

页面“导入 JSONL”先执行 dry-run。仅接受上述 submitted-like 状态；Case 不存在、
source hash 不一致、字段校验失败或目标 Case 已冻结都会阻止整批导入。确认应用后，
每条导入标签以新的不可变revision保存，不复用外部revision或audit。

页面“冻结数据集”只在所有非排除 Case 均为 `adjudicated/frozen` 时允许应用。生成的
ZIP 包含 `manifest.json`、四份 JSONL、`splits/dev.txt`、`splits/test.txt`；split 以
Trace ID 的 SHA-256 确定性分组，manifest 记录 `trace_leakage_count` 和各文件 hash。
当前 split 不按标签分层，冻结包也不代表算法准确率。

## 14. 验证命令

```bash
cd AgentDebugX-main
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_dataset_annotation_tool.py
PYTHONPATH=src .venv/bin/python -m ruff check dataset tests/test_dataset_annotation_tool.py
node --check dataset/annotation_tool/static/app.js
```

浏览器验收必须使用 `--demo` Fixture，覆盖五类Case、所有按钮、六步表单、字段校验、
revision详情/差异、裁决/冻结、质量视图、导入/导出、数据集冻结预检、桌面和390px
窄屏、控制台错误及失败网络请求。
