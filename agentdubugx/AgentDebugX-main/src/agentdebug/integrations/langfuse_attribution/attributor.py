"""Deterministic, evidence-bearing attribution for failed Langfuse tools."""

from __future__ import annotations

from typing import List, Optional, Tuple

from .models import (
    CauseKind,
    ConversionEvidence,
    FailureCategory,
    ParameterSource,
    ToolFailureAttribution,
    ToolFailureEvidence,
    ValueSource,
)


class LangfuseToolAttributor:
    """Attribute tool failures only when the supplied evidence supports it.

    This class intentionally uses no LLM.  It returns ``UNKNOWN`` when the
    converter lacks a source chain or execution fact instead of inferring
    intent from raw text.
    """

    def attribute(self, evidence: ConversionEvidence) -> List[ToolFailureAttribution]:
        return [self._attribute_failure(failure) for failure in evidence.tool_failures]

    def _attribute_failure(self, failure: ToolFailureEvidence) -> ToolFailureAttribution:
        category = _failure_category(failure)
        if category == FailureCategory.RESOURCE_NOT_FOUND:
            return self._resource_not_found(failure, category)
        if category in {FailureCategory.PARAMETER_VALIDATION, FailureCategory.TOOL_CONTRACT}:
            return self._parameter_or_contract(failure, category)
        if category == FailureCategory.AUTHORIZATION:
            return self._authorization(failure, category)
        if category == FailureCategory.RATE_LIMIT:
            return self._external(failure, category, CauseKind.EXTERNAL_DEPENDENCY_RATE_LIMIT, 0.9)
        if category == FailureCategory.TIMEOUT:
            return self._external(failure, category, CauseKind.EXTERNAL_DEPENDENCY_TIMEOUT, 0.8)
        if category == FailureCategory.DEPENDENCY_UNAVAILABLE:
            return self._external(
                failure,
                category,
                CauseKind.EXTERNAL_DEPENDENCY_UNAVAILABLE,
                0.8,
            )
        return _result(
            failure,
            category,
            CauseKind.UNKNOWN,
            0.0,
            failure.tool_result_observation_id,
            ['No deterministic attribution rule matched the recorded failure.'],
        )

    def _resource_not_found(
        self,
        failure: ToolFailureEvidence,
        category: FailureCategory,
    ) -> ToolFailureAttribution:
        existing_argument = next(
            (name for name, exists in failure.resource_existence.items() if exists is True),
            None,
        )
        if existing_argument:
            return _result(
                failure,
                category,
                CauseKind.RUNTIME_ENVIRONMENT_MISMATCH,
                0.95,
                failure.tool_result_observation_id,
                [
                    _error_evidence(failure),
                    "Resource for argument '%s' existed before execution."
                    % existing_argument,
                    'The tool could not access it from its recorded runtime context.',
                ],
            )

        missing_argument = next(
            (name for name, exists in failure.resource_existence.items() if exists is False),
            None,
        )
        if missing_argument:
            source = failure.argument_sources.get(missing_argument)
            return self._source_attribution(
                failure,
                category,
                missing_argument,
                source,
                resource_missing=True,
            )
        return _result(
            failure,
            category,
            CauseKind.UNKNOWN,
            0.35,
            failure.tool_result_observation_id,
            [
                _error_evidence(failure),
                'No pre-execution resource state was supplied for the failing argument.',
            ],
        )

    def _parameter_or_contract(
        self,
        failure: ToolFailureEvidence,
        category: FailureCategory,
    ) -> ToolFailureAttribution:
        if category == FailureCategory.TOOL_CONTRACT:
            selection = failure.tool_selection_source
            if selection is not None and selection.kind == ValueSource.MODEL:
                return _result(
                    failure,
                    category,
                    CauseKind.MODEL_TOOL_SELECTION_ERROR,
                    0.85,
                    selection.observation_id or failure.tool_result_observation_id,
                    [
                        _error_evidence(failure),
                        'The incompatible tool was selected by model evidence.',
                    ],
                )
        if not failure.argument_sources:
            return _result(
                failure,
                category,
                CauseKind.TOOL_CONTRACT_MISMATCH
                if category == FailureCategory.TOOL_CONTRACT
                else CauseKind.UNKNOWN,
                0.65 if category == FailureCategory.TOOL_CONTRACT else 0.3,
                failure.tool_result_observation_id,
                [_error_evidence(failure), 'No argument provenance was recorded.'],
            )
        argument, source = next(iter(failure.argument_sources.items()))
        return self._source_attribution(
            failure,
            category,
            argument,
            source,
            resource_missing=False,
        )

    def _authorization(
        self,
        failure: ToolFailureEvidence,
        category: FailureCategory,
    ) -> ToolFailureAttribution:
        credential_source = _source_from_context(failure)
        if credential_source is not None:
            if credential_source.kind == ValueSource.SKILL:
                cause = CauseKind.SKILL_CONFIGURATION_INVALID
            elif credential_source.kind == ValueSource.ENVIRONMENT:
                cause = CauseKind.RUNTIME_ENVIRONMENT_MISMATCH
            else:
                cause = CauseKind.CREDENTIAL_OR_PERMISSION_CONFIGURATION
            return _result(
                failure,
                category,
                cause,
                0.9,
                credential_source.observation_id or failure.tool_result_observation_id,
                [
                    _error_evidence(failure),
                    'Credential was supplied by %s evidence.'
                    % credential_source.kind.value,
                ],
            )
        return _result(
            failure,
            category,
            CauseKind.CREDENTIAL_OR_PERMISSION_CONFIGURATION,
            0.7,
            failure.tool_result_observation_id,
            [_error_evidence(failure), 'No credential provenance was recorded.'],
        )

    def _external(
        self,
        failure: ToolFailureEvidence,
        category: FailureCategory,
        cause: CauseKind,
        confidence: float,
    ) -> ToolFailureAttribution:
        dependency = failure.dependency or 'an unspecified external dependency'
        return _result(
            failure,
            category,
            cause,
            confidence,
            failure.tool_result_observation_id,
            [_error_evidence(failure), 'Affected dependency: %s.' % dependency],
        )

    def _source_attribution(
        self,
        failure: ToolFailureEvidence,
        category: FailureCategory,
        argument: str,
        source: Optional[ParameterSource],
        *,
        resource_missing: bool,
    ) -> ToolFailureAttribution:
        cause, confidence = _cause_for_source(source, resource_missing=resource_missing)
        source_observation_id = (
            source.observation_id if source and source.observation_id else failure.tool_result_observation_id
        )
        details = [_error_evidence(failure)]
        if resource_missing:
            details.append("Resource for argument '%s' does not exist before execution." % argument)
        if source is None or source.kind == ValueSource.UNKNOWN:
            details.append("No recorded source exists for argument '%s'." % argument)
        else:
            details.append(
                "Argument '%s' was supplied by %s evidence."
                % (argument, source.kind.value)
            )
        return _result(
            failure,
            category,
            cause,
            confidence,
            source_observation_id,
            details,
        )


def _failure_category(failure: ToolFailureEvidence) -> FailureCategory:
    text = ' '.join(
        item for item in [failure.error_code, failure.error_message] if item
    ).lower()
    if any(token in text for token in ('enoent', 'not found', 'no such file', 'no such directory')):
        return FailureCategory.RESOURCE_NOT_FOUND
    if any(token in text for token in ('429', 'rate limit', 'too many requests')):
        return FailureCategory.RATE_LIMIT
    if any(token in text for token in ('401', '403', 'unauthorized', 'forbidden', 'permission denied')):
        return FailureCategory.AUTHORIZATION
    if any(token in text for token in ('timeout', 'timed out', 'deadline exceeded')):
        return FailureCategory.TIMEOUT
    if any(token in text for token in ('502', '503', '504', 'connection refused', 'dns')):
        return FailureCategory.DEPENDENCY_UNAVAILABLE
    if any(token in text for token in ('schema mismatch', 'unsupported operation', 'api version')):
        return FailureCategory.TOOL_CONTRACT
    if any(token in text for token in ('invalid argument', 'validation', 'required parameter', 'malformed')):
        return FailureCategory.PARAMETER_VALIDATION
    return FailureCategory.UNKNOWN


def _cause_for_source(
    source: Optional[ParameterSource],
    *,
    resource_missing: bool,
) -> Tuple[CauseKind, float]:
    if source is None:
        return CauseKind.UNKNOWN, 0.3
    if source.kind == ValueSource.USER:
        return CauseKind.USER_INPUT_INVALID, 0.95 if resource_missing else 0.8
    if source.kind == ValueSource.SKILL:
        return CauseKind.SKILL_CONFIGURATION_INVALID, 0.95 if resource_missing else 0.8
    if source.kind == ValueSource.MODEL:
        return CauseKind.MODEL_ARGUMENT_HALLUCINATION, 0.95 if resource_missing else 0.75
    if source.kind == ValueSource.SYSTEM:
        return CauseKind.SYSTEM_DEFAULT_INVALID, 0.85
    if source.kind == ValueSource.ENVIRONMENT:
        return CauseKind.RUNTIME_ENVIRONMENT_MISMATCH, 0.85
    return CauseKind.UNKNOWN, 0.3


def _source_from_context(failure: ToolFailureEvidence) -> Optional[ParameterSource]:
    value = failure.execution_context.get('credential_source')
    if not isinstance(value, dict):
        return None
    try:
        kind = ValueSource(str(value.get('kind')).lower())
    except ValueError:
        kind = ValueSource.UNKNOWN
    observation_id = value.get('observation_id')
    return ParameterSource(
        kind=kind,
        observation_id=str(observation_id) if observation_id else None,
    )


def _error_evidence(failure: ToolFailureEvidence) -> str:
    error = failure.error_code or failure.error_message or 'unknown tool error'
    return "Tool '%s' reported %s." % (failure.tool_name, error)


def _result(
    failure: ToolFailureEvidence,
    category: FailureCategory,
    cause: CauseKind,
    confidence: float,
    source_observation_id: Optional[str],
    evidence: List[str],
) -> ToolFailureAttribution:
    return ToolFailureAttribution(
        tool_name=failure.tool_name,
        failure_event_id=failure.tool_result_observation_id or failure.tool_call_observation_id,
        failure_category=category,
        primary_cause=cause,
        confidence=confidence,
        source_observation_id=source_observation_id,
        evidence=evidence,
    )
