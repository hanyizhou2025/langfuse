from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from agentdebug.integrations.langfuse_attribution import (
    FileNotFoundDecision,
    FileNotFoundLLMAttributor,
)
from agentdebug.runtime.llm import CompletionResult


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeLLM:
    model = 'fake-model'

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.messages: List[List[Dict[str, Any]]] = []

    def complete(
        self,
        messages: List[Dict[str, Any]],
        *,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> CompletionResult:
        self.messages.append(messages)
        return CompletionResult(
            text=json.dumps(self.responses.pop(0)),
            raw={'usage': {'prompt_tokens': 100, 'completion_tokens': 40}},
        )


def _observation(
    observation_id: str,
    index: int,
    *,
    observation_type: str,
    name: str,
    input_value: object = None,
    output_value: object = None,
    parent_observation_id: Optional[str] = None,
    level: str = 'DEFAULT',
    status_message: str = '',
) -> Dict[str, object]:
    return {
        'observation_id': observation_id,
        'trace_id': 'trace-long',
        'parent_observation_id': parent_observation_id,
        'type': observation_type,
        'name': name,
        'start_time': (BASE_TIME + timedelta(seconds=index)).isoformat(),
        'input_redacted': input_value,
        'output_redacted': output_value,
        'metadata_redacted': {},
        'level': level,
        'status_message_redacted': status_message,
    }


def _runtime_unknown_observations() -> List[Dict[str, object]]:
    path = '<PATH_1>/report.md'
    return [
        _observation(
            'runtime-context',
            0,
            observation_type='SPAN',
            name='runtime.mount_check',
            output_value={
                'requested_path': path,
                'mount_state': 'workspace volume unavailable',
            },
        ),
        _observation(
            'failed-read',
            1,
            observation_type='TOOL',
            name='read_file',
            input_value={'path': path},
            output_value={'error': 'ENOENT'},
            level='ERROR',
            status_message='File not found',
        ),
    ]


def test_llm_enhances_unknown_with_grounded_runtime_attribution() -> None:
    llm = FakeLLM(
        [
            {
                'decision': 'attributed',
                'semantics': 'unexpected_failure',
                'is_agent_failure': True,
                'failure_observation_id': 'failed-read',
                'root_cause_observation_id': 'runtime-context',
                'root_cause_label': 'runtime_path_unavailable',
                'root_cause_domain': 'runtime',
                'evidence_observation_ids': [
                    'runtime-context',
                    'failed-read',
                ],
                'confidence': 0.88,
                'reason_codes': ['runtime_mount_unavailable'],
            }
        ]
    )

    result = FileNotFoundLLMAttributor(llm).attribute(
        _runtime_unknown_observations(),
        failure_observation_id='failed-read',
        case_id='case-runtime',
        trace_id='trace-long',
    )

    assert result.decision == FileNotFoundDecision.ATTRIBUTED
    assert result.root_cause_observation_id == 'runtime-context'
    assert result.root_cause_label == 'runtime_path_unavailable'
    assert result.attribution_method == 'llm_enhanced'
    assert result.model == 'fake-model'
    assert result.reason_codes[-1] == 'llm_enhanced'


def test_llm_hallucinated_observation_id_falls_back_to_baseline() -> None:
    llm = FakeLLM(
        [
            {
                'decision': 'attributed',
                'semantics': 'unexpected_failure',
                'is_agent_failure': True,
                'failure_observation_id': 'failed-read',
                'root_cause_observation_id': 'invented-observation',
                'root_cause_label': 'runtime_path_unavailable',
                'root_cause_domain': 'runtime',
                'evidence_observation_ids': [
                    'invented-observation',
                    'failed-read',
                ],
                'confidence': 0.99,
                'reason_codes': ['runtime_mount_unavailable'],
            }
        ]
    )

    result = FileNotFoundLLMAttributor(llm).attribute(
        _runtime_unknown_observations(),
        failure_observation_id='failed-read',
        case_id='case-runtime',
        trace_id='trace-long',
    )

    assert result.decision == FileNotFoundDecision.UNKNOWN
    assert result.root_cause_observation_id is None
    assert result.attribution_method == 'deterministic_fallback'
    assert 'llm_invalid_observation_reference' in result.reason_codes


def test_default_policy_does_not_review_deterministic_attribution() -> None:
    path = '<PATH_1>/invented.md'
    observations = [
        _observation(
            'generation',
            0,
            observation_type='GENERATION',
            name='planner',
            output_value={'tool': 'read_file', 'path': path},
        ),
        _observation(
            'failed-read',
            1,
            observation_type='TOOL',
            name='read_file',
            input_value={'path': path},
            output_value={'error': 'ENOENT'},
            level='ERROR',
            status_message='File not found',
        ),
    ]
    llm = FakeLLM([])

    result = FileNotFoundLLMAttributor(llm).attribute(
        observations,
        failure_observation_id='failed-read',
        trace_id='trace-long',
    )

    assert result.decision == FileNotFoundDecision.ATTRIBUTED
    assert result.root_cause_observation_id == 'generation'
    assert result.attribution_method == 'deterministic'
    assert llm.messages == []


def test_long_trace_context_is_bounded_and_keeps_causal_nodes() -> None:
    path = '<PATH_1>/report.md'
    observations = [
        _observation(
            'noise-%04d' % index,
            index,
            observation_type='SPAN',
            name='unrelated.work',
            input_value={'record': index, 'path': '<PATH_2>/%04d.txt' % index},
            output_value={'status': 'ok'},
        )
        for index in range(500)
    ]
    observations.extend(
        [
            _observation(
                'runtime-context',
                500,
                observation_type='SPAN',
                name='runtime.mount_check',
                output_value={'requested_path': path, 'mount_state': 'unavailable'},
            ),
            _observation(
                'failed-read',
                501,
                observation_type='TOOL',
                name='read_file',
                input_value={'path': path},
                output_value={'error': 'ENOENT'},
                level='ERROR',
                status_message='File not found',
            ),
        ]
    )
    llm = FakeLLM(
        [
            {
                'decision': 'unknown',
                'semantics': 'unknown',
                'is_agent_failure': None,
                'failure_observation_id': 'failed-read',
                'root_cause_observation_id': None,
                'root_cause_label': None,
                'root_cause_domain': None,
                'evidence_observation_ids': ['failed-read'],
                'confidence': 0.2,
                'reason_codes': ['insufficient_evidence'],
            }
        ]
    )

    result = FileNotFoundLLMAttributor(
        llm,
        max_context_observations=24,
    ).attribute(
        observations,
        failure_observation_id='failed-read',
        trace_id='trace-long',
    )

    prompt = str(llm.messages[0][1]['content'])
    assert result.total_observation_count == 502
    assert result.reviewed_observation_count <= 24
    assert 'runtime-context' in prompt
    assert 'failed-read' in prompt
    assert 'noise-0000' not in prompt


def test_llm_is_skipped_when_only_unredacted_payloads_are_available() -> None:
    observations = _runtime_unknown_observations()
    for observation in observations:
        observation['input'] = observation.pop('input_redacted')
        observation['output'] = observation.pop('output_redacted')
        observation['status_message'] = observation.pop('status_message_redacted')
    llm = FakeLLM([])

    result = FileNotFoundLLMAttributor(llm).attribute(
        observations,
        failure_observation_id='failed-read',
        trace_id='trace-long',
    )

    assert result.decision == FileNotFoundDecision.UNKNOWN
    assert result.attribution_method == 'deterministic'
    assert 'llm_skipped_unredacted_payload' in result.reason_codes
    assert llm.messages == []


def test_llm_cannot_force_attribution_across_conflicting_path_sources() -> None:
    path = '<PATH_1>/report.md'
    observations = [
        _observation(
            'skill-path',
            0,
            observation_type='SPAN',
            name='report_skill.memory_lookup',
            output_value={'path': path},
        ),
        _observation(
            'model-path',
            1,
            observation_type='GENERATION',
            name='planner.resolve_path',
            output_value={'path': path},
        ),
        _observation(
            'failed-read',
            2,
            observation_type='TOOL',
            name='read_file',
            input_value={'path': path},
            output_value={'error': 'ENOENT'},
            level='ERROR',
            status_message='File not found',
        ),
    ]
    llm = FakeLLM(
        [
            {
                'decision': 'attributed',
                'semantics': 'unexpected_failure',
                'is_agent_failure': True,
                'failure_observation_id': 'failed-read',
                'root_cause_observation_id': 'skill-path',
                'root_cause_label': 'upstream_path_invalid',
                'root_cause_domain': 'upstream',
                'evidence_observation_ids': [
                    'skill-path',
                    'model-path',
                    'failed-read',
                ],
                'confidence': 0.98,
                'reason_codes': ['earlier_skill_path'],
            }
        ]
    )

    result = FileNotFoundLLMAttributor(llm).attribute(
        observations,
        failure_observation_id='failed-read',
        trace_id='trace-long',
    )

    assert result.decision == FileNotFoundDecision.UNKNOWN
    assert result.attribution_method == 'deterministic_fallback'
    assert 'llm_conflicting_path_sources' in result.reason_codes


def test_successful_write_is_lifecycle_evidence_not_a_conflicting_source() -> None:
    path = '<PATH_1>/ephemeral/report.md'
    observations = [
        _observation(
            'created',
            0,
            observation_type='TOOL',
            name='write_artifact',
            input_value={'path': path},
            output_value={'success': True, 'path': path},
        ),
        _observation(
            'cleanup',
            1,
            observation_type='SPAN',
            name='runtime.ephemeral_cleanup',
            input_value={'path': path},
            output_value={'deleted': True},
        ),
        _observation(
            'failed-read',
            2,
            observation_type='TOOL',
            name='read_file',
            input_value={'path': path},
            output_value={'error': 'ENOENT'},
            level='ERROR',
            status_message='File not found',
        ),
    ]
    llm = FakeLLM(
        [
            {
                'decision': 'attributed',
                'semantics': 'unexpected_failure',
                'is_agent_failure': True,
                'failure_observation_id': 'failed-read',
                'root_cause_observation_id': 'cleanup',
                'root_cause_label': 'runtime_path_unavailable',
                'root_cause_domain': 'runtime',
                'evidence_observation_ids': ['created', 'cleanup', 'failed-read'],
                'confidence': 0.98,
                'reason_codes': ['runtime_cleanup'],
            }
        ]
    )

    result = FileNotFoundLLMAttributor(llm).attribute(
        observations,
        failure_observation_id='failed-read',
        trace_id='trace-long',
    )

    assert result.decision == FileNotFoundDecision.ATTRIBUTED
    assert result.root_cause_observation_id == 'cleanup'


def test_llm_cannot_turn_trace_entry_assistant_context_into_local_root() -> None:
    path = r'D:\workspace\missing.py'
    observations = [
        {
            **_observation(
                'trace-input-assistant',
                0,
                observation_type='GENERATION',
                name='assistant.context',
                input_value={'filePath': path},
            ),
            'metadata_redacted': {
                'synthetic_role': 'trace_input',
                'message_role': 'assistant',
            },
        },
        _observation(
            'trigger-llm',
            1,
            observation_type='GENERATION',
            name='planner',
            output_value={
                'tool_calls': [
                    {
                        'function': {
                            'name': 'read',
                            'arguments': {'filePath': path},
                        }
                    }
                ]
            },
        ),
        _observation(
            'failed-read',
            2,
            observation_type='TOOL',
            name='read',
            input_value={'filePath': path},
            output_value={'error': 'File not found: %s' % path},
            parent_observation_id='trigger-llm',
            level='ERROR',
            status_message='File not found',
        ),
    ]
    llm = FakeLLM(
        [
            {
                'decision': 'attributed',
                'semantics': 'unexpected_failure',
                'is_agent_failure': True,
                'failure_observation_id': 'failed-read',
                'root_cause_observation_id': 'trace-input-assistant',
                'root_cause_label': 'model_path_hallucination',
                'root_cause_domain': 'model',
                'evidence_observation_ids': [
                    'trace-input-assistant',
                    'trigger-llm',
                    'failed-read',
                ],
                'confidence': 0.99,
                'reason_codes': ['assistant_context_contains_path'],
            }
        ]
    )

    result = FileNotFoundLLMAttributor(llm).attribute(
        observations,
        failure_observation_id='failed-read',
        trace_id='trace-long',
    )

    assert result.decision == FileNotFoundDecision.UNKNOWN
    assert result.root_cause_observation_id is None
    assert result.root_cause_scope == 'outside_current_trace'
    assert result.local_trigger_observation_id == 'trigger-llm'
    assert result.attribution_method == 'deterministic_fallback'
    assert 'llm_cross_trace_root_unsupported' in result.reason_codes
