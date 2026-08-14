from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import httpx
import pytest

from agentdebug.integrations.langfuse_attribution.production import (
    ClickHouseHTTPClient,
    ClickHouseProductionSource,
    ProductionSourceError,
    ProductionSourceSettings,
    run_production_source,
    write_production_snapshot,
)


class FakeQueryClient:
    def __init__(
        self,
        *,
        scoped_candidates: bool = True,
        candidate_has_trace_id: bool = True,
    ) -> None:
        candidate_columns = [
            'case_id',
            'observation_id',
            'rule_id',
            'matched_field',
            'matched_text',
            'created_at',
        ]
        if scoped_candidates:
            candidate_columns.append('project_id')
        if candidate_has_trace_id:
            candidate_columns.append('trace_id')
        self.candidate_has_trace_id = candidate_has_trace_id
        self.schemas = {
            'traces': [
                'id',
                'project_id',
                'timestamp',
                'name',
                'input',
                'output',
                'metadata',
                'event_ts',
                'is_deleted',
            ],
            'observations': [
                'id',
                'trace_id',
                'project_id',
                'type',
                'parent_observation_id',
                'start_time',
                'end_time',
                'name',
                'input',
                'output',
                'metadata',
                'level',
                'status_message',
                'event_ts',
                'is_deleted',
            ],
            'tool_error': candidate_columns,
        }
        self.queries: List[tuple[str, Mapping[str, Any]]] = []
        self.path = '/workspace/missing.md'

    def columns(self, table: str) -> List[str]:
        return self.schemas[table]

    def query(
        self,
        statement: str,
        *,
        parameters: Optional[Mapping[str, Any]] = None,
        max_result_rows: Optional[int] = None,
    ) -> List[Mapping[str, Any]]:
        params = dict(parameters or {})
        self.queries.append((statement, params))
        if 'FROM `tool_error`' in statement:
            candidate = {
                'case_id': 'case-production-1',
                'failure_observation_id': 'tool-failed',
                'project_id': 'project-1',
                'rule_id': 'file-not-found-v1',
                'matched_field': 'status_message',
                'matched_text': 'File not found: /workspace/missing.md',
                'timestamp': '2026-08-01 00:00:03.000',
            }
            if self.candidate_has_trace_id:
                candidate['trace_id'] = 'trace-production-1'
            return [candidate]
        if 'FROM `traces`' in statement:
            return [
                {
                    'id': 'trace-production-1',
                    'project_id': 'project-1',
                    'timestamp': '2026-08-01 00:00:00.000',
                    'name': 'production-task',
                    'input': json.dumps({'task': 'Read the generated report.'}),
                    'output': json.dumps({'status': 'failed'}),
                    'metadata': {'environment': 'test'},
                    'event_ts': '2026-08-01 00:00:04.000',
                    'is_deleted': 0,
                }
            ]
        if (
            'FROM `observations`' in statement
            and 'AND id = {observation_id:String}' in statement
        ):
            return [
                {
                    'id': 'tool-failed',
                    'trace_id': 'trace-production-1',
                    'project_id': 'project-1',
                    'event_ts': '2026-08-01 00:00:02.500',
                    'is_deleted': 0,
                }
            ]
        if 'FROM `observations`' in statement and 'trace_id =' in statement:
            return [
                {
                    'id': 'model-plan',
                    'trace_id': 'trace-production-1',
                    'project_id': 'project-1',
                    'type': 'GENERATION',
                    'parent_observation_id': None,
                    'start_time': '2026-08-01 00:00:01.000',
                    'end_time': '2026-08-01 00:00:01.500',
                    'name': 'planner',
                    'input': None,
                    'output': json.dumps({'tool': 'read_file', 'path': self.path}),
                    'metadata': {},
                    'level': 'DEFAULT',
                    'status_message': None,
                    'event_ts': '2026-08-01 00:00:01.500',
                    'is_deleted': 0,
                },
                {
                    'id': 'tool-failed',
                    'trace_id': 'trace-production-1',
                    'project_id': 'project-1',
                    'type': 'TOOL',
                    'parent_observation_id': 'model-plan',
                    'start_time': '2026-08-01 00:00:02.000',
                    'end_time': '2026-08-01 00:00:02.500',
                    'name': 'read_file',
                    'input': json.dumps({'path': self.path}),
                    'output': json.dumps({'error': 'ENOENT'}),
                    'metadata': {},
                    'level': 'ERROR',
                    'status_message': 'File not found',
                    'event_ts': '2026-08-01 00:00:02.500',
                    'is_deleted': 0,
                },
            ]
        raise AssertionError('Unexpected query: %s' % statement)


def _settings(**overrides: Any) -> ProductionSourceSettings:
    values: Dict[str, Any] = {'project_id': 'project-1'}
    values.update(overrides)
    return ProductionSourceSettings(**values)


def test_source_builds_cases_from_three_tables_without_langfuse_imports() -> None:
    client = FakeQueryClient()
    source = ClickHouseProductionSource(client, _settings())

    snapshot = source.load(limit=50, rule_id='file-not-found-v1')

    assert len(snapshot.traces) == 1
    assert len(snapshot.observations) == 2
    assert snapshot.cases == [
        {
            'case_id': 'case-production-1',
            'trace_id': 'trace-production-1',
            'sampling_mode': 'production_rule_candidate',
            'rule_evidence': {
                'rule_id': 'file-not-found-v1',
                'matched_observation_id': 'tool-failed',
                'matched_field': 'status_message',
            },
            'tool_attempt': {'tool_result_observation_id': 'tool-failed'},
        }
    ]
    assert snapshot.observations[0]['output'] == {
        'tool': 'read_file',
        'path': client.path,
    }
    assert snapshot.manifest['contains_raw_payloads'] is True
    assert all('{project_id:String}' in query for query, _ in client.queries)
    assert all(params.get('project_id') == 'project-1' for _, params in client.queries)


def test_source_requires_explicit_opt_in_for_unscoped_candidate_table() -> None:
    source = ClickHouseProductionSource(
        FakeQueryClient(scoped_candidates=False),
        _settings(),
    )

    with pytest.raises(ProductionSourceError, match='no project_id column'):
        source.inspect()


def test_source_resolves_trace_id_from_failure_observation() -> None:
    client = FakeQueryClient(candidate_has_trace_id=False)
    source = ClickHouseProductionSource(client, _settings())

    snapshot = source.load()

    assert snapshot.cases[0]['trace_id'] == 'trace-production-1'
    assert any(
        params.get('observation_id') == 'tool-failed' for _, params in client.queries
    )


def test_source_can_use_explicit_mapping_for_single_project_candidate_view() -> None:
    client = FakeQueryClient(scoped_candidates=False)
    source = ClickHouseProductionSource(
        client,
        _settings(allow_unscoped_tool_errors=True),
    )

    report = source.inspect()

    assert report['status'] == 'ok'
    assert report['project_scoped'] is False


def test_production_run_evaluates_flat_manual_annotations(tmp_path: Path) -> None:
    source = ClickHouseProductionSource(FakeQueryClient(), _settings())
    annotations = [
        {
            'case_id': 'case-production-1',
            'technical_error_review': 'confirmed',
            'is_agent_failure': True,
            'root_cause_label': 'model_path_hallucination',
            'primary_root_cause_observation_id': 'model-plan',
        }
    ]

    summary = run_production_source(
        source,
        output_dir=tmp_path,
        annotations=annotations,
    )

    prediction = json.loads(
        (tmp_path / 'predictions.jsonl').read_text(encoding='utf-8').splitlines()[0]
    )
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert summary['prediction_count'] == 1
    assert summary['evaluated'] is True
    assert prediction['root_cause_observation_id'] == 'model-plan'
    assert report['attribution']['root_observation_correct'] == 1
    assert '/workspace/missing.md' not in (tmp_path / 'predictions.jsonl').read_text()
    assert '/workspace/missing.md' not in (tmp_path / 'trajectories.jsonl').read_text()


def test_public_llm_requires_pre_redacted_production_contract(tmp_path: Path) -> None:
    class UnusedLLM:
        model = 'unused'

        def complete(self, messages, **kwargs):
            raise AssertionError('LLM must not receive raw production payloads')

    client = FakeQueryClient()
    source = ClickHouseProductionSource(client, _settings())

    with pytest.raises(ProductionSourceError, match='requires payload_contract'):
        run_production_source(
            source,
            output_dir=tmp_path,
            llm=UnusedLLM(),
        )
    assert client.queries == []


def test_snapshot_writes_standard_portable_dataset(tmp_path: Path) -> None:
    source = ClickHouseProductionSource(
        FakeQueryClient(),
        _settings(payload_contract='redacted', matched_text_is_redacted=True),
    )
    snapshot = source.load()
    (tmp_path / 'annotations.jsonl').write_text('{"stale":true}\n', encoding='utf-8')

    summary = write_production_snapshot(snapshot, tmp_path)

    observation = json.loads(
        (tmp_path / 'observations.jsonl').read_text(encoding='utf-8').splitlines()[0]
    )
    case = json.loads(
        (tmp_path / 'cases.jsonl').read_text(encoding='utf-8').splitlines()[0]
    )
    assert summary['contains_raw_payloads'] is False
    assert 'input_redacted' in observation
    assert case['rule_evidence']['matched_text_redacted'].startswith('File not found')
    assert (tmp_path / 'manifest.json').is_file()
    assert not (tmp_path / 'annotations.jsonl').exists()


def test_http_client_enforces_read_only_query_settings() -> None:
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured['params'] = dict(request.url.params)
        captured['body'] = request.content.decode()
        return httpx.Response(200, text='{"name":"id"}\n')

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, follow_redirects=False) as http_client:
        client = ClickHouseHTTPClient(
            base_url='https://clickhouse.example.internal:8123',
            http_client=http_client,
        )
        assert client.columns('observations') == ['id']

    assert captured['params']['readonly'] == '2'
    assert captured['params']['result_overflow_mode'] == 'throw'
    assert captured['body'].endswith('FORMAT JSONEachRow')


@pytest.mark.parametrize(
    'url',
    [
        'clickhouse.example.internal:8123',
        'https://user:secret@clickhouse.example.internal',
        'https://clickhouse.example.internal?password=secret',
        'file:///tmp/clickhouse',
    ],
)
def test_http_client_rejects_unsafe_connection_urls(url: str) -> None:
    with pytest.raises(ValueError):
        ClickHouseHTTPClient(base_url=url)


def test_table_and_column_identifiers_cannot_inject_sql() -> None:
    with pytest.raises(ValueError, match='Invalid ClickHouse identifier'):
        _settings(tool_errors_table='tool_error; DROP TABLE traces')


def test_json_config_rejects_connection_credentials(tmp_path: Path) -> None:
    config = tmp_path / 'source.json'
    config.write_text(
        json.dumps({'project_id': 'project-1', 'password': 'do-not-store-here'}),
        encoding='utf-8',
    )

    with pytest.raises(ValueError, match='must use environment variables'):
        ProductionSourceSettings.from_file(config)
