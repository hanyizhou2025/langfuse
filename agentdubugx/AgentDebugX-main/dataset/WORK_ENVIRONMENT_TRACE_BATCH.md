# 工作环境指定 Trace 批量加载脚本

脚本位置：

```text
dataset/run_work_environment_trace_batch.py
```

它只处理 `tool_error.error_type = 'dependency_or_env_error/file_not_found'`，一次完成：

1. 选择 anchor Trace 及其之前的 N 条不同 Trace；
2. 写出标准 `traces/observations/cases/manifest` 数据集；
3. 运行 DebugX 确定性 File Not Found 归因，生成 predictions 和 trajectories；
4. 把同一批 Case 写入独立标注 SQLite；
5. 默认启动现有标注页面，或用 `--prepare-only` 只准备文件。

脚本不修改 ClickHouse，不改变标注工具或 DebugX 的默认加载逻辑，也不会把连接密码写入
配置、输出或日志。

## 1. 选择语义

`--limit 20` 表示总计最多 20 条不同 Trace，包含 anchor Trace，而不是“anchor 加之前
20 条”。选择规则为：

```text
同 project
+ error_type 精确等于 dependency_or_env_error/file_not_found
+ 可选 user_id
+ 可选起止时间（闭区间）
+ created_at 不晚于 anchor 的匹配错误时间
→ 按 trace_id 分组
→ 每个 Trace 取最新一条匹配 tool_error 作为 Case
→ 按匹配时间倒序取 N 条
```

用户和时间条件也应用于 anchor。若 anchor 本身不满足条件，脚本直接报错，不会悄悄改用
另一条 Trace。若范围内不足 20 条，脚本加载实际可用数量并在输出和日志中明确显示。

## 2. Windows 准备

进入解压后的项目根目录：

```powershell
cd C:\work\AgentDebugX-main

py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

`__MACOSX`、`.DS_Store` 和 `._*` 不参与运行，可以删除。

## 3. Source 配置

从已按当前 `tool_error` DDL 配好的示例创建 `source.work.json`：

```powershell
Copy-Item .\dataset\work-environment-source.example.json .\source.work.json
notepad .\source.work.json
```

不要在其中写密码。内容如下：

```json
{
  "project_id": "<实际 Langfuse project_id>",
  "payload_contract": "raw",
  "tables": {
    "traces": "traces",
    "observations": "observations",
    "tool_errors": "default.tool_error"
  },
  "candidate_columns": {
    "failure_observation_id": "observation_id",
    "trace_id": "trace_id",
    "project_id": "project_id",
    "case_id": "id",
    "rule_id": "error_type",
    "timestamp": "created_at"
  },
  "allow_unscoped_tool_errors": false,
  "matched_text_is_redacted": false
}
```

`project_id` 必须填写，也可以改用 `--project-id` 或
`AGENTDEBUG_PROJECT_ID`。项目过滤既用于数据隔离，也利用 `tool_error ORDER BY` 的首列；
脚本不会执行不带 project scope 的批量选择。

如果 `traces/observations` 实际位于其他 database，请写成
`database_name.traces` 和 `database_name.observations`。

## 4. 连接环境变量

脚本直接兼容工作环境现有变量：

```powershell
$env:CLICKHOUSE_URL = "http://<clickhouse-host>:8123"
$env:CLICKHOUSE_USER = "<readonly-user>"
$env:CLICKHOUSE_PASSWORD = "<password>"
$env:CLICKHOUSE_DATABASE = "default"
```

也支持标准名称：

```text
AGENTDEBUG_CLICKHOUSE_URL
AGENTDEBUG_CLICKHOUSE_USER
AGENTDEBUG_CLICKHOUSE_PASSWORD
AGENTDEBUG_CLICKHOUSE_DATABASE
```

如果两套变量同时存在，`AGENTDEBUG_*` 优先。`CLICKHOUSE_MIGRATION_URL` 使用 native
9000 端口，本脚本的只读 HTTP client 不使用它；`CLICKHOUSE_CLUSTER_ENABLED` 也与
本脚本无关。

建议使用只读账号。HTTP client 固定发送 `readonly=2`、执行时间和结果行数上限，并拒绝
redirect。

## 5. 针对当前 anchor 准备 20 条

先只生成文件和 SQLite，不启动长驻页面：

```powershell
.\.venv\Scripts\python.exe dataset\run_work_environment_trace_batch.py `
  --anchor-trace-id msg_ff0ad3824001AfOMAUfaX27aW9 `
  --project-id "<实际 project_id>" `
  --user-id "<实际 user_id>" `
  --start-time "2026-08-01T00:00:00+08:00" `
  --end-time "2026-08-17T23:59:59+08:00" `
  --limit 20 `
  --config .\source.work.json `
  --prepare-only
```

时间接受 ISO-8601。带 `Z` 或 `+08:00` 的值会转换为 UTC；没有时区的值按 ClickHouse
服务器时间原样解释。起止时间均为闭区间。

如果不需要用户过滤，可以省略 `--user-id`；如果不设起始时间，可能扫描该 project 在
anchor 之前的较长时间范围，不建议在大型生产表上这样做。

## 6. 输出位置

未显式指定目录时，脚本根据完整选择条件生成 12 位 selection key：

```text
dataset/data/work-selection-<key>/
├── manifest.json
├── traces.jsonl
├── observations.jsonl
└── cases.jsonl

dataset/data/work-debugx-<key>/
├── predictions.jsonl
└── trajectories.jsonl

dataset/data/work-annotations-<key>.sqlite
```

终端会输出实际绝对路径、请求数量、选中数量、成功加载数量和 DebugX 运行计数。
`manifest.json.selection` 记录 anchor、用户/时间条件以及选中的 Trace ID，便于复现。

也可以指定固定位置：

```powershell
  --dataset-dir D:\agentdebug-data\fnf-selection `
  --debugx-output-dir D:\agentdebug-data\fnf-debugx `
  --annotation-db D:\agentdebug-data\fnf-annotations.sqlite
```

脚本不删除旧 Annotation。如果显式 SQLite 中存在本批之外的 Case，会拒绝继续并要求换
一个 `--annotation-db`，避免不同选择批次悄悄混在同一页面。

## 7. 启动标注页面

去掉 `--prepare-only`：

```powershell
.\.venv\Scripts\python.exe dataset\run_work_environment_trace_batch.py `
  --anchor-trace-id msg_ff0ad3824001AfOMAUfaX27aW9 `
  --project-id "<实际 project_id>" `
  --user-id "<实际 user_id>" `
  --start-time "2026-08-01T00:00:00+08:00" `
  --end-time "2026-08-17T23:59:59+08:00" `
  --limit 20 `
  --config .\source.work.json `
  --port 8790
```

打开：

```text
http://127.0.0.1:8790/
```

页面顶部“同步候选”只重新加载本次内存选择，不会扩大到其他 Trace。停止服务后可使用
同一组参数重新运行；selection key 相同，因此会读取原 SQLite revision。

如果监听 `0.0.0.0`，必须先设置 `AGENTDEBUG_ANNOTATION_TOKEN`，并确保工作环境网络
访问控制允许这样部署。

## 8. 在 DebugX 查看 20 条 Trace

脚本已经对同一批 Case 运行确定性归因。使用 DebugX 页面中的 **Upload Trace** 上传：

```text
dataset/data/work-debugx-<key>/trajectories.jsonl
```

然后进入每条 Trace 的 **Tool Attribution** 查看 failure、root、reference chain、原因码
和置信度。没有人工 `annotations.jsonl` 时不会生成 `report.json`；这不表示运行失败。

该脚本不启用 LLM，也不做跨 Trace 自动根因追踪。Trace 外根因仍只输出当前 Trace 的
reference chain。

## 9. 参数速查

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--anchor-trace-id` | 是 | anchor Trace；本例为 `msg_ff0ad3824001AfOMAUfaX27aW9` |
| `--project-id` | 与配置/env 三选一 | Langfuse project scope |
| `--user-id` | 可选 | 精确匹配 `tool_error.user_id` |
| `--start-time` | 可选，生产建议 | inclusive 时间下界 |
| `--end-time` | 可选 | inclusive 时间上界；仍不会越过 anchor |
| `--limit` | 可选 | 不同 Trace 总数，默认 20，范围 1–1000 |
| `--config` | 可选 | source JSON；表名或映射非默认时应提供 |
| `--payload-contract` | 可选 | `raw/redacted`；必须与实际列语义一致 |
| `--max-observations` | 可选 | 单 Trace 标注页面最多加载节点数，默认 10000 |
| `--prepare-only` | 可选开关 | 只生成数据、DebugX 输出和 SQLite，不启动页面 |
| `--host/--port` | 可选 | 标注服务地址，默认 `127.0.0.1:8790` |

## 10. ClickHouse 查询约束

按照仓库 ClickHouse 规则：

- Per `schema-pk-filter-on-orderby`，批量选择始终先过滤 `project_id`，并在
  `created_at` 上使用范围；这与 `ORDER BY (project_id, created_at, id)` 对齐；
- 时间条件同时帮助 `toYYYYMMDD(created_at)` 分区裁剪；
- `user_id/error_type/trace_id` 不在排序键中，只在 project/time 已缩小范围后过滤；
- Per `query-index-skipping-indices`，脚本不擅自修改生产 DDL 或添加 skip index；应先用
  真实查询测量，再决定是否需要索引；
- 查询没有 JOIN，因此 `query-join-*` 规则不适用；每次结果都有显式 LIMIT 和 HTTP
  `max_result_rows` 上限。

## 11. 常见错误

### anchor Trace 不匹配

```text
The anchor Trace has no matching File Not Found candidate...
```

检查 project、user、时间范围以及 anchor 是否真的存在
`dependency_or_env_error/file_not_found`。

### 数量不足 20

查看 `manifest.json.selection` 和 `manifest.json.skipped_candidates`。常见原因是时间范围内
不足 20 条、failure Observation 不在对应 Trace、Trace 已删除或表字段映射不正确。

### 找不到 project_id

必须通过 `source.work.json`、`--project-id` 或 `AGENTDEBUG_PROJECT_ID` 提供。脚本不会为
方便而执行跨项目扫描。

### 连接失败

确认使用 HTTP 8123 endpoint，而不是 native 9000 migration URL，并确认 Windows 工作
环境能访问 ClickHouse 防火墙端口。

## 12. 交付前验证命令

仓库内无数据库测试：

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pytest -q tests\test_work_environment_trace_batch.py
.\.venv\Scripts\python.exe -m pytest -q `
  tests\test_work_environment_trace_batch.py `
  tests\test_langfuse_file_not_found_production.py `
  tests\test_dataset_annotation_tool.py
```

工作环境必须另外执行一次第 5 节 `--prepare-only` 命令，并核对终端中的
`selected_trace_count/loaded_trace_count/annotation_case_count` 都符合预期。当前仓库测试
不连接工作环境数据库，因此不能把本地通过表述为真实生产字段已经验收。
