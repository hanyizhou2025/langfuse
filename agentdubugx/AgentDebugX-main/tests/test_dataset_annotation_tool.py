from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, Dict
from zipfile import ZipFile

import pytest

from dataset.annotation_tool.demo import build_demo_dataset
from dataset.annotation_tool.app import AnnotationApplication
from dataset.annotation_tool.repository import AnnotationRepository
from dataset.annotation_tool.repository import SourceSnapshotConflict
from dataset.annotation_tool.source import _truncate_observations
from dataset.annotation_tool.validation import AnnotationValidationError, validate_annotation


def _case() -> Dict[str, Any]:
    return build_demo_dataset()[0]


def _valid_annotation(case: Dict[str, Any]) -> Dict[str, Any]:
    failure_id = case['case']['tool_attempt']['tool_result_observation_id']
    root_id = next(
        item['observation_id']
        for item in case['observations']
        if item['observation_id'] != failure_id
    )
    return {
        'schema_version': 'failure-attribution-annotation-v2',
        'case_id': case['case']['case_id'],
        'project_id': case['project_id'],
        'trace_id': case['trace']['trace_id'],
        'annotation_status': 'submitted',
        'technical_error_review': {
            'human_label': 'confirmed',
            'confidence': 'high',
            'comment': '',
        },
        'semantic_outcome': {
            'tool_result_semantics': 'unexpected_failure',
            'is_agent_failure': True,
            'is_task_failure': None,
            'recovery_status': 'not_recovered',
            'outcome_reason': 'The read failed.',
            'confidence': 'medium',
        },
        'failure_manifestation': {
            'failure_onset_observation_id': failure_id,
            'primary_failure_observation_id': failure_id,
            'failure_observation_ids': [failure_id],
            'downstream_symptom_observation_ids': [],
        },
        'attribution': {
            'applicable': True,
            'root_cause_scope': 'current_trace',
            'primary_root_cause_observation_id': root_id,
            'root_cause_observation_ids': [root_id],
            'root_cause_domain': 'model',
            'root_cause_label': 'model_path_hallucination',
            'path_source_kind': 'model',
            'path_source_observation_id': root_id,
            'confidence': 'medium',
        },
        'current_trace_reference': {
            'earliest_local_evidence_observation_id': root_id,
            'local_trigger_observation_id': root_id,
            'propagation_observation_ids': [root_id, failure_id],
            'evidence_observation_ids': [root_id, failure_id],
            'reference_summary': 'The model supplied the missing path.',
            'confidence': 'medium',
        },
        'review': {
            'evidence_observation_ids': [root_id, failure_id],
            'reasoning_summary': 'The model introduced the path before the failed read.',
            'limitations': ['downstream_outcome_unclear'],
            'overall_confidence': 'medium',
            'notes': '',
        },
        'source_snapshot': case['source_snapshot'],
    }


def _adjudicated_annotation(case: Dict[str, Any]) -> Dict[str, Any]:
    annotation = _valid_annotation(case)
    annotation['annotation_status'] = 'adjudicated'
    annotation['semantic_outcome']['is_task_failure'] = True
    return annotation


def test_valid_annotation_preserves_three_state_booleans() -> None:
    case = _case()
    annotation = _valid_annotation(case)

    result = validate_annotation(annotation, case, strict=True)

    assert result['semantic_outcome']['is_agent_failure'] is True
    assert result['semantic_outcome']['is_task_failure'] is None
    assert result['annotation_status'] == 'needs_review'


def test_rule_false_positive_rejects_failure_and_root_fields() -> None:
    case = _case()
    annotation = _valid_annotation(case)
    annotation['technical_error_review'] = {
        'human_label': 'rule_false_positive',
        'correction_reason': 'successful_content_mentions_error',
        'confidence': 'high',
    }

    with pytest.raises(AnnotationValidationError) as error:
        validate_annotation(annotation, case, strict=True)

    assert 'failure_manifestation' in error.value.field_errors


def test_recovered_requires_recovery_observation() -> None:
    case = _case()
    annotation = _valid_annotation(case)
    annotation['semantic_outcome']['recovery_status'] = 'recovered'

    with pytest.raises(AnnotationValidationError) as error:
        validate_annotation(annotation, case, strict=True)

    assert 'semantic_outcome.recovery_observation_id' in error.value.field_errors


def test_outside_current_trace_forbids_root_ids() -> None:
    case = _case()
    annotation = _valid_annotation(case)
    annotation['attribution']['root_cause_scope'] = 'outside_current_trace'
    annotation['attribution']['applicable'] = False

    with pytest.raises(AnnotationValidationError) as error:
        validate_annotation(annotation, case, strict=True)

    assert 'attribution.primary_root_cause_observation_id' in error.value.field_errors


def test_current_trace_requires_complete_root_fields() -> None:
    case = _case()
    annotation = _valid_annotation(case)
    annotation['attribution']['root_cause_domain'] = None

    with pytest.raises(AnnotationValidationError) as error:
        validate_annotation(annotation, case, strict=True)

    assert 'attribution.root_cause_domain' in error.value.field_errors


def test_cross_trace_observation_reference_is_rejected() -> None:
    case = _case()
    annotation = _valid_annotation(case)
    annotation['review']['evidence_observation_ids'].append('another-trace-node')

    with pytest.raises(AnnotationValidationError) as error:
        validate_annotation(annotation, case, strict=True)

    assert 'review.evidence_observation_ids' in error.value.field_errors


def test_propagation_chain_must_include_failure_and_be_ordered() -> None:
    case = _case()
    annotation = _valid_annotation(case)
    chain = annotation['current_trace_reference']['propagation_observation_ids']
    annotation['current_trace_reference']['propagation_observation_ids'] = list(
        reversed(chain)
    )

    with pytest.raises(AnnotationValidationError) as error:
        validate_annotation(annotation, case, strict=True)

    assert (
        'current_trace_reference.propagation_observation_ids'
        in error.value.field_errors
    )


def test_repository_creates_revisions_and_reads_history(tmp_path: Path) -> None:
    case = _case()
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases([case])
    annotation = _valid_annotation(case)

    first = repository.save_annotation(annotation, annotator_id='tester')
    annotation['review']['notes'] = 'second revision'
    second = repository.save_annotation(annotation, annotator_id='tester')

    assert first['annotation_revision'] == 1
    assert second['annotation_revision'] == 2
    assert len(repository.annotation_history(annotation['case_id'])) == 2


def test_repository_reads_complete_historical_revision(tmp_path: Path) -> None:
    case = _case()
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases([case])
    annotation = _valid_annotation(case)
    first = repository.save_annotation(annotation, annotator_id='tester')
    annotation['review']['notes'] = 'second revision'
    repository.save_annotation(annotation, annotator_id='tester')

    historical = repository.get_annotation(annotation['case_id'], 1)

    assert historical == first
    assert historical['review']['notes'] == ''


def test_frozen_annotation_cannot_be_modified(tmp_path: Path) -> None:
    case = _case()
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases([case])
    annotation = _valid_annotation(case)
    annotation['annotation_status'] = 'frozen'
    repository.save_annotation(annotation, annotator_id='tester')

    with pytest.raises(ValueError, match='frozen'):
        repository.save_annotation(annotation, annotator_id='tester')


def test_export_uses_latest_non_draft_revision(tmp_path: Path) -> None:
    case = _case()
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases([case])
    annotation = _valid_annotation(case)
    repository.save_annotation(annotation, annotator_id='tester')
    annotation['annotation_status'] = 'draft'
    annotation['review']['notes'] = 'unfinished edit'
    repository.save_annotation(annotation, annotator_id='tester')

    exported = [json.loads(line) for line in repository.export_jsonl().splitlines()]

    assert len(exported) == 1
    assert exported[0]['annotation_revision'] == 1


def test_case_list_exposes_latest_label_summary(tmp_path: Path) -> None:
    case = _case()
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases([case])
    repository.save_annotation(_valid_annotation(case), annotator_id='tester')

    listed = repository.list_cases()[0]

    assert listed['label_summary'] == {
        'technical_label': 'confirmed',
        'is_agent_failure': True,
        'is_task_failure': None,
        'recovery_status': 'not_recovered',
        'root_scope': 'current_trace',
        'root_domain': 'model',
        'root_label': 'model_path_hallucination',
        'overall_confidence': 'medium',
    }


def test_quality_report_counts_statuses_and_label_dimensions(tmp_path: Path) -> None:
    cases = build_demo_dataset()[:2]
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases(cases)
    repository.save_annotation(_valid_annotation(cases[0]), annotator_id='tester')

    report = repository.quality_report()

    assert report['case_count'] == 2
    assert report['annotated_case_count'] == 1
    assert report['exportable_count'] == 1
    assert report['status_counts'] == {'candidate': 1, 'needs_review': 1}
    assert report['technical_label_counts'] == {'confirmed': 1}
    assert report['root_scope_counts'] == {'current_trace': 1}
    assert report['issue_counts']['unannotated'] == 1
    assert report['issue_counts']['needs_review'] == 1


def test_annotation_import_supports_dry_run_and_atomic_apply(tmp_path: Path) -> None:
    cases = build_demo_dataset()[:2]
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases(cases)
    annotations = [_adjudicated_annotation(case) for case in cases]

    preview = repository.import_annotations(
        annotations,
        annotator_id='importer',
        dry_run=True,
    )

    assert preview['can_apply'] is True
    assert preview['importable_count'] == 2
    assert preview['imported_count'] == 0
    assert repository.annotation_history(annotations[0]['case_id']) == []

    applied = repository.import_annotations(
        annotations,
        annotator_id='importer',
        dry_run=False,
    )

    assert applied['imported_count'] == 2
    assert repository.latest_annotation(annotations[0]['case_id'])['audit'][
        'annotator_id'
    ] == 'importer'


def test_annotation_import_rejects_batch_without_partial_writes(tmp_path: Path) -> None:
    cases = build_demo_dataset()[:2]
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases(cases)
    valid = _adjudicated_annotation(cases[0])
    invalid = _adjudicated_annotation(cases[1])
    invalid['source_snapshot']['source_hash'] = 'obsolete'

    result = repository.import_annotations(
        [valid, invalid],
        annotator_id='importer',
        dry_run=False,
    )

    assert result['can_apply'] is False
    assert result['imported_count'] == 0
    assert result['errors'][0]['case_id'] == invalid['case_id']
    assert repository.annotation_history(valid['case_id']) == []


def test_dataset_freeze_builds_runner_files_hashes_and_trace_safe_splits(
    tmp_path: Path,
) -> None:
    cases = build_demo_dataset()[:2]
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases(cases)
    for case in cases:
        repository.save_annotation(
            _adjudicated_annotation(case),
            annotator_id='adjudicator',
        )

    preview = repository.freeze_dataset(
        dataset_version='fnf-test-v1',
        annotator_id='freezer',
        dry_run=True,
        test_ratio=0.5,
    )

    assert preview['can_freeze'] is True
    assert preview['frozen_case_count'] == 2
    assert repository.latest_annotation(cases[0]['case']['case_id'])[
        'annotation_status'
    ] == 'adjudicated'

    result = repository.freeze_dataset(
        dataset_version='fnf-test-v1',
        annotator_id='freezer',
        dry_run=False,
        test_ratio=0.5,
    )

    assert result['manifest']['dataset_version'] == 'fnf-test-v1'
    assert result['manifest']['annotation_schema_version'] == (
        'failure-attribution-annotation-v2'
    )
    assert set(result['files']) == {
        'annotations.jsonl',
        'cases.jsonl',
        'manifest.json',
        'observations.jsonl',
        'splits/dev.txt',
        'splits/test.txt',
        'traces.jsonl',
    }
    assert result['manifest']['file_hashes']['annotations.jsonl']
    for name, digest in result['manifest']['file_hashes'].items():
        assert sha256(result['files'][name].encode('utf-8')).hexdigest() == digest
    dev_cases = set(result['files']['splits/dev.txt'].splitlines())
    test_cases = set(result['files']['splits/test.txt'].splitlines())
    assert dev_cases.isdisjoint(test_cases)
    assert dev_cases | test_cases == {
        case['case']['case_id'] for case in cases
    }
    assert repository.latest_annotation(cases[0]['case']['case_id'])[
        'annotation_status'
    ] == 'frozen'


def test_dataset_freeze_keeps_cases_from_one_trace_in_one_split(
    tmp_path: Path,
) -> None:
    first = _case()
    second = deepcopy(first)
    second['case']['case_id'] = 'demo-current-root-second-candidate'
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases([first, second])
    for case in (first, second):
        repository.save_annotation(
            _adjudicated_annotation(case),
            annotator_id='adjudicator',
        )

    result = repository.freeze_dataset(
        dataset_version='fnf-trace-split-v1',
        annotator_id='freezer',
        dry_run=False,
        test_ratio=0.5,
    )

    dev_cases = set(result['files']['splits/dev.txt'].splitlines())
    test_cases = set(result['files']['splits/test.txt'].splitlines())
    expected = {
        first['case']['case_id'],
        second['case']['case_id'],
    }
    assert expected <= dev_cases or expected <= test_cases
    assert result['manifest']['split']['trace_leakage_count'] == 0


def test_dataset_freeze_is_blocked_by_unreviewed_cases(tmp_path: Path) -> None:
    case = _case()
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases([case])
    repository.save_annotation(_valid_annotation(case), annotator_id='tester')

    result = repository.freeze_dataset(
        dataset_version='fnf-test-v1',
        annotator_id='freezer',
        dry_run=True,
    )

    assert result['can_freeze'] is False
    assert result['blockers'][0]['status'] == 'needs_review'


def test_demo_dataset_covers_required_acceptance_scenarios() -> None:
    scenarios = {item['scenario'] for item in build_demo_dataset()}

    assert scenarios == {
        'current_trace_root',
        'outside_current_trace',
        'recovered_failure',
        'rule_false_positive',
        'unknown',
    }


def test_false_positive_can_be_submitted_without_failure_fields() -> None:
    case = _case()
    annotation = _valid_annotation(case)
    annotation['technical_error_review'] = {
        'human_label': 'rule_false_positive',
        'correction_reason': 'successful_content_mentions_error',
        'confidence': 'high',
    }
    annotation['semantic_outcome'] = {}
    annotation['failure_manifestation'] = {}
    annotation['attribution'] = {'applicable': False, 'root_cause_scope': 'unknown'}
    annotation['current_trace_reference'] = {}
    annotation['review']['evidence_observation_ids'] = []

    result = validate_annotation(annotation, case, strict=True)

    assert result['annotation_status'] == 'submitted'


def test_source_snapshot_conflict_is_rejected(tmp_path: Path) -> None:
    case = _case()
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    repository.replace_cases([case])

    with pytest.raises(SourceSnapshotConflict):
        repository.save_annotation(
            _valid_annotation(case),
            annotator_id='tester',
            expected_source_hash='obsolete',
        )


def test_truncation_always_keeps_failure_observation() -> None:
    observations = [
        {'observation_id': 'one', 'start_time': '1'},
        {'observation_id': 'two', 'start_time': '2'},
        {'observation_id': 'failure', 'start_time': '3'},
    ]

    result = _truncate_observations(observations, limit=2, failure_id='failure')

    assert [item['observation_id'] for item in result] == ['one', 'failure']


def test_application_routes_cases_and_saves_annotation(tmp_path: Path) -> None:
    cases = build_demo_dataset()
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    application = AnnotationApplication(
        repository,
        lambda: cases,
        annotator_id='api-tester',
        demo_mode=True,
    )
    assert application.sync()['case_count'] == 5

    list_response = application.handle('GET', '/api/cases', {}, None)
    listed = json.loads(list_response.body)
    assert len(listed['cases']) == 5

    case = cases[0]
    detail_response = application.handle(
        'GET', '/api/cases/%s' % case['case']['case_id'], {}, None
    )
    assert json.loads(detail_response.body)['scenario'] == 'current_trace_root'

    annotation = _valid_annotation(case)
    save_response = application.handle(
        'POST',
        '/api/cases/%s/annotations' % case['case']['case_id'],
        {},
        {
            'annotation': annotation,
            'expected_source_hash': case['source_snapshot']['source_hash'],
        },
    )
    saved = json.loads(save_response.body)['annotation']
    assert save_response.status == 201
    assert saved['audit']['annotator_id'] == 'api-tester'

    revision_response = application.handle(
        'GET',
        '/api/cases/%s/annotations/%d'
        % (case['case']['case_id'], saved['annotation_revision']),
        {},
        None,
    )
    assert json.loads(revision_response.body)['annotation'] == saved

    quality_response = application.handle('GET', '/api/quality', {}, None)
    assert json.loads(quality_response.body)['case_count'] == 5


def test_application_imports_annotations_and_downloads_frozen_archive(
    tmp_path: Path,
) -> None:
    case = _case()
    repository = AnnotationRepository(tmp_path / 'annotations.sqlite')
    application = AnnotationApplication(
        repository,
        lambda: [case],
        annotator_id='api-tester',
        demo_mode=True,
    )
    application.sync()
    annotation = _adjudicated_annotation(case)

    import_response = application.handle(
        'POST',
        '/api/import',
        {},
        {'annotations': [annotation], 'dry_run': False},
    )

    assert json.loads(import_response.body)['imported_count'] == 1

    freeze_response = application.handle(
        'POST',
        '/api/freeze',
        {},
        {
            'dataset_version': 'fnf-api-v1',
            'dry_run': False,
            'test_ratio': 0.4,
        },
    )

    assert freeze_response.status == 200
    assert freeze_response.content_type == 'application/zip'
    with ZipFile(BytesIO(freeze_response.body)) as archive:
        assert set(archive.namelist()) == {
            'annotations.jsonl',
            'cases.jsonl',
            'manifest.json',
            'observations.jsonl',
            'splits/dev.txt',
            'splits/test.txt',
            'traces.jsonl',
        }
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['dataset_version'] == 'fnf-api-v1'


class _IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids = set()
        self.external_assets = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        values = dict(attrs)
        if values.get('id'):
            self.ids.add(values['id'])
        asset = values.get('src') or values.get('href')
        if asset and '://' in asset:
            self.external_assets.append(asset)


def test_frontend_declares_every_primary_control_without_external_assets() -> None:
    static_dir = (
        Path(__file__).resolve().parents[1]
        / 'dataset'
        / 'annotation_tool'
        / 'static'
    )
    parser = _IdCollector()
    parser.feed((static_dir / 'index.html').read_text(encoding='utf-8'))

    assert not parser.external_assets
    assert {
        'syncButton',
        'exportButton',
        'refreshCasesButton',
        'locateFailureButton',
        'locateParentButton',
        'locateNextButton',
        'previousStepButton',
        'nextStepButton',
        'newRevisionButton',
        'resetButton',
        'excludeButton',
        'saveDraftButton',
        'submitNextButton',
        'autosaveToggle',
        'importButton',
        'importFile',
        'qualityButton',
        'freezeDatasetButton',
        'adjudicateButton',
        'freezeCaseButton',
        'revisionDialog',
        'qualityDialog',
        'applyImportButton',
        'transitionDialog',
        'confirmTransitionButton',
        'freezeDialog',
        'previewFreezeButton',
        'applyFreezeButton',
    } <= parser.ids
