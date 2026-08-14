# Langfuse tool-failure attribution

This integration accepts Langfuse observation dictionaries and returns two
separate products:

- `trajectory`: a safe AgentDebugX trajectory for deterministic detection.
  It never contains raw Langfuse `input`, `output`, or arbitrary metadata.
- `evidence`: argument provenance and execution facts for deterministic failure
  attribution. It is deliberately not sent to AgentDebugX's keyword matcher.

## Attribution metadata contract

Put an `attribution` object in an observation's Langfuse `metadata` field.

```python
{
    "kind": "tool_call",
    "tool_name": "read_file",
    "arguments": {"path": "/workspace/report.md"},
    "argument_sources": {
        "path": {"kind": "model", "observation_id": "generation-12"},
    },
}
```

The matching result observation supplies the actual execution facts:

```python
{
    "kind": "tool_result",
    "tool_call_observation_id": "tool-call-13",
    "error_code": "ENOENT",
    "execution_context": {"cwd": "/workspace"},
    "resource_existence": {"path": False},
}
```

Supported source kinds are `user`, `skill`, `model`, `system`, `environment`,
and `unknown`. Use `detail` to store a non-secret skill identifier/version (for
example `repo-skill@v3`) or a source transformation. Only record values
permitted by the caller's privacy policy; store secrets as a source reference,
never as an argument value.

## Example

```python
from agentdebug.diagnose.detect import HeuristicAnalyzer
from agentdebug.integrations.langfuse_attribution import (
    LangfuseToolAttributor,
    convert_langfuse_observations,
)

converted = convert_langfuse_observations(observations)
detection = HeuristicAnalyzer().analyze(converted.trajectory)
attributions = LangfuseToolAttributor().attribute(converted.evidence)
```

The attributor is deterministic and emits `unknown` if source lineage or a
required execution fact is absent. It does not use an LLM or a database.

## Historical File Not Found datasets

For frozen production-trace datasets that do not yet contain the explicit
`metadata.attribution` contract, run the conservative historical pipeline:

```bash
PYTHONPATH=src python -m agentdebug.integrations.langfuse_attribution.cli \
  /path/to/dataset \
  /path/to/outputs
```

The dataset directory must contain `traces.jsonl`, `cases.jsonl`, and
`observations.jsonl`. Trace-level input is represented as a stable synthetic
`<trace-id>:input` user-source event during offline conversion, so a path that
originated in the trace input is not misclassified as model-generated.
The command always writes `predictions.jsonl` and an Inspect-compatible
`trajectories.jsonl`. When `annotations.jsonl` is present, it also writes
`report.json`. Upload `trajectories.jsonl` through **Upload Trace**, open a
trace, and select **Tool Attribution** to review the semantic decision, failure
observation, root-cause observation, evidence observations, reason codes, and
confidence. The historical converter accepts the documented redacted aliases
such as `observation_id`, `input_redacted`, and `output_redacted`. It returns
`unknown` when source or semantic evidence is not unique instead of forcing
attribution.

## Direct production-table deployment

AgentDebugX can also run directly from ClickHouse `traces`, `observations`,
and keyword-filtered `tool_error` tables without importing any Langfuse
application code:

```bash
agentdebug langfuse-fnf doctor --config /path/to/source.json
agentdebug langfuse-fnf run /path/to/outputs \
  --config /path/to/source.json \
  --annotations /path/to/annotations.jsonl
```

Connection credentials come only from `AGENTDEBUG_CLICKHOUSE_*` environment
variables. See [the production deployment guide](../../../../docs/PRODUCTION_LANGFUSE_FNF.md)
for the table contract, configuration example, read-only/security behavior,
snapshot workflow, and the redacted-view requirement for public-model calls.

## Optional LLM enhancement

Ambiguous cases can be reviewed by any OpenAI-compatible public model API:

```bash
export AGENTDEBUG_LLM_BASE_URL="https://api.example.com/v1"
export AGENTDEBUG_LLM_API_KEY="..."
export AGENTDEBUG_LLM_MODEL="your-model"

PYTHONPATH=src python -m agentdebug.integrations.langfuse_attribution.cli \
  /path/to/dataset /path/to/outputs --llm
```

The default hybrid policy sends only deterministic `unknown` cases to the
model. Add `--review-deterministic` to review every case. Only observations
containing the complete `input_redacted`, `output_redacted`, and
`status_message_redacted` contract are model eligible. Context retrieval is
capped at 64 observations per case by default. Returned observation IDs,
labels, domains, decision invariants, and evidence are validated locally; an
invalid model response falls back to the deterministic result.

API keys are read from the process environment and are never added to stored
predictions. Environment-configured public endpoints must use HTTPS and cannot
contain URL credentials, query strings, fragments, or literal non-loopback IP
addresses. Exact loopback endpoints (`localhost`, `127.0.0.1`, and `::1`) may
use HTTP for local Ollama or LiteLLM development.

## Reproduce the synthetic long-trace experiment

Generate 12 redacted traces with at least 500 observations each:

```bash
PYTHONPATH=src python -m \
  agentdebug.integrations.langfuse_attribution.long_dataset \
  /tmp/agentdebugx-long-dataset --noise-observations 800
```

For model backends that support one structured batch call, prepare a
label-free prompt and validate its response afterward:

```bash
PYTHONPATH=src python -m \
  agentdebug.integrations.langfuse_attribution.experiment prepare \
  /tmp/agentdebugx-long-dataset /tmp/agentdebugx-long-artifacts --review-all

# Send batch_prompt.txt using batch_response_schema.json, then:
PYTHONPATH=src python -m \
  agentdebug.integrations.langfuse_attribution.experiment evaluate \
  /tmp/agentdebugx-long-dataset /tmp/agentdebugx-long-artifacts \
  /tmp/batch-response.json /tmp/agentdebugx-long-results \
  --model your-model
```

Synthetic annotations remain in a separate file and never enter the model
request.
