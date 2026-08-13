"""Conservative LLM enhancement for historical File Not Found attribution."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agentdebug.runtime.llm import LLMClient, extract_json_block

from .pipeline import (
    FileNotFoundAttributionResult,
    FileNotFoundDecision,
    attribute_historical_file_not_found,
)


LOG = logging.getLogger('agentdebug.langfuse_file_not_found_llm')

_SYSTEM_PROMPT = """You review one redacted Langfuse trace for File Not Found attribution.

Decide three layers separately:
1. Did the observed File Not Found constitute an Agent/task failure, or was it an expected probe, cache miss, fallback, or recovered control-flow signal?
2. If it was a failure, which EXISTING observation first introduced the decisive bad path or runtime/upstream condition?
3. Return only observation IDs that appear in the supplied context. Never invent an ID.

Allowed root labels and domains:
- user_path_invalid -> user
- model_path_hallucination -> model
- upstream_path_invalid -> upstream
- runtime_path_unavailable -> runtime

Use decision "unknown" when the supplied evidence cannot support a conclusion. Respond ONLY with one JSON object containing:
decision, semantics, is_agent_failure, failure_observation_id,
root_cause_observation_id, root_cause_label, root_cause_domain,
evidence_observation_ids, confidence, reason_codes.
"""

_LABEL_DOMAINS = {
    'user_path_invalid': 'user',
    'model_path_hallucination': 'model',
    'upstream_path_invalid': 'upstream',
    'runtime_path_unavailable': 'runtime',
}
_SEMANTICS = {
    'validation_probe',
    'control_flow_signal',
    'recovered_exploration',
    'expected_absence',
    'rule_false_positive',
    'unexpected_failure',
    'unknown',
}
_PATH_KEYS = ('path', 'file', 'filename', 'file_path')
_ERROR_TOKENS = ('enoent', 'file not found', 'no such file', 'no such directory')
_CONTEXT_NAME_TOKENS = (
    'user',
    'human',
    'planner',
    'skill',
    'memory',
    'runtime',
    'mount',
    'environment',
    'workspace',
    'cwd',
    'exists',
    'check',
    'probe',
    'create',
    'write',
    'save',
    'final',
    'answer',
)
_REASON_CODE = re.compile(r'^[a-z0-9_]{1,64}$')


class FileNotFoundLLMAttributor:
    """Use an LLM for ambiguous cases while preserving deterministic results.

    Only fields carrying the documented ``*_redacted`` names are sent to the
    model. By default, high-confidence deterministic decisions bypass the LLM.
    All model-returned IDs, labels, domains, and decision invariants are
    validated before a result may replace the baseline.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        review_deterministic: bool = False,
        max_context_observations: int = 64,
        max_value_chars: int = 500,
        max_tokens: int = 1200,
        timeout: float = 90.0,
    ) -> None:
        if max_context_observations < 4:
            raise ValueError('max_context_observations must be at least 4')
        self.llm = llm
        self.review_deterministic = review_deterministic
        self.max_context_observations = max_context_observations
        self.max_value_chars = max_value_chars
        self.max_tokens = max_tokens
        self.timeout = timeout

    def attribute(
        self,
        observations: Iterable[Mapping[str, Any]],
        *,
        failure_observation_id: str,
        case_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> FileNotFoundAttributionResult:
        observation_list = [dict(item) for item in observations]
        normalized = [_normalize_observation(item) for item in observation_list]
        baseline = attribute_historical_file_not_found(
            normalized,
            failure_observation_id=failure_observation_id,
            case_id=case_id,
            trace_id=trace_id,
        )
        total_count = len(observation_list)
        baseline = replace(
            baseline,
            total_observation_count=total_count,
            reviewed_observation_count=0,
        )

        if (
            not self.review_deterministic
            and baseline.decision != FileNotFoundDecision.UNKNOWN
        ):
            return baseline
        if baseline.decision == FileNotFoundDecision.INVALID_CASE:
            return baseline
        if not _has_redacted_contract(observation_list):
            return replace(
                baseline,
                reason_codes=_unique(
                    baseline.reason_codes + ['llm_skipped_unredacted_payload']
                ),
            )

        selected = _select_context(
            observation_list,
            failure_observation_id=failure_observation_id,
            limit=self.max_context_observations,
        )
        reviewed_count = len(selected)
        prompt = _render_prompt(
            selected,
            failure_observation_id=failure_observation_id,
            baseline=baseline,
            total_observation_count=total_count,
            max_value_chars=self.max_value_chars,
        )
        try:
            completion = self.llm.complete(
                messages=[
                    {'role': 'system', 'content': _SYSTEM_PROMPT},
                    {'role': 'user', 'content': prompt},
                ],
                response_format={'type': 'json_object'},
                temperature=0.0,
                max_tokens=self.max_tokens,
                timeout=self.timeout,
            )
        except Exception as exc:  # pragma: no cover - network/provider defensive path
            LOG.warning('File Not Found LLM review failed: %s', exc)
            return _fallback(
                baseline,
                model=self.llm.model,
                reviewed_count=reviewed_count,
                reason_code='llm_request_failed',
            )

        payload = extract_json_block(completion.text)
        if payload is None:
            return _fallback(
                baseline,
                model=self.llm.model,
                reviewed_count=reviewed_count,
                reason_code='llm_invalid_json',
                raw=completion.raw,
            )
        validated, error_code = _validate_payload(
            payload,
            known_observation_ids={_observation_id(item) for item in observation_list},
            failure_observation_id=failure_observation_id,
        )
        if validated is None:
            return _fallback(
                baseline,
                model=self.llm.model,
                reviewed_count=reviewed_count,
                reason_code=error_code,
                raw=completion.raw,
            )
        if validated['decision'] == 'attributed' and _has_conflicting_path_sources(
            observation_list,
            failure_observation_id=failure_observation_id,
        ):
            return _fallback(
                baseline,
                model=self.llm.model,
                reviewed_count=reviewed_count,
                reason_code='llm_conflicting_path_sources',
                raw=completion.raw,
            )

        usage = _usage(completion.raw)
        return FileNotFoundAttributionResult(
            case_id=case_id,
            trace_id=baseline.trace_id,
            decision=FileNotFoundDecision(validated['decision']),
            semantics=validated['semantics'],
            is_agent_failure=validated['is_agent_failure'],
            failure_observation_id=failure_observation_id,
            root_cause_observation_id=validated['root_cause_observation_id'],
            root_cause_label=validated['root_cause_label'],
            root_cause_domain=validated['root_cause_domain'],
            evidence_observation_ids=validated['evidence_observation_ids'],
            confidence=validated['confidence'],
            reason_codes=_unique(validated['reason_codes'] + ['llm_enhanced']),
            attribution_method='llm_enhanced',
            model=self.llm.model,
            total_observation_count=total_count,
            reviewed_observation_count=reviewed_count,
            llm_prompt_tokens=usage[0],
            llm_completion_tokens=usage[1],
        )


def _fallback(
    baseline: FileNotFoundAttributionResult,
    *,
    model: str,
    reviewed_count: int,
    reason_code: str,
    raw: Optional[Mapping[str, Any]] = None,
) -> FileNotFoundAttributionResult:
    usage = _usage(raw or {})
    return replace(
        baseline,
        attribution_method='deterministic_fallback',
        model=model,
        reviewed_observation_count=reviewed_count,
        llm_prompt_tokens=usage[0],
        llm_completion_tokens=usage[1],
        reason_codes=_unique(baseline.reason_codes + [reason_code]),
    )


def _select_context(
    observations: Sequence[Mapping[str, Any]],
    *,
    failure_observation_id: str,
    limit: int,
) -> List[Mapping[str, Any]]:
    ordered = sorted(observations, key=_sort_key)
    by_id = {_observation_id(item): item for item in ordered}
    failure_index = next(
        (
            index
            for index, item in enumerate(ordered)
            if _observation_id(item) == failure_observation_id
        ),
        None,
    )
    if failure_index is None:
        return []
    failure = ordered[failure_index]
    failed_path = _extract_path(failure.get('input_redacted'))

    priorities: Dict[str, int] = {failure_observation_id: 0}

    def add(item: Mapping[str, Any], priority: int) -> None:
        observation_id = _observation_id(item)
        if observation_id:
            priorities[observation_id] = min(
                priority,
                priorities.get(observation_id, priority),
            )

    for offset in range(-4, 9):
        index = failure_index + offset
        if 0 <= index < len(ordered):
            add(ordered[index], 20 + abs(offset))

    current = failure
    seen_parents = set()
    while True:
        parent_id = _optional_str(current.get('parent_observation_id'))
        if not parent_id or parent_id in seen_parents or parent_id not in by_id:
            break
        seen_parents.add(parent_id)
        current = by_id[parent_id]
        add(current, 5)

    for item in ordered:
        text = _redacted_search_text(item)
        name = str(item.get('name') or '').lower()
        if failed_path and failed_path in text:
            add(item, 4)
        if any(token in text.lower() for token in _ERROR_TOKENS):
            add(item, 6)
        if any(token in name for token in _CONTEXT_NAME_TOKENS):
            add(item, 10)

    ranked_ids = sorted(
        priorities,
        key=lambda observation_id: (
            priorities[observation_id],
            _sort_key(by_id[observation_id]),
        ),
    )[:limit]
    chosen = set(ranked_ids)
    return [item for item in ordered if _observation_id(item) in chosen]


def _render_prompt(
    observations: Sequence[Mapping[str, Any]],
    *,
    failure_observation_id: str,
    baseline: FileNotFoundAttributionResult,
    total_observation_count: int,
    max_value_chars: int,
) -> str:
    rows = []
    for item in observations:
        rows.append(
            {
                'id': _observation_id(item),
                'parent_id': item.get('parent_observation_id'),
                'type': item.get('type'),
                'name': item.get('name'),
                'start_time': str(item.get('start_time') or ''),
                'level': item.get('level'),
                'status': _truncate(
                    item.get('status_message_redacted'), max_value_chars
                ),
                'input': _truncate(item.get('input_redacted'), max_value_chars),
                'output': _truncate(item.get('output_redacted'), max_value_chars),
            }
        )
    return json.dumps(
        {
            'task': 'attribute_file_not_found',
            'failure_observation_id': failure_observation_id,
            'baseline': {
                'decision': baseline.decision.value,
                'semantics': baseline.semantics,
                'reason_codes': baseline.reason_codes,
            },
            'context_selection': {
                'total_observation_count': total_observation_count,
                'reviewed_observation_count': len(observations),
                'note': 'The trace was causally retrieved and may omit unrelated nodes.',
            },
            'observations': rows,
        },
        ensure_ascii=False,
        separators=(',', ':'),
    )


def _validate_payload(
    payload: Mapping[str, Any],
    *,
    known_observation_ids: set[str],
    failure_observation_id: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    decision = _optional_str(payload.get('decision'))
    if decision not in {'attributed', 'not_agent_failure', 'unknown'}:
        return None, 'llm_invalid_decision'
    if _optional_str(payload.get('failure_observation_id')) != failure_observation_id:
        return None, 'llm_changed_failure_observation'

    evidence_raw = payload.get('evidence_observation_ids')
    if not isinstance(evidence_raw, list) or not evidence_raw:
        return None, 'llm_invalid_evidence'
    evidence = [_optional_str(item) for item in evidence_raw]
    if any(item is None or item not in known_observation_ids for item in evidence):
        return None, 'llm_invalid_observation_reference'
    evidence_ids = _unique([item for item in evidence if item])
    if failure_observation_id not in evidence_ids:
        return None, 'llm_failure_missing_from_evidence'

    semantics = _optional_str(payload.get('semantics'))
    if semantics not in _SEMANTICS:
        return None, 'llm_invalid_semantics'
    confidence = payload.get('confidence')
    if isinstance(confidence, bool):
        return None, 'llm_invalid_confidence'
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        return None, 'llm_invalid_confidence'
    if not 0.0 <= confidence_value <= 1.0:
        return None, 'llm_invalid_confidence'

    reason_codes_raw = payload.get('reason_codes')
    if not isinstance(reason_codes_raw, list):
        return None, 'llm_invalid_reason_codes'
    reason_codes = [str(item) for item in reason_codes_raw[:8]]
    if any(not _REASON_CODE.fullmatch(item) for item in reason_codes):
        return None, 'llm_invalid_reason_codes'

    root_id = _optional_str(payload.get('root_cause_observation_id'))
    label = _optional_str(payload.get('root_cause_label'))
    domain = _optional_str(payload.get('root_cause_domain'))
    is_agent_failure = payload.get('is_agent_failure')

    if decision == 'attributed':
        if is_agent_failure is not True:
            return None, 'llm_inconsistent_failure_flag'
        if root_id not in known_observation_ids:
            return None, 'llm_invalid_observation_reference'
        if label not in _LABEL_DOMAINS or domain != _LABEL_DOMAINS[label]:
            return None, 'llm_invalid_root_label_domain'
        if root_id not in evidence_ids:
            return None, 'llm_root_missing_from_evidence'
    elif decision == 'not_agent_failure':
        if is_agent_failure is not False:
            return None, 'llm_inconsistent_failure_flag'
        if any(item is not None for item in (root_id, label, domain)):
            return None, 'llm_unexpected_root_for_negative'
    else:
        if is_agent_failure is not None:
            return None, 'llm_inconsistent_failure_flag'
        if any(item is not None for item in (root_id, label, domain)):
            return None, 'llm_unexpected_root_for_unknown'

    return (
        {
            'decision': decision,
            'semantics': semantics,
            'is_agent_failure': is_agent_failure,
            'root_cause_observation_id': root_id,
            'root_cause_label': label,
            'root_cause_domain': domain,
            'evidence_observation_ids': evidence_ids,
            'confidence': confidence_value,
            'reason_codes': reason_codes,
        },
        '',
    )


def _normalize_observation(observation: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = dict(observation)
    aliases = {
        'observation_id': 'id',
        'input_redacted': 'input',
        'output_redacted': 'output',
        'metadata_redacted': 'metadata',
        'status_message_redacted': 'status_message',
    }
    for source, target in aliases.items():
        if target not in normalized and source in normalized:
            normalized[target] = normalized[source]
    return normalized


def _has_redacted_contract(observations: Sequence[Mapping[str, Any]]) -> bool:
    return bool(observations) and all(
        'input_redacted' in item
        and 'output_redacted' in item
        and 'status_message_redacted' in item
        for item in observations
    )


def _observation_id(observation: Mapping[str, Any]) -> str:
    return str(observation.get('observation_id') or observation.get('id') or '')


def _extract_path(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        for key in _PATH_KEYS:
            path = _optional_str(value.get(key))
            if path:
                return path
        for child in value.values():
            path = _extract_path(child)
            if path:
                return path
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            path = _extract_path(child)
            if path:
                return path
    return None


def _redacted_search_text(observation: Mapping[str, Any]) -> str:
    values = (
        observation.get('input_redacted'),
        observation.get('output_redacted'),
        observation.get('status_message_redacted'),
    )
    return ' '.join(
        json.dumps(value, ensure_ascii=False, default=str) for value in values
    )


def _has_conflicting_path_sources(
    observations: Sequence[Mapping[str, Any]],
    *,
    failure_observation_id: str,
) -> bool:
    failure = next(
        (
            item
            for item in observations
            if _observation_id(item) == failure_observation_id
        ),
        None,
    )
    if failure is None:
        return False
    failed_path = _extract_path(failure.get('input_redacted'))
    if not failed_path:
        return False
    domains = {
        domain
        for item in observations
        if _observation_id(item) != failure_observation_id
        and failed_path in _redacted_search_text(item)
        for domain in [_source_domain(item)]
        if domain is not None
    }
    return len(domains) > 1


def _source_domain(observation: Mapping[str, Any]) -> Optional[str]:
    observation_type = str(observation.get('type') or '').upper()
    name = str(observation.get('name') or '').lower()
    if observation_type == 'TOOL' and any(
        token in name for token in ('create', 'write', 'touch', 'save')
    ):
        return None
    if observation_type == 'GENERATION':
        return 'model'
    if any(token in name for token in ('user', 'human')):
        return 'user'
    if any(token in name for token in ('skill', 'memory', 'artifact', 'registry')):
        return 'upstream'
    if any(token in name for token in ('runtime', 'mount', 'environment', 'cwd')):
        return 'runtime'
    return None


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + '…'
    if isinstance(value, Mapping):
        return {
            str(key): _truncate(item, limit) for key, item in list(value.items())[:20]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_truncate(item, limit) for item in list(value)[:20]]
    return value


def _usage(raw: Mapping[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    usage = raw.get('usage')
    if not isinstance(usage, Mapping):
        return None, None
    return _optional_int(usage.get('prompt_tokens')), _optional_int(
        usage.get('completion_tokens')
    )


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _sort_key(observation: Mapping[str, Any]) -> Tuple[datetime, str]:
    value = observation.get('start_time')
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            timestamp = datetime.min.replace(tzinfo=timezone.utc)
    else:
        timestamp = datetime.min.replace(tzinfo=timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp, _observation_id(observation)
