"""Portable, read-only production input for Langfuse File Not Found RCA.

This module intentionally depends only on AgentDebugX and ``httpx``.  It does
not import Langfuse application code, so the AgentDebugX directory can be
installed and run independently in a work environment that exposes ClickHouse
``traces``, ``observations``, and a keyword-filtered ``tool_error`` table.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlsplit

import httpx

from agentdebug.runtime.llm import LLMClient, OpenAICompatClient

from .cli import (
    _read_jsonl,
    _write_json_atomic,
    _write_jsonl_atomic,
    run_records,
)


_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_PAYLOAD_CONTRACTS = ('raw', 'redacted')
_CANDIDATE_ALIASES: Mapping[str, Sequence[str]] = {
    'failure_observation_id': (
        'observation_id',
        'tool_result_observation_id',
        'matched_observation_id',
        'observationId',
        'id',
    ),
    'trace_id': ('trace_id', 'traceId'),
    'project_id': ('project_id', 'projectId'),
    'case_id': ('case_id', 'candidate_id', 'caseId', 'candidateId'),
    'rule_id': ('rule_id', 'ruleId', 'error_type', 'errorType'),
    'matched_field': ('matched_field', 'matchedField'),
    'matched_text': (
        'matched_text_redacted',
        'matched_text',
        'error_message_redacted',
        'error_message',
    ),
    'timestamp': ('timestamp', 'created_at', 'createdAt', 'start_time'),
}


class ProductionSourceError(RuntimeError):
    """Raised when the production source cannot satisfy the input contract."""


class QueryClient(Protocol):
    """Small query boundary used by the loader and its database-free tests."""

    def columns(self, table: str) -> List[str]: ...

    def query(
        self,
        statement: str,
        *,
        parameters: Optional[Mapping[str, Any]] = None,
        max_result_rows: Optional[int] = None,
    ) -> List[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class ProductionSourceSettings:
    """Non-secret table and mapping configuration.

    Connection credentials are deliberately excluded and must come from
    ``AGENTDEBUG_CLICKHOUSE_*`` environment variables.
    """

    project_id: str
    traces_table: str = 'traces'
    observations_table: str = 'observations'
    tool_errors_table: str = 'tool_error'
    candidate_columns: Mapping[str, str] = field(default_factory=dict)
    payload_contract: str = 'raw'
    allow_unscoped_tool_errors: bool = False
    matched_text_is_redacted: bool = False

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise ValueError('project_id must not be empty')
        for table in (
            self.traces_table,
            self.observations_table,
            self.tool_errors_table,
        ):
            _quote_identifier(table)
        if self.payload_contract not in _PAYLOAD_CONTRACTS:
            raise ValueError(
                'payload_contract must be one of: %s' % ', '.join(_PAYLOAD_CONTRACTS)
            )
        unknown_keys = set(self.candidate_columns) - set(_CANDIDATE_ALIASES)
        if unknown_keys:
            raise ValueError(
                'Unknown candidate column mapping keys: %s'
                % ', '.join(sorted(unknown_keys))
            )
        for column in self.candidate_columns.values():
            _quote_identifier(column)

    @classmethod
    def from_file(
        cls,
        path: Optional[Path] = None,
        *,
        project_id: Optional[str] = None,
        payload_contract: Optional[str] = None,
    ) -> 'ProductionSourceSettings':
        payload: Mapping[str, Any] = {}
        if path is not None:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, Mapping):
                raise ValueError('Production source config must be a JSON object.')
            payload = value
            forbidden = sorted(
                set(payload)
                & {
                    'url',
                    'base_url',
                    'username',
                    'user',
                    'password',
                    'database',
                    'api_key',
                }
            )
            if forbidden:
                raise ValueError(
                    'Connection values and credentials must use environment '
                    'variables, not the JSON config: %s' % ', '.join(forbidden)
                )

        tables = _mapping(payload.get('tables'))
        columns = _mapping(payload.get('candidate_columns'))
        resolved_project_id = (
            project_id
            or _optional_str(payload.get('project_id'))
            or os.environ.get('AGENTDEBUG_PROJECT_ID')
            or ''
        )
        return cls(
            project_id=resolved_project_id,
            traces_table=str(tables.get('traces') or 'traces'),
            observations_table=str(tables.get('observations') or 'observations'),
            tool_errors_table=str(tables.get('tool_errors') or 'tool_error'),
            candidate_columns={str(key): str(value) for key, value in columns.items()},
            payload_contract=(
                payload_contract
                or _optional_str(payload.get('payload_contract'))
                or 'raw'
            ),
            allow_unscoped_tool_errors=bool(
                payload.get('allow_unscoped_tool_errors', False)
            ),
            matched_text_is_redacted=bool(
                payload.get('matched_text_is_redacted', False)
            ),
        )


@dataclass(frozen=True)
class ProductionSnapshot:
    """In-memory dataset built from the three production tables."""

    traces: List[Dict[str, Any]]
    observations: List[Dict[str, Any]]
    cases: List[Dict[str, Any]]
    manifest: Dict[str, Any]


class ClickHouseHTTPClient:
    """Minimal ClickHouse HTTP client with enforced read-only settings."""

    def __init__(
        self,
        *,
        base_url: str,
        username: str = 'default',
        password: str = '',
        database: str = 'default',
        timeout: float = 60.0,
        verify: Any = True,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = _validate_clickhouse_url(base_url)
        self.database = _required_text(database, 'database')
        self.timeout = timeout
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            headers={
                'X-ClickHouse-User': username,
                'X-ClickHouse-Key': password,
                'Content-Type': 'text/plain; charset=utf-8',
            },
            follow_redirects=False,
            timeout=timeout,
            verify=verify,
        )

    @classmethod
    def from_env(cls) -> 'ClickHouseHTTPClient':
        base_url = os.environ['AGENTDEBUG_CLICKHOUSE_URL']
        ca_file = os.environ.get('AGENTDEBUG_CLICKHOUSE_CA_FILE')
        verify_value = os.environ.get('AGENTDEBUG_CLICKHOUSE_VERIFY_TLS', 'true')
        verify: Any = ca_file or _parse_bool(verify_value)
        return cls(
            base_url=base_url,
            username=os.environ.get('AGENTDEBUG_CLICKHOUSE_USER', 'default'),
            password=os.environ.get('AGENTDEBUG_CLICKHOUSE_PASSWORD', ''),
            database=os.environ.get('AGENTDEBUG_CLICKHOUSE_DATABASE', 'default'),
            timeout=float(
                os.environ.get('AGENTDEBUG_CLICKHOUSE_TIMEOUT_SECONDS', '60')
            ),
            verify=verify,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def columns(self, table: str) -> List[str]:
        rows = self.query('DESCRIBE TABLE %s' % _quote_identifier(table))
        return [str(row.get('name') or '') for row in rows if row.get('name')]

    def query(
        self,
        statement: str,
        *,
        parameters: Optional[Mapping[str, Any]] = None,
        max_result_rows: Optional[int] = None,
    ) -> List[Mapping[str, Any]]:
        request_parameters: Dict[str, str] = {
            'database': self.database,
            'readonly': '2',
            'max_execution_time': str(max(1, int(self.timeout))),
            'result_overflow_mode': 'throw',
            'max_result_rows': str(max_result_rows or 1_000_000),
        }
        for key, value in (parameters or {}).items():
            if not _IDENTIFIER.fullmatch(key):
                raise ValueError('Invalid ClickHouse parameter name: %s' % key)
            request_parameters['param_%s' % key] = _parameter_value(value)

        response = self._client.post(
            self.base_url,
            params=request_parameters,
            content=statement.rstrip().rstrip(';') + '\nFORMAT JSONEachRow',
            timeout=self.timeout,
        )
        if response.is_redirect:
            raise ProductionSourceError('ClickHouse redirects are not accepted.')
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise ProductionSourceError(
                'ClickHouse query failed with HTTP status %d.' % response.status_code
            ) from error

        rows: List[Mapping[str, Any]] = []
        for line_number, line in enumerate(response.text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ProductionSourceError(
                    'ClickHouse returned invalid JSONEachRow at line %d.' % line_number
                ) from error
            if not isinstance(value, Mapping):
                raise ProductionSourceError(
                    'ClickHouse returned a non-object row at line %d.' % line_number
                )
            rows.append(value)
        return rows


class ClickHouseProductionSource:
    """Build the attribution dataset without relying on Langfuse runtime code."""

    def __init__(
        self,
        client: QueryClient,
        settings: ProductionSourceSettings,
    ) -> None:
        self.client = client
        self.settings = settings

    def inspect(self) -> Dict[str, Any]:
        schemas = self._schemas()
        candidate_mapping = self._candidate_mapping(schemas['tool_errors'])
        self._validate_source_schemas(schemas, candidate_mapping)
        return {
            'status': 'ok',
            'project_scoped': 'project_id' in candidate_mapping,
            'payload_contract': self.settings.payload_contract,
            'tables': {
                'traces': self.settings.traces_table,
                'observations': self.settings.observations_table,
                'tool_errors': self.settings.tool_errors_table,
            },
            'candidate_columns': candidate_mapping,
            'available_columns': {key: sorted(value) for key, value in schemas.items()},
        }

    def load(
        self,
        *,
        limit: int = 50,
        rule_id: Optional[str] = None,
    ) -> ProductionSnapshot:
        if limit < 1:
            raise ValueError('limit must be at least 1')
        schemas = self._schemas()
        mapping = self._candidate_mapping(schemas['tool_errors'])
        self._validate_source_schemas(schemas, mapping)
        candidates = self._load_candidates(
            schemas['tool_errors'],
            mapping,
            limit=limit,
            rule_id=rule_id,
        )

        traces_by_id: Dict[str, Dict[str, Any]] = {}
        observations_by_trace: Dict[str, List[Dict[str, Any]]] = {}
        cases: List[Dict[str, Any]] = []
        skipped: Dict[str, int] = {}
        seen_failures = set()

        for candidate in candidates:
            failure_id = _optional_str(candidate.get('failure_observation_id'))
            if not failure_id:
                _increment(skipped, 'missing_failure_observation_id')
                continue
            trace_id = _optional_str(candidate.get('trace_id'))
            if not trace_id:
                matched = self._load_failure_observation(
                    schemas['observations'], failure_id
                )
                trace_id = _optional_str(matched.get('trace_id')) if matched else None
            if not trace_id:
                _increment(skipped, 'trace_id_not_resolved')
                continue

            unique_failure = (trace_id, failure_id)
            if unique_failure in seen_failures:
                _increment(skipped, 'duplicate_candidate')
                continue
            seen_failures.add(unique_failure)

            if trace_id not in traces_by_id:
                trace = self._load_trace(schemas['traces'], trace_id)
                if trace is None:
                    _increment(skipped, 'trace_not_found')
                    continue
                trace_observations = self._load_observations(
                    schemas['observations'], trace_id
                )
                traces_by_id[trace_id] = trace
                observations_by_trace[trace_id] = trace_observations

            observation_ids = {
                str(item.get('observation_id') or item.get('id') or '')
                for item in observations_by_trace[trace_id]
            }
            if failure_id not in observation_ids:
                _increment(skipped, 'failure_observation_not_in_trace')
                continue
            cases.append(self._case(candidate, trace_id, failure_id))

        observations = [
            observation
            for trace_id in traces_by_id
            for observation in observations_by_trace[trace_id]
        ]
        manifest: Dict[str, Any] = {
            'dataset_id': 'agentdebugx-production-file-not-found-snapshot',
            'source': 'clickhouse',
            'project_id': self.settings.project_id,
            'payload_contract': self.settings.payload_contract,
            'contains_raw_payloads': self.settings.payload_contract == 'raw',
            'trace_count': len(traces_by_id),
            'observation_count': len(observations),
            'case_count': len(cases),
            'candidate_count': len(candidates),
            'skipped_candidates': skipped,
            'tables': {
                'traces': self.settings.traces_table,
                'observations': self.settings.observations_table,
                'tool_errors': self.settings.tool_errors_table,
            },
        }
        return ProductionSnapshot(
            traces=list(traces_by_id.values()),
            observations=observations,
            cases=cases,
            manifest=manifest,
        )

    def _schemas(self) -> Dict[str, List[str]]:
        return {
            'traces': self.client.columns(self.settings.traces_table),
            'observations': self.client.columns(self.settings.observations_table),
            'tool_errors': self.client.columns(self.settings.tool_errors_table),
        }

    def _candidate_mapping(self, available: Sequence[str]) -> Dict[str, str]:
        columns = set(available)
        resolved: Dict[str, str] = {}
        for canonical, aliases in _CANDIDATE_ALIASES.items():
            override = self.settings.candidate_columns.get(canonical)
            if override:
                if override not in columns:
                    raise ProductionSourceError(
                        'Configured tool_error column %s=%s does not exist.'
                        % (canonical, override)
                    )
                resolved[canonical] = override
                continue
            match = next((alias for alias in aliases if alias in columns), None)
            if match:
                resolved[canonical] = match
        return resolved

    def _validate_source_schemas(
        self,
        schemas: Mapping[str, Sequence[str]],
        candidate_mapping: Mapping[str, str],
    ) -> None:
        _require_columns(schemas['traces'], ('id', 'project_id'), 'traces')
        _require_columns(
            schemas['observations'],
            ('id', 'trace_id', 'project_id', 'type', 'start_time'),
            'observations',
        )
        if 'failure_observation_id' not in candidate_mapping:
            raise ProductionSourceError(
                'tool_error must expose an observation id; configure '
                'candidate_columns.failure_observation_id when auto-detection fails.'
            )
        if (
            'project_id' not in candidate_mapping
            and not self.settings.allow_unscoped_tool_errors
        ):
            raise ProductionSourceError(
                'tool_error has no project_id column. Set '
                'allow_unscoped_tool_errors=true only for a physically '
                'single-project table or view.'
            )

    def _load_candidates(
        self,
        available: Sequence[str],
        mapping: Mapping[str, str],
        *,
        limit: int,
        rule_id: Optional[str],
    ) -> List[Mapping[str, Any]]:
        selected = ',\n  '.join(
            '%s AS %s' % (_quote_identifier(source), _quote_identifier(canonical))
            for canonical, source in mapping.items()
        )
        conditions: List[str] = []
        parameters: Dict[str, Any] = {'limit': limit}
        if 'project_id' in mapping:
            conditions.append(
                '%s = {project_id:String}' % _quote_identifier(mapping['project_id'])
            )
            parameters['project_id'] = self.settings.project_id
        if rule_id:
            rule_column = mapping.get('rule_id')
            if not rule_column:
                raise ProductionSourceError(
                    'A rule_id filter was requested but tool_error has no rule column.'
                )
            conditions.append('%s = {rule_id:String}' % _quote_identifier(rule_column))
            parameters['rule_id'] = rule_id
        order_clause = ''
        if mapping.get('timestamp'):
            order_clause = '\nORDER BY %s DESC' % _quote_identifier(
                mapping['timestamp']
            )
        statement = 'SELECT\n  %s\nFROM %s' % (
            selected,
            _quote_identifier(self.settings.tool_errors_table),
        )
        if conditions:
            statement += '\nWHERE ' + '\n  AND '.join(conditions)
        statement += order_clause + '\nLIMIT {limit:UInt64}'
        return self.client.query(
            statement,
            parameters=parameters,
            max_result_rows=limit,
        )

    def _load_failure_observation(
        self,
        available: Sequence[str],
        observation_id: str,
    ) -> Optional[Mapping[str, Any]]:
        fields = _select_fields(
            available,
            required=('id', 'trace_id', 'project_id'),
            optional=('event_ts', 'is_deleted'),
        )
        statement = (
            'SELECT %s\nFROM %s\n'
            'WHERE project_id = {project_id:String}\n'
            '  AND id = {observation_id:String}'
            % (fields, _quote_identifier(self.settings.observations_table))
        )
        if 'event_ts' in available:
            statement += '\nORDER BY event_ts DESC'
        statement += '\nLIMIT 1'
        rows = self.client.query(
            statement,
            parameters={
                'project_id': self.settings.project_id,
                'observation_id': observation_id,
            },
            max_result_rows=1,
        )
        if not rows or _is_deleted(rows[0]):
            return None
        return rows[0]

    def _load_trace(
        self,
        available: Sequence[str],
        trace_id: str,
    ) -> Optional[Dict[str, Any]]:
        fields = _select_fields(
            available,
            required=('id', 'project_id'),
            optional=(
                'timestamp',
                'name',
                'input',
                'output',
                'metadata',
                'event_ts',
                'is_deleted',
            ),
        )
        statement = (
            'SELECT %s\nFROM %s\n'
            'WHERE project_id = {project_id:String}\n'
            '  AND id = {trace_id:String}'
            % (fields, _quote_identifier(self.settings.traces_table))
        )
        if 'event_ts' in available:
            statement += '\nORDER BY event_ts DESC'
        statement += '\nLIMIT 1'
        rows = self.client.query(
            statement,
            parameters={
                'project_id': self.settings.project_id,
                'trace_id': trace_id,
            },
            max_result_rows=1,
        )
        if not rows or _is_deleted(rows[0]):
            return None
        row = rows[0]
        result: Dict[str, Any] = {
            'trace_id': str(row.get('id') or trace_id),
            'timestamp': row.get('timestamp'),
            'name': row.get('name'),
        }
        result.update(self._payload(row, trace=True))
        return result

    def _load_observations(
        self,
        available: Sequence[str],
        trace_id: str,
    ) -> List[Dict[str, Any]]:
        fields = _select_fields(
            available,
            required=('id', 'trace_id', 'project_id', 'type', 'start_time'),
            optional=(
                'parent_observation_id',
                'end_time',
                'name',
                'input',
                'output',
                'metadata',
                'level',
                'status_message',
                'event_ts',
                'is_deleted',
            ),
        )
        statement = (
            'SELECT %s\nFROM %s\n'
            'WHERE project_id = {project_id:String}\n'
            '  AND trace_id = {trace_id:String}'
            % (fields, _quote_identifier(self.settings.observations_table))
        )
        if 'event_ts' in available:
            statement += '\nORDER BY event_ts DESC\nLIMIT 1 BY id, project_id'
        rows = self.client.query(
            statement,
            parameters={
                'project_id': self.settings.project_id,
                'trace_id': trace_id,
            },
        )
        result: List[Dict[str, Any]] = []
        seen = set()
        for row in rows:
            observation_id = str(row.get('id') or '')
            if not observation_id or observation_id in seen or _is_deleted(row):
                continue
            seen.add(observation_id)
            observation: Dict[str, Any] = {
                'observation_id': observation_id,
                'trace_id': str(row.get('trace_id') or trace_id),
                'parent_observation_id': row.get('parent_observation_id'),
                'type': str(row.get('type') or ''),
                'name': str(row.get('name') or ''),
                'start_time': row.get('start_time'),
                'end_time': row.get('end_time'),
                'level': str(row.get('level') or 'DEFAULT'),
            }
            observation.update(self._payload(row, trace=False))
            result.append(observation)
        return sorted(
            result,
            key=lambda item: (
                str(item.get('start_time') or ''),
                str(item.get('observation_id') or ''),
            ),
        )

    def _payload(self, row: Mapping[str, Any], *, trace: bool) -> Dict[str, Any]:
        suffix = '_redacted' if self.settings.payload_contract == 'redacted' else ''
        result = {
            'input%s' % suffix: _decode_json_value(row.get('input')),
            'output%s' % suffix: _decode_json_value(row.get('output')),
            'metadata%s' % suffix: _decode_metadata(row.get('metadata')),
        }
        if not trace:
            result['status_message%s' % suffix] = row.get('status_message')
        return result

    def _case(
        self,
        candidate: Mapping[str, Any],
        trace_id: str,
        failure_id: str,
    ) -> Dict[str, Any]:
        rule_id = _optional_str(candidate.get('rule_id')) or 'file-not-found-v1'
        case_id = _optional_str(candidate.get('case_id')) or _stable_case_id(
            self.settings.project_id,
            trace_id,
            failure_id,
            rule_id,
        )
        rule_evidence: Dict[str, Any] = {
            'rule_id': rule_id,
            'matched_observation_id': failure_id,
        }
        matched_field = _optional_str(candidate.get('matched_field'))
        if matched_field:
            rule_evidence['matched_field'] = matched_field
        matched_text = _optional_str(candidate.get('matched_text'))
        if matched_text and self.settings.matched_text_is_redacted:
            rule_evidence['matched_text_redacted'] = matched_text
        return {
            'case_id': case_id,
            'trace_id': trace_id,
            'sampling_mode': 'production_rule_candidate',
            'rule_evidence': rule_evidence,
            'tool_attempt': {'tool_result_observation_id': failure_id},
        }


def write_production_snapshot(
    snapshot: ProductionSnapshot,
    output_dir: Path,
    *,
    annotations: Iterable[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Write a frozen four-file dataset plus a source-safe manifest."""

    output_dir.mkdir(parents=True, exist_ok=True)
    annotation_rows = list(annotations)
    _write_jsonl_atomic(output_dir / 'traces.jsonl', snapshot.traces)
    _write_jsonl_atomic(output_dir / 'observations.jsonl', snapshot.observations)
    _write_jsonl_atomic(output_dir / 'cases.jsonl', snapshot.cases)
    if annotation_rows:
        _write_jsonl_atomic(output_dir / 'annotations.jsonl', annotation_rows)
    else:
        (output_dir / 'annotations.jsonl').unlink(missing_ok=True)
    _write_json_atomic(output_dir / 'manifest.json', snapshot.manifest)
    return {
        'trace_count': len(snapshot.traces),
        'observation_count': len(snapshot.observations),
        'case_count': len(snapshot.cases),
        'annotation_count': len(annotation_rows),
        'contains_raw_payloads': snapshot.manifest['contains_raw_payloads'],
    }


def run_production_source(
    source: ClickHouseProductionSource,
    *,
    output_dir: Path,
    annotations: Iterable[Mapping[str, Any]] = (),
    limit: int = 50,
    rule_id: Optional[str] = None,
    llm: Optional[LLMClient] = None,
    review_deterministic: bool = False,
    max_context_observations: int = 64,
) -> Dict[str, Any]:
    """Load candidates and write only safe predictions/review trajectories."""

    if llm is not None and source.settings.payload_contract != 'redacted':
        raise ProductionSourceError(
            'Public-model review requires payload_contract="redacted". '
            'Raw Langfuse I/O is never relabeled as redacted automatically.'
        )
    snapshot = source.load(limit=limit, rule_id=rule_id)
    summary = run_records(
        cases=snapshot.cases,
        observations=snapshot.observations,
        traces=snapshot.traces,
        output_dir=output_dir,
        annotations=list(annotations),
        llm=llm,
        review_deterministic=review_deterministic,
        max_context_observations=max_context_observations,
    )
    summary.update(
        {
            'source': 'clickhouse',
            'candidate_count': snapshot.manifest['candidate_count'],
            'skipped_candidates': snapshot.manifest['skipped_candidates'],
            'payload_contract': source.settings.payload_contract,
        }
    )
    return summary


def add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the portable production commands to the main CLI."""

    commands = parser.add_subparsers(dest='langfuse_fnf_command', required=True)
    doctor = commands.add_parser('doctor', help='Validate table access and mappings')
    _add_source_arguments(doctor)
    doctor.set_defaults(langfuse_fnf_handler=_run_doctor)

    snapshot = commands.add_parser(
        'snapshot', help='Freeze production candidates into JSONL'
    )
    snapshot.add_argument('output_dir', type=Path)
    _add_source_arguments(snapshot)
    _add_candidate_arguments(snapshot)
    snapshot.add_argument('--annotations', type=Path)
    snapshot.set_defaults(langfuse_fnf_handler=_run_snapshot)

    run = commands.add_parser(
        'run', help='Run attribution directly from the three production tables'
    )
    run.add_argument('output_dir', type=Path)
    _add_source_arguments(run)
    _add_candidate_arguments(run)
    run.add_argument('--annotations', type=Path)
    run.add_argument('--llm', action='store_true')
    run.add_argument('--review-deterministic', action='store_true')
    run.add_argument('--max-context-observations', type=int, default=64)
    run.set_defaults(langfuse_fnf_handler=_run_production)


def run_cli(args: argparse.Namespace) -> int:
    handler = args.langfuse_fnf_handler
    return int(handler(args))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m agentdebug.integrations.langfuse_attribution.production'
    )
    add_cli_arguments(parser)
    return run_cli(parser.parse_args(argv))


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--config', type=Path)
    parser.add_argument('--project-id')
    parser.add_argument(
        '--payload-contract',
        choices=_PAYLOAD_CONTRACTS,
        help='raw for local deterministic runs; redacted only for pre-redacted views',
    )


def _add_candidate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--rule-id')


def _source_from_args(
    args: argparse.Namespace,
) -> tuple[ClickHouseHTTPClient, ClickHouseProductionSource]:
    settings = ProductionSourceSettings.from_file(
        args.config,
        project_id=args.project_id,
        payload_contract=args.payload_contract,
    )
    client = ClickHouseHTTPClient.from_env()
    return client, ClickHouseProductionSource(client, settings)


def _run_doctor(args: argparse.Namespace) -> int:
    client, source = _source_from_args(args)
    try:
        print(json.dumps(source.inspect(), ensure_ascii=False, sort_keys=True))
    finally:
        client.close()
    return 0


def _run_snapshot(args: argparse.Namespace) -> int:
    annotations = _load_annotations(args.annotations)
    client, source = _source_from_args(args)
    try:
        snapshot = source.load(limit=args.limit, rule_id=args.rule_id)
        summary = write_production_snapshot(
            snapshot,
            args.output_dir,
            annotations=annotations,
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    finally:
        client.close()
    return 0


def _run_production(args: argparse.Namespace) -> int:
    annotations = _load_annotations(args.annotations)
    client, source = _source_from_args(args)
    try:
        llm = OpenAICompatClient.from_env() if args.llm else None
        summary = run_production_source(
            source,
            output_dir=args.output_dir,
            annotations=annotations,
            limit=args.limit,
            rule_id=args.rule_id,
            llm=llm,
            review_deterministic=args.review_deterministic,
            max_context_observations=args.max_context_observations,
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    finally:
        client.close()
    return 0


def _load_annotations(path: Optional[Path]) -> List[Mapping[str, Any]]:
    if path is None:
        return []
    resolved = path / 'annotations.jsonl' if path.is_dir() else path
    return _read_jsonl(resolved)


def _quote_identifier(value: str) -> str:
    parts = value.split('.')
    if not parts or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError('Invalid ClickHouse identifier: %s' % value)
    return '.'.join('`%s`' % part for part in parts)


def _validate_clickhouse_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('ClickHouse URL must be an absolute http(s) URL.')
    if parsed.username or parsed.password:
        raise ValueError('ClickHouse credentials must not be embedded in the URL.')
    if parsed.query or parsed.fragment:
        raise ValueError('ClickHouse URL must not contain a query string or fragment.')
    return value.rstrip('/')


def _require_columns(
    available: Sequence[str], required: Sequence[str], table_role: str
) -> None:
    missing = [column for column in required if column not in available]
    if missing:
        raise ProductionSourceError(
            '%s table is missing required columns: %s'
            % (table_role, ', '.join(missing))
        )


def _select_fields(
    available: Sequence[str], *, required: Sequence[str], optional: Sequence[str]
) -> str:
    _require_columns(available, required, 'source')
    selected = list(required) + [item for item in optional if item in available]
    return ', '.join(_quote_identifier(item) for item in selected)


def _decode_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in ('{', '[', '"'):
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _decode_metadata(value: Any) -> Any:
    decoded = _decode_json_value(value)
    if not isinstance(decoded, Mapping):
        return decoded
    return {str(key): _decode_json_value(item) for key, item in decoded.items()}


def _stable_case_id(
    project_id: str, trace_id: str, observation_id: str, rule_id: str
) -> str:
    digest = sha256(
        '\0'.join((project_id, trace_id, observation_id, rule_id)).encode('utf-8')
    ).hexdigest()[:20]
    return 'fnf-%s' % digest


def _parameter_value(value: Any) -> str:
    if isinstance(value, bool):
        return '1' if value else '0'
    return str(value)


def _required_text(value: str, name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError('%s must not be empty' % name)
    return text


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ('1', 'true', 'yes', 'on'):
        return True
    if normalized in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError('Expected a boolean value, got: %s' % value)


def _is_deleted(row: Mapping[str, Any]) -> bool:
    value = row.get('is_deleted', 0)
    return value is True or str(value) == '1'


def _increment(counts: Dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


if __name__ == '__main__':
    raise SystemExit(main())
