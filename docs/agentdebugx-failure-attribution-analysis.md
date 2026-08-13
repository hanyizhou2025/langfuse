# AgentDebugX 失败归因实现分析

> 审计对象：agentdubugx/AgentDebugX-main。发行包名为 agentdebugx、Python 导入名为 agentdebug；本次静态审计对应版本 0.3.1。本文以当前源码的实际执行路径为准，不把 README、注释或设计路线图当作已落地能力。

## 结论摘要

AgentDebugX 的失败归因不是单一算法，而是围绕统一 Attributor 协议实现的一组“责任事件定位器”。主路径为：

~~~mermaid
flowchart LR
  A["采集 / 导入"] --> B["AgentTrajectory + AgentEvent"]
  B --> C["Detect：规则或 LLM Judge"]
  C --> D["FailureFinding[]"]
  B --> E["Attribute：Blame hypotheses"]
  D --> E
  E --> F["report.attribution.primary"]
  F --> G["DiagnoseContext"]
  G --> H["Recover：根因目标"]
  G --> I["Rerun：根因检查点"]
  B --> J["GUI RCA：独立 CUA ReAct"]
  J --> F
~~~

静态审计得到四个关键结论：

- 常规文本归因有启发式、整轨 LLM、逐步 LLM、二分 LLM、模拟反事实、SBFL 和集成器；核心实现在 src/agentdebug/diagnose/attribute/attribution.py。
- DeepDebug 是独立的高成本 Diagnose profile：全局读取、结构路由 MoE、候选仲裁、证据校验和修复建议一体化完成，而不是普通 Attribute 策略。
- GUI/OSWorld 走 vendored cua_debugger 的多模态 ReAct RCA；它不会复用常规 attribute/ 算法，只把结果映射回标准 DiagnosticReport。
- CLI 和 UI 实际只暴露 heuristic、all_at_once、step_by_step、binary_search、counterfactual。SBFL、Ensemble 和独立 aao_moe 是库 API，尚未接入常规选择面。

## 审计范围与文件地图

对 agentdubugx/AgentDebugX-main 全树中的 attribution、root cause、RCA、critical failure、mistake_step 等实现、配置、测试和文档进行关联追踪。图片、PDF、锁文件和无调用关系的示例未逐行分析。

| 层次 | 核心文件 | 实际职责 |
| --- | --- | --- |
| 轨迹 IR | src/agentdebug/schema/models.py | 定义 AgentEvent、AgentTrajectory、FailureFinding、DiagnosticReport。 |
| 捕获与导入 | src/agentdebug/ingest/recorder.py、ingest/adapters/* | 记录事件、step、错误、artifacts，或将外部框架轨迹归一化。 |
| Detect | diagnose/detect/analyzers.py、detect/judge.py、detect/rules/* | 将可见的失败症状转换成带位置的 FailureFinding。 |
| 常规 Attribute | diagnose/attribute/attribution.py | Blame / AttributionResult 协议和七类文本归因算法。 |
| 深度 Attribute | diagnose/attribute/moe.py、attribute/deep_memory.py、profiles/deepdebug.py | MoE 定位、记忆提示、证据验证和四阶段审计。 |
| 编排与下游 | diagnose/pipeline.py、diagnose/context.py、recover/recovery.py、rerun/workflow.py | 写入 attribution，提升根因目标，产生恢复/重跑上下文。 |
| 入口与 UI | cli/legacy.py、inspect/ui/routes.py、inspect/ui/views.py | CLI、REST、网页选项和 report 保存。 |
| GUI RCA | diagnose/gui_rca.py、cua_debugger/debugger/{rca,agent,dispatch}.py | 视觉轨迹反向 RCA 的独立实现。 |
| 注册与兼容 | diagnose/component_manifests/attribute/*.json、diagnose/actions/*、__init__.py | 组件 metadata 与旧 import path re-export；不是第二套算法。 |
| 回归测试 | tests/test_diagnose_attribute.py 等 | 约束已实现的公共行为。 |

## 数据契约：归因到底输出什么

### 事件身份与失败信号

AgentEvent 包含 event_id、agent_name、event_type、module、step_index、input、output、error、metadata 和 artifacts，定义见 src/agentdebug/schema/models.py:69-87。AgentTrajectory 没有强制的 success 布尔字段；通过 AgentDebug.finish_trace(success=False) 记录时，会增加 agent_name=system、event_type=error 的终止事件，并在 metadata 中带 success=False，见 ingest/recorder.py:79-104。

Detect 的 FailureFinding 是症状定位：failure taxonomy、可选 event_id / agent_name / step_index、confidence、evidence 和 suggestion，见 schema/models.py:131-143。它不是证明过的因果根因。

归因层把这些输入转换为：

| 字段 | 含义 | 下游作用 |
| --- | --- | --- |
| span_id | 实际上承载 AgentEvent.event_id，而非独立 Span 模型 | 优先事件身份、重跑锚点。 |
| step_index | 来源框架步骤标签 | 显示、排序和旧轨迹兼容。 |
| agent_name | 被归责的 agent 或 component | 报告、评估、责任展示。 |
| confidence | 算法或 LLM 给出的数值 | 排序和展示；不代表统一校准后的概率。 |
| rationale、evidence | 解释和证据字符串 | Recover、Rerun、UI 的上下文。 |
| sources | 贡献后端标识 | 多后端 provenance。 |

Blame / AttributionResult 定义在 attribute/attribution.py:41-68。

### event_id 优先，step 只能辅助

同一步可以有 thought、agent action、tool call、environment observation，甚至来自不同 agent。因此 step_index 不是稳定主键。DeepDebug 的 resolve_candidate_event() 先验证 event_id，然后仅在 step + agent 唯一时回退；存在歧义时返回空而不暗中任选一个，见 attribute/moe.py:174-205。

常规 AllAtOnceAttributor 的 _normalize_blame() 也采用 event id 优先、同步骤匹配、再把疑似 0-based ordinal 映射到可归因事件的方式，见 attribute/attribution.py:321-367。官方 trace schema 文档同样明确 step_index 可能重复，见 docs/TRACE_SCHEMA.md:75-78。

## Detect 与 Attribute 的边界

HeuristicAnalyzer 先执行事件规则和跨事件规则，将匹配结果变为 FailureFinding，然后按“有 step 优先、最早 step 优先、同 step 高 confidence 优先”写入初步 report.root_cause_*，见 diagnose/detect/analyzers.py:30-117、163-191。

因此默认 heuristic 的“root cause”实质上是“最早被规则发现的异常”，而非严格因果证明。Attribute 的任务是从这些症状回溯到更可能负责的事件；DiagnoseContext 随后保留原 detect findings，却将 primary attribution 提升为唯一 recovery target，见 diagnose/context.py:29-78、120-205。

通用库编排在 DiagnosePipeline.run()：

1. Detector 生成 report；
2. Attributor 生成 AttributionResult 并序列化进 report.attribution；
3. DiagnoseContext 根据第一条 hypothesis 写 root_cause_* 和 recovery_report；
4. 可选 Recover 只针对该目标生成建议。

代码见 diagnose/pipeline.py:45-82。构造函数默认不启用 attributor；只有 DiagnosePipeline.local_default() 显式组合 HeuristicAnalyzer 与 HeuristicAttributor。

## 常规文本归因策略

### 能力、成本和产品入口

| 策略 | 选择机制 | 调用量 | 失败或空输入 | CLI / UI |
| --- | --- | ---: | --- | --- |
| heuristic | 最早 finding，同步骤最高 confidence | 0 | 无 finding 返回空 hypotheses | 是 |
| all_at_once | 单次读全轨迹，输出一个候选 | 1，最多重试 1 次 | LLM/JSON 失败回退 heuristic | 是 |
| step_by_step | 对每个候选步骤独立判断 | O(N)，默认最多 30 | 空轨迹回退；全阴性则空结果 | 是 |
| binary_search | LLM 反复选择上半或下半 | O(log N) | 无法定位时回退 heuristic | 是 |
| counterfactual | 以“修正该步能救回运行”的模拟概率排序 | O(K)，默认 K=5 | 全 probe 无结果时回退 | 是 |
| sbfl | 通过/失败语料的频谱可疑度 | 0 推理调用 | 无语料或无分数回退 | 否，仅 Python |
| ensemble | Borda 或 Bayesian 合并子后端 | 子后端之和 | 全部无结果时回退 | 否，仅 Python |
| aao_moe | 全局读 + 结构探针 + 仲裁 | 多次 | 分支性降级 | 否，DeepDebug 内部使用 |

常规 manifest 注册 heuristic、all_at_once、step_by_step、binary_search、counterfactual 和 DeepDebug compatibility entry，见 diagnose/component_manifests/attribute/*.json。CLI aliases 和 _run_attributor() 只识别前五种，见 cli/legacy.py:83-110、1668-1691；UI 回归测试将列表固定在 tests/test_ui_routes.py:117-140。

### 1. HeuristicAttributor：零成本基线

实现：attribute/attribution.py:71-109。

- 对 findings 排序，排序键为 step 是否为空、step、负 confidence。
- 将第一条 finding 的 event_id、agent、步骤、证据直接复制为 Blame。
- 没有 finding 时返回空 hypotheses。

它不重新阅读轨迹，隐含“最早被 Detect 标出的症状就是根因”的假设。tests/test_diagnose_attribute.py:8-36 验证了即使下游 finding 的 confidence 更高，早期步骤仍会被优先选择。

### 2. AllAtOnceAttributor：整轨单次 LLM

实现：attribute/attribution.py:112-367。

执行过程：

1. 排除 run.start / run.end，将每个事件渲染成步骤、event id、agent、output 或 input、error；
2. 以强制 JSON 的 system prompt 要求输出 span_id、step、agent、confidence、rationale、evidence；
3. LLM 调用最多两次，第一次异常固定等待 2 秒，第二次异常或无 JSON 时回退 heuristic；
4. 对返回的 event id / step / agent 做 grounding；
5. 若同一步存在唯一兼容的 detector finding，则强制把 span_id 锚定到 detector event，并加入 detector_event_anchor source；
6. 可选从 metadata.mistake_agent / mistake_step 计算离线评估指标。

已有测试覆盖 ordinal 归一化、非法 JSON fallback 和同一步 event anchoring，见 tests/test_diagnose_attribute.py:39-140。

关键实现事实：

- 类签名和模块注释说会接收 findings，但 _render_prompt() 没有渲染 findings；_render_finding() 也没有调用点。findings 当前只用于 fallback、相同步骤的 event anchoring 和 raw.finding_count，见 attribute/attribution.py:161-163、193、248-288。
- 因而 max_findings 不会约束 prompt；轨迹文本也没有输入长度截断。
- prompt 允许“没有失败”时返回 null / 0，但当前代码仍会包装成一条全 null、confidence=0 的 hypothesis，而非空列表。
- confidence 的 coercion 只转 float，不 clamp 到 [0,1]。

### 3. StepByStepAttributor：逐候选判断

实现：attribute/attribution.py:404-600。

- _candidate_step_events() 根据 metadata.step_semantics 选择候选；env_agent_pairs 只保留 llm.response / agent.step，默认 all_events 则保留所有非生命周期事件，见 :938-1054。
- 默认只扫描前 30 个候选。每个候选询问 LLM 它是否是“直接导致级联失败”的决定性错误。
- 历史本应是 prefix，但默认 context_window=3，只会保留候选之前最近 3 个原始 event；paired-turn 会按环境/agent 对改写。
- 所有 is_failure_step=true 的结果成为 hypotheses，并只按步骤升序排列，不按 confidence 重排。

单个 LLM 异常或坏 JSON 只会跳过该步骤；即使全部调用失败或为阴性，方法也返回空 hypotheses，不回退 heuristic。findings 参数及 _render_finding() 同样不进入 LLM prompt。save_full_generation=True 会把请求和模型文本写到 raw，便于审计但也可能保存敏感 trace 内容。当前没有该策略的专门回归测试。

### 4. BinarySearchAttributor：半区间递归

实现：attribute/attribution.py:603-898。

它不断将候选事件分为 upper / lower，让 LLM 选哪半包含决定性错误，递归到单一 event。代码假定轨迹已经失败，但没有验证该前置条件。无候选或某个 probe 无法解析时，整次归因回退 heuristic。

返回 confidence 不是 LLM confidence，而是按 probe 深度合成的 0.6 + 0.1 × min(probe_count, 4)，最高 1.0，见 :645-684。它以同一步附近环境事件补充 agent decision 的上下文，见 :813-874。

构造函数有 context_window=6 和 _render_prefix()，但实际 probe 路径没有调用 _render_prefix()；该参数当前不限制二分 prompt。findings 也只在 fallback 时被使用。当前没有专门回归测试。

### 5. CounterfactualAttributor：模拟反事实

实现：attribute/attribution.py:1090-1274。

候选优先级为：

1. Detect findings 指向的事件；
2. 直接带 error 的事件；
3. 轨迹尾部事件。

最多 probe 5 个候选。每个 prompt 都询问：“如果这个步骤正确完成，整次运行成功的概率是多少？”结果按 rescue_probability 降序、模型 confidence 次序排序。

它是 LLM 的文本模拟，而非真实 replay、rollback 或重新执行。每个 probe 重复携带整条轨迹，因此 token 成本约为 O(K×N)；响应中的 would_block_downstream_failures 未参与排序或输出；调用没有重试。

当没有 findings 时，finish_trace(success=False) 写入的系统终止 error 也可进入第二类候选。不同于 SBFL，算法没有主动排除这种合成终止事件。当前没有专门回归测试。

### 6. SBFLAttributor：跨轨迹频谱定位

实现：attribute/attribution.py:1276-1458。

调用者必须传入 passing_corpus 与 failing_corpus，代码不验证它们是否真来自同一任务。对每个事件构造：

~~~text
event_type | agent_name | module | normalize(output or input)[:120]
~~~

然后计算 ef、ep、nf、np，支持 Tarantula、Ochiai（默认）和 DStar，按可疑度降序、较早步骤破同分。run.start、run.end 与 system 产生的 error 会被排除，以免所有失败 trace 的终止标记压倒真实根因。

注释称最后一项为 normalized_io_hash，但代码实际上直接拼接截断文本，并未 hash，见 :1403-1424。这既可能造成同前缀碰撞，也使内存 signature 带有原始 I/O 片段。该策略没有 CLI、UI、manifest 或专门测试入口。

### 7. EnsembleAttributor：多后端合并

实现：attribute/attribution.py:1462-1676。

- borda：同一后端第 1 名得 N 分、第 2 名得 N-1 分，乘 weight 汇总；输出 confidence 是加权平均。
- bayesian：同一 (span_id, step_index) 以 1 - Π(1 - clamp(weight × confidence)) 合并，隐含后端独立且置信度可校准。
- AttributionBudget 可按完成后端数或墙钟时间停止后续后端；异常后端会跳过。

合并保留所有 sources，去重 evidence，拼接每个后端 rationale。预算只在启动下一个后端前检查，无法中断已经很慢的 LLM 后端；max_backends 还会计入“正常返回但没有 hypothesis”的后端。该策略仅为库级能力。

## DeepDebug：高成本、可审计的归因 profile

DeepDebugAnalyzer 位于 diagnose/profiles/deepdebug.py:125-362，定位内核在 diagnose/attribute/moe.py:523-767。CLI 和 REST 在 mode=deep 时禁止普通 attributor 叠加，避免覆盖它内建的归因结果。

### 四个实际阶段

1. global_read：复用 AllAtOnceAttributor 全局读取，得到候选 A。
2. structure_probe：真实多 agent 走 _cascade()；单 agent 走 _bisect_refine()，得到候选 B 与每次 range decision。
3. cross_examine：A/B 的 event identity 一致则以 0.8 confidence 通过；不一致时只展示二者各自默认 ±1 step 的窗口，交给 LLM 仲裁。
4. diagnose_and_suggest：根事件已经固定，只生成摘要、逐事件原文证据和修复建议，不能移动根因。

多 agent cascade 至多 8 次半区间收缩、窗口阈值为 6；单 agent bisection 收缩至 3 步窗口后，依据最近 half probe 的 0.75 阈值决定继续二分还是全上下文 endgame 选择，见 attribute/moe.py:315-507。

决策事件集合会排除 environment、observation、run marker、tool.call 与 tool.result，优先追责“错误决策”而非“错误显现点”，见 attribute/moe.py:147-159。仲裁无法明确选 A 时会落到 B/MoE，见 :629-671。

### 证据、先验与记忆

DeepDebug 将 root 落地到唯一事件；无法 grounding 会抛错，见 profiles/deepdebug.py:180-200。最终 refine 仅查看根因附近 ±3 步，要求 evidence 是附带 event_id 的逐字引用：

- _validate_evidence() 只保留能在被引用 event 的 input / output / error 中找到的 quote；
- 伪造、错误归属、无法唯一匹配的证据会被丢弃；
- 空证据时回退 root event 的 error / output / input 前 300 字；
- 最终 report 记录 evidence_verified 和 rejected_evidence_count。

代码见 profiles/deepdebug.py:417-527；tests/test_deepdebug_profile.py:170-240 验证重复步骤号下的 event identity 和幻觉证据拒绝。

Deep mode 会先执行 HeuristicAnalyzer，把 findings 以“可能错误的线索”传给 prior_findings。_render_prior_findings() 最多渲染 5 条，并明确要求模型自行验证，见 profiles/deepdebug.py:366-390；CLI 的接线在 cli/legacy.py:1594-1624，测试在 tests/test_cli_commands.py:236-270。

记忆默认是无副作用 NullMemoryStore。SQLiteDeepMemoryStore 可用 embedding cosine 或 Jaccard token similarity 从最多 200 个 episode 取 reference，并保存 trajectory、描述、evidence 和 suggestion，见 attribute/deep_memory.py:45-342。值得注意的是 CLI deep mode 创建 SQLiteDeepMemoryStore，但没有把 use_memory=True 传给 DeepDebugAnalyzer；因此默认会在结束时 save_run，却不执行历史 retrieval，见 cli/legacy.py:1597-1606 与 profiles/deepdebug.py:392-403。

AaoMoeAttributor 可从 agentdebug.diagnose.attribute.moe 导入，但不在 diagnose.attribute.__all__、常规 component manifest 或 CLI alias 中；用户入口是 --mode deep，而非 --attributor aao_moe。

## GUI / OSWorld：独立的多模态 RCA

GuiRcaAnalyzer 位于 diagnose/gui_rca.py:50-202。它通过 lazy import 加载 cua_debugger，从 trajectory.metadata.source_dir 或图片 artifact 解析 OSWorld 目录，然后调用 debugger.rca.run_rca()。

CUA 的 RCA prompt 将终止失败记作 F，要求从 F 向前检查，找到最早引入独立错误、而非继承前序错误的 N，见 cua_debugger/debugger/rca.py:64-178。ReAct loop 的 get_step_details 提供动作代码、reasoning、tool use、执行 error 和动作前后的截图，见 cua_debugger/debugger/dispatch.py:134-172。

finish 必须返回：

~~~text
root_error_step, taxonomy_tag, evidence, correction, confidence,
per_step_summaries
~~~

runtime/llm_channel.py:56-148 用 Anthropic 风格 messages.create 外观桥接到 AgentDebugX 的 OpenAI-compatible client，并转换图片输入。GuiRcaAnalyzer 随后把 RCAResult 映射为一条 FailureFinding 和 root_cause_* 字段。

该路径与文本 Attribute 的差异和边界：

- 它要求本地可读的 OSWorld 目录/截图、支持 tool calling 与 vision 的 LLM。
- 同一步有多个 IR event 时，_map_result() 取第一个匹配 step_index 的 event；没有 DeepDebug 的歧义拒绝。
- GUI report 不写标准 report.attribution，而是直接写 findings 和 root_cause_*；其 Recover 语义与普通 Attribute 不能假设完全同构。
- CUA standalone 不 import agentdebug；主项目通过适配器单向调用它。GUI RCA 的主项目测试当前主要覆盖选项暴露，不覆盖真实 GuiRcaAnalyzer 映射或跨边界 LLM channel。

## 报告、恢复与重跑

常规 Attribute 的结果会序列化到：

~~~json
{
  "method": "all_at_once",
  "elapsed_ms": 0,
  "hypotheses": [{"span_id": "evt_...", "step_index": 3}],
  "primary": {"span_id": "evt_...", "step_index": 3},
  "raw": {}
}
~~~

DiagnoseContext 优先按 span_id / event_id 找事件；只有 step_index + agent_name 唯一才回退。成功后，它克隆 detect finding 并迁移到根事件；若 report 没有 findings，则合成 mode_id=attribution.root_cause 的 finding，见 diagnose/context.py:120-205。Recover 只接收 recovery_report，见 diagnose/recover/recovery.py:57-64。

Rerun 默认 checkpoint policy 为 from_root_cause。build_rerun_request() 将 primary attribution、detect/recover 摘要和 root_cause_* 写入 request metadata，见 rerun/workflow.py:272-432；真实执行仍需要框架 runner、工具和环境状态，只有轨迹时只是计划。

一个输出差异：metadata.analyzer 为 HeuristicAnalyzer 或 DeepDebugAnalyzer 时，model_to_dict() 会递归删除所有 confidence，包括 attribution 内的值；LLM Judge 报告则保留，见 schema/models.py:180-218。大多数常规 attributor 也未计算 elapsed_ms，保留默认 0；不要把该字段当成统一端到端耗时。

## 测试证据与已知覆盖缺口

| 测试文件 | 已覆盖内容 |
| --- | --- |
| tests/test_diagnose_attribute.py | heuristic 排序/空值，all-at-once ordinal grounding、非法 JSON fallback、detector event anchor。 |
| tests/test_deepdebug_profile.py | 四阶段审计、事件身份、伪造证据拒绝、fallback guidance。 |
| tests/test_diagnose_recover.py | primary attribution 到 recovery target 的传递，空 detect finding 时合成目标。 |
| tests/test_cli_commands.py | 显式 pipeline、DeepDebug 注入 detect findings、禁止外部 attributor 覆盖。 |
| tests/test_ui_routes.py | REST 选项列表、report 保存、DeepDebug 互斥规则。 |
| cua_debugger/tests/test_rca_lesson_injection.py | CUA lesson table prompt 注入和 finish 工具输入。 |

对测试目录中策略类名的静态检索没有发现 StepByStepAttributor、BinarySearchAttributor、CounterfactualAttributor、SBFLAttributor、EnsembleAttributor 的专门算法回归测试，也未发现 GuiRcaAnalyzer._map_result() 的直接单元测试。

建议未来补齐：重复步骤号、LLM timeout/坏 JSON、无 span id、越界 confidence、系统终止 error、step_semantics、多 agent、SBFL signature 碰撞、counterfactual 的“模拟而非真实执行”标识，以及 GUI RCA 的越界 root step / 未知 taxonomy / 无来源 evidence。

## 静态审计发现与维护建议

以下是事实性观察，不代表本次已改动代码：

1. AllAtOnceAttributor 和 StepByStepAttributor 接收 findings，却不把它们送进 LLM prompt；Counterfactual 只用 findings 选候选，DeepDebug 才将其作为可错先验。
2. max_findings、BinarySearch.context_window 和 BinarySearch._render_prefix() 与实际 prompt 路径脱节，未形成预期的上下文上限。
3. 当前“反事实”是 LLM 判断，不是框架真实 replay；任何产品输出都不应把它描述成实证因果验证。
4. SBFL、Ensemble、AaoMoe 的库实现与 CLI/UI/manifest 的产品能力面不一致；若要公开，需要同步 aliases、LLM 需求判断、dispatch、UI options 和测试。
5. docs/ARCHITECTURE.md 中仍把 DeepDebug findings injection 和 Error Hub 到 memory 的部分工作描述为缺口；代码实际已有 prior_findings 渲染和 import_hub_bundles() helper。该文档在路线决策前应与源码同步。
6. trace 原文、错误文本和可选历史案例会直接进入 LLM prompt；实现未见专门的 prompt-injection 隔离层。使用不可信轨迹时应把模型输出视为不可信建议，并保留 DeepDebug 的证据核验与人工审批边界。

新增普通归因器时，应实现 Attributor.attribute(trajectory, findings)，返回可落地 event_id 的 Blame，并添加 component manifest。若要让用户从 CLI/UI 选择，还必须同步 cli/legacy.py、UI options 和回归测试；仅导出 Python 类不会自动成为产品入口。
