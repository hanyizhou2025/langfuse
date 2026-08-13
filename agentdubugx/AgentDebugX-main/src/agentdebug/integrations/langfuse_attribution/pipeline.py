"""End-to-end deterministic attribution for historical file-not-found cases."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, List, Mapping, Optional

from .attributor import LangfuseToolAttributor
from .failure_gate import FailureGateDecision, evaluate_file_not_found_failure
from .historical_converter import convert_historical_langfuse_observations
from .models import (
    CauseKind,
    ConversionEvidence,
    ToolFailureAttribution,
    ToolFailureEvidence,
)


class FileNotFoundDecision(str, Enum):
    """Stable public decisions for one dataset case."""

    NOT_AGENT_FAILURE = 'not_agent_failure'
    ATTRIBUTED = 'attributed'
    UNKNOWN = 'unknown'
    INVALID_CASE = 'invalid_case'


@dataclass(frozen=True)
class FileNotFoundAttributionResult:
    """Dataset-aligned output without raw path values."""

    case_id: Optional[str]
    trace_id: str
    decision: FileNotFoundDecision
    semantics: str
    is_agent_failure: Optional[bool]
    failure_observation_id: Optional[str]
    root_cause_observation_id: Optional[str]
    root_cause_label: Optional[str]
    root_cause_domain: Optional[str]
    evidence_observation_ids: List[str] = field(default_factory=list)
    confidence: float = 0.0
    reason_codes: List[str] = field(default_factory=list)


_CAUSE_LABELS = {
    CauseKind.USER_INPUT_INVALID: ('user_path_invalid', 'user'),
    CauseKind.MODEL_ARGUMENT_HALLUCINATION: (
        'model_path_hallucination',
        'model',
    ),
    CauseKind.SKILL_CONFIGURATION_INVALID: ('upstream_path_invalid', 'upstream'),
    CauseKind.SYSTEM_DEFAULT_INVALID: ('upstream_path_invalid', 'upstream'),
    CauseKind.RUNTIME_ENVIRONMENT_MISMATCH: (
        'runtime_path_unavailable',
        'runtime',
    ),
}


def attribute_historical_file_not_found(
    observations: Iterable[Mapping[str, Any]],
    *,
    failure_observation_id: str,
    case_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> FileNotFoundAttributionResult:
    """Run conversion, semantic gating, and deterministic attribution.

    Only the evidence bundle reaches ``LangfuseToolAttributor``.  The result
    exposes observation identifiers and reason codes, never raw tool arguments.
    """

    observation_list = list(observations)
    converted = convert_historical_langfuse_observations(
        observation_list,
        trace_id=trace_id,
    )
    failure = _find_failure(
        converted.evidence.tool_failures,
        failure_observation_id,
    )
    source_observation_id = _source_observation_id(failure)
    gate = evaluate_file_not_found_failure(
        observation_list,
        failure_observation_id=failure_observation_id,
        path_source_observation_id=source_observation_id,
    )

    if gate.decision == FailureGateDecision.INVALID_CASE:
        return _result(
            case_id,
            converted.trajectory.trace_id,
            FileNotFoundDecision.INVALID_CASE,
            gate.semantics,
            gate.is_agent_failure,
            None,
            None,
            None,
            None,
            gate.evidence_observation_ids,
            0.0,
            gate.reason_codes,
        )
    if gate.decision == FailureGateDecision.NOT_AGENT_FAILURE:
        return _result(
            case_id,
            converted.trajectory.trace_id,
            FileNotFoundDecision.NOT_AGENT_FAILURE,
            gate.semantics,
            False,
            failure_observation_id,
            None,
            None,
            None,
            gate.evidence_observation_ids,
            1.0,
            gate.reason_codes,
        )
    if gate.decision == FailureGateDecision.UNKNOWN or failure is None:
        return _result(
            case_id,
            converted.trajectory.trace_id,
            FileNotFoundDecision.UNKNOWN,
            gate.semantics,
            None,
            failure_observation_id,
            None,
            None,
            None,
            gate.evidence_observation_ids,
            0.0,
            gate.reason_codes,
        )

    attribution = LangfuseToolAttributor().attribute(
        ConversionEvidence(
            trace_id=converted.evidence.trace_id,
            tool_failures=[failure],
            observation_ids=list(converted.evidence.observation_ids),
        )
    )[0]
    cause = _cause_label(attribution)
    if cause is None or attribution.source_observation_id is None:
        return _result(
            case_id,
            converted.trajectory.trace_id,
            FileNotFoundDecision.UNKNOWN,
            gate.semantics,
            None,
            failure_observation_id,
            None,
            None,
            None,
            gate.evidence_observation_ids,
            attribution.confidence,
            gate.reason_codes + ['unsupported_or_unknown_cause'],
        )

    label, domain = cause
    evidence_ids = _unique_ids(
        [attribution.source_observation_id, failure_observation_id]
    )
    return _result(
        case_id,
        converted.trajectory.trace_id,
        FileNotFoundDecision.ATTRIBUTED,
        gate.semantics,
        True,
        failure_observation_id,
        attribution.source_observation_id,
        label,
        domain,
        evidence_ids,
        attribution.confidence,
        gate.reason_codes,
    )


def _find_failure(
    failures: Iterable[ToolFailureEvidence],
    failure_observation_id: str,
) -> Optional[ToolFailureEvidence]:
    return next(
        (
            failure
            for failure in failures
            if failure.tool_result_observation_id == failure_observation_id
        ),
        None,
    )


def _source_observation_id(
    failure: Optional[ToolFailureEvidence],
) -> Optional[str]:
    if failure is None:
        return None
    sources = {
        source.observation_id
        for source in failure.argument_sources.values()
        if source.observation_id
    }
    if len(sources) != 1:
        return None
    return next(iter(sources))


def _cause_label(
    attribution: ToolFailureAttribution,
) -> Optional[tuple[str, str]]:
    return _CAUSE_LABELS.get(attribution.primary_cause)


def _unique_ids(values: Iterable[Optional[str]]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _result(
    case_id: Optional[str],
    trace_id: str,
    decision: FileNotFoundDecision,
    semantics: str,
    is_agent_failure: Optional[bool],
    failure_observation_id: Optional[str],
    root_cause_observation_id: Optional[str],
    root_cause_label: Optional[str],
    root_cause_domain: Optional[str],
    evidence_observation_ids: List[str],
    confidence: float,
    reason_codes: List[str],
) -> FileNotFoundAttributionResult:
    return FileNotFoundAttributionResult(
        case_id=case_id,
        trace_id=trace_id,
        decision=decision,
        semantics=semantics,
        is_agent_failure=is_agent_failure,
        failure_observation_id=failure_observation_id,
        root_cause_observation_id=root_cause_observation_id,
        root_cause_label=root_cause_label,
        root_cause_domain=root_cause_domain,
        evidence_observation_ids=evidence_observation_ids,
        confidence=confidence,
        reason_codes=reason_codes,
    )
