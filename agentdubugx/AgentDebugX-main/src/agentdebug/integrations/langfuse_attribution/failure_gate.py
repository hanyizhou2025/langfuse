"""Deterministic semantic gate for historical file-not-found tool results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple


class FailureGateDecision(str, Enum):
    """Whether an observed technical error should enter attribution."""

    NOT_AGENT_FAILURE = 'not_agent_failure'
    FAILURE_CANDIDATE = 'failure_candidate'
    UNKNOWN = 'unknown'
    INVALID_CASE = 'invalid_case'


@dataclass(frozen=True)
class FailureGateResult:
    """Auditable, deliberately small output for the attribution gate."""

    decision: FailureGateDecision
    semantics: str
    is_agent_failure: Optional[bool]
    evidence_observation_ids: List[str] = field(default_factory=list)
    reason_codes: List[str] = field(default_factory=list)


_FILE_NOT_FOUND_TOKENS = (
    'enoent',
    'file not found',
    'no such file',
    'no such directory',
)
_PROBE_NAME_TOKENS = ('exists', 'existence', 'stat', 'check', 'probe')
_CREATE_NAME_TOKENS = ('create', 'write', 'touch', 'save')
_PATH_ARGUMENT_KEYS = ('path', 'file', 'filename', 'file_path')


def evaluate_file_not_found_failure(
    observations: Iterable[Mapping[str, Any]],
    *,
    failure_observation_id: str,
    path_source_observation_id: Optional[str] = None,
) -> FailureGateResult:
    """Classify one file-not-found result before root-cause attribution.

    The gate intentionally recognizes only high-precision semantic shapes.  A
    missing path source or an unsupported control-flow pattern returns
    ``UNKNOWN`` instead of forcing blame onto an observation.
    """

    ordered = sorted(observations, key=_observation_sort_key)
    failure_index = next(
        (
            index
            for index, observation in enumerate(ordered)
            if str(observation.get('id') or '') == failure_observation_id
        ),
        None,
    )
    if failure_index is None:
        return _result(
            FailureGateDecision.INVALID_CASE,
            'invalid_case',
            None,
            [],
            ['failure_observation_missing'],
        )

    failure = ordered[failure_index]
    if not _is_file_not_found_result(failure):
        return _result(
            FailureGateDecision.INVALID_CASE,
            'invalid_case',
            None,
            [failure_observation_id],
            ['not_file_not_found_result'],
        )

    tool_name = str(failure.get('name') or '').lower()
    if any(token in tool_name for token in _PROBE_NAME_TOKENS):
        return _result(
            FailureGateDecision.NOT_AGENT_FAILURE,
            'validation_probe',
            False,
            [failure_observation_id],
            ['probe_tool_name'],
        )

    failed_path = _extract_path(failure.get('input'))
    recovery = _find_successful_create(
        ordered[failure_index + 1 :],
        failed_path,
    )
    if recovery is not None:
        recovery_id = str(recovery.get('id') or '')
        return _result(
            FailureGateDecision.NOT_AGENT_FAILURE,
            'control_flow_signal',
            False,
            [failure_observation_id, recovery_id],
            ['same_path_created_after_not_found'],
        )

    if path_source_observation_id:
        observation_ids = {str(observation.get('id') or '') for observation in ordered}
        if path_source_observation_id not in observation_ids:
            return _result(
                FailureGateDecision.UNKNOWN,
                'unknown',
                None,
                [failure_observation_id],
                ['path_source_observation_missing'],
            )
        return _result(
            FailureGateDecision.FAILURE_CANDIDATE,
            'unexpected_failure',
            True,
            [path_source_observation_id, failure_observation_id],
            ['known_path_source', 'unrecovered_file_not_found'],
        )

    return _result(
        FailureGateDecision.UNKNOWN,
        'unknown',
        None,
        [failure_observation_id],
        ['insufficient_semantic_or_source_evidence'],
    )


def _find_successful_create(
    observations: Sequence[Mapping[str, Any]],
    failed_path: Optional[str],
) -> Optional[Mapping[str, Any]]:
    if not failed_path:
        return None
    for observation in observations:
        if str(observation.get('type') or '').upper() != 'TOOL':
            continue
        name = str(observation.get('name') or '').lower()
        if not any(token in name for token in _CREATE_NAME_TOKENS):
            continue
        if _extract_path(observation.get('input')) != failed_path:
            continue
        if _is_success(observation):
            return observation
    return None


def _is_success(observation: Mapping[str, Any]) -> bool:
    if str(observation.get('level') or '').upper() == 'ERROR':
        return False
    status = _optional_str(observation.get('status_message'))
    if status and any(token in status.lower() for token in _FILE_NOT_FOUND_TOKENS):
        return False
    output = observation.get('output')
    if isinstance(output, Mapping):
        if output.get('success') is False:
            return False
        error = _optional_str(output.get('error'))
        if error:
            return False
    return True


def _is_file_not_found_result(observation: Mapping[str, Any]) -> bool:
    if str(observation.get('type') or '').upper() != 'TOOL':
        return False
    status = _optional_str(observation.get('status_message'))
    output = observation.get('output')
    parts = [status]
    if isinstance(output, Mapping):
        parts.extend(
            _optional_str(output.get(key))
            for key in ('error_code', 'error', 'code', 'message')
        )
    else:
        parts.append(_optional_str(output))
    text = ' '.join(item for item in parts if item).lower()
    return any(token in text for token in _FILE_NOT_FOUND_TOKENS)


def _extract_path(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        for key in _PATH_ARGUMENT_KEYS:
            path = _optional_str(value.get(key))
            if path:
                return path
        for child in value.values():
            path = _extract_path(child)
            if path:
                return path
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            path = _extract_path(child)
            if path:
                return path
    return None


def _result(
    decision: FailureGateDecision,
    semantics: str,
    is_agent_failure: Optional[bool],
    evidence_observation_ids: List[str],
    reason_codes: List[str],
) -> FailureGateResult:
    return FailureGateResult(
        decision=decision,
        semantics=semantics,
        is_agent_failure=is_agent_failure,
        evidence_observation_ids=evidence_observation_ids,
        reason_codes=reason_codes,
    )


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _observation_sort_key(observation: Mapping[str, Any]) -> Tuple[datetime, str]:
    value = observation.get('start_time')
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            timestamp = datetime.min.replace(tzinfo=timezone.utc)
    else:
        timestamp = datetime.min.replace(tzinfo=timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp, str(observation.get('id') or '')
