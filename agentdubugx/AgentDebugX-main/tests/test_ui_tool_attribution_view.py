from typing import Optional

import pytest

from agentdebug.integrations.langfuse_attribution import (
    file_not_found_attribution_to_dict,
)
from agentdebug.inspect.ui.views import render_tool_attribution_page
from agentdebug.schema import AgentTrajectory


@pytest.mark.parametrize(
    ('decision', 'semantics', 'label', 'reason_code', 'expected_status'),
    [
        (
            'attributed',
            'unexpected_failure',
            'model_path_hallucination',
            'task_terminated_after_tool_error',
            'Root cause attributed',
        ),
        (
            'not_agent_failure',
            'validation_probe',
            None,
            'expected_error_used_for_validation',
            'Not an agent failure',
        ),
        (
            'unknown',
            'unknown',
            None,
            'insufficient_downstream_context',
            'Needs human review',
        ),
    ],
)
def test_render_tool_attribution_page_displays_file_not_found_decision(
    failed_trajectory: AgentTrajectory,
    decision: str,
    semantics: str,
    label: Optional[str],
    reason_code: str,
    expected_status: str,
) -> None:
    failed_trajectory.metadata['langfuse_file_not_found_attributions'] = [
        {
            'case_id': 'case-one',
            'trace_id': failed_trajectory.trace_id,
            'decision': decision,
            'semantics': semantics,
            'is_agent_failure': decision == 'attributed',
            'failure_observation_id': 'evt_tool',
            'root_cause_observation_id': 'evt_plan' if label else None,
            'root_cause_label': label,
            'root_cause_domain': 'model' if label else None,
            'evidence_observation_ids': ['evt_plan', 'evt_tool'],
            'confidence': 0.95 if label else 0.0,
            'reason_codes': [reason_code],
            'attribution_method': 'llm_enhanced',
            'model': 'public-model-mini',
            'total_observation_count': 1200,
            'reviewed_observation_count': 48,
            'llm_prompt_tokens': 2400,
            'llm_completion_tokens': 180,
            'root_cause_scope': 'outside_current_trace',
            'earliest_local_evidence_observation_id': 'evt_context',
            'local_trigger_observation_id': 'evt_plan',
            'propagation_observation_ids': [
                'evt_context',
                'evt_plan',
                'evt_tool',
            ],
            'reference_confidence': 0.91,
        }
    ]

    page = render_tool_attribution_page(failed_trajectory)

    assert 'File Not Found MVP' in page
    assert expected_status in page
    assert semantics.replace('_', ' ') in page
    assert reason_code.replace('_', ' ') in page
    assert 'Failure observation' in page
    assert '/trace/trace_failed/event/evt_tool' in page
    assert 'LLM enhanced' in page
    assert 'public-model-mini' in page
    assert '48 / 1200 observations' in page
    assert '2400 + 180 tokens' in page
    assert 'Root cause scope' in page
    assert 'Outside current trace' in page
    assert 'Earliest local evidence' in page
    assert '/trace/trace_failed/event/evt_context' in page
    assert 'Local trigger' in page
    assert 'Reference chain' in page
    assert '91%' in page
    if label:
        assert label.replace('_', ' ') in page
        assert 'Root cause observation' in page
        assert '/trace/trace_failed/event/evt_plan' in page


def test_file_not_found_presentation_drops_unknown_metadata() -> None:
    summary = file_not_found_attribution_to_dict(
        {
            'case_id': 'case-safe',
            'trace_id': 'trace-safe',
            'decision': 'unknown',
            'raw_path': '/private/customer/secret.txt',
        }
    )

    assert 'raw_path' not in summary


def test_deterministic_attribution_does_not_show_zero_model_context(
    failed_trajectory: AgentTrajectory,
) -> None:
    failed_trajectory.metadata['langfuse_file_not_found_attributions'] = [
        {
            'case_id': 'case-deterministic',
            'trace_id': failed_trajectory.trace_id,
            'decision': 'unknown',
            'semantics': 'unknown',
            'is_agent_failure': None,
            'failure_observation_id': 'evt_tool',
            'attribution_method': 'deterministic',
            'model': None,
            'total_observation_count': 0,
            'reviewed_observation_count': 0,
        }
    ]

    page = render_tool_attribution_page(failed_trajectory)

    assert 'Model context</strong><span>Not applicable' in page
