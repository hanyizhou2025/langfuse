from __future__ import annotations

import json
from pathlib import Path

from agentdebug.integrations.langfuse_attribution.experiment import (
    evaluate_batch_response,
    prepare_batch_experiment,
)
from agentdebug.integrations.langfuse_attribution.long_dataset import (
    build_long_file_not_found_dataset,
    write_long_file_not_found_dataset,
)


def test_prepare_batch_experiment_only_includes_baseline_unknowns(
    tmp_path: Path,
) -> None:
    dataset_dir = tmp_path / 'dataset'
    artifact_dir = tmp_path / 'artifacts'
    dataset = build_long_file_not_found_dataset(noise_observation_count=500)
    write_long_file_not_found_dataset(dataset, dataset_dir)

    summary = prepare_batch_experiment(
        dataset_dir=dataset_dir,
        artifact_dir=artifact_dir,
        max_context_observations=64,
    )

    request = json.loads((artifact_dir / 'batch_request.json').read_text())
    baseline = [
        json.loads(line)
        for line in (artifact_dir / 'baseline_predictions.jsonl')
        .read_text()
        .splitlines()
    ]
    assert summary['review_case_count'] == sum(
        prediction['decision'] == 'unknown' for prediction in baseline
    )
    assert all(len(case['observations']) <= 64 for case in request['cases'])
    assert all(case['failure_observation_id'] for case in request['cases'])
    assert (artifact_dir / 'batch_response_schema.json').is_file()


def test_evaluate_batch_response_reuses_production_validation(tmp_path: Path) -> None:
    dataset_dir = tmp_path / 'dataset'
    artifact_dir = tmp_path / 'artifacts'
    output_dir = tmp_path / 'outputs'
    dataset = build_long_file_not_found_dataset(noise_observation_count=500)
    write_long_file_not_found_dataset(dataset, dataset_dir)
    prepare_batch_experiment(dataset_dir=dataset_dir, artifact_dir=artifact_dir)
    request = json.loads((artifact_dir / 'batch_request.json').read_text())
    annotations = {
        annotation['case_id']: annotation for annotation in dataset.annotations
    }

    results = []
    for case in request['cases']:
        annotation = annotations[case['case_id']]
        results.append(_response_from_annotation(case, annotation))
    results[0]['root_cause_observation_id'] = 'hallucinated-node'
    response_path = tmp_path / 'response.json'
    response_path.write_text(json.dumps({'results': results}))

    summary = evaluate_batch_response(
        dataset_dir=dataset_dir,
        artifact_dir=artifact_dir,
        response_path=response_path,
        output_dir=output_dir,
        model='batch-test-model',
    )

    predictions = [
        json.loads(line)
        for line in (output_dir / 'hybrid_predictions.jsonl').read_text().splitlines()
    ]
    invalid = next(
        prediction
        for prediction in predictions
        if prediction['case_id'] == results[0]['case_id']
    )
    assert summary['model'] == 'batch-test-model'
    assert invalid['attribution_method'] == 'deterministic_fallback'
    assert 'llm_invalid_observation_reference' in invalid['reason_codes']
    assert (output_dir / 'comparison.json').is_file()


def _response_from_annotation(case: dict, annotation: dict) -> dict:
    is_failure = annotation['semantic_outcome']['is_agent_failure']
    failure_id = case['failure_observation_id']
    if is_failure is True:
        attribution = annotation['attribution']
        return {
            'case_id': case['case_id'],
            'decision': 'attributed',
            'semantics': 'unexpected_failure',
            'is_agent_failure': True,
            'failure_observation_id': failure_id,
            'root_cause_observation_id': attribution[
                'primary_root_cause_observation_id'
            ],
            'root_cause_label': attribution['root_cause_label'],
            'root_cause_domain': attribution['root_cause_domain'],
            'evidence_observation_ids': annotation['evidence_observation_ids'],
            'confidence': 0.9,
            'reason_codes': ['batch_oracle'],
        }
    if is_failure is False:
        return {
            'case_id': case['case_id'],
            'decision': 'not_agent_failure',
            'semantics': annotation['semantic_outcome']['semantic_label'],
            'is_agent_failure': False,
            'failure_observation_id': failure_id,
            'root_cause_observation_id': None,
            'root_cause_label': None,
            'root_cause_domain': None,
            'evidence_observation_ids': annotation['evidence_observation_ids'],
            'confidence': 0.9,
            'reason_codes': ['batch_oracle'],
        }
    return {
        'case_id': case['case_id'],
        'decision': 'unknown',
        'semantics': 'unknown',
        'is_agent_failure': None,
        'failure_observation_id': failure_id,
        'root_cause_observation_id': None,
        'root_cause_label': None,
        'root_cause_domain': None,
        'evidence_observation_ids': annotation['evidence_observation_ids'],
        'confidence': 0.2,
        'reason_codes': ['insufficient_evidence'],
    }
