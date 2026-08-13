"""Convert Langfuse observations into a detector-safe AgentDebugX trajectory."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from agentdebug.schema import AgentEvent, AgentTrajectory, EventType

from .models import (
    ConversionEvidence,
    LangfuseConversionResult,
    ParameterSource,
    ToolFailureEvidence,
    ValueSource,
)


_ROLE_EVENT_TYPES = {
    'user': EventType.HUMAN_FEEDBACK,
    'skill': EventType.MEMORY_READ,
    'model': EventType.LLM_RESPONSE,
    'tool_call': EventType.TOOL_CALL,
    'tool_result': EventType.TOOL_RESULT,
}


def convert_langfuse_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    trace_id: Optional[str] = None,
) -> LangfuseConversionResult:
    """Convert one trace's observations without exposing arbitrary JSON to rules.

    The optional ``metadata.attribution`` contract is intentionally explicit:

    ``kind``
        ``user``, ``skill``, ``model``, ``tool_call``, or ``tool_result``.
    ``tool_call``
        supplies ``tool_name``, ``arguments``, and optional
        ``argument_sources`` keyed by argument name.
    ``tool_result``
        supplies ``tool_call_observation_id``, ``error_code``, optional
        ``execution_context``, ``resource_existence``, and ``dependency``.

    Raw Langfuse ``input``, ``output``, and arbitrary metadata are never copied
    into ``AgentEvent``.  Tool arguments needed for attribution remain only in
    the returned ``ConversionEvidence`` bundle.
    """

    ordered = sorted(observations, key=_observation_sort_key)
    resolved_trace_id = trace_id or _first_trace_id(ordered)
    trajectory = AgentTrajectory(trace_id=resolved_trace_id, framework='langfuse')
    evidence = ConversionEvidence(trace_id=resolved_trace_id)
    tool_calls: Dict[str, ToolFailureEvidence] = {}
    last_tool_call_id: Optional[str] = None

    for step_index, observation in enumerate(ordered):
        observation_id = str(observation.get('id') or 'langfuse-observation-%d' % step_index)
        metadata = _mapping(observation.get('metadata'))
        hint = _mapping(metadata.get('attribution'))
        role = _role_for(observation, hint)
        event_type = _ROLE_EVENT_TYPES.get(role, EventType.OBSERVATION)
        parent_event_id = _optional_str(observation.get('parent_observation_id'))
        error = _detector_error(observation, hint, role)

        # Do not put observation IDs, names, arbitrary metadata, or tool names
        # here: HeuristicAnalyzer stringifies metadata before keyword matching.
        safe_metadata: Dict[str, Any] = {'integration': 'langfuse'}
        tool_name = _optional_str(hint.get('tool_name'))
        if error:
            safe_metadata['has_recorded_tool_error'] = True

        # Preserve repeated-tool-call detection using an opaque, deterministic
        # signature rather than raw arguments.  The signature is not an input
        # value and cannot accidentally match the keyword rules.
        detector_input: Optional[Dict[str, str]] = None
        if role == 'tool_call':
            detector_input = {
                'call_signature': _signature(
                    tool_name or str(observation.get('name') or 'unknown_tool'),
                    _mapping(hint.get('arguments')),
                )
            }

        trajectory.add_event(
            AgentEvent(
                event_id=observation_id,
                trace_id=resolved_trace_id,
                parent_event_id=parent_event_id,
                agent_name=str(observation.get('name') or 'langfuse'),
                event_type=event_type,
                module=None,
                step_index=step_index,
                timestamp=_timestamp(observation.get('start_time')),
                input=detector_input,
                error=error,
                metadata=safe_metadata,
            )
        )
        evidence.observation_ids.append(observation_id)

        if role == 'tool_call':
            tool_calls[observation_id] = ToolFailureEvidence(
                trace_id=resolved_trace_id,
                tool_name=tool_name or str(observation.get('name') or 'unknown_tool'),
                tool_call_observation_id=observation_id,
                arguments=dict(_mapping(hint.get('arguments'))),
                argument_sources=_argument_sources(hint.get('argument_sources')),
                tool_selection_source=_parameter_source(hint.get('tool_selection_source')),
            )
            last_tool_call_id = observation_id
        elif role == 'tool_result' and error:
            call_id = _optional_str(hint.get('tool_call_observation_id'))
            if not call_id and parent_event_id in tool_calls:
                call_id = parent_event_id
            if not call_id:
                call_id = last_tool_call_id
            failure = tool_calls.get(call_id or '')
            if failure is None:
                failure = ToolFailureEvidence(
                    trace_id=resolved_trace_id,
                    tool_name=tool_name or str(observation.get('name') or 'unknown_tool'),
                    tool_call_observation_id=call_id or observation_id,
                )
            failure.tool_result_observation_id = observation_id
            failure.error_code = _optional_str(hint.get('error_code'))
            failure.error_message = error
            failure.execution_context = dict(_mapping(hint.get('execution_context')))
            failure.resource_existence = _resource_existence(
                hint.get('resource_existence')
            )
            failure.dependency = _optional_str(hint.get('dependency'))
            evidence.tool_failures.append(failure)

    return LangfuseConversionResult(trajectory=trajectory, evidence=evidence)


def _role_for(observation: Mapping[str, Any], hint: Mapping[str, Any]) -> str:
    explicit = _optional_str(hint.get('kind'))
    if explicit and explicit.lower() in _ROLE_EVENT_TYPES:
        return explicit.lower()
    observation_type = str(observation.get('type') or '').upper()
    if observation_type == 'GENERATION':
        return 'model'
    return 'observation'


def _detector_error(
    observation: Mapping[str, Any], hint: Mapping[str, Any], role: str,
) -> Optional[str]:
    if role != 'tool_result':
        return None
    explicit_error = _optional_str(hint.get('error_message'))
    if explicit_error:
        return explicit_error
    status_message = _optional_str(observation.get('status_message'))
    if status_message:
        return status_message
    if str(observation.get('level') or '').upper() == 'ERROR':
        return _optional_str(hint.get('error_code')) or 'Tool execution failed.'
    return None


def _argument_sources(value: Any) -> Dict[str, ParameterSource]:
    sources: Dict[str, ParameterSource] = {}
    for argument, raw_source in _mapping(value).items():
        sources[str(argument)] = _parameter_source(raw_source)
    return sources


def _parameter_source(value: Any) -> ParameterSource:
    source = _mapping(value)
    return ParameterSource(
        kind=_value_source(source.get('kind')),
        observation_id=_optional_str(source.get('observation_id')),
        detail=_optional_str(source.get('detail')),
    )


def _resource_existence(value: Any) -> Dict[str, Optional[bool]]:
    result: Dict[str, Optional[bool]] = {}
    for argument, exists in _mapping(value).items():
        result[str(argument)] = exists if isinstance(exists, bool) else None
    return result


def _value_source(value: Any) -> ValueSource:
    try:
        return ValueSource(str(value).lower())
    except ValueError:
        return ValueSource.UNKNOWN


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_trace_id(observations: Iterable[Mapping[str, Any]]) -> str:
    for observation in observations:
        trace_id = _optional_str(observation.get('trace_id'))
        if trace_id:
            return trace_id
    return 'langfuse-trace'


def _observation_sort_key(observation: Mapping[str, Any]) -> Tuple[datetime, str]:
    timestamp = _timestamp(observation.get('start_time'))
    return timestamp, str(observation.get('id') or '')


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return (
                parsed.replace(tzinfo=timezone.utc)
                if parsed.tzinfo is None
                else parsed
            )
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _signature(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """Produce a stable detector token without placing argument text in events."""

    material = repr((tool_name, sorted(arguments.items(), key=lambda item: item[0])))
    return sha256(material.encode('utf-8')).hexdigest()
