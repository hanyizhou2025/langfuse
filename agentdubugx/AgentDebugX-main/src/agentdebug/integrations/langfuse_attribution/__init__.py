"""Langfuse conversion and deterministic tool-failure attribution."""

from .attributor import LangfuseToolAttributor
from .converter import convert_langfuse_observations
from .failure_gate import (
    FailureGateDecision,
    FailureGateResult,
    evaluate_file_not_found_failure,
)
from .evaluation import (
    build_file_not_found_review_trajectories,
    evaluate_file_not_found_predictions,
    predict_file_not_found_cases,
)
from .historical_converter import convert_historical_langfuse_observations
from .llm_attributor import FileNotFoundLLMAttributor
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
    FILE_NOT_FOUND_ATTRIBUTION_METADATA_KEY,
    attach_file_not_found_attributions,
    attach_tool_attributions,
    file_not_found_attribution_summaries,
    file_not_found_attribution_to_dict,
    tool_attribution_summaries,
    tool_attribution_to_dict,
)

__all__ = [
    'CauseKind',
    'ConversionEvidence',
    'ATTRIBUTION_METADATA_KEY',
    'FILE_NOT_FOUND_ATTRIBUTION_METADATA_KEY',
    'FailureCategory',
    'FailureGateDecision',
    'FailureGateResult',
    'FileNotFoundAttributionResult',
    'FileNotFoundDecision',
    'FileNotFoundLLMAttributor',
    'LangfuseConversionResult',
    'LangfuseToolAttributor',
    'ParameterSource',
    'ToolFailureAttribution',
    'ToolFailureEvidence',
    'ValueSource',
    'attach_file_not_found_attributions',
    'attach_tool_attributions',
    'attribute_historical_file_not_found',
    'build_file_not_found_review_trajectories',
    'convert_historical_langfuse_observations',
    'convert_langfuse_observations',
    'evaluate_file_not_found_failure',
    'evaluate_file_not_found_predictions',
    'file_not_found_attribution_summaries',
    'file_not_found_attribution_to_dict',
    'predict_file_not_found_cases',
    'tool_attribution_summaries',
    'tool_attribution_to_dict',
]
