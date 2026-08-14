"""Dataset adapters and layered metrics for file-not-found attribution."""

from __future__ import annotations

from dataclasses import asdict
from enum import Enum
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

from agentdebug.runtime.llm import LLMClient
from agentdebug.schema import model_to_json

from .historical_converter import convert_historical_langfuse_observations
from .llm_attributor import FileNotFoundLLMAttributor
from .pipeline import attribute_historical_file_not_found
from .presentation import attach_file_not_found_attributions


def predict_file_not_found_cases(
    cases: Iterable[Mapping[str, Any]],
    observations: Iterable[Mapping[str, Any]],
    *,
    traces: Iterable[Mapping[str, Any]] = (),
    llm: Optional[LLMClient] = None,
    review_deterministic: bool = False,
    max_context_observations: int = 64,
) -> List[Dict[str, Any]]:
    """Predict dataset cases using the documented redacted JSONL shape."""

    observations_by_trace: Dict[str, List[Dict[str, Any]]] = {}
    for observation in observations:
        normalized = _normalize_observation(observation)
        trace_id = str(normalized.get('trace_id') or '')
        observations_by_trace.setdefault(trace_id, []).append(normalized)

    for trace in traces:
        trace_input = _trace_input_observation(trace)
        if trace_input is None:
            continue
        trace_id = str(trace_input.get('trace_id') or '')
        observations_by_trace.setdefault(trace_id, []).append(trace_input)

    llm_attributor = (
        FileNotFoundLLMAttributor(
            llm,
            review_deterministic=review_deterministic,
            max_context_observations=max_context_observations,
        )
        if llm is not None
        else None
    )
    predictions: List[Dict[str, Any]] = []
    for case in cases:
        case_id = _optional_str(case.get('case_id'))
        trace_id = _optional_str(case.get('trace_id')) or ''
        failure_observation_id = _failure_observation_id(case)
        if not case_id or not trace_id or not failure_observation_id:
            predictions.append(
                {
                    'case_id': case_id,
                    'trace_id': trace_id,
                    'decision': 'invalid_case',
                    'reason_codes': ['missing_case_identity_or_failure_id'],
                }
            )
            continue

        trace_observations = observations_by_trace.get(trace_id, [])
        result = (
            llm_attributor.attribute(
                trace_observations,
                failure_observation_id=failure_observation_id,
                case_id=case_id,
                trace_id=trace_id,
            )
            if llm_attributor is not None
            else attribute_historical_file_not_found(
                trace_observations,
                failure_observation_id=failure_observation_id,
                case_id=case_id,
                trace_id=trace_id,
            )
        )
        predictions.append(_serialize_result(result))
    return predictions


def build_file_not_found_review_trajectories(
    predictions: Iterable[Mapping[str, Any]],
    observations: Iterable[Mapping[str, Any]],
    *,
    traces: Iterable[Mapping[str, Any]] = (),
) -> List[Dict[str, Any]]:
    """Build Inspect-compatible trajectories with persisted MVP decisions."""

    predictions_by_trace: Dict[str, List[Mapping[str, Any]]] = {}
    trace_order: List[str] = []
    for prediction in predictions:
        trace_id = _optional_str(prediction.get('trace_id'))
        if not trace_id:
            continue
        if trace_id not in predictions_by_trace:
            trace_order.append(trace_id)
        predictions_by_trace.setdefault(trace_id, []).append(prediction)

    observations_by_trace = _group_observations(observations, traces=traces)
    trajectories: List[Dict[str, Any]] = []
    for trace_id in trace_order:
        converted = convert_historical_langfuse_observations(
            observations_by_trace.get(trace_id, []),
            trace_id=trace_id,
        )
        attach_file_not_found_attributions(
            converted.trajectory,
            predictions_by_trace[trace_id],
        )
        trajectories.append(json.loads(model_to_json(converted.trajectory)))
    return trajectories


def _group_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    traces: Iterable[Mapping[str, Any]] = (),
) -> Dict[str, List[Dict[str, Any]]]:
    observations_by_trace: Dict[str, List[Dict[str, Any]]] = {}
    for observation in observations:
        normalized = _normalize_observation(observation)
        trace_id = str(normalized.get('trace_id') or '')
        observations_by_trace.setdefault(trace_id, []).append(normalized)

    for trace in traces:
        trace_input = _trace_input_observation(trace)
        if trace_input is None:
            continue
        trace_id = str(trace_input.get('trace_id') or '')
        observations_by_trace.setdefault(trace_id, []).append(trace_input)
    return observations_by_trace


def evaluate_file_not_found_predictions(
    predictions: Iterable[Mapping[str, Any]],
    annotations: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Compute small-sample counts for semantics, attribution, and abstention."""

    prediction_by_case = {
        str(prediction.get('case_id') or ''): prediction for prediction in predictions
    }
    report: Dict[str, Any] = {
        'case_count': 0,
        'missing_prediction_count': 0,
        'semantic_negative': {
            'correct': 0,
            'total': 0,
            'forced_attribution': 0,
        },
        'attribution': {
            'label_correct': 0,
            'root_observation_correct': 0,
            'total': 0,
        },
        'abstention': {
            'correct': 0,
            'total': 0,
        },
    }

    for annotation in annotations:
        report['case_count'] += 1
        case_id = str(annotation.get('case_id') or '')
        prediction = prediction_by_case.get(case_id)
        if prediction is None:
            report['missing_prediction_count'] += 1
            continue

        human_label = _nested_or_flat(
            annotation,
            'technical_error_review',
            'human_label',
            'technical_error_review',
        )
        is_agent_failure = _nested_or_flat(
            annotation,
            'semantic_outcome',
            'is_agent_failure',
            'is_agent_failure',
        )
        attribution_applicable_value = _nested_or_flat(
            annotation,
            'attribution',
            'applicable',
            'attribution_applicable',
        )
        expected_root = _nested_or_flat(
            annotation,
            'attribution',
            'primary_root_cause_observation_id',
            'primary_root_cause_observation_id',
        )
        attribution_applicable = (
            bool(attribution_applicable_value)
            if attribution_applicable_value is not None
            else is_agent_failure is True and expected_root is not None
        )
        decision = str(prediction.get('decision') or '')

        if human_label == 'insufficient_evidence' or is_agent_failure is None:
            report['abstention']['total'] += 1
            if decision == 'unknown':
                report['abstention']['correct'] += 1
            continue

        if is_agent_failure is False:
            report['semantic_negative']['total'] += 1
            if decision == 'not_agent_failure':
                report['semantic_negative']['correct'] += 1
            if decision == 'attributed':
                report['semantic_negative']['forced_attribution'] += 1
            continue

        if is_agent_failure is True and attribution_applicable:
            report['attribution']['total'] += 1
            expected_label = _nested_or_flat(
                annotation,
                'attribution',
                'root_cause_label',
                'root_cause_label',
            )
            if prediction.get('root_cause_label') == expected_label:
                report['attribution']['label_correct'] += 1
            if prediction.get('root_cause_observation_id') == expected_root:
                report['attribution']['root_observation_correct'] += 1

    return report


def _normalize_observation(observation: Mapping[str, Any]) -> Dict[str, Any]:
    """Map the documented redacted export back to converter field names."""

    normalized = dict(observation)
    aliases = {
        'observation_id': 'id',
        'input_redacted': 'input',
        'output_redacted': 'output',
        'metadata_redacted': 'metadata',
        'status_message_redacted': 'status_message',
    }
    for source, target in aliases.items():
        if target not in normalized and source in normalized:
            normalized[target] = normalized[source]
    return normalized


def _trace_input_observation(trace: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    trace_id = _optional_str(trace.get('trace_id')) or _optional_str(trace.get('id'))
    input_value = trace.get('input_redacted', trace.get('input'))
    if not trace_id or input_value is None:
        return None
    return {
        'id': '%s:input' % trace_id,
        'observation_id': '%s:input' % trace_id,
        'trace_id': trace_id,
        'parent_observation_id': None,
        'type': 'SPAN',
        'name': 'user.request',
        'start_time': trace.get('timestamp'),
        'input': input_value,
        'input_redacted': input_value,
        'output': None,
        'output_redacted': None,
        'metadata': {'synthetic_role': 'trace_input'},
        'metadata_redacted': {'synthetic_role': 'trace_input'},
        'level': 'DEFAULT',
        'status_message': None,
        'status_message_redacted': None,
    }


def _failure_observation_id(case: Mapping[str, Any]) -> Optional[str]:
    tool_attempt = case.get('tool_attempt')
    if isinstance(tool_attempt, Mapping):
        result_id = _optional_str(tool_attempt.get('tool_result_observation_id'))
        if result_id:
            return result_id
    rule_evidence = case.get('rule_evidence')
    if isinstance(rule_evidence, Mapping):
        return _optional_str(rule_evidence.get('matched_observation_id'))
    return None


def _serialize_result(value: Any) -> Dict[str, Any]:
    payload = asdict(value)
    serialized = _serialize_enums(payload)
    if not isinstance(serialized, dict):
        raise TypeError('Serialized attribution result must be an object.')
    return serialized


def _serialize_enums(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _serialize_enums(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_enums(item) for item in value]
    return value


def _nested_value(value: Mapping[str, Any], parent: str, child: str) -> Any:
    nested = value.get(parent)
    return nested.get(child) if isinstance(nested, Mapping) else None


def _nested_or_flat(
    value: Mapping[str, Any],
    parent: str,
    child: str,
    flat_key: str,
) -> Any:
    nested = _nested_value(value, parent, child)
    return nested if nested is not None else value.get(flat_key)


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
