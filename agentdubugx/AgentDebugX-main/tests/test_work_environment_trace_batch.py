from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from agentdebug.integrations.langfuse_attribution.production import (
    ProductionSnapshot,
    ProductionSourceSettings,
)
from dataset.run_work_environment_trace_batch import (
    SelectionSpec,
    _validate_selection_columns,
    build_annotation_envelopes,
    normalize_datetime,
    select_trace_candidates,
)


class SelectionClient:
    def __init__(self) -> None:
        self.queries: List[tuple[str, Dict[str, Any], Optional[int]]] = []

    def query(
        self,
        statement: str,
        *,
        parameters: Optional[Mapping[str, Any]] = None,
        max_result_rows: Optional[int] = None,
    ) -> List[Mapping[str, Any]]:
        values = dict(parameters or {})
        self.queries.append((statement, values, max_result_rows))
        if 'maxOrNull(created_at)' in statement:
            return [{'anchor_created_at': '2026-08-17 12:00:00.123'}]
        return [
            {
                'case_id': 'error-anchor',
                'trace_id': 'trace-anchor',
                'failure_observation_id': 'obs-anchor',
                'project_id': 'project-1',
                'rule_id': 'dependency_or_env_error/file_not_found',
                'matched_field': 'output',
                'timestamp': '2026-08-17 12:00:00.123',
            },
            {
                'case_id': 'error-before',
                'trace_id': 'trace-before',
                'failure_observation_id': 'obs-before',
                'project_id': 'project-1',
                'rule_id': 'dependency_or_env_error/file_not_found',
                'matched_field': 'output',
                'timestamp': '2026-08-17 11:59:00.000',
            },
        ]


def test_selection_uses_project_time_user_and_anchor_filters() -> None:
    client = SelectionClient()
    settings = ProductionSourceSettings(
        project_id='project-1',
        tool_errors_table='default.tool_error',
    )
    spec = SelectionSpec(
        anchor_trace_id='trace-anchor',
        user_id='user-7',
        start_time='2026-08-17 00:00:00.000',
        end_time='2026-08-17 23:59:59.999',
        limit=20,
    )

    candidates, anchor_time = select_trace_candidates(client, settings, spec)

    assert anchor_time == '2026-08-17 12:00:00.123'
    assert [row['trace_id'] for row in candidates] == [
        'trace-anchor',
        'trace-before',
    ]
    assert len(client.queries) == 2
    for statement, parameters, _ in client.queries:
        assert 'error_type = {error_type:String}' in statement
        assert 'user_id = {user_id:String}' in statement
        assert parameters['project_id'] == 'project-1'
        assert parameters['user_id'] == 'user-7'
        assert parameters['error_type'] == (
            'dependency_or_env_error/file_not_found'
        )
    batch_statement, batch_parameters, max_rows = client.queries[1]
    assert 'FROM `default`.`tool_error` AS source' in batch_statement
    assert (
        'PREWHERE source.project_id = {project_id:String}'
        in batch_statement
    )
    assert 'source.created_at <= {anchor_time:DateTime64(3)}' in batch_statement
    assert 'GROUP BY trace_id' in batch_statement
    assert 'LIMIT {limit:UInt64}' in batch_statement
    assert batch_parameters['limit'] == 20
    assert max_rows is None


def test_selection_column_validation_accepts_direct_describe_columns() -> None:
    _validate_selection_columns(
        [
            'id',
            'error_type',
            'trace_id',
            'observation_id',
            'project_id',
            'user_id',
            'created_at',
        ]
    )


def test_selection_rejects_missing_anchor() -> None:
    client = SelectionClient()

    def no_anchor(*args: Any, **kwargs: Any) -> List[Mapping[str, Any]]:
        return [{'anchor_created_at': None}]

    client.query = no_anchor  # type: ignore[method-assign]
    settings = ProductionSourceSettings(project_id='project-1')

    with pytest.raises(ValueError, match='anchor Trace'):
        select_trace_candidates(
            client,
            settings,
            SelectionSpec(anchor_trace_id='missing', limit=20),
        )


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('2026-08-17T08:01:02.345Z', '2026-08-17 08:01:02.345'),
        ('2026-08-17 08:01:02', '2026-08-17 08:01:02.000'),
        ('2026-08-17T16:01:02+08:00', '2026-08-17 08:01:02.000'),
    ],
)
def test_normalize_datetime(value: str, expected: str) -> None:
    assert normalize_datetime(value) == expected


def test_normalize_datetime_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match='ISO-8601'):
        normalize_datetime('17/08/2026')


def test_script_is_kept_as_a_standalone_dataset_entrypoint() -> None:
    script = Path('dataset/run_work_environment_trace_batch.py')

    assert script.is_file()
    assert 'if __name__' in script.read_text(encoding='utf-8')


def test_selected_snapshot_becomes_annotation_envelopes() -> None:
    snapshot = ProductionSnapshot(
        traces=[{'trace_id': 'trace-1', 'name': 'selected'}],
        observations=[
            {
                'observation_id': 'obs-before',
                'trace_id': 'trace-1',
                'start_time': '2026-08-17 00:00:00.000',
            },
            {
                'observation_id': 'obs-failure',
                'trace_id': 'trace-1',
                'start_time': '2026-08-17 00:00:01.000',
            },
        ],
        cases=[
            {
                'case_id': 'case-1',
                'trace_id': 'trace-1',
                'tool_attempt': {
                    'tool_result_observation_id': 'obs-failure',
                },
            }
        ],
        manifest={'project_id': 'project-1'},
    )

    envelopes = build_annotation_envelopes(
        snapshot,
        max_observations_per_trace=1,
    )

    assert len(envelopes) == 1
    assert envelopes[0]['scenario'] == 'production_selected_batch'
    assert envelopes[0]['source_snapshot']['context_truncated'] is True
    assert [
        row['observation_id'] for row in envelopes[0]['observations']
    ] == ['obs-failure']
    assert len(envelopes[0]['source_snapshot']['source_hash']) == 64
