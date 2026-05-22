"""Kill switch behaviour."""
from __future__ import annotations

import pytest

from src.core.exceptions import KillSwitchArmed
from src.core.kill_switch import KillSwitch, ensure_disarmed, get_kill_switch


def test_default_is_disarmed():
    ks = KillSwitch()
    assert ks.armed is False


def test_arm_sets_state_and_reason():
    ks = KillSwitch()
    ks.arm(reason="test reason")
    assert ks.armed is True
    assert ks.reason() == "test reason"


def test_disarm_clears_state():
    ks = KillSwitch()
    ks.arm(reason="x")
    ks.disarm()
    assert ks.armed is False
    assert ks.reason() == ""


def test_ensure_disarmed_raises_when_local_armed():
    ks = get_kill_switch()
    ks.arm(reason="unit test")
    try:
        with pytest.raises(KillSwitchArmed):
            ensure_disarmed()
    finally:
        ks.disarm()


def test_ensure_disarmed_raises_when_env_killed():
    with pytest.raises(KillSwitchArmed):
        ensure_disarmed(env_killed=True)


def test_ensure_disarmed_passes_when_clean():
    # Clean state
    ks = get_kill_switch()
    ks.disarm()
    # Should not raise
    ensure_disarmed(env_killed=False)
