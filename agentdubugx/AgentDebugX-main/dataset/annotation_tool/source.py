"""Read-only ClickHouse source adapter for the annotation workbench."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentdebug.integrations.langfuse_attribution.production import (
    ClickHouseHTTPClient,
    ClickHouseProductionSource,
    ProductionSourceSettings,
)


def load_clickhouse_cases(
    *,
    config_path: Optional[Path],
    limit: int,
    rule_id: Optional[str],
    max_observations_per_trace: int = 10_000,
) -> List[Dict[str, Any]]:
    """Hydrate production candidates into portable per-Case snapshots."""

    settings = ProductionSourceSettings.from_file(config_path)
    client = ClickHouseHTTPClient.from_env()
    try:
        snapshot = ClickHouseProductionSource(client, settings).load(
            limit=limit,
            rule_id=rule_id,
        )
    finally:
        client.close()

    traces = {
        str(trace.get('trace_id') or trace.get('id') or ''): dict(trace)
        for trace in snapshot.traces
    }
    observations_by_trace: Dict[str, List[Dict[str, Any]]] = {}
    for observation in snapshot.observations:
        trace_id = str(observation.get('trace_id') or '')
        observations_by_trace.setdefault(trace_id, []).append(dict(observation))

    now = datetime.now(timezone.utc).isoformat()
    result: List[Dict[str, Any]] = []
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
        context_truncated = len(all_observations) > max_observations_per_trace
        observations = _truncate_observations(
            all_observations,
            limit=max_observations_per_trace,
            failure_id=str(
                case.get('tool_attempt', {}).get('tool_result_observation_id') or ''
            ),
        )
        stable_payload = {
            'project_id': settings.project_id,
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
        result.append(
            {
                **stable_payload,
                'scenario': 'production',
                'source_snapshot': {
                    'snapshot_at': now,
                    'observation_count': len(all_observations),
                    'loaded_observation_count': len(observations),
                    'context_truncated': context_truncated,
                    'source_updated_at': now,
                    'source_hash': source_hash,
                },
            }
        )
    return result


def _truncate_observations(
    observations: List[Dict[str, Any]],
    *,
    limit: int,
    failure_id: str,
) -> List[Dict[str, Any]]:
    if limit < 1:
        raise ValueError('max_observations_per_trace must be at least 1')
    if len(observations) <= limit:
        return observations
    selected = observations[:limit]
    if failure_id and not any(
        str(item.get('observation_id') or item.get('id') or '') == failure_id
        for item in selected
    ):
        failure = next(
            (
                item
                for item in observations
                if str(item.get('observation_id') or item.get('id') or '')
                == failure_id
            ),
            None,
        )
        if failure is not None:
            selected[-1] = failure
    return sorted(
        selected,
        key=lambda item: (
            str(item.get('start_time') or ''),
            str(item.get('observation_id') or item.get('id') or ''),
        ),
    )
