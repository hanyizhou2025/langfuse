"""Reconstruct minimal attribution evidence from historical Langfuse rows.

Historical observations often predate the explicit ``metadata.attribution``
contract.  This module derives only evidence that can be grounded in the trace
itself and deliberately returns ``UNKNOWN`` when a path source is ambiguous.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .converter import convert_langfuse_observations
from .models import LangfuseConversionResult, ParameterSource, ValueSource


_FILE_NOT_FOUND_TOKENS = (
    'enoent',
    'file not found',
    'no such file',
    'no such directory',
)
_PATH_ARGUMENT_KEYS = ('path', 'file', 'filename', 'file_path')
_ERROR_KEYS = ('error_code', 'error', 'code')


def convert_historical_langfuse_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    trace_id: Optional[str] = None,
) -> LangfuseConversionResult:
    """Convert historical rows and reconstruct high-confidence path evidence.

    The function never mutates caller-owned observations.  Existing explicit
    attribution hints remain authoritative.  Inferred hints are used only for
    observations whose metadata does not already contain the contract.
    """

    ordered = sorted(observations, key=_observation_sort_key)
    normalized: List[Dict[str, Any]] = [deepcopy(dict(item)) for item in ordered]
    inferred: Dict[str, Tuple[Dict[str, Any], ParameterSource]] = {}

    for index, observation in enumerate(normalized):
        observation_id = str(observation.get('id') or '')
        metadata = _mapping(observation.get('metadata'))
        if _mapping(metadata.get('attribution')):
            continue

        if _is_file_not_found_result(observation):
            argument = _extract_path_argument(observation.get('input'))
            source = _infer_path_source(ordered[:index], argument)
            error_code = _extract_error_code(observation)
            inferred[observation_id] = (argument, source)
            _set_attribution_hint(
                observation,
                {
                    'kind': 'tool_result',
                    'tool_name': str(observation.get('name') or 'unknown_tool'),
                    'tool_call_observation_id': observation_id,
                    'error_code': error_code or 'FILE_NOT_FOUND',
                    'error_message': _error_message(observation),
                    'resource_existence': dict.fromkeys(argument, False),
                },
            )
            continue

        source_role = _source_role(observation)
        if source_role:
            _set_attribution_hint(observation, {'kind': source_role})

    converted = convert_langfuse_observations(normalized, trace_id=trace_id)
    for failure in converted.evidence.tool_failures:
        result_id = failure.tool_result_observation_id or ''
        reconstructed = inferred.get(result_id)
        if reconstructed is None:
            continue
        arguments, source = reconstructed
        failure.arguments = dict(arguments)
        failure.argument_sources = dict.fromkeys(arguments, source)

    return converted


def _set_attribution_hint(
    observation: Dict[str, Any],
    hint: Mapping[str, Any],
) -> None:
    metadata = dict(_mapping(observation.get('metadata')))
    metadata['attribution'] = dict(hint)
    observation['metadata'] = metadata


def _is_file_not_found_result(observation: Mapping[str, Any]) -> bool:
    if str(observation.get('type') or '').upper() != 'TOOL':
        return False

    status_message = _optional_str(observation.get('status_message'))
    raw_output = observation.get('output')
    output = _mapping(raw_output)
    candidates = [status_message]
    candidates.extend(_optional_str(output.get(key)) for key in _ERROR_KEYS)
    candidates.append(_optional_str(output.get('message')))
    if not output:
        candidates.append(_optional_str(raw_output))
    error_text = ' '.join(item for item in candidates if item).lower()
    return any(token in error_text for token in _FILE_NOT_FOUND_TOKENS)


def _extract_path_argument(value: Any) -> Dict[str, Any]:
    found = _find_path_argument(value)
    if found is None:
        return {}
    key, path = found
    return {key: path}


def _find_path_argument(value: Any) -> Optional[Tuple[str, str]]:
    if isinstance(value, Mapping):
        for key in _PATH_ARGUMENT_KEYS:
            path = _optional_str(value.get(key))
            if path:
                return key, path
        for child in value.values():
            found = _find_path_argument(child)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            found = _find_path_argument(child)
            if found is not None:
                return found
    return None


def _infer_path_source(
    prior_observations: Sequence[Mapping[str, Any]],
    argument: Mapping[str, Any],
) -> ParameterSource:
    path = next(
        (_optional_str(value) for value in argument.values() if _optional_str(value)),
        None,
    )
    if not path:
        return ParameterSource(kind=ValueSource.UNKNOWN)

    matches: List[Tuple[ValueSource, str]] = []
    for observation in prior_observations:
        if not _observation_contains_path(observation, path):
            continue
        kind = _source_kind(observation)
        observation_id = _optional_str(observation.get('id'))
        if kind != ValueSource.UNKNOWN and observation_id:
            matches.append((kind, observation_id))

    if not matches:
        return ParameterSource(kind=ValueSource.UNKNOWN)

    kinds = {kind for kind, _ in matches}
    if ValueSource.USER in kinds:
        kind, observation_id = next(
            item for item in matches if item[0] == ValueSource.USER
        )
        return ParameterSource(kind=kind, observation_id=observation_id)
    if len(kinds) != 1:
        return ParameterSource(kind=ValueSource.UNKNOWN)

    kind, observation_id = matches[0]
    return ParameterSource(kind=kind, observation_id=observation_id)


def _observation_contains_path(
    observation: Mapping[str, Any],
    path: str,
) -> bool:
    return _contains_text(observation.get('input'), path) or _contains_text(
        observation.get('output'), path
    )


def _contains_text(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, Mapping):
        return any(_contains_text(item, needle) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_text(item, needle) for item in value)
    return False


def _source_role(observation: Mapping[str, Any]) -> Optional[str]:
    kind = _source_kind(observation)
    if kind == ValueSource.USER:
        return 'user'
    if kind == ValueSource.MODEL:
        return 'model'
    if kind == ValueSource.SKILL:
        return 'skill'
    return None


def _source_kind(observation: Mapping[str, Any]) -> ValueSource:
    metadata = _mapping(observation.get('metadata'))
    hint = _mapping(metadata.get('attribution'))
    explicit = _optional_str(hint.get('kind'))
    if explicit:
        try:
            return ValueSource(explicit.lower())
        except ValueError:
            pass

    observation_type = str(observation.get('type') or '').upper()
    name = str(observation.get('name') or '').lower()
    if observation_type == 'GENERATION':
        return ValueSource.MODEL
    if any(token in name for token in ('user', 'human')):
        return ValueSource.USER
    if 'skill' in name or 'memory' in name:
        return ValueSource.SKILL
    return ValueSource.UNKNOWN


def _extract_error_code(observation: Mapping[str, Any]) -> Optional[str]:
    output = _mapping(observation.get('output'))
    for key in _ERROR_KEYS:
        value = _optional_str(output.get(key))
        if value:
            return value
    return None


def _error_message(observation: Mapping[str, Any]) -> str:
    status = _optional_str(observation.get('status_message'))
    if status:
        return status
    output = _mapping(observation.get('output'))
    return _optional_str(output.get('message')) or 'File not found.'


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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
