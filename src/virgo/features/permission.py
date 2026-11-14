"""Deprecated permission import path; enforcement now belongs to Core.

Use ``virgo.harness.permission_policy`` for policy types and
``virgo.plugins.auto_review`` for the optional model reviewer.
"""
from ..harness.permission_policy import (  # noqa: F401
    MODES,
    Permissions,
    ReviewDecision,
    ReviewFailure,
    describe,
    normalize_permission_mode,
    parse_rule,
    permission_mode_label,
    plan_tool_allowed,
    rule_matches,
)


def review_tool_call(session, event, boundary_reason):
    """Adapt the former direct-session helper during the compatibility window."""
    from ..harness.extensions import ExtensionAPI
    from ..plugins.auto_review import review_tool_call as review

    return review(ExtensionAPI(session, "permission-compat"), event, boundary_reason)


def setup(api):
    from ..plugins.auto_review import setup as install_review

    install_review(api)
