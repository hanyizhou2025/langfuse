"""Synthetic, redacted cases used for local review and browser acceptance."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Dict, List, Optional


def _observation(
    observation_id: str,
    trace_id: str,
    start_time: str,
    *,
    name: str,
    observation_type: str,
    parent: Optional[str] = None,
    input_value: Any = None,
    output_value: Any = None,
    level: str = 'DEFAULT',
    status_message: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        'observation_id': observation_id,
        'trace_id': trace_id,
        'parent_observation_id': parent,
        'type': observation_type,
        'name': name,
        'start_time': start_time,
        'end_time': start_time,
        'level': level,
        'input_redacted': input_value,
        'output_redacted': output_value,
        'status_message_redacted': status_message,
    }


def _envelope(
    scenario: str,
    case_id: str,
    trace_id: str,
    observations: List[Dict[str, Any]],
    failure_id: str,
) -> Dict[str, Any]:
    project_id = 'demo-project'
    trace = {
        'trace_id': trace_id,
        'name': scenario.replace('_', ' ').title(),
        'timestamp': observations[0]['start_time'],
        'input_redacted': {'task': 'Synthetic annotation acceptance fixture'},
        'output_redacted': None,
    }
    case = {
        'case_id': case_id,
        'trace_id': trace_id,
        'sampling_mode': 'demo_fixture',
        'rule_evidence': {
            'rule_id': 'file-not-found-v1',
            'matched_observation_id': failure_id,
            'matched_field': 'output',
            'matched_text_redacted': 'File not found: <redacted-path>',
        },
        'tool_attempt': {'tool_result_observation_id': failure_id},
    }
    digest = sha256(
        json.dumps(
            {'trace': trace, 'case': case, 'observations': observations},
            ensure_ascii=False,
            sort_keys=True,
        ).encode('utf-8')
    ).hexdigest()
    return {
        'scenario': scenario,
        'project_id': project_id,
        'case': case,
        'trace': trace,
        'observations': observations,
        'source_snapshot': {
            'snapshot_at': '2026-08-14T10:00:00Z',
            'observation_count': len(observations),
            'context_truncated': False,
            'source_updated_at': '2026-08-14T10:00:00Z',
            'source_hash': digest,
        },
    }


def build_demo_dataset() -> List[Dict[str, Any]]:
    """Return five cases covering every mandatory browser-acceptance branch."""

    current_trace = 'trace-current-root'
    current_observations = [
        _observation(
            'current-user',
            current_trace,
            '2026-08-14T10:00:00Z',
            name='user.request',
            observation_type='EVENT',
            input_value={'role': 'user', 'content': 'Read the generated report.'},
        ),
        _observation(
            'current-model',
            current_trace,
            '2026-08-14T10:00:01Z',
            name='assistant.plan',
            observation_type='GENERATION',
            parent='current-user',
            output_value={'tool': 'read', 'filePath': '/workspace/invented.md'},
        ),
        _observation(
            'current-failure',
            current_trace,
            '2026-08-14T10:00:02Z',
            name='tool:read',
            observation_type='SPAN',
            parent='current-model',
            input_value={'filePath': '/workspace/invented.md'},
            output_value={'error': 'File not found: /workspace/invented.md'},
            level='ERROR',
            status_message='ENOENT',
        ),
    ]
    outside_trace = 'trace-outside-root'
    outside_observations = [
        _observation(
            'outside-entry',
            outside_trace,
            '2026-08-14T11:00:00Z',
            name='assistant.history',
            observation_type='GENERATION',
            input_value={
                'role': 'assistant',
                'content': 'Worker 5 reported <redacted-script> missing.',
            },
        ),
        _observation(
            'outside-trigger',
            outside_trace,
            '2026-08-14T11:00:01Z',
            name='assistant.tool_call',
            observation_type='GENERATION',
            parent='outside-entry',
            output_value={'tool': 'read', 'filePath': '<redacted-script>'},
        ),
        _observation(
            'outside-failure',
            outside_trace,
            '2026-08-14T11:08:30Z',
            name='tool:read',
            observation_type='SPAN',
            parent='outside-trigger',
            output_value={'error': 'File not found: <redacted-script>'},
            level='ERROR',
        ),
        _observation(
            'outside-followup',
            outside_trace,
            '2026-08-14T11:08:31Z',
            name='assistant.followup',
            observation_type='GENERATION',
            parent='outside-trigger',
            output_value='Searching for alternatives.',
        ),
    ]

    recovered_trace = 'trace-recovered'
    recovered_observations = [
        _observation(
            'recovered-probe',
            recovered_trace,
            '2026-08-14T12:00:00Z',
            name='tool:read',
            observation_type='SPAN',
            input_value={'path': '/tmp/cache.json'},
            output_value={'error': 'File not found: /tmp/cache.json'},
            level='ERROR',
        ),
        _observation(
            'recovered-create',
            recovered_trace,
            '2026-08-14T12:00:01Z',
            name='tool:write',
            observation_type='SPAN',
            output_value={'created': '/tmp/cache.json'},
        ),
        _observation(
            'recovered-success',
            recovered_trace,
            '2026-08-14T12:00:02Z',
            name='tool:read',
            observation_type='SPAN',
            input_value={'path': '/tmp/cache.json'},
            output_value={'cached': True},
        ),
    ]

    false_trace = 'trace-rule-false-positive'
    false_observations = [
        _observation(
            'false-content',
            false_trace,
            '2026-08-14T13:00:00Z',
            name='tool:read_documentation',
            observation_type='SPAN',
            output_value={
                'content': 'The message "file not found" is documented here.'
            },
        )
    ]

    unknown_trace = 'trace-unknown'
    unknown_observations = [
        _observation(
            'unknown-failure',
            unknown_trace,
            '2026-08-14T14:00:00Z',
            name='tool:read',
            observation_type='SPAN',
            output_value={'error': 'ENOENT'},
            level='ERROR',
        )
    ]

    return [
        _envelope(
            'current_trace_root',
            'demo-current-root',
            current_trace,
            current_observations,
            'current-failure',
        ),
        _envelope(
            'outside_current_trace',
            'demo-outside-root',
            outside_trace,
            outside_observations,
            'outside-failure',
        ),
        _envelope(
            'recovered_failure',
            'demo-recovered',
            recovered_trace,
            recovered_observations,
            'recovered-probe',
        ),
        _envelope(
            'rule_false_positive',
            'demo-false-positive',
            false_trace,
            false_observations,
            'false-content',
        ),
        _envelope(
            'unknown',
            'demo-unknown',
            unknown_trace,
            unknown_observations,
            'unknown-failure',
        ),
    ]
