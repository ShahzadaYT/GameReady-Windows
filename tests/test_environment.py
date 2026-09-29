"""Environment capability tests."""
from __future__ import annotations

import pytest

from travelready import environment as env_mod
from travelready.environment import (
    LIVE_NETWORK_REQUIRED, PURE_LOGIC, ROG_ALLY_REQUIRED, WINDOWS_REQUIRED,
    Capability, offline_environment, probe_network,
)


def test_pure_logic_is_always_satisfied():
    assert offline_environment().satisfies(PURE_LOGIC)


def test_unmet_requirements_report_why():
    env = offline_environment()
    assert not env.satisfies(WINDOWS_REQUIRED)
    assert "not Windows" in env.why_not(WINDOWS_REQUIRED)
    assert "not a ROG Ally" in env.why_not(ROG_ALLY_REQUIRED)
    assert "no internet" in env.why_not(LIVE_NETWORK_REQUIRED)


def test_a_met_requirement_has_no_reason():
    assert offline_environment().why_not(PURE_LOGIC) == ""


def test_capability_is_truthy_only_when_available():
    assert not Capability("x", False, "nope")
    assert Capability("x", True)


def test_detect_off_windows_is_honest_rather_than_failing():
    env = env_mod.detect(check_network=False)
    assert not env.windows
    assert env.notes and "off Windows" in env.notes[0]
    assert "Pure-logic features" in env.notes[0]


def test_network_probe_reports_the_real_reason_when_blocked():
    def blocked(url, timeout=None):
        raise OSError("CONNECT tunnel failed, response 403")

    capability = probe_network(opener=blocked)
    assert not capability
    assert "403" in capability.detail


def test_network_probe_succeeds_when_reachable():
    class Ok:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    capability = probe_network(opener=lambda url, timeout=None: Ok())
    assert capability
    assert "reachable" in capability.detail


def test_summary_covers_every_capability():
    summary = offline_environment().summary()
    assert set(summary) == {"Windows", "PowerShell", "Process table",
                            "ShellExecute", "Registry", "Internet", "ROG Ally"}
