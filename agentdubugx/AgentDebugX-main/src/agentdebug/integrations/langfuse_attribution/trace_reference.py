"""Build no-LLM reference chains without claiming an unsupported root cause."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from .path_evidence import contains_path, extract_path, optional_str, paths_equal


@dataclass(frozen=True)
class CurrentTraceReference:
    """Current-trace evidence that remains useful when attribution abstains."""

    root_cause_scope: str = 'unknown'
    earliest_local_evidence_observation_id: Optional[str] = None
    local_trigger_observation_id: Optional[str] = None
    propagation_observation_ids: List[str] = field(default_factory=list)
    confidence: float = 0.0
    reason_codes: List[str] = field(default_factory=list)


def build_current_trace_reference(
    observations: Iterable[Mapping[str, Any]],
    *,
    failure_observation_id: str,
) -> CurrentTraceReference:
    """Locate earliest evidence and the direct LLM-to-tool trigger.

    An assistant/system value already present at trace entry is treated as a
    boundary marker.  It can be shown as evidence, but never promoted to an
    in-trace root cause.
    """

    ordered = sorted(observations, key=_observation_sort_key)
    failure_index = next(
        (
            index
            for index, observation in enumerate(ordered)
            if _observation_id(observation) == failure_observation_id
        ),
        None,
    )
    if failure_index is None:
        return CurrentTraceReference()

    failure = ordered[failure_index]
    failed_path = extract_path(failure.get('input'))
    if not failed_path:
        return CurrentTraceReference(
            propagation_observation_ids=[failure_observation_id],
            reason_codes=['file_not_found_path_missing'],
        )

    prior = ordered[:failure_index]
    matches = [
        observation
        for observation in prior
        if _observation_contains_path(observation, failed_path)
    ]
    earliest = matches[0] if matches else None
    trigger = _find_local_trigger(prior, failure, failed_path)
    boundary = next(
        (
            observation
            for observation in matches
            if _is_inherited_boundary_context(observation, failed_path)
        ),
        None,
    )

    earliest_id = _observation_id(earliest) if earliest is not None else None
    trigger_id = _observation_id(trigger) if trigger is not None else None
    propagation = _unique_ids(
        [earliest_id, trigger_id, failure_observation_id]
    )
    reasons = ['file_not_found_confirmed']
    if boundary is not None:
        reasons.extend(
            ['path_inherited_at_trace_entry', 'root_precedes_trace_boundary']
        )
    if trigger is not None:
        reasons.append('local_tool_call_trigger_found')
    else:
        reasons.append('local_tool_call_trigger_missing')

    if boundary is not None:
        scope = 'outside_current_trace'
    elif earliest is not None:
        scope = 'current_trace'
    else:
        scope = 'unknown'

    if earliest is not None and trigger is not None:
        confidence = 0.95 if boundary is not None else 0.9
    elif earliest is not None or trigger is not None:
        confidence = 0.7
    else:
        confidence = 0.3
    return CurrentTraceReference(
        root_cause_scope=scope,
        earliest_local_evidence_observation_id=earliest_id,
        local_trigger_observation_id=trigger_id,
        propagation_observation_ids=propagation,
        confidence=confidence,
        reason_codes=reasons,
    )


def _find_local_trigger(
    prior: Sequence[Mapping[str, Any]],
    failure: Mapping[str, Any],
    failed_path: str,
) -> Optional[Mapping[str, Any]]:
    parent_id = optional_str(failure.get('parent_observation_id'))
    if parent_id:
        parent = next(
            (
                observation
                for observation in reversed(prior)
                if _observation_id(observation) == parent_id
            ),
            None,
        )
        if parent is not None and _has_matching_tool_call(parent, failed_path):
            return parent

    return next(
        (
            observation
            for observation in reversed(prior)
            if str(observation.get('type') or '').upper() == 'GENERATION'
            and _has_matching_tool_call(observation, failed_path)
        ),
        None,
    )


def _has_matching_tool_call(
    observation: Mapping[str, Any],
    failed_path: str,
) -> bool:
    return _payload_has_matching_tool_call(observation.get('output'), failed_path)


def _payload_has_matching_tool_call(value: Any, failed_path: str) -> bool:
    if isinstance(value, Mapping):
        arguments = value.get('arguments')
        if arguments is not None and paths_equal(
            extract_path(arguments),
            failed_path,
        ):
            return True
        function = value.get('function')
        if isinstance(function, Mapping) and _payload_has_matching_tool_call(
            function,
            failed_path,
        ):
            return True
        return any(
            _payload_has_matching_tool_call(item, failed_path)
            for key, item in value.items()
            if key not in {'arguments', 'function'}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(
            _payload_has_matching_tool_call(item, failed_path) for item in value
        )
    return False


def _is_inherited_boundary_context(
    observation: Mapping[str, Any],
    path: str,
) -> bool:
    metadata = _mapping(
        observation.get('metadata', observation.get('metadata_redacted'))
    )
    message_role = str(metadata.get('message_role') or '').casefold()
    if (
        metadata.get('synthetic_role') == 'trace_input'
        and message_role in {'assistant', 'system', 'unknown'}
    ):
        return True
    if message_role in {'assistant', 'system'} and contains_path(
        observation.get('input'), path
    ):
        return True
    return _contains_path_for_role(
        observation.get('input'),
        path,
        {'assistant', 'system'},
    )


def _contains_path_for_role(
    value: Any,
    path: str,
    roles: set[str],
) -> bool:
    if isinstance(value, Mapping):
        declared_role = str(value.get('role') or '').casefold()
        if declared_role in roles and contains_path(value, path):
            return True
        for key, item in value.items():
            if str(key).casefold() in roles and contains_path(item, path):
                return True
            if _contains_path_for_role(item, path, roles):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_path_for_role(item, path, roles) for item in value)
    return False


def _observation_contains_path(
    observation: Mapping[str, Any],
    path: str,
) -> bool:
    return contains_path(observation.get('input'), path) or contains_path(
        observation.get('output'), path
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _observation_id(observation: Optional[Mapping[str, Any]]) -> Optional[str]:
    if observation is None:
        return None
    return optional_str(observation.get('id') or observation.get('observation_id'))


def _unique_ids(values: Iterable[Optional[str]]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


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
    return timestamp, _observation_id(observation) or ''
