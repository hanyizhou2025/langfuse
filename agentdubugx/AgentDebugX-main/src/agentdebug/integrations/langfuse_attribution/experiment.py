"""Batch driver for real-model File Not Found enhancement experiments."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from agentdebug.runtime.llm import CompletionResult

from .evaluation import (
    evaluate_file_not_found_predictions,
    predict_file_not_found_cases,
)
from .llm_attributor import _select_context
from .trace_input import build_trace_input_observations


_BATCH_INSTRUCTIONS = """Analyze each redacted Langfuse File Not Found case below.

For every case, answer three layers separately:
1. Did File Not Found constitute an Agent/task failure, or was it an expected
   probe, cache miss, fallback, recovered branch, or requested absence?
2. If it was a failure, which EXISTING observation first introduced the
   decisive bad path or runtime/upstream condition?
3. Use only observation IDs present in that case. Never invent an ID.

Allowed root labels and domains:
- user_path_invalid -> user
- model_path_hallucination -> model
- upstream_path_invalid -> upstream
- runtime_path_unavailable -> runtime

For insufficient evidence return decision=unknown, is_agent_failure=null, and
no root. For non-failures return decision=not_agent_failure,
is_agent_failure=false, and no root. For failures return decision=attributed,
is_agent_failure=true, and a grounded root. The failure observation and root
(when present) must be included in evidence_observation_ids. Return every
case_id exactly once. Do not call tools; use only the supplied redacted data.
"""


@dataclass
class _BatchResponseLLM:
    responses: Mapping[str, Mapping[str, Any]]
    model: str

    def complete(
        self,
        messages: List[Dict[str, Any]],
        *,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> CompletionResult:
        prompt = json.loads(str(messages[1]['content']))
        failure_id = str(prompt['failure_observation_id'])
        payload = self.responses.get(failure_id)
        if payload is None:
            raise ValueError('No batch response for failure %s' % failure_id)
        return CompletionResult(text=json.dumps(payload), raw={})


def prepare_batch_experiment(
    *,
    dataset_dir: Path,
    artifact_dir: Path,
    max_context_observations: int = 64,
    review_all: bool = False,
) -> Dict[str, Any]:
    """Freeze baseline outputs plus the redacted, retrieved model request."""

    cases = _read_jsonl(dataset_dir / 'cases.jsonl')
    observations = _read_jsonl(dataset_dir / 'observations.jsonl')
    traces = _read_jsonl(dataset_dir / 'traces.jsonl')
    annotations = _read_jsonl(dataset_dir / 'annotations.jsonl')
    baseline = predict_file_not_found_cases(cases, observations, traces=traces)
    baseline_by_case = {str(item['case_id']): item for item in baseline}
    observations_by_trace = _group_observations(observations, traces)

    request_cases: List[Dict[str, Any]] = []
    for case in cases:
        case_id = str(case.get('case_id') or '')
        baseline_result = baseline_by_case[case_id]
        if not review_all and baseline_result.get('decision') != 'unknown':
            continue
        trace_id = str(case.get('trace_id') or '')
        failure_id = _failure_observation_id(case)
        selected = _select_context(
            observations_by_trace.get(trace_id, []),
            failure_observation_id=failure_id,
            limit=max_context_observations,
        )
        request_cases.append(
            {
                'case_id': case_id,
                'trace_id': trace_id,
                'failure_observation_id': failure_id,
                'baseline': {
                    'decision': baseline_result.get('decision'),
                    'semantics': baseline_result.get('semantics'),
                    'reason_codes': baseline_result.get('reason_codes'),
                },
                'context_selection': {
                    'total_observation_count': len(
                        observations_by_trace.get(trace_id, [])
                    ),
                    'reviewed_observation_count': len(selected),
                },
                'observations': [_model_observation(item) for item in selected],
            }
        )

    request = {
        'experiment': 'agentdebugx_file_not_found_llm_v1',
        'privacy': 'all payload fields are redacted synthetic data',
        'review_policy': 'all' if review_all else 'deterministic_unknown_only',
        'cases': request_cases,
    }
    artifact_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(artifact_dir / 'baseline_predictions.jsonl', baseline)
    _write_json(artifact_dir / 'baseline_report.json', _evaluate(baseline, annotations))
    _write_json(artifact_dir / 'batch_request.json', request)
    _write_json(artifact_dir / 'batch_response_schema.json', _response_schema())
    (artifact_dir / 'batch_prompt.txt').write_text(
        _BATCH_INSTRUCTIONS
        + '\n\nINPUT DATA:\n'
        + json.dumps(request, ensure_ascii=False, separators=(',', ':'))
        + '\n',
        encoding='utf-8',
    )
    return {
        'case_count': len(cases),
        'review_case_count': len(request_cases),
        'review_policy': request['review_policy'],
        'total_observation_count': len(observations),
        'reviewed_observation_count': sum(
            item['context_selection']['reviewed_observation_count']
            for item in request_cases
        ),
    }


def evaluate_batch_response(
    *,
    dataset_dir: Path,
    artifact_dir: Path,
    response_path: Path,
    output_dir: Path,
    model: str,
) -> Dict[str, Any]:
    """Validate a batch model response through the production LLM adapter."""

    cases = _read_jsonl(dataset_dir / 'cases.jsonl')
    observations = _read_jsonl(dataset_dir / 'observations.jsonl')
    traces = _read_jsonl(dataset_dir / 'traces.jsonl')
    annotations = _read_jsonl(dataset_dir / 'annotations.jsonl')
    baseline = _read_jsonl(artifact_dir / 'baseline_predictions.jsonl')
    raw_response = json.loads(response_path.read_text(encoding='utf-8'))
    result_rows = (
        raw_response.get('results') if isinstance(raw_response, dict) else None
    )
    if not isinstance(result_rows, list):
        raise ValueError('Batch response must contain a results list.')

    by_failure: Dict[str, Mapping[str, Any]] = {}
    response_case_ids = set()
    for row in result_rows:
        if not isinstance(row, Mapping):
            raise ValueError('Every batch result must be an object.')
        case_id = str(row.get('case_id') or '')
        failure_id = str(row.get('failure_observation_id') or '')
        if not case_id or not failure_id or case_id in response_case_ids:
            raise ValueError('Batch result case IDs must be unique and non-empty.')
        response_case_ids.add(case_id)
        by_failure[failure_id] = dict(row)

    llm = _BatchResponseLLM(responses=by_failure, model=model)
    hybrid = predict_file_not_found_cases(
        cases,
        observations,
        traces=traces,
        llm=llm,
    )
    baseline_report = _evaluate(baseline, annotations)
    hybrid_report = _evaluate(hybrid, annotations)
    comparison: Dict[str, Any] = {
        'model': model,
        'case_count': len(cases),
        'model_response_count': len(result_rows),
        'baseline': baseline_report,
        'hybrid_unknown_review': hybrid_report,
        'hybrid_delta': _metric_delta(baseline_report, hybrid_report),
    }

    all_case_ids = {str(case.get('case_id') or '') for case in cases}
    if response_case_ids == all_case_ids:
        review_all = predict_file_not_found_cases(
            cases,
            observations,
            traces=traces,
            llm=llm,
            review_deterministic=True,
        )
        review_all_report = _evaluate(review_all, annotations)
        comparison['llm_review_all'] = review_all_report
        comparison['llm_review_all_delta'] = _metric_delta(
            baseline_report,
            review_all_report,
        )
    else:
        review_all = []

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / 'hybrid_predictions.jsonl', hybrid)
    if review_all:
        _write_jsonl(output_dir / 'llm_review_all_predictions.jsonl', review_all)
    _write_json(output_dir / 'comparison.json', comparison)
    return comparison


def _group_observations(
    observations: Sequence[Mapping[str, Any]],
    traces: Sequence[Mapping[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for observation in observations:
        trace_id = str(observation.get('trace_id') or '')
        grouped.setdefault(trace_id, []).append(dict(observation))
    for trace in traces:
        for trace_input in build_trace_input_observations(trace):
            trace_id = str(trace_input.get('trace_id') or '')
            grouped.setdefault(trace_id, []).append(trace_input)
    return grouped


def _model_observation(observation: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        'id': observation.get('observation_id') or observation.get('id'),
        'parent_id': observation.get('parent_observation_id'),
        'type': observation.get('type'),
        'name': observation.get('name'),
        'start_time': observation.get('start_time'),
        'level': observation.get('level'),
        'status': observation.get('status_message_redacted'),
        'input': observation.get('input_redacted'),
        'output': observation.get('output_redacted'),
    }


def _failure_observation_id(case: Mapping[str, Any]) -> str:
    attempt = case.get('tool_attempt')
    if isinstance(attempt, Mapping):
        value = attempt.get('tool_result_observation_id')
        if value:
            return str(value)
    evidence = case.get('rule_evidence')
    if isinstance(evidence, Mapping) and evidence.get('matched_observation_id'):
        return str(evidence['matched_observation_id'])
    return ''


def _evaluate(
    predictions: Iterable[Mapping[str, Any]],
    annotations: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    return evaluate_file_not_found_predictions(predictions, annotations)


def _metric_delta(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> Dict[str, int]:
    return {
        'semantic_negative_correct': int(
            candidate['semantic_negative']['correct']
            - baseline['semantic_negative']['correct']
        ),
        'semantic_negative_forced_attribution': int(
            candidate['semantic_negative']['forced_attribution']
            - baseline['semantic_negative']['forced_attribution']
        ),
        'attribution_label_correct': int(
            candidate['attribution']['label_correct']
            - baseline['attribution']['label_correct']
        ),
        'root_observation_correct': int(
            candidate['attribution']['root_observation_correct']
            - baseline['attribution']['root_observation_correct']
        ),
        'abstention_correct': int(
            candidate['abstention']['correct'] - baseline['abstention']['correct']
        ),
    }


def _response_schema() -> Dict[str, Any]:
    nullable_string = {'type': ['string', 'null']}
    return {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'results': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'additionalProperties': False,
                    'properties': {
                        'case_id': {'type': 'string'},
                        'decision': {
                            'type': 'string',
                            'enum': ['attributed', 'not_agent_failure', 'unknown'],
                        },
                        'semantics': {
                            'type': 'string',
                            'enum': [
                                'validation_probe',
                                'control_flow_signal',
                                'recovered_exploration',
                                'expected_absence',
                                'rule_false_positive',
                                'unexpected_failure',
                                'unknown',
                            ],
                        },
                        'is_agent_failure': {'type': ['boolean', 'null']},
                        'failure_observation_id': {'type': 'string'},
                        'root_cause_observation_id': nullable_string,
                        'root_cause_label': nullable_string,
                        'root_cause_domain': nullable_string,
                        'evidence_observation_ids': {
                            'type': 'array',
                            'items': {'type': 'string'},
                            'minItems': 1,
                        },
                        'confidence': {
                            'type': 'number',
                            'minimum': 0,
                            'maximum': 1,
                        },
                        'reason_codes': {
                            'type': 'array',
                            'items': {'type': 'string', 'pattern': '^[a-z0-9_]{1,64}$'},
                            'maxItems': 8,
                        },
                    },
                    'required': [
                        'case_id',
                        'decision',
                        'semantics',
                        'is_agent_failure',
                        'failure_observation_id',
                        'root_cause_observation_id',
                        'root_cause_label',
                        'root_cause_domain',
                        'evidence_observation_ids',
                        'confidence',
                        'reason_codes',
                    ],
                },
            }
        },
        'required': ['results'],
    }


def _read_jsonl(path: Path) -> List[Mapping[str, Any]]:
    return [
        value
        for line in path.read_text(encoding='utf-8').splitlines()
        if line.strip()
        for value in [json.loads(line)]
    ]


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.write_text(
        ''.join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in rows
        ),
        encoding='utf-8',
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Run long-trace LLM experiments.')
    subparsers = parser.add_subparsers(dest='command', required=True)
    prepare = subparsers.add_parser('prepare')
    prepare.add_argument('dataset_dir', type=Path)
    prepare.add_argument('artifact_dir', type=Path)
    prepare.add_argument('--review-all', action='store_true')
    prepare.add_argument('--max-context-observations', type=int, default=64)
    evaluate = subparsers.add_parser('evaluate')
    evaluate.add_argument('dataset_dir', type=Path)
    evaluate.add_argument('artifact_dir', type=Path)
    evaluate.add_argument('response_path', type=Path)
    evaluate.add_argument('output_dir', type=Path)
    evaluate.add_argument('--model', required=True)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        result = prepare_batch_experiment(
            dataset_dir=args.dataset_dir,
            artifact_dir=args.artifact_dir,
            max_context_observations=args.max_context_observations,
            review_all=args.review_all,
        )
    else:
        result = evaluate_batch_response(
            dataset_dir=args.dataset_dir,
            artifact_dir=args.artifact_dir,
            response_path=args.response_path,
            output_dir=args.output_dir,
            model=args.model,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
