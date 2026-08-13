"""Portable evidence models for Langfuse tool-failure attribution.

The models deliberately keep provenance separate from ``AgentEvent``.  The
heuristic detector treats every event field as searchable text, so putting raw
Langfuse input/output in an event would turn data values into false failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from agentdebug.schema import AgentTrajectory


class ValueSource(str, Enum):
    """The recorded origin of a tool argument or credential."""

    USER = 'user'
    SKILL = 'skill'
    MODEL = 'model'
    SYSTEM = 'system'
    ENVIRONMENT = 'environment'
    UNKNOWN = 'unknown'


class FailureCategory(str, Enum):
    """The direct technical failure observed from a tool result."""

    RESOURCE_NOT_FOUND = 'resource_not_found'
    PARAMETER_VALIDATION = 'parameter_validation'
    AUTHORIZATION = 'authorization'
    RATE_LIMIT = 'rate_limit'
    TIMEOUT = 'timeout'
    DEPENDENCY_UNAVAILABLE = 'dependency_unavailable'
    TOOL_CONTRACT = 'tool_contract'
    UNKNOWN = 'unknown'


class CauseKind(str, Enum):
    """Auditable failure sources; ``UNKNOWN`` is a deliberate valid result."""

    USER_INPUT_INVALID = 'user_input_invalid'
    SKILL_CONFIGURATION_INVALID = 'skill_configuration_invalid'
    MODEL_ARGUMENT_HALLUCINATION = 'model_argument_hallucination'
    MODEL_TOOL_SELECTION_ERROR = 'model_tool_selection_error'
    SYSTEM_DEFAULT_INVALID = 'system_default_invalid'
    RUNTIME_ENVIRONMENT_MISMATCH = 'runtime_environment_mismatch'
    TOOL_CONTRACT_MISMATCH = 'tool_contract_mismatch'
    CREDENTIAL_OR_PERMISSION_CONFIGURATION = 'credential_or_permission_configuration'
    EXTERNAL_DEPENDENCY_RATE_LIMIT = 'external_dependency_rate_limit'
    EXTERNAL_DEPENDENCY_TIMEOUT = 'external_dependency_timeout'
    EXTERNAL_DEPENDENCY_UNAVAILABLE = 'external_dependency_unavailable'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class ParameterSource:
    """One declared source for a value passed to a tool."""

    kind: ValueSource
    observation_id: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class ToolFailureEvidence:
    """The evidence bundle for one failed tool attempt.

    ``arguments`` and source details never enter the AgentDebugX event used by
    heuristic detection.  Callers should apply their own redaction policy
    before persisting this bundle outside the current process.
    """

    trace_id: str
    tool_name: str
    tool_call_observation_id: str
    tool_result_observation_id: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    argument_sources: Dict[str, ParameterSource] = field(default_factory=dict)
    tool_selection_source: Optional[ParameterSource] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    execution_context: Dict[str, Any] = field(default_factory=dict)
    resource_existence: Dict[str, Optional[bool]] = field(default_factory=dict)
    dependency: Optional[str] = None


@dataclass
class ConversionEvidence:
    """Evidence intentionally isolated from the detector-facing trajectory."""

    trace_id: str
    tool_failures: List[ToolFailureEvidence] = field(default_factory=list)
    observation_ids: List[str] = field(default_factory=list)


@dataclass
class LangfuseConversionResult:
    """A detector-safe trajectory plus the separate provenance evidence."""

    trajectory: AgentTrajectory
    evidence: ConversionEvidence


@dataclass
class ToolFailureAttribution:
    """One deterministic, evidence-bearing attribution conclusion."""

    tool_name: str
    failure_event_id: str
    failure_category: FailureCategory
    primary_cause: CauseKind
    confidence: float
    source_observation_id: Optional[str]
    evidence: List[str] = field(default_factory=list)
    contributing_causes: List[CauseKind] = field(default_factory=list)
