"""Safe presentation helpers for tool-failure attribution results."""

from __future__ import annotations

from dataclasses import asdict
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Union

from agentdebug.schema import AgentTrajectory

from .attributor import LangfuseToolAttributor
from .models import ConversionEvidence, ToolFailureAttribution
from .pipeline import FileNotFoundAttributionResult


ATTRIBUTION_METADATA_KEY = 'langfuse_tool_attributions'
FILE_NOT_FOUND_ATTRIBUTION_METADATA_KEY = 'langfuse_file_not_found_attributions'
_FILE_NOT_FOUND_DISPLAY_FIELDS = (
    'case_id',
    'trace_id',
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
)


def attach_tool_attributions(
    trajectory: AgentTrajectory,
    evidence: ConversionEvidence,
) -> List[ToolFailureAttribution]:
    """Attach a safe, UI-ready summary without copying raw tool arguments."""

    attributions = LangfuseToolAttributor().attribute(evidence)
    trajectory.metadata[ATTRIBUTION_METADATA_KEY] = [
        tool_attribution_to_dict(attribution) for attribution in attributions
    ]
    return attributions


def tool_attribution_to_dict(attribution: ToolFailureAttribution) -> Dict[str, Any]:
    """Serialize only display-safe attribution fields for a trajectory store."""

    return {
        'tool_name': attribution.tool_name,
        'failure_event_id': attribution.failure_event_id,
        'failure_category': attribution.failure_category.value,
        'primary_cause': attribution.primary_cause.value,
        'confidence': attribution.confidence,
        'source_observation_id': attribution.source_observation_id,
        'evidence': list(attribution.evidence),
        'contributing_causes': [
            cause.value for cause in attribution.contributing_causes
        ],
    }


def tool_attribution_summaries(value: Any) -> List[Dict[str, Any]]:
    """Return only well-formed attribution summaries from stored metadata."""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def attach_file_not_found_attributions(
    trajectory: AgentTrajectory,
    results: Iterable[Union[FileNotFoundAttributionResult, Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Attach display-safe File Not Found decisions to a stored trajectory."""

    summaries = [file_not_found_attribution_to_dict(result) for result in results]
    trajectory.metadata[FILE_NOT_FOUND_ATTRIBUTION_METADATA_KEY] = summaries
    return summaries


def file_not_found_attribution_to_dict(
    result: Union[FileNotFoundAttributionResult, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Serialize the stable decision contract without raw path values."""

    raw_payload = (
        asdict(result)
        if isinstance(result, FileNotFoundAttributionResult)
        else dict(result)
    )
    payload = {key: raw_payload.get(key) for key in _FILE_NOT_FOUND_DISPLAY_FIELDS}
    return _serialize_enums(payload)


def file_not_found_attribution_summaries(value: Any) -> List[Dict[str, Any]]:
    """Return only well-formed File Not Found summaries from metadata."""

    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _serialize_enums(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _serialize_enums(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_enums(item) for item in value]
    return value
