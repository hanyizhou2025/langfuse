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
When `annotations.jsonl` is present, the command also writes `report.json`;
otherwise it writes only `predictions.jsonl`. The historical converter accepts
the documented redacted aliases such as `observation_id`, `input_redacted`,
and `output_redacted`. It returns `unknown` when source or semantic evidence is
not unique instead of forcing attribution.
