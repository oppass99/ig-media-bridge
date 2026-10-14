"""
oneframe_cloud/gate.py  -- the global ONEFRAME cloud cutover kill switch.

Publishing is possible ONLY when ALL of these hold:

  1. state/control.json        ONEFRAME_CLOUD_CUTOVER == "ENABLED"  (committed file)
  2. repository variable       ONEFRAME_CLOUD_CUTOVER == "ENABLED"  (env of the run)
  3. control.json cutover_enabled_at is set
  4. state/local_reconcile.json  local_scheduler == "DISABLED_CONFIRMED" and
     generated_at >= cutover_enabled_at  (the local Module 5 history was
     exported AFTER the local scheduler stopped, so nothing it posted can be
     posted again)

Anything else -- missing file, typo, unset variable -- is DISABLED.
Default: DISABLED. The workflow does not even receive the Instagram secrets
unless the repository variable says ENABLED.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ENABLED = "ENABLED"
DISABLED = "DISABLED"


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _dt(s):
    try:
        d = datetime.fromisoformat(str(s))
        return d if d.tzinfo else None
    except (TypeError, ValueError):
        return None


class Gate:
    def __init__(self, enabled: bool, reasons: list):
        self.enabled = bool(enabled)
        self.reasons = list(reasons)

    @property
    def state(self) -> str:
        return ENABLED if self.enabled else DISABLED

    def assert_open(self, action: str):
        if not self.enabled:
            raise PermissionError(f"CUTOVER_DISABLED: {action} refused ({'; '.join(self.reasons)})")

    def to_dict(self) -> dict:
        return {"ONEFRAME_CLOUD_CUTOVER": self.state, "reasons": self.reasons}


def read_gate(state_dir, env: dict) -> Gate:
    sd = Path(state_dir)
    control = _read(sd / "control.json")
    reasons = []
    if control.get("ONEFRAME_CLOUD_CUTOVER") != ENABLED:
        reasons.append("control.json ONEFRAME_CLOUD_CUTOVER != ENABLED")
    if (env.get("ONEFRAME_CLOUD_CUTOVER") or "") != ENABLED:
        reasons.append("repository variable ONEFRAME_CLOUD_CUTOVER != ENABLED")
    enabled_at = _dt(control.get("cutover_enabled_at"))
    if enabled_at is None:
        reasons.append("cutover_enabled_at not set")
    rec = _read(sd / "local_reconcile.json")
    if rec.get("local_scheduler") != "DISABLED_CONFIRMED":
        reasons.append("local scheduler not confirmed DISABLED in local_reconcile.json")
    gen = _dt(rec.get("generated_at"))
    if gen is None or enabled_at is None or gen < enabled_at:
        reasons.append("local history reconcile missing or older than cutover")
    return Gate(not reasons, reasons)
