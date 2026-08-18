#!/usr/bin/env python3
"""Select a bounded production Trace batch for annotation and DebugX.

The script is intentionally a separate work-environment entrypoint. It does
not change the default candidate loading behavior of either the annotation
tool or ``agentdebug langfuse-fnf``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
for import_path in (ROOT, SRC):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from agentdebug.integrations.langfuse_attribution.cli import run_records  # noqa: E402
from agentdebug.integrations.langfuse_attribution.production import (  # noqa: E402
    ClickHouseHTTPClient,
    ClickHouseProductionSource,
    ProductionSnapshot,
    ProductionSourceError,
    ProductionSourceSettings,
    QueryClient,
    write_production_snapshot,
)
from dataset.annotation_tool.app import AnnotationApplication, serve  # noqa: E402
from dataset.annotation_tool.repository import AnnotationRepository  # noqa: E402
from dataset.annotation_tool.source import _truncate_observations  # noqa: E402


LOGGER = logging.getLogger('agentdebug.dataset.work_environment_trace_batch')
FILE_NOT_FOUND_ERROR_TYPE = 'dependency_or_env_error/file_not_found'
_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


@dataclass(frozen=True)
class SelectionSpec:
    """Bounded, reproducible selection criteria for distinct Trace IDs."""

    anchor_trace_id: str
    limit: int = 20
    user_id: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None


class SelectedCandidateSource(ClickHouseProductionSource):  # type: ignore[misc]
    """Reuse the production loader with an explicitly selected candidate list."""

    def __init__(
        self,
        client: QueryClient,
        settings: ProductionSourceSettings,
        candidates: Sequence[Mapping[str, Any]],
    ) -> None:
        super().__init__(client, settings)
        self._selected_candidates: List[Mapping[str, Any]] = [
            dict(value) for value in candidates
        ]

    def _load_candidates(
        self,
        available: Sequence[str],
        mapping: Mapping[str, str],
        *,
        limit: int,
        rule_id: Optional[str],
    ) -> List[Mapping[str, Any]]:
        del available, mapping, rule_id
        return self._selected_candidates[:limit]


def normalize_datetime(value: str) -> str:
    """Normalize ISO-8601 input to a ClickHouse DateTime64(3) value."""

    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(
            candidate[:-1] + '+00:00' if candidate.endswith('Z') else candidate
        )
    except ValueError as error:
        raise ValueError(
            'Time values must use ISO-8601, for example '
            '2026-08-17T08:00:00Z.'
        ) from error
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(sep=' ', timespec='milliseconds')


def select_trace_candidates(
    client: QueryClient,
    settings: ProductionSourceSettings,
    spec: SelectionSpec,
) -> tuple[List[Mapping[str, Any]], str]:
    """Return the anchor and preceding distinct Trace candidates, newest first."""

    if not spec.anchor_trace_id.strip():
        raise ValueError('anchor_trace_id must not be empty.')
    if spec.limit < 1 or spec.limit > 1_000:
        raise ValueError('limit must be between 1 and 1000.')
    if spec.start_time and spec.end_time and spec.start_time > spec.end_time:
        raise ValueError('start_time must not be later than end_time.')

    table = _quote_identifier(settings.tool_errors_table)
    parameters: Dict[str, Any] = {
        'project_id': settings.project_id,
        'anchor_trace_id': spec.anchor_trace_id,
        'error_type': FILE_NOT_FOUND_ERROR_TYPE,
    }
    prewhere = ['project_id = {project_id:String}']
    if spec.start_time:
        prewhere.append('created_at >= {start_time:DateTime64(3)}')
        parameters['start_time'] = spec.start_time
    if spec.end_time:
        prewhere.append('created_at <= {end_time:DateTime64(3)}')
        parameters['end_time'] = spec.end_time
    where = [
        'trace_id = {anchor_trace_id:String}',
        'error_type = {error_type:String}',
    ]
    if spec.user_id:
        where.append('user_id = {user_id:String}')
        parameters['user_id'] = spec.user_id

    anchor_statement = (
        'SELECT maxOrNull(created_at) AS anchor_created_at\n'
        'FROM %s\nPREWHERE %s\nWHERE %s'
        % (
            table,
            '\n  AND '.join(prewhere),
            '\n  AND '.join(where),
        )
    )
    anchor_rows = client.query(
        anchor_statement,
        parameters=parameters,
        max_result_rows=1,
    )
    anchor_time = (
        str(anchor_rows[0].get('anchor_created_at') or '')
        if anchor_rows
        else ''
    )
    if not anchor_time:
        raise ValueError(
            'The anchor Trace has no matching File Not Found candidate inside '
            'the requested project/user/time filters.'
        )

    batch_parameters = dict(parameters)
    batch_parameters.pop('end_time', None)
    batch_parameters['anchor_time'] = anchor_time
    batch_parameters['limit'] = spec.limit
    batch_prewhere = [
        'source.project_id = {project_id:String}',
        'source.created_at <= {anchor_time:DateTime64(3)}',
    ]
    if spec.start_time:
        batch_prewhere.append(
            'source.created_at >= {start_time:DateTime64(3)}'
        )
    batch_where = [
        'error_type = {error_type:String}',
        "trace_id != ''",
    ]
    if spec.user_id:
        batch_where.append('user_id = {user_id:String}')

    batch_statement = (
        'SELECT\n'
        '  argMax(id, tuple(created_at, id)) AS case_id,\n'
        '  trace_id,\n'
        '  argMax(observation_id, tuple(created_at, id)) '
        'AS failure_observation_id,\n'
        '  any(project_id) AS project_id,\n'
        '  argMax(error_type, tuple(created_at, id)) AS rule_id,\n'
        "  'output' AS matched_field,\n"
        '  max(created_at) AS timestamp\n'
        'FROM %s AS source\nPREWHERE %s\nWHERE %s\n'
        'GROUP BY trace_id\n'
        'ORDER BY timestamp DESC, '
        '(trace_id = {anchor_trace_id:String}) DESC, trace_id DESC\n'
        'LIMIT {limit:UInt64}'
        % (
            table,
            '\n  AND '.join(batch_prewhere),
            '\n  AND '.join(batch_where),
        )
    )
    candidates = client.query(
        batch_statement,
        parameters=batch_parameters,
        max_result_rows=None,
    )
    if not any(
        str(candidate.get('trace_id') or '') == spec.anchor_trace_id
        for candidate in candidates
    ):
        raise ValueError(
            'The selected batch did not contain the anchor Trace; verify the '
            'source timestamps and filters.'
        )
    return candidates, anchor_time


def build_annotation_envelopes(
    snapshot: ProductionSnapshot,
    *,
    max_observations_per_trace: int,
) -> List[Dict[str, Any]]:
    """Convert one selected snapshot into annotation-tool Case envelopes."""

    if max_observations_per_trace < 1:
        raise ValueError('max_observations_per_trace must be at least 1.')
    traces = {
        str(trace.get('trace_id') or trace.get('id') or ''): dict(trace)
        for trace in snapshot.traces
    }
    observations_by_trace: Dict[str, List[Dict[str, Any]]] = {}
    for observation in snapshot.observations:
        trace_id = str(observation.get('trace_id') or '')
        observations_by_trace.setdefault(trace_id, []).append(dict(observation))

    now = datetime.now(timezone.utc).isoformat()
    envelopes: List[Dict[str, Any]] = []
    for case_value in snapshot.cases:
        case = dict(case_value)
        trace_id = str(case.get('trace_id') or '')
        trace = traces.get(trace_id)
        if trace is None:
            continue
        all_observations = sorted(
            observations_by_trace.get(trace_id, []),
            key=lambda item: (
                str(item.get('start_time') or ''),
                str(item.get('observation_id') or item.get('id') or ''),
            ),
        )
        observations = _truncate_observations(
            all_observations,
            limit=max_observations_per_trace,
            failure_id=str(
                dict(case.get('tool_attempt') or {}).get(
                    'tool_result_observation_id'
                )
                or ''
            ),
        )
        stable_payload = {
            'project_id': snapshot.manifest['project_id'],
            'case': case,
            'trace': trace,
            'observations': observations,
        }
        source_hash = sha256(
            json.dumps(
                stable_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode('utf-8')
        ).hexdigest()
        envelopes.append(
            {
                **stable_payload,
                'scenario': 'production_selected_batch',
                'source_snapshot': {
                    'snapshot_at': now,
                    'observation_count': len(all_observations),
                    'loaded_observation_count': len(observations),
                    'context_truncated': len(observations) < len(all_observations),
                    'source_updated_at': now,
                    'source_hash': source_hash,
                },
            }
        )
    return envelopes


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    _copy_work_environment_variables()
    start_time = normalize_datetime(args.start_time) if args.start_time else None
    end_time = normalize_datetime(args.end_time) if args.end_time else None
    spec = SelectionSpec(
        anchor_trace_id=args.anchor_trace_id,
        user_id=args.user_id,
        start_time=start_time,
        end_time=end_time,
        limit=args.limit,
    )
    settings = ProductionSourceSettings.from_file(
        args.config,
        project_id=args.project_id,
        payload_contract=args.payload_contract,
    )
    client = ClickHouseHTTPClient.from_env()
    try:
        source = ClickHouseProductionSource(client, settings)
        source.inspect()
        _validate_selection_columns(client.columns(settings.tool_errors_table))
        candidates, anchor_time = select_trace_candidates(client, settings, spec)
        selected_source = SelectedCandidateSource(client, settings, candidates)
        snapshot = selected_source.load(limit=len(candidates))
    finally:
        client.close()

    selected_trace_ids = [str(row.get('trace_id') or '') for row in candidates]
    selection_key = _selection_key(settings.project_id, spec)
    dataset_dir = args.dataset_dir or (
        ROOT / 'dataset' / 'data' / ('work-selection-' + selection_key)
    )
    debugx_output_dir = args.debugx_output_dir or (
        ROOT / 'dataset' / 'data' / ('work-debugx-' + selection_key)
    )
    annotation_db = args.annotation_db or (
        ROOT / 'dataset' / 'data' / ('work-annotations-' + selection_key + '.sqlite')
    )
    snapshot.manifest['selection'] = {
        'anchor_trace_id': spec.anchor_trace_id,
        'anchor_created_at': anchor_time,
        'error_type': FILE_NOT_FOUND_ERROR_TYPE,
        'user_id': spec.user_id,
        'start_time': spec.start_time,
        'end_time': spec.end_time,
        'requested_trace_count': spec.limit,
        'selected_trace_count': len(selected_trace_ids),
        'loaded_trace_count': len(snapshot.traces),
        'selected_trace_ids': selected_trace_ids,
    }
    snapshot_summary = write_production_snapshot(snapshot, dataset_dir)
    debugx_summary = run_records(
        cases=snapshot.cases,
        observations=snapshot.observations,
        traces=snapshot.traces,
        output_dir=debugx_output_dir,
    )
    envelopes = build_annotation_envelopes(
        snapshot,
        max_observations_per_trace=args.max_observations,
    )
    repository = AnnotationRepository(annotation_db)
    selected_case_ids = {
        str(dict(envelope.get('case') or {}).get('case_id') or '')
        for envelope in envelopes
    }
    existing_case_ids = {
        str(value.get('case_id') or '') for value in repository.list_cases()
    }
    unexpected_cases = sorted(existing_case_ids - selected_case_ids)
    if unexpected_cases:
        raise ValueError(
            'The annotation DB contains Cases outside this selection. Use a '
            'new --annotation-db to avoid mixing batches.'
        )
    repository.replace_cases(envelopes)

    summary = {
        'selection_key': selection_key,
        'requested_trace_count': spec.limit,
        'selected_trace_count': len(selected_trace_ids),
        'loaded_trace_count': len(snapshot.traces),
        'annotation_case_count': len(envelopes),
        'dataset_dir': str(dataset_dir.resolve()),
        'debugx_output_dir': str(debugx_output_dir.resolve()),
        'annotation_db': str(annotation_db.resolve()),
        'snapshot': snapshot_summary,
        'debugx': debugx_summary,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if len(snapshot.traces) < spec.limit:
        LOGGER.warning(
            'Loaded %d Trace(s), fewer than requested %d. Check the time/user '
            'range and snapshot skipped_candidates.',
            len(snapshot.traces),
            spec.limit,
        )
    if args.prepare_only:
        return 0

    application = AnnotationApplication(
        repository,
        lambda: envelopes,
        annotator_id=os.environ.get(
            'AGENTDEBUG_ANNOTATOR_ID',
            'local-annotator',
        ),
        demo_mode=False,
        access_token=os.environ.get('AGENTDEBUG_ANNOTATION_TOKEN'),
    )
    serve(application, host=args.host, port=args.port)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Select one anchor Trace and preceding distinct File Not Found '
            'Traces, prepare DebugX output, and seed the annotation tool.'
        )
    )
    parser.add_argument('--anchor-trace-id', required=True)
    parser.add_argument('--project-id')
    parser.add_argument('--user-id')
    parser.add_argument('--start-time', help='Inclusive ISO-8601 lower bound')
    parser.add_argument('--end-time', help='Inclusive ISO-8601 upper bound')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--payload-contract', choices=('raw', 'redacted'))
    parser.add_argument('--dataset-dir', type=Path)
    parser.add_argument('--debugx-output-dir', type=Path)
    parser.add_argument('--annotation-db', type=Path)
    parser.add_argument('--max-observations', type=int, default=10_000)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument(
        '--prepare-only',
        action='store_true',
        help='Prepare files and SQLite without starting the annotation server.',
    )
    parser.add_argument('--log-level', default='INFO')
    return parser


def _copy_work_environment_variables() -> None:
    aliases = {
        'AGENTDEBUG_CLICKHOUSE_URL': 'CLICKHOUSE_URL',
        'AGENTDEBUG_CLICKHOUSE_USER': 'CLICKHOUSE_USER',
        'AGENTDEBUG_CLICKHOUSE_PASSWORD': 'CLICKHOUSE_PASSWORD',
        'AGENTDEBUG_CLICKHOUSE_DATABASE': 'CLICKHOUSE_DATABASE',
    }
    for target, source in aliases.items():
        if not os.environ.get(target) and os.environ.get(source):
            os.environ[target] = os.environ[source]
    if not os.environ.get('AGENTDEBUG_CLICKHOUSE_URL'):
        raise ValueError(
            'Set CLICKHOUSE_URL or AGENTDEBUG_CLICKHOUSE_URL to the HTTP '
            'endpoint. CLICKHOUSE_MIGRATION_URL uses the native port and is '
            'not used by this script.'
        )


def _validate_selection_columns(available_columns: Sequence[str]) -> None:
    available = set(available_columns)
    required = {
        'id',
        'error_type',
        'trace_id',
        'observation_id',
        'project_id',
        'user_id',
        'created_at',
    }
    missing = sorted(required - available)
    if missing:
        raise ProductionSourceError(
            'tool_error is missing selection columns: %s' % ', '.join(missing)
        )


def _selection_key(project_id: str, spec: SelectionSpec) -> str:
    value = {
        'project_id': project_id,
        'anchor_trace_id': spec.anchor_trace_id,
        'user_id': spec.user_id,
        'start_time': spec.start_time,
        'end_time': spec.end_time,
        'limit': spec.limit,
        'error_type': FILE_NOT_FOUND_ERROR_TYPE,
    }
    return sha256(
        json.dumps(value, sort_keys=True).encode('utf-8')
    ).hexdigest()[:12]


def _quote_identifier(value: str) -> str:
    parts = value.split('.')
    if not parts or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError('Invalid ClickHouse identifier: %s' % value)
    return '.'.join('`%s`' % part for part in parts)


if __name__ == '__main__':
    raise SystemExit(main())
