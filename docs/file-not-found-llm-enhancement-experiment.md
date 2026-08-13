# File Not Found 失败归因：LLM 增强实验

## 结论

在本次受控压力实验中，LLM 对 AgentDebugX 的主要价值是补足确定性规则无法处理的“跨节点语义和因果关系”，而不是替代已有规则。

- 确定性基线：真实失败的原因标签和根因节点均为 `3/6`；语义负例为 `2/4`；证据不足拒答为 `2/2`；错误强制归因 `1` 条。
- 安全混合方案（仅复核规则 `unknown`）：原因标签和根因节点提升到 `6/6`；语义负例提升到 `3/4`；拒答保持 `2/2`；错误强制归因仍为 `1` 条。
- 全量 LLM 复核：原因标签、根因节点均为 `6/6`；语义负例为 `4/4`；拒答保持 `2/2`；错误强制归因降为 `0`。

因此，MVP 推荐先上线“规则优先 + LLM 复核 unknown”，成本和回归风险较小；如果生产人工数据证明规则存在较多高置信语义误判，再增加“高风险确定性结论复核”，不建议一开始无条件覆盖全部规则结果。

这些分数来自 12 条合成长 Trace，不是生产准确率，也不能宣称泛化已经得到证明。下一阶段必须在正在标注的约 50 条真实生产 Case 上冻结测试集后复跑。

## 1. 实验设置

### 1.1 数据规模

- Trace：12 条；每条对应一个 File Not Found Case。
- Observation：共 9624 个；每条 Trace 为 801–803 个节点。
- 场景：模型臆造路径、用户错误路径、Skill 旧配置、运行时挂载缺失、预期存在性检查、失败后创建并恢复、缓存 miss 回退、用户要求验证不存在、路径来源冲突、临时文件被运行时删除、上游旧产物路径、证据不足。
- 数据字段：模型只接收 `input_redacted`、`output_redacted`、`status_message_redacted` 等合成脱敏字段；Oracle 独立保存，未进入 prompt。
- 防标签泄漏：送模的 `case_id`、`trace_id` 和 Observation ID 均为中性编号，不含 `runtime_mount_unavailable` 等场景名；单测会检查场景名未进入任何 ID。

这不是简单复制节点：每条 Trace 含约 800 个无关成功节点，关键因果链仅有 1–3 个节点，同时覆盖任务语义、恢复分支、来源冲突和运行环境状态。

### 1.2 模型与运行方式

- 模型：`gpt-5.6-sol`，reasoning effort 为 low。
- 接入：本机已登录的 Codex CLI，只读、临时会话、JSON Schema 约束；未读取或复制任何凭证。
- 模型输出必须再经过 AgentDebugX 本地校验器：failure ID 不可变；所有 evidence/root ID 必须存在；根因 label 与 domain 必须匹配；decision、`is_agent_failure` 和 root 字段必须一致；失败则回退到确定性结果。

Codex CLI 不是建议的生产服务接入方式。本次使用它是为了在没有外部 API Key 环境变量的前提下完成一次真实模型实验。生产应使用已有 OpenAI-compatible HTTPS API 和 `AGENTDEBUG_LLM_*` 环境变量。

## 2. 极长 Trace 如何送模

直接把 9624 个 Observation 全量送模成本高、噪声大，也更容易超过上下文。因此先做确定性因果检索：

1. 始终保留失败 Tool Observation；
2. 保留其时间窗口与祖先节点；
3. 保留含相同脱敏路径的候选来源；
4. 保留运行时、Skill、Memory、User、Planner、Probe、Create/Write、Final 等语义节点；
5. 每个 Case 设置最多 64 个 Observation 的硬上限；
6. 单字段再进行字符和集合长度截断。

本次仅复核 unknown 时，模型实际看到 `37/9624` 个 Observation，即过滤约 99.6% 的节点；每条 Case 保留 6–7 个节点，批量 prompt 约 16 KB。全量复核时为 `75/9624` 个 Observation，prompt 约 32 KB。

这里最重要的能力不是“大上下文硬塞进去”，而是检索不能漏掉真正根因。生产评测应增加 `root_retrieval_recall`：人工根因节点是否进入模型上下文。若根因没被召回，模型能力再强也无法正确归因。

## 3. 分层结果

| 指标 | 确定性基线 | 仅复核 unknown | 全量 LLM 复核 |
| --- | ---: | ---: | ---: |
| 失败原因标签正确 | 3/6 | 6/6 | 6/6 |
| 根因 Observation 正确 | 3/6 | 6/6 | 6/6 |
| 语义负例正确 | 2/4 | 3/4 | 4/4 |
| 负例被强制归因 | 1 | 1 | 0 |
| 正确拒答 | 2/2 | 2/2 | 2/2 |

### 3.1 LLM 补上的能力

- `runtime_mount_unavailable`：从挂载检查节点识别运行时不可见，而不是把失败节点自身当根因。
- `runtime_file_deleted`：把“先创建成功 → 容器轮换清理 → 读取失败”串成因果链，根因定位到清理节点。
- `upstream_artifact_stale`：识别 artifact registry 返回 expired/stale 路径，归入 upstream。
- `expected_cache_miss_fallback`：结合用户任务和后续 compute success，判定 File Not Found 是预期控制流，而非任务失败。
- `rule_match_expected_absence`：全量复核时理解“验证文件不存在”的自然语言目标，纠正规则基线的高置信误判。

### 3.2 原始模型的过度归因与系统护栏

无标签泄漏实验中，原始模型把“同一路径先后出现在 Skill 和模型节点”的冲突 Case 强行归因给更早的 Skill 节点，置信度为 0.98；人工 Oracle 要求 `unknown`。原始模型因此把正确拒答从 `2/2` 降为 `1/2`。这证明 JSON Schema、合法 ID 和高置信度都不能保证语义结论正确。

系统随后增加确定性冲突护栏：同一失败路径存在多个不同来源域，且 Trace 没有唯一 provenance 时，LLM 不得把基线 `unknown` 改成 `attributed`。护栏把该 Case 回退为 `unknown`，最终得到表中的 `2/2`。成功的 create/write/save 被定义为文件生命周期证据而非路径来源，因而不会错误阻断“创建 → 运行时删除 → 读取失败”的因果链。

只有失败 Tool、没有来源和下游语义的 Case，原始模型和护栏后的系统都保持 `unknown`。

模型对证据不足的 `unknown` 仍给出 0.99，对错误的冲突归因给出 0.98，说明模型自报置信度不可直接校准。系统应以结构校验、冲突护栏、证据覆盖和人工评测为准，不能把模型 confidence 当作可靠概率。

## 4. 为什么先推荐安全混合方案

仅复核 unknown 用 6 次 Case 评审，就补回 3 条失败归因和 1 条语义负例，且没有覆盖规则已经判对的结论。它的缺点是无法修正规则“确定性但错误”的预期缺失案例。

全量复核在这个小型合成集上没有回归并修掉了最后一个误判，但还不能据此上线全覆盖：

- 真实生产 Trace 的语言、工具命名、结构缺失和脱敏损失更复杂；
- LLM 结果有随机性和模型版本漂移；
- 全覆盖增加调用量、延迟与外发数据面；
- 本机 CLI 请求多次经历 WebSocket 超时后回退 HTTPS，端到端约 2 分钟；这主要是当前实验入口传输问题，不能当作公共 API 的推理延迟，但说明生产必须直接接 API、设置超时重试和熔断。

最终无泄漏全量请求由 Codex CLI 报告 23,475 tokens，其中包含 CLI 自身的系统上下文，不能直接当作公共 Chat Completions API 的 Case 成本。正式成本评测必须读取目标公共 API 返回的 `prompt_tokens` 和 `completion_tokens`，按 Case 汇总。

建议生产策略分三档：

1. P0：规则 `unknown` 必须 LLM 复核；请求失败时保留 `unknown`。
2. P1：对可能误判的确定性结论选择性复核，例如用户目标含“检查不存在”、Trace 最终成功、或失败后出现 fallback/final success。
3. P2：只有在冻结真实测试集证明收益稳定后，才评估全量复核。

## 5. 安全和可靠性边界

- API Key 只从环境变量读取，不进入数据集、prediction、日志或 Git。
- 公共 `BASE_URL` 必须 HTTPS；拒绝 URL 内嵌凭证、query、fragment 和字面量非回环 IP；本地 `localhost/127.0.0.1/::1` 可使用 HTTP。
- 未提供完整 `*_redacted` 字段时默认不调用模型。
- 返回不存在的 Observation ID、错误的 label/domain 组合、矛盾决策或无 failure evidence 时，回退规则结果。
- `reviewed_observation_count` 和 `total_observation_count` 会保存到 UI 元数据；模型名称也可审计。
- 模型输出不保存原始路径，展示层仍采用字段白名单。

生产还需要网关层允许列表、DNS 解析后私网地址阻断、redirect 禁止/重新校验、租户级外发开关和数据保留协议。Python 客户端的 URL 检查不能单独防止 DNS rebinding。

## 6. 在 50 条真实数据上如何得出可信结论

冻结人工标签后，至少报告：

- 语义负例准确数、强制归因数；
- 失败原因标签正确数、根因 Observation 正确数；
- unknown 的正确拒答数与错误拒答数；
- 根因检索召回率；
- 模型无效响应、幻觉 ID、超时和回退率；
- 每 Case prompt/completion token、p50/p95 延迟与估算成本；
- 按 `model/user/upstream/runtime/not_failure/unknown` 分层的命中数，而非只给总准确率。

同一 Trace 的 Case 必须进入同一 split。先用约 30 条开发集调整检索和 prompt，再冻结约 20 条测试集；不要根据测试结果继续改规则。50 条仍属于 pilot，应给出命中数和置信区间，不宣称广泛泛化。

## 7. 复现入口

- 长 Trace 生成器：`agentdebug.integrations.langfuse_attribution.long_dataset`
- 批量实验驱动：`agentdebug.integrations.langfuse_attribution.experiment`
- 标准数据集 CLI：`agentdebug.integrations.langfuse_attribution.cli --llm`
- 公共模型变量：`AGENTDEBUG_LLM_BASE_URL`、`AGENTDEBUG_LLM_API_KEY`、`AGENTDEBUG_LLM_MODEL`

具体命令见 AgentDebugX 内的 `src/agentdebug/integrations/langfuse_attribution/README.md`。
