from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List

from agentdebug.integrations.langfuse_attribution import (
    evaluate_file_not_found_predictions,
    predict_file_not_found_cases,
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
    status_message: str = '',
) -> Dict[str, object]:
    return {
        'observation_id': observation_id,
        'trace_id': 'trace-eval',
        'parent_observation_id': None,
        'type': observation_type,
        'name': name,
        'start_time': (BASE_TIME + timedelta(seconds=index)).isoformat(),
        'input_redacted': input_value,
        'output_redacted': output_value,
        'metadata_redacted': {},
        'level': level,
        'status_message_redacted': status_message,
    }


def _model_failure_case() -> tuple[Dict[str, object], List[Dict[str, object]]]:
    path = '<PATH_1>/missing.md'
    case = {
        'case_id': 'case-model',
        'trace_id': 'trace-eval',
        'rule_evidence': {'matched_observation_id': 'failed-read'},
        'tool_attempt': {'tool_result_observation_id': 'failed-read'},
    }
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
    return case, observations


def test_predict_cases_accepts_redacted_dataset_shape() -> None:
    case, observations = _model_failure_case()

    predictions = predict_file_not_found_cases(
        [case],
        observations,
    )

    assert len(predictions) == 1
    assert predictions[0]['case_id'] == 'case-model'
    assert predictions[0]['decision'] == 'attributed'
    assert predictions[0]['root_cause_label'] == 'model_path_hallucination'
    assert predictions[0]['root_cause_observation_id'] == 'generation'


def test_predict_cases_uses_trace_input_as_user_path_source() -> None:
    path = '<PATH_1>/user-provided.md'
    case = {
        'case_id': 'case-user',
        'trace_id': 'trace-user',
        'tool_attempt': {'tool_result_observation_id': 'failed-read'},
    }
    traces = [
        {
            'trace_id': 'trace-user',
            'timestamp': '2026-01-01T00:00:00Z',
            'input_redacted': {'message': 'Read %s' % path},
            'output_redacted': {'message': 'Unable to read file.'},
        }
    ]
    observations = [
        {
            **_observation(
                'generation',
                1,
                observation_type='GENERATION',
                name='planner',
                output_value={'tool': 'read_file', 'path': path},
            ),
            'trace_id': 'trace-user',
        },
        {
            **_observation(
                'failed-read',
                2,
                observation_type='TOOL',
                name='read_file',
                input_value={'path': path},
                output_value={'error': 'ENOENT'},
                level='ERROR',
                status_message='File not found',
            ),
            'trace_id': 'trace-user',
        },
    ]

    predictions = predict_file_not_found_cases(
        [case],
        observations,
        traces=traces,
    )

    assert predictions[0]['decision'] == 'attributed'
    assert predictions[0]['root_cause_label'] == 'user_path_invalid'
    assert predictions[0]['root_cause_observation_id'] == 'trace-user:input'


def test_evaluation_reports_layered_counts() -> None:
    predictions = [
        {
            'case_id': 'negative',
            'decision': 'not_agent_failure',
            'root_cause_label': None,
            'root_cause_observation_id': None,
        },
        {
            'case_id': 'model',
            'decision': 'attributed',
            'root_cause_label': 'model_path_hallucination',
            'root_cause_observation_id': 'generation',
        },
        {
            'case_id': 'unknown',
            'decision': 'unknown',
            'root_cause_label': None,
            'root_cause_observation_id': None,
        },
    ]
    annotations = [
        {
            'case_id': 'negative',
            'technical_error_review': {'human_label': 'confirmed'},
            'semantic_outcome': {'is_agent_failure': False},
            'attribution': {'applicable': False},
        },
        {
            'case_id': 'model',
            'technical_error_review': {'human_label': 'confirmed'},
            'semantic_outcome': {'is_agent_failure': True},
            'attribution': {
                'applicable': True,
                'root_cause_label': 'model_path_hallucination',
                'primary_root_cause_observation_id': 'generation',
            },
        },
        {
            'case_id': 'unknown',
            'technical_error_review': {'human_label': 'insufficient_evidence'},
            'semantic_outcome': {'is_agent_failure': None},
            'attribution': {'applicable': False},
        },
    ]

    report = evaluate_file_not_found_predictions(predictions, annotations)

    assert report['case_count'] == 3
    assert report['semantic_negative']['correct'] == 1
    assert report['semantic_negative']['total'] == 1
    assert report['attribution']['label_correct'] == 1
    assert report['attribution']['root_observation_correct'] == 1
    assert report['attribution']['total'] == 1
    assert report['abstention']['correct'] == 1
    assert report['abstention']['total'] == 1


def test_evaluation_counts_forced_attribution_on_negative() -> None:
    report = evaluate_file_not_found_predictions(
        [
            {
                'case_id': 'negative',
                'decision': 'attributed',
                'root_cause_label': 'model_path_hallucination',
                'root_cause_observation_id': 'generation',
            }
        ],
        [
            {
                'case_id': 'negative',
                'technical_error_review': {'human_label': 'confirmed'},
                'semantic_outcome': {'is_agent_failure': False},
                'attribution': {'applicable': False},
            }
        ],
    )

    assert report['semantic_negative']['forced_attribution'] == 1
