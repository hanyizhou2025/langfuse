from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pytest

from agentdebug.integrations.langfuse_attribution import (
    FailureGateDecision,
    evaluate_file_not_found_failure,
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
        'trace_id': 'trace-gate',
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


def test_failure_gate_accepts_explicit_existence_probe() -> None:
    path = '/workspace/report.md'
    observations = [
        _observation(
            'decision',
            0,
            observation_type='GENERATION',
            name='planner',
            output_value={'plan': 'Check whether the report exists.'},
        ),
        _failed_read(path, name='check_file_exists'),
    ]

    result = evaluate_file_not_found_failure(
        observations,
        failure_observation_id='failed-read',
    )

    assert result.decision == FailureGateDecision.NOT_AGENT_FAILURE
    assert result.semantics == 'validation_probe'


def test_failure_gate_accepts_create_after_missing_control_flow() -> None:
    path = '/workspace/report.md'
    observations: List[Dict[str, object]] = [
        _observation(
            'decision',
            0,
            observation_type='GENERATION',
            name='planner',
            output_value={'tool': 'read_file', 'path': path},
        ),
        _failed_read(path),
        _observation(
            'create-file',
            3,
            observation_type='TOOL',
            name='write_file',
            input_value={'path': path, 'content': '# Report'},
            output_value={'created': True},
        ),
    ]

    result = evaluate_file_not_found_failure(
        observations,
        failure_observation_id='failed-read',
    )

    assert result.decision == FailureGateDecision.NOT_AGENT_FAILURE
    assert result.semantics == 'control_flow_signal'
    assert result.evidence_observation_ids == ['failed-read', 'create-file']


def test_failure_gate_returns_failure_candidate_for_unrecovered_known_source() -> None:
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
        _observation(
            'final',
            3,
            observation_type='AGENT',
            name='file-agent',
            output_value={'message': 'Unable to complete the task.'},
            level='ERROR',
        ),
    ]

    result = evaluate_file_not_found_failure(
        observations,
        failure_observation_id='failed-read',
        path_source_observation_id='generation',
    )

    assert result.decision == FailureGateDecision.FAILURE_CANDIDATE
    assert result.semantics == 'unexpected_failure'
    assert result.is_agent_failure is True


def test_failure_gate_returns_unknown_without_source_or_semantic_evidence() -> None:
    observations = [_failed_read('/workspace/missing.md')]

    result = evaluate_file_not_found_failure(
        observations,
        failure_observation_id='failed-read',
    )

    assert result.decision == FailureGateDecision.UNKNOWN
    assert result.is_agent_failure is None


def test_failure_gate_rejects_non_file_not_found_observation() -> None:
    observations = [
        _observation(
            'success',
            0,
            observation_type='TOOL',
            name='read_file',
            output_value={'content': 'ok'},
        )
    ]

    result = evaluate_file_not_found_failure(
        observations,
        failure_observation_id='success',
    )

    assert result.decision == FailureGateDecision.INVALID_CASE


@pytest.mark.parametrize(
    'output_value',
    [
        {'error': 'ENOENT'},
        'File not found: /workspace/missing.md',
    ],
)
def test_failure_gate_accepts_rule_candidate_without_error_level(
    output_value: object,
) -> None:
    observations = [
        _observation(
            'failed-read',
            0,
            observation_type='TOOL',
            name='read_file',
            input_value={'path': '/workspace/missing.md'},
            output_value=output_value,
            level='DEFAULT',
        )
    ]

    result = evaluate_file_not_found_failure(
        observations,
        failure_observation_id='failed-read',
    )

    assert result.decision == FailureGateDecision.UNKNOWN
