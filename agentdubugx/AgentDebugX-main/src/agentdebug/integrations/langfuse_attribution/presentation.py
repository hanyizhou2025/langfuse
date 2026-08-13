"""Safe presentation helpers for tool-failure attribution results."""

from __future__ import annotations

from typing import Any, Dict, List

from agentdebug.schema import AgentTrajectory

from .attributor import LangfuseToolAttributor
from .models import ConversionEvidence, ToolFailureAttribution


ATTRIBUTION_METADATA_KEY = 'langfuse_tool_attributions'


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
