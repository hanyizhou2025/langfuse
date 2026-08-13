from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentdebug.integrations.langfuse_attribution import (
    evaluate_file_not_found_predictions,
    predict_file_not_found_cases,
)
from agentdebug.integrations.langfuse_attribution.long_dataset import (
    build_long_file_not_found_dataset,
    write_long_file_not_found_dataset,
)
from agentdebug.runtime.llm import CompletionResult


class OracleLLM:
    """Return the fixture oracle selected by the failure observation ID."""

    model = 'oracle-test-model'

    def __init__(self, responses: Dict[str, Dict[str, Any]]) -> None:
        self.responses = responses
        self.call_count = 0

    def complete(
        self,
        messages: List[Dict[str, Any]],
        *,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> CompletionResult:
        self.call_count += 1
        prompt = json.loads(str(messages[1]['content']))
        failure_id = str(prompt['failure_observation_id'])
        return CompletionResult(
            text=json.dumps(self.responses[failure_id]),
            raw={'usage': {'prompt_tokens': 500, 'completion_tokens': 80}},
        )


def test_long_dataset_has_complex_scenarios_and_redacted_payloads() -> None:
    dataset = build_long_file_not_found_dataset(noise_observation_count=600)

    assert len(dataset.traces) == 12
    assert len(dataset.cases) == 12
    assert len(dataset.annotations) == 12
    assert len(dataset.observations) >= 7200
    assert dataset.manifest['minimum_observations_per_trace'] >= 500
    assert dataset.manifest['scenario_count'] == 12
    assert {
        'model_hallucination',
        'user_invalid_path',
        'skill_stale_path',
        'runtime_mount_unavailable',
        'expected_validation_probe',
        'recovered_after_create',
        'expected_cache_miss_fallback',
        'rule_match_expected_absence',
        'conflicting_path_sources',
        'runtime_file_deleted',
        'upstream_artifact_stale',
        'insufficient_evidence',
    } == set(dataset.manifest['scenarios'])
    assert all(
        'input_redacted' in observation
        and 'output_redacted' in observation
        and 'metadata_redacted' in observation
        and 'status_message_redacted' in observation
        and 'input' not in observation
        and 'output' not in observation
        for observation in dataset.observations
    )
    annotations = {
        annotation['case_id']: annotation for annotation in dataset.annotations
    }
    for case in dataset.cases:
        scenario = str(annotations[case['case_id']]['scenario'])
        trace_id = str(case['trace_id'])
        assert scenario not in str(case['case_id'])
        assert scenario not in trace_id
        assert all(
            scenario not in str(observation['observation_id'])
            for observation in dataset.observations
            if observation['trace_id'] == trace_id
        )


def test_long_dataset_can_be_written_as_standard_jsonl(tmp_path: Path) -> None:
    dataset = build_long_file_not_found_dataset(noise_observation_count=500)

    write_long_file_not_found_dataset(dataset, tmp_path)

    assert len((tmp_path / 'traces.jsonl').read_text().splitlines()) == 12
    assert len((tmp_path / 'cases.jsonl').read_text().splitlines()) == 12
    assert len((tmp_path / 'annotations.jsonl').read_text().splitlines()) == 12
    assert json.loads((tmp_path / 'manifest.json').read_text())['synthetic'] is True


def test_hybrid_llm_only_reviews_deterministic_unknown_cases() -> None:
    dataset = build_long_file_not_found_dataset(noise_observation_count=500)
    oracle_by_failure = {
        str(annotation['failure_observation_id']): _oracle_response(annotation)
        for annotation in dataset.annotations
    }
    llm = OracleLLM(oracle_by_failure)

    baseline_predictions = predict_file_not_found_cases(
        dataset.cases,
        dataset.observations,
        traces=dataset.traces,
    )
    hybrid_predictions = predict_file_not_found_cases(
        dataset.cases,
        dataset.observations,
        traces=dataset.traces,
        llm=llm,
    )
    baseline_report = evaluate_file_not_found_predictions(
        baseline_predictions,
        dataset.annotations,
    )
    hybrid_report = evaluate_file_not_found_predictions(
        hybrid_predictions,
        dataset.annotations,
    )

    baseline_unknown_count = sum(
        prediction['decision'] == 'unknown' for prediction in baseline_predictions
    )
    assert llm.call_count == baseline_unknown_count
    assert (
        hybrid_report['attribution']['label_correct']
        >= baseline_report['attribution']['label_correct']
    )
    assert (
        hybrid_report['semantic_negative']['correct']
        >= baseline_report['semantic_negative']['correct']
    )
    assert all(
        prediction['total_observation_count'] >= 500
        for prediction in hybrid_predictions
    )
    assert all(
        prediction['reviewed_observation_count'] <= 64
        for prediction in hybrid_predictions
    )


def _oracle_response(annotation: Dict[str, Any]) -> Dict[str, Any]:
    is_agent_failure = annotation['semantic_outcome']['is_agent_failure']
    attribution = annotation['attribution']
    failure_id = str(annotation['failure_observation_id'])
    if is_agent_failure is False:
        return {
            'decision': 'not_agent_failure',
            'semantics': annotation['semantic_outcome']['semantic_label'],
            'is_agent_failure': False,
            'failure_observation_id': failure_id,
            'root_cause_observation_id': None,
            'root_cause_label': None,
            'root_cause_domain': None,
            'evidence_observation_ids': annotation['evidence_observation_ids'],
            'confidence': 0.9,
            'reason_codes': ['oracle_fixture'],
        }
    if is_agent_failure is None:
        return {
            'decision': 'unknown',
            'semantics': 'unknown',
            'is_agent_failure': None,
            'failure_observation_id': failure_id,
            'root_cause_observation_id': None,
            'root_cause_label': None,
            'root_cause_domain': None,
            'evidence_observation_ids': annotation['evidence_observation_ids'],
            'confidence': 0.3,
            'reason_codes': ['insufficient_evidence'],
        }
    return {
        'decision': 'attributed',
        'semantics': 'unexpected_failure',
        'is_agent_failure': True,
        'failure_observation_id': failure_id,
        'root_cause_observation_id': attribution['primary_root_cause_observation_id'],
        'root_cause_label': attribution['root_cause_label'],
        'root_cause_domain': attribution['root_cause_domain'],
        'evidence_observation_ids': annotation['evidence_observation_ids'],
        'confidence': 0.9,
        'reason_codes': ['oracle_fixture'],
    }
