# AgentDebugX File Not Found 失败归因项目：对话汇总与续接文档

> 更新时间：2026-08-14  
> 仓库：`/Users/hanyizhou/project_vscode/langfuse`  
> 分支：`mac-dev`  
> AgentDebugX 目录：`agentdubugx/AgentDebugX-main`  
> 用途：供后续新对话快速恢复上下文，避免重新梳理整个历史对话。

## 1. 一句话现状

项目已经从“研究 Langfuse Trace/Observation 数据链路”推进到一个可运行的 File Not Found 失败归因 MVP：支持离线数据集和生产 ClickHouse 三表输入、确定性语义门控和根因归因、可选公共 LLM 增强、极长 Trace 压力实验、前端展示、当前 Trace 边界参考链，以及交付给工作环境 Agent 的标注工具增强提示文档。

当前最重要的下一步不是继续扩充合成数据，而是：

1. 在工作环境升级人工标注工具；
2. 用真实生产候选完成约 50 条高质量 Annotation；
3. 冻结测试集；
4. 在真实数据上重新评测确定性基线与 LLM 增强方案。

## 2. 用户已经明确的约束和决策

后续对话应直接继承以下决策，不要重新询问或擅自改变：

- 当前聚焦 Tool 的 `file not found`，暂不覆盖所有 Tool 错误类型。
- 生产数据来源是 Langfuse `traces`、`observations` 和关键字初筛后的 `tool_error` 表。
- `tool_error` 只是候选召回，不是人工真值。
- 用户负责在真实生产数据上完成约 50 条人工标注。
- AgentDebugX 代码和 Langfuse 数据转换/读取工具应能脱离 Langfuse 原生工作目录部署。
- MVP 可以不依赖 LLM；LLM 是可选增强层。
- 如果使用公共模型，只允许发送符合组织策略的脱敏字段。
- 当前不实现跨 Trace 根因追踪。
- 对根因发生在上一条 Trace 的案例，只输出当前 Trace 内可验证的参考信息，不伪造根因节点。
- 长 Trace 数据集不能让所有错误都固定在尾部；该问题已列为后续数据集 TODO，尚未实施。
- 初次人工标注页面不应显示 DebugX/LLM 预测，避免锚定偏差；裁决阶段才可对比。
- 开发工作要求小步提交、补充单元测试并实际运行验证；前端改动需要浏览器验收。

## 3. 核心概念已经统一

整个项目采用以下分层，不应再把这些概念混在一个“根因”字段中：

### 3.1 技术错误

回答：是否真的观察到了 File Not Found？

可能结果：

```text
confirmed | rule_false_positive | insufficient_evidence
```

例如 Tool 成功返回的文档正文中包含“file not found”，属于规则误报，不是 Tool 失败。

### 3.2 语义结果

回答：这个技术错误是否构成 Agent 或最终任务失败？

必须允许三态：

```text
true | false | unknown(null)
```

典型语义包括预期探测、控制流信号、已恢复失败、容忍失败、真实意外失败和未知。

### 3.3 错误显现节点

回答：错误在哪里客观出现？

通常是 Tool Result，但可以包含：

- `failure_onset_observation_id`；
- `primary_failure_observation_id`；
- `failure_observation_ids`；
- `downstream_symptom_observation_ids`。

错误显现节点不等于真正责任节点。

### 3.4 根因节点

回答：当前 Trace 中哪个节点真正引入了决定性的坏参数或失败条件？

根因可能来自 user、model、skill、upstream、runtime、environment、tool 或 external dependency。证据不足时必须输出 unknown。

### 3.5 当前 Trace 参考链

当真正根因位于当前 Trace 之前时，输出：

- `root_cause_scope=outside_current_trace`；
- `root_cause_observation_id=null`；
- `earliest_local_evidence_observation_id`；
- `local_trigger_observation_id`；
- `propagation_observation_ids`；
- 独立的 `reference_confidence`。

这些字段用于帮助 Agent 用户排查，不代表完成了跨 Trace 追踪。

## 4. 对话推进过程

### 4.1 Langfuse 数据链路与 AgentDebugX 原生能力分析

最初遍历了 Langfuse Trace/Observation 相关数据链路，并静态审计 AgentDebugX 的 Detect、Attribute、Diagnose、Recover、Rerun 和 GUI RCA 能力。

结论：AgentDebugX 原生归因器可以作为基础，但默认 heuristic 更接近“最早被规则发现的异常”，不能直接等价为严格因果根因。针对历史 Langfuse Tool 数据，需要专门的转换、语义门控和路径来源证据。

详见：[AgentDebugX 失败归因实现分析](./agentdebugx-failure-attribution-analysis.md)。

### 4.2 数据集设计

数据集被定义为：

```text
数据 = Trace + Observation 树 + Case 候选
标签 = 技术错误审核 + 语义结果 + 错误节点 + 根因/范围 + 证据 + 置信度
```

关键原则：

- 原始数据和人工 Oracle 分文件保存；
- 同一 Trace 可产生多个 Case；
- 所有 ID 使用 Observation ID，不使用数组下标或 step 作为稳定主键；
- 规则误报、预期缺失、已恢复失败和 unknown 是必须保留的负例；
- 同一 Trace 的 Case 必须进入同一数据集 split，避免泄漏；
- 真实生产分布和为了覆盖稀有类型的 oversample 必须分别记录。

相关文档：

- [失败归因测试数据集计划](./failure-attribution-test-dataset-plan.md)
- [File Not Found 生产 Trace MVP 计划](./file-not-found-production-trace-mvp-plan.md)
- [Tool 失败归因特性说明](./tool-failure-attribution-feature-spec.md)

### 4.3 File Not Found MVP 收敛

在 1–2 周和有限人力下，将第一版失败模式缩小为：

- 规则误报或预期不存在；
- 用户提供错误路径；
- 模型生成/臆造错误路径；
- 上游/Skill 提供旧路径；
- 运行环境中路径不可用；
- 错误后恢复；
- 证据冲突或不足，返回 unknown。

结论是：不引入 LLM 也能实现有价值的 MVP，但 Langfuse 历史数据转换和 DebugX 内部都需要增强。

## 5. 已经实现的代码能力

代码位于：

```text
agentdubugx/AgentDebugX-main/src/agentdebug/integrations/langfuse_attribution/
```

### 5.1 历史 Langfuse 转换

主要文件：

- `converter.py`
- `historical_converter.py`
- `models.py`
- `path_evidence.py`
- `trace_input.py`

已实现：

- 把 Langfuse Observation 转为 detector-safe AgentTrajectory；
- 路径和 provenance 证据与 detector event 分离；
- 从历史数据重建 Tool Failure evidence；
- 支持 `path`、`file`、`filename`、`file_path`、`filePath`；
- 支持 `function.arguments` 为 JSON 字符串；
- Windows 路径按 Windows 语义比较，不依赖运行 DebugX 的宿主 OS；
- Trace 输入保留 `user/assistant/system` 角色；
- 不再把历史 assistant 上下文统一伪装成 `user.request`。

### 5.2 确定性语义门控和归因

主要文件：

- `failure_gate.py`
- `attributor.py`
- `pipeline.py`
- `trace_reference.py`

已实现：

- File Not Found 技术错误确认；
- 存在性探测等语义负例；
- File Not Found 后同路径成功创建的恢复/控制流识别；
- user/model/skill/upstream/runtime 等路径来源归因；
- 证据冲突或不足时返回 unknown；
- current-trace reference；
- `root_cause_scope=outside_current_trace` 边界保护；
- 真正根因在 Trace 外时，不把入口 LLM 或本地 Tool Call 触发节点强行当根因。

### 5.3 数据集运行和评测

主要文件：

- `evaluation.py`
- `cli.py`
- `presentation.py`

支持标准四文件：

```text
traces.jsonl
observations.jsonl
cases.jsonl
annotations.jsonl（可选）
```

输出：

```text
predictions.jsonl
trajectories.jsonl
report.json（存在 Annotation 时）
```

### 5.4 生产三表直接运行

主要文件：`production.py`。

已经支持从工作环境直接读取：

- `traces`；
- `observations`；
- `tool_error`；
- 可选人工 `annotations.jsonl`。

入口示例：

```bash
agentdebug langfuse-fnf doctor --config /path/to/source.json

agentdebug langfuse-fnf snapshot /data/fnf-v1 \
  --config /path/to/source.json \
  --limit 50 \
  --rule-id file-not-found-v1

agentdebug langfuse-fnf run /data/fnf-results \
  --config /path/to/source.json \
  --annotations /data/fnf-v1/annotations.jsonl
```

当前连接器使用 ClickHouse HTTP，凭证只从 `AGENTDEBUG_CLICKHOUSE_*` 环境变量读取。代码不依赖 Langfuse 原生源码目录。

生产部署说明：[AgentDebugX File Not Found 生产部署](../agentdubugx/AgentDebugX-main/docs/PRODUCTION_LANGFUSE_FNF.md)。

### 5.5 可选 LLM 增强

主要文件：

- `llm_attributor.py`
- `long_dataset.py`
- `experiment.py`

设计策略：

- 默认只把确定性 `unknown` 交给 LLM；
- 只发送完整的 `*_redacted` 数据契约；
- 最多召回 64 个因果相关 Observation；
- failure/root/evidence ID 必须存在；
- label/domain、decision、failure flag 必须一致；
- 路径来源冲突时禁止 LLM 强制归因；
- 当前 Trace 边界外的根因不能被 LLM 改写成当前 Trace 根因；
- 校验失败或请求失败时回退确定性结果。

## 6. LLM 极长 Trace 实验结果

合成实验包含：

- 12 条 Trace；
- 9624 个 Observation；
- 每条约 801–803 个节点；
- 每条只有 1–3 个关键因果节点；
- 多种正例、负例、恢复和冲突场景。

结果：

| 指标 | 确定性基线 | 仅复核 unknown | 全量 LLM |
| --- | ---: | ---: | ---: |
| 失败原因标签正确 | 3/6 | 6/6 | 6/6 |
| 根因 Observation 正确 | 3/6 | 6/6 | 6/6 |
| 语义负例正确 | 2/4 | 3/4 | 4/4 |
| 负例被强制归因 | 1 | 1 | 0 |
| 正确拒答 | 2/2 | 2/2 | 2/2 |

结论：LLM 的价值是补充跨节点语义和因果关系，不是替代规则。当前推荐“规则优先 + LLM 复核 unknown”。

限制：这些是合成数据结果，不是生产准确率。必须在约 50 条真实人工标注 Case 上重跑。

详见：[File Not Found LLM 增强实验](./file-not-found-llm-enhancement-experiment.md)。

## 7. 关键真实案例及最终结论

用户提供的案例结构：

- 当前 Trace 初始 LLM 输入已经包含上一阶段 assistant 上下文；
- assistant 上下文提到 Worker 5 报告缺失，并已经包含目标脚本路径；
- 16.56s LLM 再次产生两个 `read` Tool Call；
- 8m30s `tool:read` 对脚本路径返回 File Not Found；
- 另一个 Spec read 成功；
- 下游仍有大量 LLM、glob、read、grep，最终任务结果不清楚；
- 真正背景来源早于当前 Trace。

确定性输出应为：

```json
{
  "decision": "unknown",
  "semantics": "unknown",
  "is_agent_failure": null,
  "failure_observation_id": "<failed-read-id>",
  "root_cause_observation_id": null,
  "root_cause_label": null,
  "root_cause_scope": "outside_current_trace",
  "earliest_local_evidence_observation_id": "<initial-llm-id>",
  "local_trigger_observation_id": "<16.56s-llm-id>",
  "propagation_observation_ids": [
    "<initial-llm-id>",
    "<16.56s-llm-id>",
    "<failed-read-id>"
  ],
  "reference_confidence": 0.95
}
```

解释：

- 失败 Tool 是错误显现节点；
- 16.56s LLM 是当前 Trace 的本地触发节点；
- 初始 LLM 是当前 Trace 最早可见证据；
- 二者都不是已证明的真正根因；
- 因为不做跨 Trace 追踪，所以 root 为空；
- 因为下游结果不明确，所以 Agent/任务失败保持 unknown。

## 8. 前端已经实现和验证

AgentDebugX 前端 Tool Attribution 页面已经增加：

- failure observation；
- root cause observation/type；
- root cause scope；
- earliest local evidence；
- local trigger；
- reference chain；
- reference confidence；
- reason codes 和可点击 Observation 链接；
- deterministic 模式显示“Model context: Not applicable”。

相关代码：

- `agentdubugx/AgentDebugX-main/src/agentdebug/inspect/ui/views.py`
- `agentdubugx/AgentDebugX-main/src/agentdebug/integrations/langfuse_attribution/presentation.py`

最近一次浏览器验收：

- 复现 Trace：23 个事件、1 个错误；
- 桌面端通过；
- 390px 移动端无横向溢出；
- 页面控制台无错误；
- 三个参考节点均可点击跳转。

本地演示地址（仅当前机器和服务仍运行时有效）：

- Trace：`http://127.0.0.1:8766/trace/trace-file-not-found-cross-trace-reference`
- Attribution：`http://127.0.0.1:8766/trace/trace-file-not-found-cross-trace-reference/tool-attribution`
- 临时 SQLite：`/private/tmp/agentdebugx-live-demo.sqlite`

该 Trace 是根据用户提供内容构造的复现实例，因为真实 `observation_id/trace_id/project_id` 被隐藏，并非生产原记录。临时服务和 `/private/tmp` 数据不能视为持久交付物。

## 9. 标注工具现状与增强方案

用户在工作环境已有独立标注工具，本地仓库中没有其源码。截图显示现有页面至少具备：

- Case ID；
- 有问题的 Observation ID；
- 从列表选择问题节点；
- 失败根因节点 ID；
- 从列表选择根因节点；
- 失败推断原因；
- 证据 Observation IDs；
- 总体置信度；
- 备注；
- 保存和重置。

当前问题是：技术错误、任务语义、错误显现、根因和证据混在同一长表单中，且无法表达 `outside_current_trace`。

已经产出可直接交给工作环境 Agent 的完整提示文档：

[失败归因标注工具增强提示](./annotation-tool-enhancement-agent-prompt.md)

建议完整功能拆分：

```text
完整方案：38 个可独立开发/验收功能点
MVP 核心：18 个
效率与质量增强：12 个
数据集治理与规模化：8 个
```

MVP 重点：

- 技术错误审核；
- 规则误报纠错；
- Agent/任务失败三态；
- 恢复状态；
- 错误节点多选；
- 根因范围；
- 当前 Trace 根因；
- earliest evidence/local trigger/propagation chain；
- 分层置信度；
- 服务端校验；
- Annotation revision；
- JSONL 导出。

## 10. 最新测试与代码状态

最后一次完整测试使用 AgentDebugX 自己的虚拟环境：

```bash
cd agentdubugx/AgentDebugX-main
PYTHONPATH=src .venv/bin/python -m pytest -q -rs
```

结果：

```text
294 passed, 2 skipped in 2.01s
```

跳过原因：

- 环境不允许测试监听本地 socket；
- 可选依赖 `pyarrow` 未安装。

静态检查：

```text
Ruff: All checks passed!
Mypy: Success: no issues found in 10 source files
```

在创建本文之前，工作区为 clean。本文只包含对话续接文档，不包含代码改动，并作为独立 docs commit 提交。

## 11. 关键提交记录

从 AgentDebugX 引入到当前阶段的主要提交：

| Commit | 内容 |
| --- | --- |
| `cde55356d` | 导入 AgentDebugX |
| `27db7f855` | Langfuse-DebugX 初步转换器和前端小组件 |
| `33f663af8` | 重建历史 Langfuse Tool evidence |
| `b58870429` | File Not Found 语义门控 |
| `31c77fc8a` | 确定性 File Not Found pipeline |
| `32fb822bc` | 数据集评测 |
| `27b96c2f8` | 离线 File Not Found 数据集运行 |
| `e4cd4133e` | Trace input 路径来源 |
| `f24e23b4f` | 前端展示 File Not Found 归因 |
| `c4c13668e` | LLM File Not Found 归因 |
| `34f808846` | 极长 Trace 数据集 |
| `17a7228c9` | LLM 归因实验入口 |
| `c284e0176` | 修复数据集模块预加载 |
| `520aefa75` | LLM 增强实验报告 |
| `621ebf30c` | 记录长 Trace 位置偏置 TODO |
| `8a4e83647` | 从生产表直接运行归因 |
| `379ed1b97` | 生产部署指南 |
| `a51f72699` | 兼容预筛选 tool_error 输出 |
| `2e60e72cd` | 当前 Trace 参考链和边界语义 |
| `093747e72` | deterministic 前端上下文显示修正 |
| `c1f44c5ce` | 标注工具增强提示文档 |

## 12. 明确的边界和未完成事项

### 12.1 当前不支持

- 跨 Trace 自动根因追踪；
- 从生产运行环境验证文件是否曾真实存在；
- 根据一个 File Not Found 自动证明最终任务失败；
- 自动区分所有自然语言中的预期缺失；
- 将 LLM confidence 当成校准后的概率；
- 直接在本地修改工作环境的标注工具，因为其源码不在当前仓库。

### 12.2 长 Trace 数据集 TODO

现有合成长 Trace 的关键因果链偏向尾部，只能证明长噪声前缀下的上下文压缩能力。

下一版需要：

- 错误位置覆盖前部、中部和尾部；
- 根因到错误距离覆盖相邻、20–100、300–800 节点；
- 错误后保留 100–500 个节点；
- 同一 Trace 放入多个技术错误；
- 区分预期错误、已恢复错误和真正致命错误；
- 记录 `failure_position_ratio`、`root_failure_distance`、`downstream_observation_count`、`technical_error_count`、`causal_error_index`；
- 分桶评测位置鲁棒性和候选召回率。

### 12.3 生产落地仍需确认

- 工作环境实际 `tool_error` 表字段映射；
- ClickHouse endpoint 和组织允许的鉴权方式；
- `payload_contract` 是 raw 还是 redacted；
- 真实 Trace 是否可能超过当前响应大小；
- 标注工具的数据库、前端框架、权限系统和发布流程；
- 真实 Annotation v1 数据量和迁移方式。

## 13. 推荐的下一阶段顺序

### Phase A：工作环境标注工具

把 [标注工具增强提示](./annotation-tool-enhancement-agent-prompt.md) 完整交给工作环境 Agent，要求先分析现有代码和数据库，再实现 18 个 MVP 功能点。

首期验收必须覆盖本文第 7 节的 Trace 外根因案例。

### Phase B：真实 50 条标注

建议：

- 前 30 条作为开发集；
- 后 20 条冻结为测试集；
- 同一 Trace 不跨 split；
- 保留自然分布和 coverage oversample 标记；
- 至少覆盖规则误报、预期缺失、用户路径、模型路径、已恢复和 unknown。

### Phase C：真实数据评测

至少比较：

1. 关键字规则；
2. DebugX 确定性 pipeline；
3. 规则优先 + LLM 复核 unknown；
4. 选择性复核高风险确定性结论。

指标：

- 技术错误审核准确数；
- 语义负例正确数和强制归因数；
- 根因 label 准确数；
- root Observation Top-1；
- unknown 正确拒答和错误拒答；
- root retrieval recall；
- 前/中/尾位置分桶准确率；
- 单 Case token、延迟和成本。

## 14. 新对话可直接使用的启动提示

复制下面内容到新对话：

```text
请先阅读以下续接文档，不要从头重新分析项目：

/Users/hanyizhou/project_vscode/langfuse/docs/conversation-handoff-agentdebugx-file-not-found.md

当前项目聚焦基于 Langfuse traces、observations、tool_error 和人工 Annotation 的 Tool File Not Found 失败归因。AgentDebugX 的离线/生产表 pipeline、确定性门控、可选 LLM 增强、前端归因展示和 Trace 边界参考链已经完成。当前不做跨 Trace 根因追踪。

请先检查文档中的当前分支、提交、测试和明确边界，然后继续以下任务：
<在这里填写下一项具体任务>

继续保持小步提交、补充测试、实际运行验证；如果涉及前端，完成真实浏览器验收。不要把合成长 Trace 的实验结果描述成生产准确率。
```

如果下一项任务是在工作环境开发标注工具，还应同时提供：

```text
/Users/hanyizhou/project_vscode/langfuse/docs/annotation-tool-enhancement-agent-prompt.md
```

工作环境 Agent 应使用提示文档中的 v2 Annotation、Trace 边界案例、服务端校验、迁移和测试要求，但必须先根据工作环境实际技术栈调整实现路径。

## 15. 相关图片

本轮对话引用过以下本地图片：

- `/Users/hanyizhou/Downloads/IMG_2165.HEIC`：早期数据集构建方式讨论；
- `/Users/hanyizhou/Downloads/IMG_2173.HEIC`：Worker 5 File Not Found Trace 结构；
- `/Users/hanyizhou/Downloads/IMG_2174.HEIC`：工作环境现有标注工具页面。

图片只是上下文材料，关键结论已经结构化写入本文和相关设计文档，后续对话不必依赖图片才能继续。
