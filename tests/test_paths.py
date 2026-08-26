"""Deterministic filesystem-location policy."""

from pathlib import Path

from automationctl import paths


def test_state_dir_uses_the_xdg_state_home() -> None:
    assert paths.state_dir({"XDG_STATE_HOME": "/var/lib/example"}) == Path(
        "/var/lib/example/automationctl"
    )


def test_state_dir_has_no_automationctl_specific_override() -> None:
    env = {
        "XDG_STATE_HOME": "/var/lib/example",
        "AUTOMATIONCTL_STATE_DIR": "/tmp/legacy-override",
    }

    assert paths.state_dir(env) == Path("/var/lib/example/automationctl")
