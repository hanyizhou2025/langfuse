"""Reproducible, redacted long-trace dataset for File Not Found experiments."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class LongFileNotFoundDataset:
    """Four-file dataset plus a manifest describing synthetic coverage."""

    traces: List[Dict[str, Any]]
    observations: List[Dict[str, Any]]
    cases: List[Dict[str, Any]]
    annotations: List[Dict[str, Any]]
    manifest: Dict[str, Any]


@dataclass(frozen=True)
class _Scenario:
    name: str
    semantic_label: str
    is_agent_failure: Optional[bool]
    root_cause_label: Optional[str]
    root_cause_domain: Optional[str]
    root_kind: Optional[str]
    tool_name: str = 'read_file'
    failed_path: str = '<PATH_1>/target/report.md'
    user_task: str = 'Read the requested report and summarize it.'


_SCENARIOS = (
    _Scenario(
        'model_hallucination',
        'unexpected_failure',
        True,
        'model_path_hallucination',
        'model',
        'model',
    ),
    _Scenario(
        'user_invalid_path',
        'unexpected_failure',
        True,
        'user_path_invalid',
        'user',
        'user',
        user_task='Read <PATH_1>/target/report.md and summarize it.',
    ),
    _Scenario(
        'skill_stale_path',
        'unexpected_failure',
        True,
        'upstream_path_invalid',
        'upstream',
        'skill',
    ),
    _Scenario(
        'runtime_mount_unavailable',
        'unexpected_failure',
        True,
        'runtime_path_unavailable',
        'runtime',
        'runtime_mount',
    ),
    _Scenario(
        'expected_validation_probe',
        'validation_probe',
        False,
        None,
        None,
        None,
        tool_name='path_exists_probe',
        user_task='Check whether the optional report exists before using it.',
    ),
    _Scenario(
        'recovered_after_create',
        'control_flow_signal',
        False,
        None,
        None,
        'model',
        user_task='Read the report; create a default report if it is absent.',
    ),
    _Scenario(
        'expected_cache_miss_fallback',
        'expected_absence',
        False,
        None,
        None,
        None,
        tool_name='cache_read',
        failed_path='<PATH_1>/cache/result.json',
        user_task='Use cache when present, otherwise compute the result.',
    ),
    _Scenario(
        'rule_match_expected_absence',
        'expected_absence',
        False,
        None,
        None,
        'user',
        failed_path='<PATH_1>/must-not-exist.flag',
        user_task='Verify <PATH_1>/must-not-exist.flag is absent, then finish.',
    ),
    _Scenario(
        'conflicting_path_sources',
        'unknown',
        None,
        None,
        None,
        'conflict',
    ),
    _Scenario(
        'runtime_file_deleted',
        'unexpected_failure',
        True,
        'runtime_path_unavailable',
        'runtime',
        'runtime_delete',
        failed_path='<PATH_1>/ephemeral/generated.md',
    ),
    _Scenario(
        'upstream_artifact_stale',
        'unexpected_failure',
        True,
        'upstream_path_invalid',
        'upstream',
        'upstream_tool',
        failed_path='<PATH_1>/artifacts/old-build/report.md',
    ),
    _Scenario(
        'insufficient_evidence',
        'unknown',
        None,
        None,
        None,
        None,
        failed_path='<PATH_1>/opaque/missing.bin',
    ),
)


def build_long_file_not_found_dataset(
    *,
    noise_observation_count: int = 800,
) -> LongFileNotFoundDataset:
    """Build 12 long traces containing distinct causal and semantic shapes."""

    if noise_observation_count < 500:
        raise ValueError('noise_observation_count must be at least 500')

    traces: List[Dict[str, Any]] = []
    observations: List[Dict[str, Any]] = []
    cases: List[Dict[str, Any]] = []
    annotations: List[Dict[str, Any]] = []
    observation_counts: Dict[str, int] = {}

    for scenario_index, scenario in enumerate(_SCENARIOS):
        trace_id = 'trace-long-%02d-%s' % (scenario_index + 1, scenario.name)
        case_id = 'case-long-%02d-%s' % (scenario_index + 1, scenario.name)
        trace_time = _BASE_TIME + timedelta(hours=scenario_index)
        traces.append(
            {
                'trace_id': trace_id,
                'timestamp': trace_time.isoformat(),
                'input_redacted': {'task': scenario.user_task},
                'output_redacted': _trace_output(scenario),
                'metadata_redacted': {
                    'fixture': 'long_file_not_found_v1',
                    'scenario': scenario.name,
                    'synthetic': True,
                },
            }
        )
        trace_observations = _noise_observations(
            trace_id,
            trace_time,
            noise_observation_count,
        )
        causal, failure_id, root_id, evidence_ids = _causal_observations(
            scenario,
            trace_id=trace_id,
            trace_time=trace_time,
            start_index=noise_observation_count,
        )
        trace_observations.extend(causal)
        trace_observations.sort(key=lambda item: str(item['start_time']))
        observations.extend(trace_observations)
        observation_counts[trace_id] = len(trace_observations)
        cases.append(
            {
                'case_id': case_id,
                'trace_id': trace_id,
                'sampling_mode': 'synthetic_coverage',
                'scenario': scenario.name,
                'rule_evidence': {
                    'rule_id': 'file-not-found-v1',
                    'matched_observation_id': failure_id,
                    'matched_field': 'status_message_redacted',
                    'matched_text_redacted': 'File not found: <PATH_1>',
                },
                'tool_attempt': {'tool_result_observation_id': failure_id},
            }
        )
        annotations.append(
            _annotation(
                case_id=case_id,
                scenario=scenario,
                failure_id=failure_id,
                root_id=root_id,
                evidence_ids=evidence_ids,
            )
        )

    manifest: Dict[str, Any] = {
        'dataset_id': 'agentdebugx-file-not-found-long-v1',
        'synthetic': True,
        'purpose': 'stress and compare deterministic, LLM, and hybrid attribution',
        'scenario_count': len(_SCENARIOS),
        'scenarios': [scenario.name for scenario in _SCENARIOS],
        'trace_count': len(traces),
        'case_count': len(cases),
        'observation_count': len(observations),
        'noise_observation_count_per_trace': noise_observation_count,
        'minimum_observations_per_trace': min(observation_counts.values()),
        'maximum_observations_per_trace': max(observation_counts.values()),
        'observation_counts': observation_counts,
        'privacy_contract': 'only *_redacted payload fields are model eligible',
    }
    return LongFileNotFoundDataset(
        traces=traces,
        observations=observations,
        cases=cases,
        annotations=annotations,
        manifest=manifest,
    )


def write_long_file_not_found_dataset(
    dataset: LongFileNotFoundDataset,
    output_dir: Path,
) -> None:
    """Write the standard four JSONL files and one manifest."""

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / 'traces.jsonl', dataset.traces)
    _write_jsonl(output_dir / 'observations.jsonl', dataset.observations)
    _write_jsonl(output_dir / 'cases.jsonl', dataset.cases)
    _write_jsonl(output_dir / 'annotations.jsonl', dataset.annotations)
    (output_dir / 'manifest.json').write_text(
        json.dumps(dataset.manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + '\n',
        encoding='utf-8',
    )


def _noise_observations(
    trace_id: str,
    trace_time: datetime,
    count: int,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for index in range(count):
        group = index // 25
        parent_id = (
            None
            if index % 25 == 0
            else '%s-noise-%04d'
            % (
                trace_id,
                group * 25,
            )
        )
        result.append(
            _observation(
                trace_id,
                '%s-noise-%04d' % (trace_id, index),
                trace_time,
                index,
                observation_type='SPAN' if index % 5 else 'GENERATION',
                name='unrelated.batch_%02d' % (index % 17),
                parent_id=parent_id,
                input_value={
                    'record_id': '<ID_%04d>' % index,
                    'resource': '<PATH_2>/unrelated/%04d.json' % index,
                },
                output_value={
                    'status': 'ok',
                    'summary': 'Unrelated successful processing step %04d.' % index,
                },
            )
        )
    return result


def _causal_observations(
    scenario: _Scenario,
    *,
    trace_id: str,
    trace_time: datetime,
    start_index: int,
) -> tuple[List[Dict[str, Any]], str, Optional[str], List[str]]:
    path = scenario.failed_path
    failure_id = '%s-failed-read' % trace_id
    root_id: Optional[str] = None
    evidence_ids: List[str] = []
    rows: List[Dict[str, Any]] = []

    if scenario.root_kind == 'model':
        root_id = '%s-model-plan' % trace_id
        rows.append(
            _observation(
                trace_id,
                root_id,
                trace_time,
                start_index,
                observation_type='GENERATION',
                name='planner.generate_tool_call',
                output_value={'tool': scenario.tool_name, 'path': path},
            )
        )
    elif scenario.root_kind == 'user':
        root_id = '%s:input' % trace_id
    elif scenario.root_kind == 'skill':
        root_id = '%s-skill-config' % trace_id
        rows.append(
            _observation(
                trace_id,
                root_id,
                trace_time,
                start_index,
                observation_type='SPAN',
                name='report_skill.memory_lookup',
                output_value={'configured_report_path': path, 'config_version': 'v1'},
            )
        )
    elif scenario.root_kind == 'runtime_mount':
        root_id = '%s-runtime-mount' % trace_id
        rows.append(
            _observation(
                trace_id,
                root_id,
                trace_time,
                start_index,
                observation_type='SPAN',
                name='runtime.mount_check',
                output_value={
                    'requested_path': path,
                    'workspace_mount': 'unavailable',
                    'cwd': '<PATH_1>/workspace',
                },
            )
        )
    elif scenario.root_kind == 'conflict':
        skill_id = '%s-skill-path' % trace_id
        model_id = '%s-model-path' % trace_id
        rows.extend(
            [
                _observation(
                    trace_id,
                    skill_id,
                    trace_time,
                    start_index,
                    observation_type='SPAN',
                    name='report_skill.memory_lookup',
                    output_value={'path': path},
                ),
                _observation(
                    trace_id,
                    model_id,
                    trace_time,
                    start_index + 1,
                    observation_type='GENERATION',
                    name='planner.resolve_path',
                    output_value={'path': path},
                ),
            ]
        )
        evidence_ids.extend([skill_id, model_id])
    elif scenario.root_kind == 'runtime_delete':
        created_id = '%s-created-artifact' % trace_id
        root_id = '%s-runtime-cleanup' % trace_id
        rows.extend(
            [
                _observation(
                    trace_id,
                    created_id,
                    trace_time,
                    start_index,
                    observation_type='TOOL',
                    name='write_artifact',
                    input_value={'path': path},
                    output_value={'success': True, 'path': path},
                ),
                _observation(
                    trace_id,
                    root_id,
                    trace_time,
                    start_index + 1,
                    observation_type='SPAN',
                    name='runtime.ephemeral_cleanup',
                    input_value={'path': path},
                    output_value={'deleted': True, 'reason': 'container rotation'},
                ),
            ]
        )
        evidence_ids.append(created_id)
    elif scenario.root_kind == 'upstream_tool':
        root_id = '%s-upstream-build' % trace_id
        rows.append(
            _observation(
                trace_id,
                root_id,
                trace_time,
                start_index,
                observation_type='TOOL',
                name='artifact_registry.lookup',
                output_value={
                    'artifact_path': path,
                    'build_state': 'expired',
                    'registry_version': 'stale',
                },
            )
        )

    failure_index = start_index + len(rows) + 1
    rows.append(
        _observation(
            trace_id,
            failure_id,
            trace_time,
            failure_index,
            observation_type='TOOL',
            name=scenario.tool_name,
            input_value={'path': path},
            output_value={'error_code': 'ENOENT', 'error': 'File not found'},
            level='ERROR',
            status_message='File not found: <PATH_1>',
        )
    )
    evidence_ids.extend([item for item in (root_id, failure_id) if item])

    if scenario.name == 'recovered_after_create':
        recovery_id = '%s-recovery-write' % trace_id
        rows.append(
            _observation(
                trace_id,
                recovery_id,
                trace_time,
                failure_index + 1,
                observation_type='TOOL',
                name='write_file',
                input_value={'path': path, 'content': 'default report'},
                output_value={'success': True},
            )
        )
        evidence_ids.append(recovery_id)
    elif scenario.name == 'expected_cache_miss_fallback':
        fallback_id = '%s-cache-fallback' % trace_id
        rows.append(
            _observation(
                trace_id,
                fallback_id,
                trace_time,
                failure_index + 1,
                observation_type='TOOL',
                name='compute_fresh_result',
                input_value={'cache_policy': 'miss_fallback'},
                output_value={'success': True, 'result': '<REDACTED_RESULT>'},
            )
        )
        evidence_ids.append(fallback_id)
    elif scenario.name == 'rule_match_expected_absence':
        final_id = '%s-final-confirmation' % trace_id
        rows.append(
            _observation(
                trace_id,
                final_id,
                trace_time,
                failure_index + 1,
                observation_type='GENERATION',
                name='final.answer',
                output_value={
                    'message': 'Confirmed the marker is absent as requested.'
                },
            )
        )
        evidence_ids.append(final_id)

    return rows, failure_id, root_id, _unique(evidence_ids)


def _observation(
    trace_id: str,
    observation_id: str,
    trace_time: datetime,
    index: int,
    *,
    observation_type: str,
    name: str,
    parent_id: Optional[str] = None,
    input_value: Any = None,
    output_value: Any = None,
    level: str = 'DEFAULT',
    status_message: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        'observation_id': observation_id,
        'trace_id': trace_id,
        'parent_observation_id': parent_id,
        'type': observation_type,
        'name': name,
        'start_time': (trace_time + timedelta(milliseconds=index)).isoformat(),
        'end_time': (
            trace_time + timedelta(milliseconds=index, microseconds=500)
        ).isoformat(),
        'input_redacted': input_value,
        'output_redacted': output_value,
        'metadata_redacted': {'synthetic': True},
        'level': level,
        'status_message_redacted': status_message,
    }


def _trace_output(scenario: _Scenario) -> Dict[str, Any]:
    if scenario.is_agent_failure is False:
        return {'status': 'success', 'message': 'Expected branch completed.'}
    if scenario.is_agent_failure is True:
        return {'status': 'failed', 'message': 'Required report could not be read.'}
    return {'status': 'unknown', 'message': 'Outcome evidence is incomplete.'}


def _annotation(
    *,
    case_id: str,
    scenario: _Scenario,
    failure_id: str,
    root_id: Optional[str],
    evidence_ids: List[str],
) -> Dict[str, Any]:
    human_label = (
        'insufficient_evidence' if scenario.is_agent_failure is None else 'confirmed'
    )
    return {
        'case_id': case_id,
        'scenario': scenario.name,
        'failure_observation_id': failure_id,
        'technical_error_review': {'human_label': human_label},
        'semantic_outcome': {
            'semantic_label': scenario.semantic_label,
            'is_agent_failure': scenario.is_agent_failure,
        },
        'attribution': {
            'applicable': scenario.is_agent_failure is True,
            'primary_root_cause_observation_id': root_id,
            'root_cause_label': scenario.root_cause_label,
            'root_cause_domain': scenario.root_cause_domain,
        },
        'evidence_observation_ids': evidence_ids,
        'annotation_confidence': 'high'
        if scenario.is_agent_failure is not None
        else 'low',
        'notes': 'Synthetic oracle for scenario %s.' % scenario.name,
    }


def _unique(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        ''.join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in rows
        ),
        encoding='utf-8',
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Generate the redacted long-trace File Not Found dataset.',
    )
    parser.add_argument('output_dir', type=Path)
    parser.add_argument('--noise-observations', type=int, default=800)
    args = parser.parse_args(argv)
    dataset = build_long_file_not_found_dataset(
        noise_observation_count=args.noise_observations,
    )
    write_long_file_not_found_dataset(dataset, args.output_dir)
    print(json.dumps(dataset.manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
