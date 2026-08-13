"""Offline JSONL runner for historical file-not-found datasets."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .evaluation import (
    build_file_not_found_review_trajectories,
    evaluate_file_not_found_predictions,
    predict_file_not_found_cases,
)


def run_dataset(
    *,
    dataset_dir: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    """Run predictions and optional evaluation for one frozen dataset."""

    cases = _read_jsonl(dataset_dir / 'cases.jsonl')
    observations = _read_jsonl(dataset_dir / 'observations.jsonl')
    traces = _read_jsonl(dataset_dir / 'traces.jsonl')
    predictions = predict_file_not_found_cases(
        cases,
        observations,
        traces=traces,
    )
    trajectories = build_file_not_found_review_trajectories(
        predictions,
        observations,
        traces=traces,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl_atomic(output_dir / 'predictions.jsonl', predictions)
    _write_jsonl_atomic(output_dir / 'trajectories.jsonl', trajectories)

    annotations_path = dataset_dir / 'annotations.jsonl'
    evaluated = annotations_path.exists()
    if evaluated:
        annotations = _read_jsonl(annotations_path)
        report = evaluate_file_not_found_predictions(predictions, annotations)
        _write_json_atomic(output_dir / 'report.json', report)

    return {
        'prediction_count': len(predictions),
        'trajectory_count': len(trajectories),
        'evaluated': evaluated,
    }


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
    args = parser.parse_args(argv)
    summary = run_dataset(
        dataset_dir=args.dataset_dir,
        output_dir=args.output_dir,
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
