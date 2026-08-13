from __future__ import annotations

import json
from pathlib import Path

from agentdebug.integrations.langfuse_attribution.cli import run_dataset


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        ''.join(json.dumps(row) + '\n' for row in rows),
        encoding='utf-8',
    )


def test_run_dataset_writes_predictions_and_report(tmp_path: Path) -> None:
    dataset_dir = tmp_path / 'dataset'
    output_dir = tmp_path / 'outputs'
    dataset_dir.mkdir()
    path = '<PATH_1>/missing.md'

    _write_jsonl(
        dataset_dir / 'cases.jsonl',
        [
            {
                'case_id': 'case-model',
                'trace_id': 'trace-cli',
                'tool_attempt': {
                    'tool_result_observation_id': 'failed-read',
                },
            }
        ],
    )
    _write_jsonl(
        dataset_dir / 'observations.jsonl',
        [
            {
                'observation_id': 'generation',
                'trace_id': 'trace-cli',
                'parent_observation_id': None,
                'type': 'GENERATION',
                'name': 'planner',
                'start_time': '2026-01-01T00:00:00Z',
                'input_redacted': None,
                'output_redacted': {'tool': 'read_file', 'path': path},
                'metadata_redacted': {},
                'level': 'DEFAULT',
                'status_message_redacted': None,
            },
            {
                'observation_id': 'failed-read',
                'trace_id': 'trace-cli',
                'parent_observation_id': None,
                'type': 'TOOL',
                'name': 'read_file',
                'start_time': '2026-01-01T00:00:01Z',
                'input_redacted': {'path': path},
                'output_redacted': {'error': 'ENOENT'},
                'metadata_redacted': {},
                'level': 'ERROR',
                'status_message_redacted': 'File not found',
            },
        ],
    )
    _write_jsonl(
        dataset_dir / 'annotations.jsonl',
        [
            {
                'case_id': 'case-model',
                'technical_error_review': {'human_label': 'confirmed'},
                'semantic_outcome': {'is_agent_failure': True},
                'attribution': {
                    'applicable': True,
                    'root_cause_label': 'model_path_hallucination',
                    'primary_root_cause_observation_id': 'generation',
                },
            }
        ],
    )

    summary = run_dataset(dataset_dir=dataset_dir, output_dir=output_dir)

    predictions = [
        json.loads(line)
        for line in (output_dir / 'predictions.jsonl')
        .read_text(encoding='utf-8')
        .splitlines()
    ]
    report = json.loads((output_dir / 'report.json').read_text(encoding='utf-8'))

    assert summary['prediction_count'] == 1
    assert summary['evaluated'] is True
    assert predictions[0]['decision'] == 'attributed'
    assert report['attribution']['label_correct'] == 1


def test_run_dataset_without_annotations_only_writes_predictions(
    tmp_path: Path,
) -> None:
    dataset_dir = tmp_path / 'dataset'
    output_dir = tmp_path / 'outputs'
    dataset_dir.mkdir()
    _write_jsonl(dataset_dir / 'cases.jsonl', [])
    _write_jsonl(dataset_dir / 'observations.jsonl', [])

    summary = run_dataset(dataset_dir=dataset_dir, output_dir=output_dir)

    assert summary == {'prediction_count': 0, 'evaluated': False}
    assert (output_dir / 'predictions.jsonl').read_text(encoding='utf-8') == ''
    assert not (output_dir / 'report.json').exists()
