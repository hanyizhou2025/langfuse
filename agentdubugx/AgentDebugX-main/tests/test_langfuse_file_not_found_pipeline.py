from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from agentdebug.integrations.langfuse_attribution import (
    FileNotFoundDecision,
    attribute_historical_file_not_found,
)


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _observation(
    observation_id: str,
    index: int,
    *,
    observation_type: str,
    name: str,
    input_value: object = None,
    output_value: object = None,
    level: str = 'DEFAULT',
    status_message: Optional[str] = None,
) -> Dict[str, object]:
    return {
        'id': observation_id,
        'trace_id': 'trace-pipeline',
        'parent_observation_id': None,
        'type': observation_type,
        'name': name,
        'start_time': (BASE_TIME + timedelta(seconds=index)).isoformat(),
        'input': input_value,
        'output': output_value,
        'metadata': {},
        'level': level,
        'status_message': status_message,
    }


def _failed_read(path: str, *, name: str = 'read_file') -> Dict[str, object]:
    return _observation(
        'failed-read',
        2,
        observation_type='TOOL',
        name=name,
        input_value={'path': path},
        output_value={'error': 'ENOENT'},
        level='ERROR',
        status_message='File not found',
    )


def test_pipeline_attributes_model_generated_path() -> None:
    path = '/workspace/missing.md'
    observations: List[Dict[str, object]] = [
        _observation(
            'generation',
            0,
            observation_type='GENERATION',
            name='planner',
            output_value={'tool': 'read_file', 'path': path},
        ),
        _failed_read(path),
    ]

    result = attribute_historical_file_not_found(
        observations,
        failure_observation_id='failed-read',
        case_id='case-model',
    )

    assert result.case_id == 'case-model'
    assert result.decision == FileNotFoundDecision.ATTRIBUTED
    assert result.is_agent_failure is True
    assert result.failure_observation_id == 'failed-read'
    assert result.root_cause_observation_id == 'generation'
    assert result.root_cause_label == 'model_path_hallucination'
    assert result.evidence_observation_ids == ['generation', 'failed-read']


def test_pipeline_attributes_user_provided_path() -> None:
    path = '/workspace/user.md'
    observations: List[Dict[str, object]] = [
        _observation(
            'user-request',
            0,
            observation_type='SPAN',
            name='user.request',
            input_value={'message': 'Read %s' % path},
        ),
        _observation(
            'generation',
            1,
            observation_type='GENERATION',
            name='planner',
            output_value={'tool': 'read_file', 'path': path},
        ),
        _failed_read(path),
    ]

    result = attribute_historical_file_not_found(
        observations,
        failure_observation_id='failed-read',
    )

    assert result.decision == FileNotFoundDecision.ATTRIBUTED
    assert result.root_cause_observation_id == 'user-request'
    assert result.root_cause_label == 'user_path_invalid'


def test_pipeline_stops_before_attribution_for_expected_missing_file() -> None:
    observations = [
        _failed_read('/workspace/report.md', name='check_file_exists'),
    ]

    result = attribute_historical_file_not_found(
        observations,
        failure_observation_id='failed-read',
    )

    assert result.decision == FileNotFoundDecision.NOT_AGENT_FAILURE
    assert result.root_cause_observation_id is None
    assert result.root_cause_label is None
    assert result.semantics == 'validation_probe'


def test_pipeline_returns_unknown_when_path_source_is_missing() -> None:
    observations = [_failed_read('/workspace/unseen.md')]

    result = attribute_historical_file_not_found(
        observations,
        failure_observation_id='failed-read',
    )

    assert result.decision == FileNotFoundDecision.UNKNOWN
    assert result.root_cause_observation_id is None
    assert 'insufficient_semantic_or_source_evidence' in result.reason_codes
    assert 'file_not_found_confirmed' in result.reason_codes


def test_pipeline_returns_invalid_case_for_unknown_failure_id() -> None:
    result = attribute_historical_file_not_found(
        [_failed_read('/workspace/unseen.md')],
        failure_observation_id='missing-id',
    )

    assert result.decision == FileNotFoundDecision.INVALID_CASE


def test_pipeline_reports_current_trace_reference_when_root_precedes_trace() -> None:
    path = r'D:\workspace\missing.py'
    observations: List[Dict[str, object]] = [
        {
            **_observation(
                'initial-llm',
                0,
                observation_type='GENERATION',
                name='initial-context',
                input_value={
                    'assistant': {
                        'text': 'Worker 5 report is missing.',
                        'tool_calls': [
                            {
                                'function': {
                                    'name': 'read',
                                    'arguments': {'filePath': path},
                                }
                            }
                        ],
                    }
                },
            ),
            'metadata': {'message_role': 'assistant'},
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
        {
            **_failed_read(path, name='read'),
            'input': {'filePath': path},
            'parent_observation_id': 'trigger-llm',
        },
        _observation(
            'downstream-search',
            3,
            observation_type='TOOL',
            name='glob',
            input_value={'pattern': '**/missing.py'},
            output_value={'matches': []},
        ),
    ]

    result = attribute_historical_file_not_found(
        observations,
        failure_observation_id='failed-read',
        case_id='case-inherited',
    )

    assert result.decision == FileNotFoundDecision.UNKNOWN
    assert result.is_agent_failure is None
    assert result.failure_observation_id == 'failed-read'
    assert result.root_cause_observation_id is None
    assert result.root_cause_label is None
    assert result.root_cause_scope == 'outside_current_trace'
    assert result.earliest_local_evidence_observation_id == 'initial-llm'
    assert result.local_trigger_observation_id == 'trigger-llm'
    assert result.propagation_observation_ids == [
        'initial-llm',
        'trigger-llm',
        'failed-read',
    ]
    assert result.reference_confidence == 0.95
    assert result.evidence_observation_ids == [
        'initial-llm',
        'trigger-llm',
        'failed-read',
    ]
    assert 'root_precedes_trace_boundary' in result.reason_codes
    assert 'downstream_outcome_unclear' in result.reason_codes
