"""Offline JSONL runner for historical file-not-found datasets."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from agentdebug.runtime.llm import LLMClient, OpenAICompatClient

from .evaluation import (
    build_file_not_found_review_trajectories,
    evaluate_file_not_found_predictions,
    predict_file_not_found_cases,
)


def run_dataset(
    *,
    dataset_dir: Path,
    output_dir: Path,
    llm: Optional[LLMClient] = None,
    review_deterministic: bool = False,
    max_context_observations: int = 64,
) -> Dict[str, Any]:
    """Run predictions and optional evaluation for one frozen dataset."""

    cases = _read_jsonl(dataset_dir / 'cases.jsonl')
    observations = _read_jsonl(dataset_dir / 'observations.jsonl')
    traces = _read_jsonl(dataset_dir / 'traces.jsonl')
    annotations_path = dataset_dir / 'annotations.jsonl'
    annotations = _read_jsonl(annotations_path) if annotations_path.exists() else []
    return run_records(
        cases=cases,
        observations=observations,
        traces=traces,
        annotations=annotations,
        output_dir=output_dir,
        llm=llm,
        review_deterministic=review_deterministic,
        max_context_observations=max_context_observations,
    )


def run_records(
    *,
    cases: Iterable[Mapping[str, Any]],
    observations: Iterable[Mapping[str, Any]],
    traces: Iterable[Mapping[str, Any]],
    output_dir: Path,
    annotations: Iterable[Mapping[str, Any]] = (),
    llm: Optional[LLMClient] = None,
    review_deterministic: bool = False,
    max_context_observations: int = 64,
) -> Dict[str, Any]:
    """Run the same pipeline for database-backed or frozen input records."""

    case_rows = list(cases)
    observation_rows = list(observations)
    trace_rows = list(traces)
    annotation_rows = list(annotations)
    predictions = predict_file_not_found_cases(
        case_rows,
        observation_rows,
        traces=trace_rows,
        llm=llm,
        review_deterministic=review_deterministic,
        max_context_observations=max_context_observations,
    )
    trajectories = build_file_not_found_review_trajectories(
        predictions,
        observation_rows,
        traces=trace_rows,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl_atomic(output_dir / 'predictions.jsonl', predictions)
    _write_jsonl_atomic(output_dir / 'trajectories.jsonl', trajectories)

    evaluated = bool(annotation_rows)
    if evaluated:
        report = evaluate_file_not_found_predictions(predictions, annotation_rows)
        _write_json_atomic(output_dir / 'report.json', report)
    else:
        (output_dir / 'report.json').unlink(missing_ok=True)

    summary = {
        'prediction_count': len(predictions),
        'trajectory_count': len(trajectories),
        'evaluated': evaluated,
    }
    if llm is not None:
        summary.update(
            {
                'llm_enabled': True,
                'llm_review_count': sum(
                    str(prediction.get('attribution_method') or '').startswith('llm_')
                    or prediction.get('attribution_method') == 'deterministic_fallback'
                    for prediction in predictions
                ),
            }
        )
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point used through ``python -m``."""

    parser = argparse.ArgumentParser(
        description='Run deterministic File Not Found attribution on JSONL data.',
    )
    parser.add_argument(
        'dataset_dir',
        type=Path,
        help='Directory containing traces, cases, and observations JSONL files.',
    )
    parser.add_argument(
        'output_dir',
        type=Path,
        help='Directory for predictions.jsonl and optional report.json.',
    )
    parser.add_argument(
        '--llm',
        action='store_true',
        help=(
            'Enhance ambiguous cases through AGENTDEBUG_LLM_BASE_URL, '
            'AGENTDEBUG_LLM_API_KEY, and AGENTDEBUG_LLM_MODEL.'
        ),
    )
    parser.add_argument(
        '--review-deterministic',
        action='store_true',
        help='Also ask the LLM to review deterministic decisions.',
    )
    parser.add_argument(
        '--max-context-observations',
        type=int,
        default=64,
        help='Maximum causally retrieved observations sent per LLM call.',
    )
    args = parser.parse_args(argv)
    llm = OpenAICompatClient.from_env() if args.llm else None
    summary = run_dataset(
        dataset_dir=args.dataset_dir,
        output_dir=args.output_dir,
        llm=llm,
        review_deterministic=args.review_deterministic,
        max_context_observations=args.max_context_observations,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def _read_jsonl(path: Path) -> List[Mapping[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError('Required dataset file does not exist: %s' % path)

    rows: List[Mapping[str, Any]] = []
    with path.open('r', encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    'Invalid JSON in %s at line %d: %s' % (path, line_number, error.msg)
                ) from error
            if not isinstance(value, dict):
                raise ValueError(
                    'Expected a JSON object in %s at line %d.' % (path, line_number)
                )
            rows.append(value)
    return rows


def _write_jsonl_atomic(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
) -> None:
    content = ''.join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in rows
    )
    _write_text_atomic(path, content)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    _write_text_atomic(path, content)


def _write_text_atomic(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix='.%s.' % path.name,
        suffix='.tmp',
        dir=str(path.parent),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    raise SystemExit(main())
