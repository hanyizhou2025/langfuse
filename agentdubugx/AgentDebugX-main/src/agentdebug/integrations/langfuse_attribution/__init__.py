"""Langfuse conversion and deterministic tool-failure attribution."""

from .attributor import LangfuseToolAttributor
from .converter import convert_langfuse_observations
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
    'LangfuseConversionResult',
    'LangfuseToolAttributor',
    'ParameterSource',
    'ToolFailureAttribution',
    'ToolFailureEvidence',
    'ValueSource',
    'attach_tool_attributions',
    'convert_langfuse_observations',
    'tool_attribution_summaries',
    'tool_attribution_to_dict',
]
