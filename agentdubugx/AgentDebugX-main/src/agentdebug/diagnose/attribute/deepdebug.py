"""Backward-compatible DeepDebug import path.

DeepDebug is a complete Diagnose profile, not an Attribute-stage component.
New code should import it from :mod:`agentdebug.diagnose.profiles.deepdebug`.
"""

from agentdebug.diagnose.profiles.deepdebug import (
    DeepDebugAnalyzer,
    DeepDebugDiagnosis,
    DeepDebugEvidence,
    DeepDebugResult,
    DeepDebugRound,
)

__all__ = [
    'DeepDebugAnalyzer',
    'DeepDebugDiagnosis',
    'DeepDebugEvidence',
    'DeepDebugResult',
    'DeepDebugRound',
]
