"""Langfuse conversion and deterministic tool-failure attribution."""

from .attributor import LangfuseToolAttributor
from .converter import convert_langfuse_observations
from .failure_gate import (
    FailureGateDecision,
    FailureGateResult,
    evaluate_file_not_found_failure,
)
from .historical_converter import convert_historical_langfuse_observations
from .models import (
    CauseKind,
    ConversionEvidence,
    FailureCategory,
    LangfuseConversionResult,
    ParameterSource,
    ToolFailureAttribution,
    ToolFailureEvidence,
    ValueSource,
)
from .pipeline import (
    FileNotFoundAttributionResult,
    FileNotFoundDecision,
    attribute_historical_file_not_found,
)
from .presentation import (
    ATTRIBUTION_METADATA_KEY,
    attach_tool_attributions,
    tool_attribution_summaries,
    tool_attribution_to_dict,
)

__all__ = [
    'CauseKind',
    'ConversionEvidence',
    'ATTRIBUTION_METADATA_KEY',
    'FailureCategory',
    'FailureGateDecision',
    'FailureGateResult',
    'FileNotFoundAttributionResult',
    'FileNotFoundDecision',
    'LangfuseConversionResult',
    'LangfuseToolAttributor',
    'ParameterSource',
    'ToolFailureAttribution',
    'ToolFailureEvidence',
    'ValueSource',
    'attach_tool_attributions',
    'attribute_historical_file_not_found',
    'convert_historical_langfuse_observations',
    'convert_langfuse_observations',
    'evaluate_file_not_found_failure',
    'tool_attribution_summaries',
    'tool_attribution_to_dict',
]
