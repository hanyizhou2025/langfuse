"""Server-side validation for failure-attribution Annotation v2."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Set


CONFIDENCE = {'high', 'medium', 'low'}
ANNOTATION_STATUSES = {
    'draft',
    'submitted',
    'needs_review',
    'adjudicated',
    'excluded',
    'frozen',
}
TECHNICAL_LABELS = {'confirmed', 'rule_false_positive', 'insufficient_evidence'}
CORRECTION_REASONS = {
    'successful_content_mentions_error',
    'expected_text_assertion',
    'log_or_documentation_only',
    'wrong_observation_type',
    'duplicate_candidate',
    'other',
}
SEMANTICS = {
    'unexpected_failure',
    'expected_negative_result',
    'validation_probe',
    'control_flow_signal',
    'recovered_failure',
    'tolerated_failure',
    'user_requested_negative_test',
    'external_failure',
    'unknown',
}
RECOVERY_STATUSES = {'recovered', 'not_recovered', 'unknown'}
ROOT_SCOPES = {'current_trace', 'outside_current_trace', 'unknown'}
ROOT_DOMAINS = {
    'user',
    'model',
    'skill',
    'upstream',
    'runtime',
    'environment',
    'tool',
    'external_dependency',
    'unknown',
}
ROOT_LABELS = {
    'user_path_invalid',
    'model_path_hallucination',
    'upstream_path_invalid',
    'runtime_path_unavailable',
    'tool_contract_mismatch',
    'external_dependency_failure',
    'unknown',
}
PATH_SOURCES = {
    'user',
    'assistant_history',
    'model',
    'skill',
    'system',
    'environment',
    'unknown',
}


class AnnotationValidationError(ValueError):
    """A validation failure with stable field-level messages for the UI."""

    def __init__(self, field_errors: Mapping[str, str]) -> None:
        self.field_errors = dict(field_errors)
        super().__init__('Annotation validation failed.')


def validate_annotation(
    payload: Mapping[str, Any],
    case_envelope: Mapping[str, Any],
    *,
    strict: bool,
) -> Dict[str, Any]:
    """Validate and normalize a draft or submitted annotation."""

    annotation: Dict[str, Any] = deepcopy(dict(payload))
    errors: Dict[str, str] = {}
    case = _mapping(case_envelope.get('case'))
    trace = _mapping(case_envelope.get('trace'))
    expected_case = str(case.get('case_id') or '')
    expected_project = str(case_envelope.get('project_id') or '')
    expected_trace = str(trace.get('trace_id') or case.get('trace_id') or '')

    _equal(errors, annotation, 'schema_version', 'failure-attribution-annotation-v2')
    _equal(errors, annotation, 'case_id', expected_case)
    _equal(errors, annotation, 'project_id', expected_project)
    _equal(errors, annotation, 'trace_id', expected_trace)
    status = str(annotation.get('annotation_status') or 'draft')
    if status not in ANNOTATION_STATUSES:
        errors['annotation_status'] = 'Unsupported annotation status.'
    annotation['annotation_status'] = status

    technical = _section(annotation, 'technical_error_review')
    human_label = _enum(
        errors,
        technical,
        'technical_error_review.human_label',
        TECHNICAL_LABELS,
        required=strict,
    )
    _confidence(errors, technical, 'technical_error_review.confidence', strict)
    if human_label == 'rule_false_positive':
        correction = technical.get('correction_reason')
        if correction not in CORRECTION_REASONS:
            errors['technical_error_review.correction_reason'] = (
                'A correction reason is required for a rule false positive.'
            )
        if _has_failure_fields(annotation):
            errors['failure_manifestation'] = (
                'Rule false positives cannot contain failure observations.'
            )
        if _has_root_fields(annotation):
            errors['attribution'] = 'Rule false positives cannot contain root causes.'
        semantic = _mapping(annotation.get('semantic_outcome'))
        if semantic.get('recovery_observation_id'):
            errors['semantic_outcome.recovery_observation_id'] = (
                'Rule false positives cannot contain a recovery observation.'
            )
    elif strict and human_label != 'confirmed':
        annotation['annotation_status'] = 'needs_review'

    semantic = _section(annotation, 'semantic_outcome')
    _enum(
        errors,
        semantic,
        'semantic_outcome.tool_result_semantics',
        SEMANTICS,
        required=strict and human_label == 'confirmed',
    )
    for key in ('is_agent_failure', 'is_task_failure'):
        value = semantic.get(key)
        if value is not None and not isinstance(value, bool):
            errors['semantic_outcome.%s' % key] = 'Use true, false, or null.'
    recovery_status = _enum(
        errors,
        semantic,
        'semantic_outcome.recovery_status',
        RECOVERY_STATUSES,
        required=strict and human_label == 'confirmed',
    )
    if recovery_status == 'recovered' and not semantic.get('recovery_observation_id'):
        errors['semantic_outcome.recovery_observation_id'] = (
            'A recovered result requires a recovery observation.'
        )
    _confidence(
        errors,
        semantic,
        'semantic_outcome.confidence',
        strict and human_label == 'confirmed',
    )

    manifestation = _section(annotation, 'failure_manifestation')
    if strict and human_label == 'confirmed':
        for key in ('failure_onset_observation_id', 'primary_failure_observation_id'):
            if not manifestation.get(key):
                errors['failure_manifestation.%s' % key] = 'This field is required.'
        if not _string_list(manifestation.get('failure_observation_ids')):
            errors['failure_manifestation.failure_observation_ids'] = (
                'Select at least one failure observation.'
            )

    attribution = _section(annotation, 'attribution')
    scope = _enum(
        errors,
        attribution,
        'attribution.root_cause_scope',
        ROOT_SCOPES,
        required=strict and human_label == 'confirmed',
    )
    roots = _string_list(attribution.get('root_cause_observation_ids'))
    primary_root = attribution.get('primary_root_cause_observation_id')
    if scope == 'current_trace':
        required_root_fields = {
            'primary_root_cause_observation_id': primary_root,
            'root_cause_observation_ids': roots,
            'root_cause_domain': attribution.get('root_cause_domain'),
            'root_cause_label': attribution.get('root_cause_label'),
        }
        for key, value in required_root_fields.items():
            if strict and not value:
                errors['attribution.%s' % key] = 'This field is required.'
        if attribution.get('applicable') is not True:
            errors['attribution.applicable'] = (
                'Current-trace attribution must be applicable.'
            )
        _enum(
            errors,
            attribution,
            'attribution.root_cause_domain',
            ROOT_DOMAINS,
            required=strict,
        )
        _enum(
            errors,
            attribution,
            'attribution.root_cause_label',
            ROOT_LABELS,
            required=strict,
        )
    elif scope in {'outside_current_trace', 'unknown'}:
        if primary_root:
            errors['attribution.primary_root_cause_observation_id'] = (
                'No current-trace root is allowed for this scope.'
            )
        if roots:
            errors['attribution.root_cause_observation_ids'] = (
                'No current-trace roots are allowed for this scope.'
            )
        if attribution.get('applicable') is True:
            errors['attribution.applicable'] = (
                'Attribution is not applicable without a current-trace root.'
            )
    _enum(
        errors,
        attribution,
        'attribution.path_source_kind',
        PATH_SOURCES,
        required=False,
    )
    _confidence(
        errors,
        attribution,
        'attribution.confidence',
        strict and human_label == 'confirmed',
    )

    reference = _section(annotation, 'current_trace_reference')
    propagation = _string_list(reference.get('propagation_observation_ids'))
    primary_failure = manifestation.get('primary_failure_observation_id')
    if propagation and primary_failure and primary_failure not in propagation:
        errors['current_trace_reference.propagation_observation_ids'] = (
            'The propagation chain must include the primary failure.'
        )

    observations = list(case_envelope.get('observations') or [])
    observation_ids = {
        str(item.get('observation_id') or item.get('id') or '')
        for item in observations
    }
    _validate_references(annotation, observation_ids, errors)
    _validate_propagation_order(propagation, observations, errors)

    review = _section(annotation, 'review')
    _confidence(errors, review, 'review.overall_confidence', strict)
    if strict and human_label == 'confirmed' and not review.get('reasoning_summary'):
        errors['review.reasoning_summary'] = 'Explain the evidence for this label.'
    if strict and scope == 'current_trace' and not _string_list(
        review.get('evidence_observation_ids')
    ):
        errors['review.evidence_observation_ids'] = (
            'Current-trace attribution requires evidence observations.'
        )

    if errors:
        raise AnnotationValidationError(errors)

    snapshot = _mapping(case_envelope.get('source_snapshot'))
    annotation['source_snapshot'] = deepcopy(snapshot)
    if annotation['annotation_status'] not in {'draft', 'excluded', 'frozen'}:
        should_review = human_label == 'insufficient_evidence' or (
            human_label == 'confirmed'
            and (
                semantic.get('is_agent_failure') is None
                or semantic.get('is_task_failure') is None
                or _has_low_confidence(annotation)
                or bool(snapshot.get('context_truncated'))
            )
        )
        if should_review:
            annotation['annotation_status'] = 'needs_review'
    return annotation


def _validate_references(
    annotation: Mapping[str, Any],
    valid_ids: Set[str],
    errors: MutableMapping[str, str],
) -> None:
    scalar_paths = (
        ('semantic_outcome', 'recovery_observation_id'),
        ('failure_manifestation', 'failure_onset_observation_id'),
        ('failure_manifestation', 'primary_failure_observation_id'),
        ('attribution', 'primary_root_cause_observation_id'),
        ('attribution', 'path_source_observation_id'),
        ('current_trace_reference', 'earliest_local_evidence_observation_id'),
        ('current_trace_reference', 'local_trigger_observation_id'),
    )
    list_paths = (
        ('failure_manifestation', 'failure_observation_ids'),
        ('failure_manifestation', 'downstream_symptom_observation_ids'),
        ('attribution', 'root_cause_observation_ids'),
        ('current_trace_reference', 'propagation_observation_ids'),
        ('current_trace_reference', 'evidence_observation_ids'),
        ('review', 'evidence_observation_ids'),
    )
    for section, key in scalar_paths:
        value = _mapping(annotation.get(section)).get(key)
        if value and str(value) not in valid_ids:
            errors['%s.%s' % (section, key)] = (
                'Observation must belong to this case snapshot.'
            )
    for section, key in list_paths:
        values = _string_list(_mapping(annotation.get(section)).get(key))
        if any(value not in valid_ids for value in values):
            errors['%s.%s' % (section, key)] = (
                'Every observation must belong to this case snapshot.'
            )


def _validate_propagation_order(
    propagation: List[str],
    observations: Iterable[Mapping[str, Any]],
    errors: MutableMapping[str, str],
) -> None:
    if not propagation:
        return
    times = {
        str(item.get('observation_id') or item.get('id') or ''): str(
            item.get('start_time') or ''
        )
        for item in observations
    }
    chain_times = [times.get(item, '') for item in propagation]
    if chain_times != sorted(chain_times):
        errors['current_trace_reference.propagation_observation_ids'] = (
            'Propagation observations must be ordered by Trace time.'
        )


def _equal(
    errors: MutableMapping[str, str],
    annotation: Mapping[str, Any],
    key: str,
    expected: str,
) -> None:
    if str(annotation.get(key) or '') != expected:
        errors[key] = 'Expected %s.' % expected


def _enum(
    errors: MutableMapping[str, str],
    section: Mapping[str, Any],
    path: str,
    choices: Set[str],
    *,
    required: bool,
) -> Optional[str]:
    key = path.rsplit('.', 1)[-1]
    value = section.get(key)
    if value in (None, ''):
        if required:
            errors[path] = 'This field is required.'
        return None
    if value not in choices:
        errors[path] = 'Unsupported value.'
        return None
    return str(value)


def _confidence(
    errors: MutableMapping[str, str],
    section: Mapping[str, Any],
    path: str,
    required: bool,
) -> None:
    _enum(errors, section, path, CONFIDENCE, required=required)


def _section(annotation: MutableMapping[str, Any], key: str) -> Dict[str, Any]:
    value = annotation.get(key)
    if not isinstance(value, Mapping):
        value = {}
        annotation[key] = value
    return dict(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _has_failure_fields(annotation: Mapping[str, Any]) -> bool:
    section = _mapping(annotation.get('failure_manifestation'))
    return any(
        section.get(key)
        for key in (
            'failure_onset_observation_id',
            'primary_failure_observation_id',
            'failure_observation_ids',
            'downstream_symptom_observation_ids',
        )
    )


def _has_root_fields(annotation: Mapping[str, Any]) -> bool:
    section = _mapping(annotation.get('attribution'))
    return any(
        section.get(key)
        for key in (
            'primary_root_cause_observation_id',
            'root_cause_observation_ids',
            'root_cause_domain',
            'root_cause_label',
        )
    )


def _has_low_confidence(annotation: Mapping[str, Any]) -> bool:
    for key in (
        'technical_error_review',
        'semantic_outcome',
        'attribution',
        'current_trace_reference',
    ):
        if _mapping(annotation.get(key)).get('confidence') == 'low':
            return True
    return _mapping(annotation.get('review')).get('overall_confidence') == 'low'
