"""Role-aware synthetic observations for Langfuse trace-level input."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


_ROLES = {'assistant', 'system', 'user'}


def build_trace_input_observations(
    trace: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Preserve declared message roles instead of assuming every input is user."""

    trace_id = _optional_str(trace.get('trace_id')) or _optional_str(trace.get('id'))
    input_value = trace.get('input_redacted', trace.get('input'))
    if not trace_id or input_value is None:
        return []

    role_payloads = _role_payloads(input_value)
    if not role_payloads:
        role_payloads = [('user', input_value)]
    explicit_roles = _has_explicit_role(input_value)
    observations: List[Dict[str, Any]] = []
    role_counts: Dict[str, int] = {}
    for role, payload in role_payloads:
        index = role_counts.get(role, 0)
        role_counts[role] = index + 1
        observation_id = (
            f'{trace_id}:input:{role}:{index}'
            if explicit_roles
            else f'{trace_id}:input'
        )
        metadata = {
            'synthetic_role': 'trace_input',
            'message_role': role,
        }
        observations.append(
            {
                'id': observation_id,
                'observation_id': observation_id,
                'trace_id': trace_id,
                'parent_observation_id': None,
                'type': 'GENERATION' if role == 'assistant' else 'SPAN',
                'name': f'{role}.context' if role != 'user' else 'user.request',
                'start_time': trace.get('timestamp'),
                'input': payload,
                'input_redacted': payload,
                'output': None,
                'output_redacted': None,
                'metadata': metadata,
                'metadata_redacted': metadata,
                'level': 'DEFAULT',
                'status_message': None,
                'status_message_redacted': None,
            }
        )
    return observations


def _role_payloads(value: Any) -> List[Tuple[str, Any]]:
    if isinstance(value, Mapping):
        role = str(value.get('role') or '').casefold()
        if role in _ROLES:
            return [(role, value.get('content', value))]

        result: List[Tuple[str, Any]] = []
        messages = value.get('messages')
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            for message in messages:
                result.extend(_role_payloads(message))
        for key, payload in value.items():
            normalized = str(key).casefold()
            if normalized in _ROLES:
                result.append((normalized, payload))
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        result = []
        for item in value:
            result.extend(_role_payloads(item))
        return result
    return []


def _has_explicit_role(value: Any) -> bool:
    if isinstance(value, Mapping):
        if str(value.get('role') or '').casefold() in _ROLES:
            return True
        if any(str(key).casefold() in _ROLES for key in value):
            return True
        messages = value.get('messages')
        return isinstance(messages, Sequence) and not isinstance(
            messages, (str, bytes)
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_has_explicit_role(item) for item in value)
    return False


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
