from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from agentdebug.integrations.langfuse_attribution import (
    ValueSource,
    convert_historical_langfuse_observations,
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
    parent_observation_id: Optional[str] = None,
) -> Dict[str, object]:
    return {
        'id': observation_id,
        'trace_id': 'trace-historical',
        'parent_observation_id': parent_observation_id,
        'type': observation_type,
        'name': name,
        'start_time': (BASE_TIME + timedelta(seconds=index)).isoformat(),
        'input': input_value,
        'output': output_value,
        'metadata': {},
        'level': level,
        'status_message': status_message,
    }


def _failed_read(path: str) -> Dict[str, object]:
    return _observation(
        'tool-result',
        3,
        observation_type='TOOL',
        name='read_file',
        input_value={'path': path},
        output_value={
            'error': 'ENOENT',
            'message': 'no such file or directory',
        },
        level='ERROR',
        status_message='File not found',
        parent_observation_id='agent',
    )


def test_historical_converter_reconstructs_model_path_source() -> None:
    path = '/workspace/missing.md'
    observations: List[Dict[str, object]] = [
        _observation(
            'agent',
            0,
            observation_type='AGENT',
            name='file-agent',
            input_value={'task': 'Summarize the report.'},
        ),
        _observation(
            'generation',
            1,
            observation_type='GENERATION',
            name='planner',
            output_value={
                'tool_calls': [
                    {'name': 'read_file', 'arguments': {'path': path}},
                ]
            },
            parent_observation_id='agent',
        ),
        _failed_read(path),
    ]

    converted = convert_historical_langfuse_observations(observations)

    assert len(converted.evidence.tool_failures) == 1
    failure = converted.evidence.tool_failures[0]
    assert failure.tool_result_observation_id == 'tool-result'
    assert failure.arguments == {'path': path}
    assert failure.argument_sources['path'].kind == ValueSource.MODEL
    assert failure.argument_sources['path'].observation_id == 'generation'


def test_historical_converter_keeps_user_as_original_path_source() -> None:
    path = '/workspace/user-provided.md'
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

    converted = convert_historical_langfuse_observations(observations)

    source = converted.evidence.tool_failures[0].argument_sources['path']
    assert source.kind == ValueSource.USER
    assert source.observation_id == 'user-request'


def test_historical_converter_does_not_treat_successful_document_text_as_failure() -> (
    None
):
    observations = [
        _observation(
            'successful-read',
            0,
            observation_type='TOOL',
            name='read_file',
            input_value={'path': '/workspace/help.md'},
            output_value={
                'content': 'Troubleshooting section: file not found errors.',
            },
        )
    ]

    converted = convert_historical_langfuse_observations(observations)

    assert converted.evidence.tool_failures == []


def test_historical_converter_accepts_scalar_rule_candidate_without_error_level() -> (
    None
):
    path = '/workspace/missing.md'
    observations = [
        _observation(
            'tool-result',
            0,
            observation_type='TOOL',
            name='read_file',
            input_value={'path': path},
            output_value='File not found: %s' % path,
        )
    ]

    converted = convert_historical_langfuse_observations(observations)

    assert (
        converted.evidence.tool_failures[0].tool_result_observation_id == 'tool-result'
    )


def test_historical_converter_marks_unseen_path_source_unknown() -> None:
    path = '/workspace/unseen.md'
    observations = [
        _observation(
            'agent',
            0,
            observation_type='AGENT',
            name='file-agent',
            input_value={'task': 'Read the requested file.'},
        ),
        _failed_read(path),
    ]

    converted = convert_historical_langfuse_observations(observations)

    source = converted.evidence.tool_failures[0].argument_sources['path']
    assert source.kind == ValueSource.UNKNOWN
    assert source.observation_id is None


def test_historical_converter_accepts_camel_case_path_in_json_arguments() -> None:
    path = r'D:\workspace\missing.py'
    observations: List[Dict[str, object]] = [
        _observation(
            'generation',
            1,
            observation_type='GENERATION',
            name='planner',
            output_value={
                'tool_calls': [
                    {
                        'function': {
                            'name': 'read',
                            'arguments': '{"filePath":"D:\\\\workspace\\\\missing.py"}',
                        }
                    }
                ]
            },
        ),
        _observation(
            'tool-result',
            2,
            observation_type='TOOL',
            name='read',
            input_value={'filePath': path},
            output_value={'error': 'File not found: %s' % path},
            level='ERROR',
            status_message='File not found',
        ),
    ]

    converted = convert_historical_langfuse_observations(observations)

    failure = converted.evidence.tool_failures[0]
    assert failure.arguments == {'filePath': path}
    assert failure.argument_sources['filePath'].kind == ValueSource.MODEL
    assert failure.argument_sources['filePath'].observation_id == 'generation'
