"""Crash-safe JSON state: strategy memory, risk counters and the paper account.

Paper and live keep separate files so a paper experiment never leaks into real trading.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from forex_bot.models import format_time, utcnow

log = logging.getLogger(__name__)
STATE_VERSION = 1


def state_path(base: Path, mode: str) -> Path:
    """``state/forex_bot_state.json`` becomes ``state/forex_bot_state.paper.json``."""
    return base.with_name(f"{base.stem}.{mode}{base.suffix or '.json'}")


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": STATE_VERSION}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state is not a JSON object")
            return data
        except (OSError, ValueError) as exc:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{utcnow():%Y%m%d%H%M%S}")
            self.path.replace(backup)
            log.error(
                "state file was unreadable (%s); moved it to %s and started fresh", exc, backup
            )
            return {"version": STATE_VERSION}

    def save(self, data: dict[str, Any]) -> None:
        data["version"] = STATE_VERSION
        data["updated"] = format_time(utcnow())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and rename, so a crash never leaves a half-written state.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
