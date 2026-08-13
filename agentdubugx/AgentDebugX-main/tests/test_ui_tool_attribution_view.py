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
        }
    ]

    page = render_tool_attribution_page(failed_trajectory)

    assert 'File Not Found MVP' in page
    assert expected_status in page
    assert semantics.replace('_', ' ') in page
    assert reason_code.replace('_', ' ') in page
    assert 'Failure observation' in page
    assert '/trace/trace_failed/event/evt_tool' in page
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
