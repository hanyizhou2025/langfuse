"""SQLite persistence for snapshots and immutable Annotation revisions."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .validation import AnnotationValidationError, validate_annotation


_EXPORTABLE_STATUSES = {'submitted', 'needs_review', 'adjudicated', 'frozen'}
_IMPORTABLE_STATUSES = _EXPORTABLE_STATUSES
_DATASET_VERSION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


class AnnotationRepository:
    """Persist portable annotation state without an external writable service."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA journal_mode = WAL')
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                '''
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    scenario TEXT,
                    source_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    synced_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS annotations (
                    case_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    annotator_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    change_reason TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (case_id, revision),
                    FOREIGN KEY (case_id) REFERENCES cases(case_id)
                );
                CREATE INDEX IF NOT EXISTS annotations_case_status
                    ON annotations(case_id, status, revision DESC);
                '''
            )

    def replace_cases(self, cases: Iterable[Mapping[str, Any]]) -> int:
        """Upsert source snapshots while retaining all Annotation revisions."""

        now = _now()
        count = 0
        with self._connect() as connection:
            for case in cases:
                envelope = dict(case)
                case_payload = dict(envelope.get('case') or {})
                trace_payload = dict(envelope.get('trace') or {})
                case_id = str(case_payload.get('case_id') or '')
                project_id = str(envelope.get('project_id') or '')
                trace_id = str(
                    trace_payload.get('trace_id') or case_payload.get('trace_id') or ''
                )
                source_hash = str(
                    dict(envelope.get('source_snapshot') or {}).get('source_hash')
                    or ''
                )
                if not all((case_id, project_id, trace_id, source_hash)):
                    raise ValueError('Case snapshot is missing stable identifiers.')
                connection.execute(
                    '''
                    INSERT INTO cases (
                        case_id, project_id, trace_id, scenario, source_hash,
                        payload_json, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(case_id) DO UPDATE SET
                        project_id=excluded.project_id,
                        trace_id=excluded.trace_id,
                        scenario=excluded.scenario,
                        source_hash=excluded.source_hash,
                        payload_json=excluded.payload_json,
                        synced_at=excluded.synced_at
                    ''',
                    (
                        case_id,
                        project_id,
                        trace_id,
                        str(envelope.get('scenario') or ''),
                        source_hash,
                        json.dumps(envelope, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                count += 1
        return count

    def list_cases(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                '''
                SELECT c.payload_json, a.status, a.revision, a.annotator_id,
                       a.created_at, a.payload_json AS annotation_json
                FROM cases c
                LEFT JOIN annotations a ON a.case_id = c.case_id
                  AND a.revision = (
                    SELECT MAX(a2.revision) FROM annotations a2
                    WHERE a2.case_id = c.case_id
                  )
                ORDER BY c.synced_at DESC, c.case_id
                '''
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            envelope = json.loads(row['payload_json'])
            latest_status = str(row['status'] or 'candidate')
            if status and latest_status != status:
                continue
            case = dict(envelope.get('case') or {})
            annotation = (
                json.loads(row['annotation_json'])
                if row['annotation_json']
                else None
            )
            result.append(
                {
                    'case_id': case.get('case_id'),
                    'trace_id': case.get('trace_id'),
                    'scenario': envelope.get('scenario'),
                    'tool_name': case.get('tool_attempt', {}).get('tool_name'),
                    'rule_id': case.get('rule_evidence', {}).get('rule_id'),
                    'matched_field': case.get('rule_evidence', {}).get(
                        'matched_field'
                    ),
                    'status': latest_status,
                    'revision': row['revision'],
                    'annotator_id': row['annotator_id'],
                    'updated_at': row['created_at'],
                    'observation_count': len(envelope.get('observations') or []),
                    'context_truncated': bool(
                        envelope.get('source_snapshot', {}).get('context_truncated')
                    ),
                    'label_summary': _annotation_summary(annotation),
                }
            )
        return result

    def get_case(self, case_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cases WHERE case_id = ?', (case_id,)
            ).fetchone()
        if row is None:
            raise KeyError(case_id)
        envelope: Dict[str, Any] = json.loads(row['payload_json'])
        latest = self.latest_annotation(case_id)
        envelope['latest_annotation'] = latest
        envelope['annotation_history'] = self.annotation_history(case_id)
        return envelope

    def save_annotation(
        self,
        payload: Mapping[str, Any],
        *,
        annotator_id: str,
        change_reason: Optional[str] = None,
        expected_source_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        case_id = str(payload.get('case_id') or '')
        if not case_id:
            raise ValueError('case_id is required')
        case = self.get_case(case_id)
        source_hash = str(case.get('source_snapshot', {}).get('source_hash') or '')
        if expected_source_hash and expected_source_hash != source_hash:
            raise SourceSnapshotConflict('The source Trace changed after it was opened.')
        strict = str(payload.get('annotation_status') or 'draft') in {
            'submitted',
            'needs_review',
            'adjudicated',
            'frozen',
        }
        normalized = validate_annotation(payload, case, strict=strict)
        now = _now()
        with self._connect() as connection:
            latest = connection.execute(
                '''SELECT revision, status FROM annotations
                   WHERE case_id = ? ORDER BY revision DESC LIMIT 1''',
                (case_id,),
            ).fetchone()
            if latest is not None and latest['status'] == 'frozen':
                raise ValueError('A frozen annotation cannot be modified.')
            revision = int(latest['revision'] if latest else 0) + 1
            normalized['annotation_revision'] = revision
            normalized['audit'] = {
                'annotator_id': annotator_id,
                'created_at': now,
                'updated_at': now,
                'change_reason': change_reason,
            }
            connection.execute(
                '''INSERT INTO annotations (
                    case_id, revision, status, annotator_id, created_at,
                    change_reason, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (
                    case_id,
                    revision,
                    normalized['annotation_status'],
                    annotator_id,
                    now,
                    change_reason,
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                ),
            )
        return normalized

    def latest_annotation(self, case_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                '''SELECT payload_json FROM annotations
                   WHERE case_id = ? ORDER BY revision DESC LIMIT 1''',
                (case_id,),
            ).fetchone()
        return json.loads(row['payload_json']) if row else None

    def get_annotation(self, case_id: str, revision: int) -> Dict[str, Any]:
        """Return one immutable historical revision payload."""

        with self._connect() as connection:
            row = connection.execute(
                '''SELECT payload_json FROM annotations
                   WHERE case_id = ? AND revision = ?''',
                (case_id, revision),
            ).fetchone()
        if row is None:
            raise KeyError('%s revision %s' % (case_id, revision))
        return json.loads(row['payload_json'])

    def annotation_history(self, case_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                '''SELECT revision, status, annotator_id, created_at, change_reason
                   FROM annotations WHERE case_id = ? ORDER BY revision DESC''',
                (case_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_jsonl(self) -> str:
        """Export the newest submitted-like revision for every Case."""

        with self._connect() as connection:
            rows = connection.execute(
                '''SELECT case_id, revision, status, payload_json
                   FROM annotations ORDER BY case_id, revision DESC'''
            ).fetchall()
        latest: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            case_id = str(row['case_id'])
            if case_id in latest or row['status'] not in _EXPORTABLE_STATUSES:
                continue
            latest[case_id] = json.loads(row['payload_json'])
        return ''.join(
            json.dumps(latest[case_id], ensure_ascii=False, sort_keys=True) + '\n'
            for case_id in sorted(latest)
        )

    def quality_report(self) -> Dict[str, Any]:
        """Summarize current labels without treating counts as model metrics."""

        rows = self._case_rows_with_latest()
        status_counts: Counter[str] = Counter()
        technical_counts: Counter[str] = Counter()
        scope_counts: Counter[str] = Counter()
        domain_counts: Counter[str] = Counter()
        label_counts: Counter[str] = Counter()
        confidence_counts: Counter[str] = Counter()
        issue_counts: Counter[str] = Counter()
        annotated = 0
        exportable = 0
        truncated = 0

        for envelope, annotation in rows:
            status = str(
                (annotation or {}).get('annotation_status') or 'candidate'
            )
            status_counts[status] += 1
            if annotation is None:
                issue_counts['unannotated'] += 1
            else:
                annotated += 1
                summary = _annotation_summary(annotation)
                _count_value(technical_counts, summary.get('technical_label'))
                _count_value(scope_counts, summary.get('root_scope'))
                _count_value(domain_counts, summary.get('root_domain'))
                _count_value(label_counts, summary.get('root_label'))
                _count_value(
                    confidence_counts,
                    summary.get('overall_confidence'),
                )
            if status in _EXPORTABLE_STATUSES:
                exportable += 1
            if status in {'candidate', 'draft', 'submitted', 'needs_review'}:
                issue_counts[status if status != 'candidate' else 'unreviewed'] += 1
            if bool(
                dict(envelope.get('source_snapshot') or {}).get(
                    'context_truncated'
                )
            ):
                truncated += 1
                issue_counts['context_truncated'] += 1

        return {
            'case_count': len(rows),
            'annotated_case_count': annotated,
            'exportable_count': exportable,
            'needs_review_count': status_counts.get('needs_review', 0),
            'frozen_count': status_counts.get('frozen', 0),
            'context_truncated_count': truncated,
            'status_counts': dict(sorted(status_counts.items())),
            'technical_label_counts': dict(sorted(technical_counts.items())),
            'root_scope_counts': dict(sorted(scope_counts.items())),
            'root_domain_counts': dict(sorted(domain_counts.items())),
            'root_label_counts': dict(sorted(label_counts.items())),
            'overall_confidence_counts': dict(
                sorted(confidence_counts.items())
            ),
            'issue_counts': dict(sorted(issue_counts.items())),
        }

    def import_annotations(
        self,
        payloads: Iterable[Mapping[str, Any]],
        *,
        annotator_id: str,
        dry_run: bool,
    ) -> Dict[str, Any]:
        """Validate a JSONL batch completely before creating revisions."""

        values = [dict(payload) for payload in payloads]
        prepared: List[tuple[Dict[str, Any], str]] = []
        errors: List[Dict[str, Any]] = []
        for index, payload in enumerate(values, start=1):
            case_id = str(payload.get('case_id') or '')
            try:
                if not case_id:
                    raise ValueError('case_id is required')
                status = str(payload.get('annotation_status') or '')
                if status not in _IMPORTABLE_STATUSES:
                    raise ValueError(
                        'Only submitted-like annotations can be imported.'
                    )
                case = self.get_case(case_id)
                latest = case.get('latest_annotation')
                if isinstance(latest, Mapping) and latest.get(
                    'annotation_status'
                ) == 'frozen':
                    raise ValueError('A frozen annotation cannot be modified.')
                current_hash = str(
                    dict(case.get('source_snapshot') or {}).get('source_hash')
                    or ''
                )
                imported_hash = str(
                    dict(payload.get('source_snapshot') or {}).get('source_hash')
                    or ''
                )
                if not imported_hash or imported_hash != current_hash:
                    raise SourceSnapshotConflict(
                        'The imported source hash does not match the current Case.'
                    )
                normalized = validate_annotation(payload, case, strict=True)
                prepared.append((normalized, current_hash))
            except AnnotationValidationError as error:
                errors.append(
                    {
                        'index': index,
                        'case_id': case_id or None,
                        'message': 'Annotation validation failed.',
                        'field_errors': error.field_errors,
                    }
                )
            except (KeyError, ValueError, SourceSnapshotConflict) as error:
                errors.append(
                    {
                        'index': index,
                        'case_id': case_id or None,
                        'message': str(error),
                    }
                )

        can_apply = not errors and bool(prepared)
        imported_count = 0
        if can_apply and not dry_run:
            for normalized, source_hash in prepared:
                self.save_annotation(
                    normalized,
                    annotator_id=annotator_id,
                    change_reason='Imported from annotations.jsonl',
                    expected_source_hash=source_hash,
                )
                imported_count += 1
        return {
            'dry_run': dry_run,
            'can_apply': can_apply,
            'total_count': len(values),
            'importable_count': len(prepared),
            'imported_count': imported_count,
            'errors': errors,
        }

    def freeze_dataset(
        self,
        *,
        dataset_version: str,
        annotator_id: str,
        dry_run: bool,
        test_ratio: float = 0.4,
    ) -> Dict[str, Any]:
        """Freeze adjudicated labels and build a self-contained runner bundle."""

        if not _DATASET_VERSION.fullmatch(dataset_version):
            raise ValueError(
                'dataset_version must use 1-64 letters, numbers, dots, dashes, or underscores.'
            )
        if not 0.0 < test_ratio < 1.0:
            raise ValueError('test_ratio must be greater than 0 and less than 1.')

        rows = self._case_rows_with_latest()
        blockers: List[Dict[str, Any]] = []
        eligible: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
        excluded_count = 0
        for envelope, annotation in rows:
            case_id = str(dict(envelope.get('case') or {}).get('case_id') or '')
            status = str(
                (annotation or {}).get('annotation_status') or 'candidate'
            )
            if status == 'excluded':
                excluded_count += 1
                continue
            if status in {'adjudicated', 'frozen'} and annotation is not None:
                eligible.append((envelope, annotation))
                continue
            blockers.append(
                {
                    'case_id': case_id,
                    'status': status,
                    'reason': 'Case must be adjudicated or excluded before dataset freeze.',
                }
            )

        can_freeze = bool(eligible) and not blockers
        preview: Dict[str, Any] = {
            'dry_run': dry_run,
            'can_freeze': can_freeze,
            'dataset_version': dataset_version,
            'frozen_case_count': len(eligible),
            'excluded_case_count': excluded_count,
            'blockers': blockers,
        }
        if not can_freeze:
            return preview
        if dry_run:
            return preview

        for _, annotation in eligible:
            if annotation.get('annotation_status') == 'frozen':
                continue
            frozen = dict(annotation)
            frozen['annotation_status'] = 'frozen'
            self.save_annotation(
                frozen,
                annotator_id=annotator_id,
                change_reason='Dataset freeze %s' % dataset_version,
                expected_source_hash=str(
                    dict(annotation.get('source_snapshot') or {}).get(
                        'source_hash'
                    )
                    or ''
                ),
            )

        frozen_rows: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
        for envelope, _ in eligible:
            case_id = str(dict(envelope.get('case') or {}).get('case_id') or '')
            frozen = self.latest_annotation(case_id)
            if frozen is None or frozen.get('annotation_status') != 'frozen':
                raise RuntimeError('Dataset freeze did not persist every Case.')
            frozen_rows.append((envelope, frozen))
        bundle = _build_freeze_bundle(
            frozen_rows,
            dataset_version=dataset_version,
            test_ratio=test_ratio,
            excluded_count=excluded_count,
        )
        return {**preview, **bundle, 'dry_run': False}

    def _case_rows_with_latest(
        self,
    ) -> List[tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
        with self._connect() as connection:
            rows = connection.execute(
                '''
                SELECT c.payload_json AS case_json,
                       a.payload_json AS annotation_json
                FROM cases c
                LEFT JOIN annotations a ON a.case_id = c.case_id
                  AND a.revision = (
                    SELECT MAX(a2.revision) FROM annotations a2
                    WHERE a2.case_id = c.case_id
                  )
                ORDER BY c.case_id
                '''
            ).fetchall()
        return [
            (
                json.loads(row['case_json']),
                json.loads(row['annotation_json'])
                if row['annotation_json']
                else None,
            )
            for row in rows
        ]


class SourceSnapshotConflict(RuntimeError):
    """Raised when a submit is based on an obsolete source snapshot."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _annotation_summary(
    annotation: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    if annotation is None:
        return {}
    technical = dict(annotation.get('technical_error_review') or {})
    semantic = dict(annotation.get('semantic_outcome') or {})
    attribution = dict(annotation.get('attribution') or {})
    review = dict(annotation.get('review') or {})
    return {
        'technical_label': technical.get('human_label'),
        'is_agent_failure': semantic.get('is_agent_failure'),
        'is_task_failure': semantic.get('is_task_failure'),
        'recovery_status': semantic.get('recovery_status'),
        'root_scope': attribution.get('root_cause_scope'),
        'root_domain': attribution.get('root_cause_domain'),
        'root_label': attribution.get('root_cause_label'),
        'overall_confidence': review.get('overall_confidence'),
    }


def _count_value(counter: Counter[str], value: Any) -> None:
    if value not in (None, ''):
        counter[str(value)] += 1


def _build_freeze_bundle(
    rows: List[tuple[Dict[str, Any], Dict[str, Any]]],
    *,
    dataset_version: str,
    test_ratio: float,
    excluded_count: int,
) -> Dict[str, Any]:
    annotations: List[Dict[str, Any]] = []
    cases: List[Dict[str, Any]] = []
    traces_by_id: Dict[str, Dict[str, Any]] = {}
    observations_by_id: Dict[tuple[str, str], Dict[str, Any]] = {}
    case_ids_by_trace: Dict[str, List[str]] = {}
    scenario_counts: Counter[str] = Counter()

    for envelope, annotation in rows:
        case = dict(envelope.get('case') or {})
        trace = dict(envelope.get('trace') or {})
        case_id = str(case.get('case_id') or '')
        trace_id = str(trace.get('trace_id') or case.get('trace_id') or '')
        annotations.append(annotation)
        cases.append(case)
        traces_by_id[trace_id] = trace
        case_ids_by_trace.setdefault(trace_id, []).append(case_id)
        scenario_counts[str(envelope.get('scenario') or 'production')] += 1
        for observation in envelope.get('observations') or []:
            value = dict(observation)
            observation_id = str(
                value.get('observation_id') or value.get('id') or ''
            )
            observations_by_id[(trace_id, observation_id)] = value

    dev_case_ids: List[str] = []
    test_case_ids: List[str] = []
    dev_trace_count = 0
    test_trace_count = 0
    threshold = int(test_ratio * 10_000)
    for trace_id in sorted(case_ids_by_trace):
        bucket = int(sha256(trace_id.encode('utf-8')).hexdigest()[:8], 16) % 10_000
        target = test_case_ids if bucket < threshold else dev_case_ids
        target.extend(sorted(case_ids_by_trace[trace_id]))
        if target is test_case_ids:
            test_trace_count += 1
        else:
            dev_trace_count += 1

    files = {
        'traces.jsonl': _jsonl(traces_by_id.values()),
        'observations.jsonl': _jsonl(observations_by_id.values()),
        'cases.jsonl': _jsonl(cases),
        'annotations.jsonl': _jsonl(annotations),
        'splits/dev.txt': ''.join('%s\n' % value for value in dev_case_ids),
        'splits/test.txt': ''.join('%s\n' % value for value in test_case_ids),
    }
    file_hashes = {
        name: sha256(content.encode('utf-8')).hexdigest()
        for name, content in files.items()
    }
    summaries = [_annotation_summary(value) for value in annotations]
    manifest = {
        'dataset_version': dataset_version,
        'annotation_schema_version': 'failure-attribution-annotation-v2',
        'frozen_at': _now(),
        'case_count': len(cases),
        'trace_count': len(traces_by_id),
        'observation_count': len(observations_by_id),
        'excluded_case_count': excluded_count,
        'scenario_counts': dict(sorted(scenario_counts.items())),
        'label_distribution': {
            'technical_label': _distribution(summaries, 'technical_label'),
            'root_scope': _distribution(summaries, 'root_scope'),
            'root_domain': _distribution(summaries, 'root_domain'),
            'root_label': _distribution(summaries, 'root_label'),
        },
        'split': {
            'strategy': 'sha256_trace_id',
            'test_ratio': test_ratio,
            'dev_case_count': len(dev_case_ids),
            'test_case_count': len(test_case_ids),
            'dev_trace_count': dev_trace_count,
            'test_trace_count': test_trace_count,
            'trace_leakage_count': 0,
        },
        'file_hashes': file_hashes,
        'known_limitations': [
            'Split balance is deterministic but not stratified by label.',
            'Synthetic and demo Cases must not enter production accuracy denominators.',
            'Trace-external roots remain references rather than local root labels.',
        ],
    }
    files['manifest.json'] = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + '\n'
    return {'manifest': manifest, 'files': files}


def _jsonl(values: Iterable[Mapping[str, Any]]) -> str:
    return ''.join(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n'
        for value in sorted(
            values,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    )


def _distribution(values: Iterable[Mapping[str, Any]], key: str) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for value in values:
        _count_value(counter, value.get(key))
    return dict(sorted(counter.items()))
