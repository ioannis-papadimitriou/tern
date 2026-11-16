"""Where tern keeps data, state, the bridge socket and config (architecture §1, decisions #22).

By default the platform dirs are used. ``TERN_HOME=<dir>`` relocates all four together:
``<dir>/data/``, ``<dir>/state/``, ``<dir>/run/`` (bridge socket) and ``<dir>/config/``.
Tests, smoke and dev sessions always set ``TERN_HOME`` to a synthetic location.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP = "tern"
SOCKET_NAME = "bridge.sock"
LOCK_NAME = "ternd.lock"


class PathsError(RuntimeError):
    """The environment does not determine a usable location; never guessed."""


@dataclass(frozen=True)
class TernPaths:
    data: Path
    state: Path
    run: Path
    config: Path

    @property
    def socket(self) -> Path:
        return self.run / SOCKET_NAME

    @property
    def lock(self) -> Path:
        return self.run / LOCK_NAME

    @property
    def config_file(self) -> Path:
        return self.config / "config.toml"


def resolve(env: dict[str, str] | None = None) -> TernPaths:
    """Resolve the four tern dirs from ``env`` (default: ``os.environ``).

    The runtime dir is ``$XDG_RUNTIME_DIR/tern`` exactly; if ``XDG_RUNTIME_DIR`` is unset
    this raises instead of falling back to a shared location (platformdirs would fall back
    silently, which could put the socket somewhere another user can reach).
    """
    e = os.environ if env is None else env
    home = e.get("TERN_HOME")
    if home is not None:
        if not home:
            raise PathsError("TERN_HOME is set but empty")
        base = Path(home)
        if not base.is_absolute():
            raise PathsError("TERN_HOME must be an absolute path")
        return TernPaths(
            data=base / "data", state=base / "state", run=base / "run", config=base / "config"
        )
    xdg_run = e.get("XDG_RUNTIME_DIR")
    if not xdg_run:
        raise PathsError("XDG_RUNTIME_DIR is not set; set it or TERN_HOME")
    return TernPaths(
        data=Path(platformdirs.user_data_dir(APP)),
        state=Path(platformdirs.user_state_dir(APP)),
        run=Path(xdg_run) / APP,
        config=Path(platformdirs.user_config_dir(APP)),
    )
