from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Dict, List, Optional

from agentdebug.diagnose.detect import HeuristicAnalyzer
from agentdebug.integrations.langfuse_attribution import (
    CauseKind,
    LangfuseToolAttributor,
    attach_tool_attributions,
    convert_langfuse_observations,
)


def _observation(
    observation_id: str,
    *,
    observation_type: str = 'SPAN',
    name: str = 'step',
    input_value: object = None,
    output_value: object = None,
    status_message: Optional[str] = None,
    attribution: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    metadata: Dict[str, object] = {}
    if attribution is not None:
        metadata['attribution'] = attribution
    return {
        'id': observation_id,
        'trace_id': 'trace-1',
        'parent_observation_id': None,
        'type': observation_type,
        'name': name,
        'start_time': datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        'input': input_value,
        'output': output_value,
        'metadata': metadata,
        'level': 'DEFAULT',
        'status_message': status_message,
    }


def _missing_path_observations(source_kind: str) -> List[Dict[str, object]]:
    return [
        _observation(
            'source',
            observation_type='GENERATION' if source_kind == 'model' else 'SPAN',
            name=source_kind,
            output_value={'path': '/workspace/missing.md'},
            attribution={
                'kind': source_kind,
                'values': {'path': '/workspace/missing.md'},
            },
        ),
        _observation(
            'call',
            name='read_file',
            input_value={'path': '/workspace/missing.md'},
            attribution={
                'kind': 'tool_call',
                'tool_name': 'read_file',
                'arguments': {'path': '/workspace/missing.md'},
                'argument_sources': {
                    'path': {'kind': source_kind, 'observation_id': 'source'},
                },
            },
        ),
        _observation(
            'result',
            name='read_file',
            status_message='[ENOENT] no such file or directory',
            attribution={
                'kind': 'tool_result',
                'tool_call_observation_id': 'call',
                'error_code': 'ENOENT',
                'execution_context': {'cwd': '/workspace'},
                'resource_existence': {'path': False},
            },
        ),
    ]


def test_converter_excludes_arbitrary_json_from_heuristic_text() -> None:
    converted = convert_langfuse_observations(
        [
            _observation(
                'safe-json',
                input_value={
                    'error': 'parse error',
                    'status': 'failed',
                    'payload': {'json': 'malformed'},
                },
                output_value={'message': 'tool execution error'},
            )
        ]
    )

    event = converted.trajectory.events[0]
    report = HeuristicAnalyzer().analyze(converted.trajectory)

    assert event.input is None
    assert event.output is None
    assert event.event_id == 'safe-json'
    assert report.findings == []


def test_converter_keeps_true_tool_error_detectable_without_raw_io() -> None:
    converted = convert_langfuse_observations(_missing_path_observations('model'))

    report = HeuristicAnalyzer().analyze(converted.trajectory)

    assert [finding.event_id for finding in report.findings] == ['result']
    assert report.findings[0].failure_mode.mode_id == 'system.tool_execution_error'
    assert converted.trajectory.events[-1].input is None
    assert converted.trajectory.events[-1].output is None


def test_attributor_identifies_model_generated_missing_path() -> None:
    converted = convert_langfuse_observations(_missing_path_observations('model'))

    attribution = LangfuseToolAttributor().attribute(converted.evidence)[0]

    assert attribution.primary_cause == CauseKind.MODEL_ARGUMENT_HALLUCINATION
    assert attribution.confidence == 0.95
    assert attribution.source_observation_id == 'source'
    assert any('does not exist' in item for item in attribution.evidence)


def test_attributor_identifies_user_and_skill_parameter_sources() -> None:
    user = LangfuseToolAttributor().attribute(
        convert_langfuse_observations(_missing_path_observations('user')).evidence
    )[0]
    skill = LangfuseToolAttributor().attribute(
        convert_langfuse_observations(_missing_path_observations('skill')).evidence
    )[0]

    assert user.primary_cause == CauseKind.USER_INPUT_INVALID
    assert skill.primary_cause == CauseKind.SKILL_CONFIGURATION_INVALID


def test_attributor_prefers_runtime_cause_when_resource_existed() -> None:
    observations = _missing_path_observations('model')
    result_hint = observations[-1]['metadata']['attribution']
    assert isinstance(result_hint, dict)
    result_hint['resource_existence'] = {'path': True}

    attribution = LangfuseToolAttributor().attribute(
        convert_langfuse_observations(observations).evidence
    )[0]

    assert attribution.primary_cause == CauseKind.RUNTIME_ENVIRONMENT_MISMATCH
    assert attribution.source_observation_id == 'result'


def test_attributor_classifies_external_rate_limit_without_blame() -> None:
    observations = [
        _observation(
            'call',
            name='search',
            attribution={
                'kind': 'tool_call',
                'tool_name': 'search',
                'arguments': {'query': 'weather'},
            },
        ),
        _observation(
            'result',
            name='search',
            status_message='upstream rate limit',
            attribution={
                'kind': 'tool_result',
                'tool_call_observation_id': 'call',
                'error_code': 'HTTP_429',
                'dependency': 'search-api',
            },
        ),
    ]

    attribution = LangfuseToolAttributor().attribute(
        convert_langfuse_observations(observations).evidence
    )[0]

    assert attribution.primary_cause == CauseKind.EXTERNAL_DEPENDENCY_RATE_LIMIT
    assert attribution.confidence == 0.9


def test_attributor_identifies_model_tool_selection_error() -> None:
    observations = [
        _observation(
            'decision',
            observation_type='GENERATION',
            attribution={'kind': 'model'},
        ),
        _observation(
            'call',
            name='read_file',
            attribution={
                'kind': 'tool_call',
                'tool_name': 'read_file',
                'arguments': {'url': 'https://example.com'},
                'tool_selection_source': {
                    'kind': 'model',
                    'observation_id': 'decision',
                },
            },
        ),
        _observation(
            'result',
            name='read_file',
            status_message='unsupported operation',
            attribution={
                'kind': 'tool_result',
                'tool_call_observation_id': 'call',
                'error_code': 'UNSUPPORTED_OPERATION',
            },
        ),
    ]

    attribution = LangfuseToolAttributor().attribute(
        convert_langfuse_observations(observations).evidence
    )[0]

    assert attribution.primary_cause == CauseKind.MODEL_TOOL_SELECTION_ERROR
    assert attribution.source_observation_id == 'decision'


def test_attributor_labels_user_credential_failure_as_configuration() -> None:
    observations = [
        _observation(
            'call',
            name='private_api',
            attribution={
                'kind': 'tool_call',
                'tool_name': 'private_api',
                'arguments': {},
            },
        ),
        _observation(
            'result',
            name='private_api',
            status_message='403 forbidden',
            attribution={
                'kind': 'tool_result',
                'tool_call_observation_id': 'call',
                'error_code': 'HTTP_403',
                'execution_context': {
                    'credential_source': {
                        'kind': 'user',
                        'observation_id': 'user-settings',
                    },
                },
            },
        ),
    ]

    attribution = LangfuseToolAttributor().attribute(
        convert_langfuse_observations(observations).evidence
    )[0]

    assert attribution.primary_cause == CauseKind.CREDENTIAL_OR_PERMISSION_CONFIGURATION
    assert attribution.source_observation_id == 'user-settings'


def test_attach_tool_attributions_persists_only_safe_ui_summary() -> None:
    converted = convert_langfuse_observations(_missing_path_observations('model'))

    attach_tool_attributions(converted.trajectory, converted.evidence)

    payload = converted.trajectory.metadata['langfuse_tool_attributions']
    assert payload[0]['primary_cause'] == 'model_argument_hallucination'
    assert payload[0]['source_observation_id'] == 'source'
    assert '/workspace/missing.md' not in json.dumps(payload)
