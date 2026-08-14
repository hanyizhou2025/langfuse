# 失败归因标注工具增强：交付工作环境 Agent 的完整提示文档

> 使用方式：将本文从“任务提示词”开始完整交给工作环境中的开发 Agent。该 Agent 可以读取标注工具源码、数据库结构以及工作环境中的 Langfuse Trace/Observation 数据，但不能假设它能访问本地 AgentDebugX 工作目录。

## 任务提示词

你需要增强工作环境中现有的“Langfuse 数据标注工具”，使其能够为 Tool `file not found` 失败归因数据集提供稳定、可复核、可导出的人工标签。

这不是新建一个演示页面。请先遍历现有标注工具代码、路由、持久化结构和测试，在保留现有功能与历史数据兼容性的前提下完成增量改造。实现过程中小步提交，每个逻辑改动需要对应自动化测试，最后必须启动真实页面进行浏览器验收。

### 一、业务背景

当前可用数据包括：

- Langfuse `traces` 表；
- Langfuse `observations` 表；
- `tool_error` 候选表：由 `file not found`、`ENOENT`、`no such file or directory` 等关键字规则从 Tool Observation 中初筛得到；
- 人工标注结果；
- 一个已有标注页面，目前至少包含：
  - Case ID；
  - 有问题的 Observation ID；
  - 失败根因节点 ID；
  - 失败推断原因；
  - 证据 Observation IDs；
  - 总体置信度；
  - 备注；
  - “从列表选择”“保存标注”“重置表单”。

关键字命中只代表“观察到了技术错误文本”，不等于 Agent 或任务失败，也不等于命中 Tool 节点就是真正根因。标注流程必须明确区分以下问题：

1. 是否真的发生了技术错误，还是规则误报？
2. 这个技术错误是否构成 Agent 失败或最终任务失败？
3. 错误在哪些节点显现？
4. 真正责任节点是否存在于当前 Trace？
5. 如果真正根因不在当前 Trace，当前 Trace 内还能提供哪些可靠参考节点？

### 二、必须正确处理的典型案例

一个 Tool `read` Observation 返回：

```text
Error: File not found: D:\...\test_largeamountexposure_limit_index_config.py
```

当前 Trace 中：

- 初始 LLM Observation（约 8.03s）的输入已经包含历史 `assistant` 上下文；该上下文中已经出现“Worker 5 报告缺失”和相关文件路径；
- 后续 LLM Observation（约 16.56s）再次输出 `read` Tool Call，并把该路径放入 `filePath` 参数；
- `tool:read` Observation（约 8m30s）返回 File Not Found；
- 错误后仍有大量 `glob/read/grep/LLM` 节点，当前 Trace 不能仅凭关键字确认任务最终失败，也不能确认是否完成恢复；
- 真正引入错误背景的过程发生在上一条 Trace，但本项目当前不实现跨 Trace 根因追踪。

这个案例的人工标签应表达为：

- 技术错误：确认发生；
- 错误显现节点：失败的 `tool:read`；
- 当前 Trace 内最早证据：初始 8.03s LLM；
- 当前 Trace 内本地触发节点：16.56s LLM；
- 真正根因范围：当前 Trace 外；
- 当前 Trace 根因节点：不填写，禁止把初始 LLM 或 16.56s LLM强行标成真正根因；
- Agent 是否失败：未知；
- 最终任务是否失败：未知；
- 恢复状态：未知；
- 证据链：初始 LLM → 触发 LLM → 失败 Tool；
- 该样本应进入人工复核队列。

### 三、设计原则

实现必须遵守以下原则：

1. **事实、判断、归因分层**：技术错误、任务结果、错误节点、根因节点和参考节点分别保存，不能共用一个字段替代。
2. **允许未知和拒绝归因**：未知是合法标签，不得为了填满表单强制选择根因。
3. **根因范围先于根因节点**：只有 `root_cause_scope=current_trace` 时才允许选择当前 Trace 根因节点。
4. **跨 Trace 只标边界，不追踪**：可以标记 `outside_current_trace`，但不能查询上一条 Trace 或伪造跨 Trace 根因 ID。
5. **节点 ID 必须来自当前快照**：所有错误、根因、恢复、证据和参考节点都必须属于该 Case 的 `project_id + trace_id`。
6. **树上选择优先**：不要求标注者手工复制长 Observation ID；保留手工输入仅作为兼容或高级入口。
7. **不向标注者展示模型预测**：初次标注阶段隐藏 DebugX/LLM 预测，防止锚定偏差；裁决阶段可单独对比。
8. **原始数据与标签分离**：Trace/Observation/Case 数据和人工 Annotation 分开保存、分开导出。
9. **版本化而非覆盖**：每次保存生成 Annotation revision，保留标注人、时间、旧值和修改原因。

### 四、目标页面结构

将当前单一长表单增强为“上下文 + Observation 树 + 分步标签”工作台：

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Case：状态、规则版本、命中字段、Tool 名称、配对状态、分配人、快照时间 │
├───────────────────────────┬──────────────────────────────────────────┤
│ Observation 树            │ 当前节点详情                             │
│ 搜索/类型/错误过滤         │ name/type/time/level/parent              │
│ [F] 错误显现              │ input/output/status（按权限展示/脱敏）    │
│ [R] 当前 Trace 根因        │ 与失败 Tool 的父子/时间/参数关系         │
│ [E] 证据                  │                                          │
│ [C] 最早上下文证据         │                                          │
│ [T] 本地触发              │                                          │
├───────────────────────────┴──────────────────────────────────────────┤
│ ①技术错误 → ②语义结果 → ③错误显现 → ④归因范围 → ⑤根因/参考链      │
├──────────────────────────────────────────────────────────────────────┤
│ 校验信息 / 保存草稿 / 提交标注并打开下一条 / 重置未保存修改          │
└──────────────────────────────────────────────────────────────────────┘
```

### 五、标注步骤与字段

#### Step 1：技术错误审核

字段：

```ts
technical_error_review: {
  human_label: "confirmed" | "rule_false_positive" | "insufficient_evidence";
  correction_reason?:
    | "successful_content_mentions_error"
    | "expected_text_assertion"
    | "log_or_documentation_only"
    | "wrong_observation_type"
    | "duplicate_candidate"
    | "other";
  comment?: string;
  confidence: "high" | "medium" | "low";
}
```

交互要求：

- 根据规则候选默认预填 `confirmed`，但必须明显提示“规则预填，未经人工确认”；
- 选择 `rule_false_positive` 时必须填写纠错原因；
- 规则误报时隐藏并清空失败节点、根因和恢复相关字段；
- `insufficient_evidence` 自动进入复核队列。

#### Step 2：Tool 结果和任务语义

字段：

```ts
semantic_outcome: {
  tool_result_semantics:
    | "unexpected_failure"
    | "expected_negative_result"
    | "validation_probe"
    | "control_flow_signal"
    | "recovered_failure"
    | "tolerated_failure"
    | "user_requested_negative_test"
    | "external_failure"
    | "unknown";
  is_agent_failure: true | false | null;
  is_task_failure: true | false | null;
  recovery_status: "recovered" | "not_recovered" | "unknown";
  recovery_observation_id?: string;
  outcome_reason?: string;
  confidence: "high" | "medium" | "low";
}
```

这里必须使用三态值。`null` 表示不知道，不能用 `false` 代替未知。

交互要求：

- Tool 技术错误可以成立，但 Agent/任务失败仍可为未知；
- `recovery_status=recovered` 时必须选择恢复节点；
- 选择 `validation_probe/control_flow_signal/expected_negative_result` 时，默认将 Agent/任务失败设为 `false`，标注者仍可修改并填写原因；
- Trace 上下文不完整时展示醒目的 `context_truncated` 警告。

#### Step 3：错误显现节点

字段：

```ts
failure_manifestation: {
  failure_onset_observation_id: string;
  primary_failure_observation_id: string;
  failure_observation_ids: string[];
  downstream_symptom_observation_ids: string[];
}
```

定义：

- `failure_onset_observation_id`：当前 Trace 中错误第一次客观显现的位置；
- `primary_failure_observation_id`：本 Case 的主要失败 Tool/Result 节点；
- `failure_observation_ids`：同一次失败尝试中的全部错误节点；
- `downstream_symptom_observation_ids`：由该错误引发的后续症状，不是根因。

当前页面的“有问题的 Observation ID”应迁移为以上结构，保留旧值到 `primary_failure_observation_id`，但不能继续只存一个模糊字段。

#### Step 4：归因适用性和根因范围

字段：

```ts
attribution: {
  applicable: boolean;
  root_cause_scope:
    | "current_trace"
    | "outside_current_trace"
    | "unknown";
  primary_root_cause_observation_id?: string;
  root_cause_observation_ids: string[];
  root_cause_domain?:
    | "user"
    | "model"
    | "skill"
    | "upstream"
    | "runtime"
    | "environment"
    | "tool"
    | "external_dependency"
    | "unknown";
  root_cause_label?:
    | "user_path_invalid"
    | "model_path_hallucination"
    | "upstream_path_invalid"
    | "runtime_path_unavailable"
    | "tool_contract_mismatch"
    | "external_dependency_failure"
    | "unknown";
  path_source_kind?:
    | "user"
    | "assistant_history"
    | "model"
    | "skill"
    | "system"
    | "environment"
    | "unknown";
  path_source_observation_id?: string;
  confidence: "high" | "medium" | "low";
}
```

校验规则：

- `root_cause_scope=current_trace`：`applicable=true`，必须选择 primary root、根因节点列表、原因域、原因标签和证据；
- `root_cause_scope=outside_current_trace`：`applicable=false`，当前 Trace 根因字段必须为空；
- `root_cause_scope=unknown`：允许 `applicable=false`，根因字段必须为空；
- `is_agent_failure=false` 时，默认 `applicable=false`；
- 不允许把错误显现 Tool 节点自动复制成根因节点；
- 如果路径只出现在当前 LLM 的历史 `assistant` 输入中，`path_source_kind=assistant_history`，不得标成当前模型新生成。

当前页面的“失败根因节点 ID”只在 `current_trace` 分支显示。选择 `outside_current_trace` 时，页面应显示：

> 当前 Trace 不包含可验证的真正根因。请标注当前 Trace 参考链，不要强制选择根因节点。

#### Step 5：当前 Trace 参考链

新增字段：

```ts
current_trace_reference: {
  earliest_local_evidence_observation_id?: string;
  local_trigger_observation_id?: string;
  propagation_observation_ids: string[];
  evidence_observation_ids: string[];
  boundary_reason?:
    | "assistant_context_at_trace_entry"
    | "system_context_at_trace_entry"
    | "upstream_reference_without_source"
    | "trace_context_truncated"
    | "other";
  reference_summary: string;
  confidence: "high" | "medium" | "low";
}
```

定义：

- 最早本地证据：当前 Trace 中第一次能看到相关错误路径或错误背景的节点；
- 本地触发节点：直接产生失败 Tool Call 的当前 Trace 节点；
- 传播链：从最早本地证据到 Tool 报错的有序节点列表；
- 这些节点是“参考”，不等同于真正根因。

页面应以有序链方式展示，并允许从 Observation 树点击追加或调整顺序。

#### Step 6：证据、说明和置信度

将现有“失败推断原因”拆分为结构化字段和自由说明：

```ts
review: {
  evidence_observation_ids: string[];
  reasoning_summary: string;
  limitations: string[];
  overall_confidence: "high" | "medium" | "low";
  notes?: string;
}
```

要求：

- 技术错误、语义结果、归因、参考链各自有置信度；
- `overall_confidence` 保留用于兼容现有页面，但不得替代分层置信度；
- `reasoning_summary` 描述“为什么这样标”，不要复制整段原始 Trace；
- `limitations` 至少支持：`root_outside_trace`、`downstream_outcome_unclear`、`context_truncated`、`tool_call_pairing_ambiguous`、`runtime_state_unavailable`。

### 六、完整 Annotation 建议结构

```json
{
  "schema_version": "failure-attribution-annotation-v2",
  "case_id": "case-id",
  "project_id": "project-id",
  "trace_id": "trace-id",
  "annotation_revision": 1,
  "annotation_status": "draft",
  "technical_error_review": {},
  "semantic_outcome": {},
  "failure_manifestation": {},
  "attribution": {},
  "current_trace_reference": {},
  "review": {},
  "source_snapshot": {
    "snapshot_at": "ISO-8601",
    "observation_count": 0,
    "context_truncated": false,
    "source_updated_at": "ISO-8601"
  },
  "audit": {
    "annotator_id": "internal-user-id",
    "created_at": "ISO-8601",
    "updated_at": "ISO-8601",
    "change_reason": null
  }
}
```

`annotation_status` 至少支持：

```text
draft | submitted | needs_review | adjudicated | excluded | frozen
```

### 七、典型案例的标准标注示例

请将下列内容加入测试 Fixture。Observation ID 使用工作环境实际 ID 替换：

```json
{
  "schema_version": "failure-attribution-annotation-v2",
  "case_id": "worker-5-file-not-found",
  "project_id": "<known-project-id>",
  "trace_id": "<known-trace-id>",
  "annotation_revision": 1,
  "annotation_status": "needs_review",
  "technical_error_review": {
    "human_label": "confirmed",
    "confidence": "high",
    "comment": "read Tool 明确返回 File not found。"
  },
  "semantic_outcome": {
    "tool_result_semantics": "unexpected_failure",
    "is_agent_failure": null,
    "is_task_failure": null,
    "recovery_status": "unknown",
    "outcome_reason": "错误后仍有大量节点，当前 Trace 无法确认最终任务结果。",
    "confidence": "low"
  },
  "failure_manifestation": {
    "failure_onset_observation_id": "<tool-read-error-id>",
    "primary_failure_observation_id": "<tool-read-error-id>",
    "failure_observation_ids": ["<tool-read-error-id>"],
    "downstream_symptom_observation_ids": []
  },
  "attribution": {
    "applicable": false,
    "root_cause_scope": "outside_current_trace",
    "root_cause_observation_ids": [],
    "path_source_kind": "assistant_history",
    "path_source_observation_id": "<initial-llm-id>",
    "confidence": "high"
  },
  "current_trace_reference": {
    "earliest_local_evidence_observation_id": "<initial-llm-id>",
    "local_trigger_observation_id": "<16.56s-llm-id>",
    "propagation_observation_ids": [
      "<initial-llm-id>",
      "<16.56s-llm-id>",
      "<tool-read-error-id>"
    ],
    "evidence_observation_ids": [
      "<initial-llm-id>",
      "<16.56s-llm-id>",
      "<tool-read-error-id>"
    ],
    "boundary_reason": "assistant_context_at_trace_entry",
    "reference_summary": "文件路径在当前 Trace 初始 LLM 的历史 assistant 输入中已经存在；16.56s LLM 在当前 Trace 内再次把它传给 read；read 随后返回 File Not Found。真正来源早于当前 Trace。",
    "confidence": "high"
  },
  "review": {
    "evidence_observation_ids": [
      "<initial-llm-id>",
      "<16.56s-llm-id>",
      "<tool-read-error-id>"
    ],
    "reasoning_summary": "确认技术错误，但不把当前 Trace 的入口上下文或本地 Tool Call 触发节点当作真正根因。",
    "limitations": [
      "root_outside_trace",
      "downstream_outcome_unclear",
      "runtime_state_unavailable"
    ],
    "overall_confidence": "medium",
    "notes": "当前阶段不实现跨 Trace 追踪。"
  }
}
```

### 八、后端与持久化要求

1. 先识别现有存储。如果当前标签保存在关系数据库中，新增 v2 字段或 JSON 列时必须提供迁移；如果当前保存 JSON 文件，也要保持原子写入和 revision 历史。
2. 不修改 Langfuse `traces`、`observations` 和 `tool_error` 原表；这些表是只读数据源。
3. 标注记录必须至少以 `case_id + annotation_revision` 唯一。
4. 保存时再次从服务端验证所有 Observation ID，不信任前端校验。
5. 验证 Observation 属于同一 `project_id + trace_id`，防止跨项目引用。
6. Case 打开时记录 source snapshot；提交时如果源 Trace 已更新，提示标注者刷新或确认基于旧快照提交。
7. 保存草稿与提交标注使用不同操作；提交后默认只读，修改需创建新 revision。
8. 记录 `annotator_id`，不要由客户端任意传入可信身份。
9. 导出 `annotations.jsonl` 时每个 Case 只导出指定数据集版本绑定的有效 revision。
10. API 错误返回字段级错误码，前端不得只显示“保存失败”。
11. Tool 参数展示与辅助解析至少兼容 `path`、`file`、`filename`、`file_path`、`filePath`，以及 `function.arguments` 为 JSON 字符串的情况。
12. 保留 Trace 输入和 LLM 输入中的 `user/assistant/system` 消息角色；不能把历史 `assistant` 内容统一包装成 `user.request`。
13. 路径在某个 Observation 文本中出现，只能作为证据候选；不得仅凭字符串首次出现位置自动写入人工根因标签。

### 九、兼容旧页面和旧数据

必须提供兼容迁移：

| 旧字段 | v2 字段 |
| --- | --- |
| Case ID | `case_id` |
| 有问题的 Observation ID | `failure_manifestation.primary_failure_observation_id`，并加入 `failure_observation_ids` |
| 失败根因节点 ID | 只有旧值非空时迁移到 `attribution.primary_root_cause_observation_id` 和根因列表，迁移后标记 `root_cause_scope=current_trace`；不要为缺失值猜测 |
| 失败推断原因 | `review.reasoning_summary` |
| 证据 Observation IDs | `review.evidence_observation_ids` |
| 置信度 | `review.overall_confidence` |
| 备注 | `review.notes` |

迁移要求：

- 历史数据原文必须保留；
- 无法映射的新字段保持 `null/unknown`，禁止自动补成否定值；
- 记录 `migrated_from_schema_version`；
- 提供可重复执行且幂等的迁移脚本或启动迁移；
- 为至少一条旧完整标注、一条旧缺根因标注、一条规则误报标注编写迁移测试。

### 十、效率与质量增强

在核心正确性完成后实现以下功能：

- Case 队列：待标注、草稿、待复核、已提交、已排除；
- “保存并下一条”；
- 自动保存草稿，但提交仍需显式操作；
- 快捷键：`F` 标错误节点、`R` 标当前 Trace 根因、`E` 标证据、`C` 标最早上下文、`T` 标本地触发；
- Observation 搜索：ID、name、type、错误文本；
- 一键定位规则命中 Tool、其父 LLM、相邻 Tool Call 和后续节点；
- 多选节点后批量设置角色；
- 标注耗时、退回率、复核率统计；
- 两名标注者独立标注和裁决模式；
- 初次标注不展示自动归因结果，裁决页面可以并排显示但必须标明来源。

### 十一、必须实现的服务端校验

至少覆盖：

1. 规则误报不能提交失败、根因、恢复节点；
2. 技术错误确认后才能填写语义结果；
3. `recovered` 必须有恢复 Observation；
4. `current_trace + applicable=true` 必须有 primary root、根因列表、域、标签和证据；
5. `outside_current_trace` 不允许有当前 Trace 根因 ID；
6. `unknown` 不允许伪造根因；
7. propagation chain 必须包含失败节点，节点按 Trace 时间非递减；
8. earliest evidence、local trigger 和 failure 必须属于当前 Trace；
9. 所有 ID 必须存在于所选快照；
10. `is_agent_failure/is_task_failure=null` 时 Annotation 自动成为 `needs_review`，除非仍是 draft；
11. `confidence=low` 或 `context_truncated=true` 的提交进入复核队列；
12. 被冻结的数据集 revision 不可原地修改。

### 十二、测试要求

请先写失败测试，再实现功能。至少包括：

#### 单元测试

- 三态布尔的序列化和反序列化；
- `outside_current_trace` 禁止根因 ID；
- `current_trace` 要求根因字段完整；
- assistant 历史路径不被当成当前模型生成；
- 规则误报清除不适用字段；
- 恢复状态校验；
- Observation 跨 Trace/跨项目引用被拒绝；
- 旧标注迁移的幂等性。

#### API/集成测试

- 保存草稿、提交、创建 revision、读取历史；
- Trace 更新冲突；
- 大 Trace 的结构先加载、节点详情延迟加载；
- `context_truncated` 传播到 UI 和导出；
- 导出的 Annotation 与原始 Trace 数据分离。

#### 前端测试

- 五步表单的条件显示；
- 树上选择 F/R/E/C/T 后字段同步；
- outside 分支禁用根因选择并显示解释；
- 未满足校验时不能提交且能定位字段；
- 保存并下一条；
- 刷新后恢复草稿；
- 桌面主分辨率和窄屏不产生不可操作区域。

#### 关键验收 Fixture

必须使用“Worker 5 历史 assistant 上下文 → 16.56s LLM → 8m30s read File Not Found”案例。验收结果必须是：

```text
technical_error = confirmed
agent_failure = unknown
task_failure = unknown
root_cause_scope = outside_current_trace
primary_root_cause_observation_id = null
earliest_local_evidence = initial LLM
local_trigger = 16.56s LLM
primary_failure = failed read Tool
annotation_status = needs_review
```

### 十三、实施顺序

按以下顺序实施并分别提交：

1. 梳理现有代码、数据表、API 和旧标注字段，输出简短影响清单；
2. 增加 v2 Annotation 类型、枚举和服务端校验；
3. 增加持久化迁移与旧数据兼容；
4. 增加 Observation 树节点角色选择和分步表单；
5. 增加 current-trace reference 与 outside-trace 分支；
6. 增加 revision、草稿、提交和复核状态；
7. 增加 JSONL 导出和完整性校验；
8. 使用关键 Fixture 跑单测、集成测试和浏览器验收；
9. 更新 README/部署文档，给出迁移、回滚和数据备份步骤。

每次提交遵循 Conventional Commits，例如：

```text
feat(annotation): add layered failure labels
feat(annotation): support trace-boundary references
feat(annotation-ui): select evidence from observation tree
test(annotation): cover inherited assistant path case
docs(annotation): document v2 migration and export
```

### 十四、非目标

本轮不要实现：

- 跨 Trace 自动查询和根因追踪；
- 使用 LLM 自动替代人工标签；
- 修改 Langfuse 原生 Trace/Observation 数据；
- 将规则候选当成已确认真值；
- 在初次人工标注页面展示 DebugX 预测；
- 把所有未知值强制改成 `false` 或任意原因类别。

### 十五、最终交付物

完成后请提供：

1. 现有工具分析和改动范围；
2. 数据库迁移及回滚方式；
3. v2 Annotation 类型和 API 契约；
4. 更新后的标注页面；
5. 旧数据迁移结果；
6. `annotations.jsonl` 示例；
7. 自动化测试命令及完整摘要；
8. 浏览器验收截图或结果，覆盖典型案例、规则误报、已恢复、当前 Trace 根因、Trace 外根因和 unknown；
9. 所有提交的 hash 与说明；
10. 尚未解决的风险，不得把未验证能力描述为已完成。

最终汇报必须用证据说明完成情况：列出测试通过数量、跳过项原因、浏览器验证页面、迁移验证数量和工作区状态。
