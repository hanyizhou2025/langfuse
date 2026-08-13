from __future__ import annotations

import json
from pathlib import Path

from agentdebug.inspect.ui.upload import import_upload_text
from agentdebug.inspect.ui.views import render_tool_attribution_page
from agentdebug.integrations.langfuse_attribution.cli import run_dataset
from agentdebug.runtime import SQLiteTraceStore


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
        dataset_dir / 'traces.jsonl',
        [
            {
                'trace_id': 'trace-cli',
                'timestamp': '2026-01-01T00:00:00Z',
                'input_redacted': {'task': 'Summarize the report.'},
                'output_redacted': {'message': 'Unable to read file.'},
            }
        ],
    )
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
    trajectories = [
        json.loads(line)
        for line in (output_dir / 'trajectories.jsonl')
        .read_text(encoding='utf-8')
        .splitlines()
    ]

    assert summary['prediction_count'] == 1
    assert summary['evaluated'] is True
    assert summary['trajectory_count'] == 1
    assert predictions[0]['decision'] == 'attributed'
    assert report['attribution']['label_correct'] == 1
    assert trajectories[0]['trace_id'] == 'trace-cli'
    assert (
        trajectories[0]['metadata']['langfuse_file_not_found_attributions']
        == predictions
    )

    review_store = SQLiteTraceStore(str(tmp_path / 'review.sqlite'))
    imported = import_upload_text(
        review_store,
        (output_dir / 'trajectories.jsonl').read_text(encoding='utf-8'),
        allow_llm=False,
    )
    review_trace = review_store.load_trajectory('trace-cli')
    assert imported['imported'] == ['trace-cli']
    assert review_trace is not None
    assert 'Root cause attributed' in render_tool_attribution_page(review_trace)


def test_run_dataset_without_annotations_only_writes_predictions(
    tmp_path: Path,
) -> None:
    dataset_dir = tmp_path / 'dataset'
    output_dir = tmp_path / 'outputs'
    dataset_dir.mkdir()
    _write_jsonl(dataset_dir / 'cases.jsonl', [])
    _write_jsonl(dataset_dir / 'observations.jsonl', [])
    _write_jsonl(dataset_dir / 'traces.jsonl', [])

    summary = run_dataset(dataset_dir=dataset_dir, output_dir=output_dir)

    assert summary == {
        'prediction_count': 0,
        'trajectory_count': 0,
        'evaluated': False,
    }
    assert (output_dir / 'predictions.jsonl').read_text(encoding='utf-8') == ''
    assert (output_dir / 'trajectories.jsonl').read_text(encoding='utf-8') == ''
    assert not (output_dir / 'report.json').exists()
