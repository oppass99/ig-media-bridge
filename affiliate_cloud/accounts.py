"""
affiliate_cloud/accounts.py  -- the affiliate account registry and the per-account gate.

state/affiliate/accounts.json  (public; no secret in it)

  {"schema_version": "affiliate-cloud-1.0",
   "accounts": {"IG_SIDE_B99": {
       "channel_profile": "COUPANG_KR", "username": "side_b99", "language": "KO",
       "account_fingerprint": "<16 hex>",       # sha256 of the real ig-user-id, salted
       "cloud_publish": "DISABLED", "enabled_at": null, "paused": false,
       "INSTAGRAM_DAILY_LIMIT": null, "ALLOWED_POST_TIMES": [], "TIMEZONE": "Asia/Seoul",
       "min_spacing_hours": 4, "max_attempts": 3,
       "retry_backoff_minutes": 30, "retry_backoff_cap_minutes": 240}}}

An account can publish ONLY when ALL of these hold (anything else -> closed):

  1. accounts.json cloud_publish == "ENABLED" and enabled_at is set
  2. repository variable AFFILIATE_CLOUD_<ACCOUNT> == "ENABLED"
  3. paused is false                                  (manual PAUSE kill switch)
  4. no state/affiliate/<ACCOUNT>/halt.json           (auth / restriction stop)
  5. local_reconcile.json local_publisher == "CLOUD_ONLY_CONFIRMED" and
     generated_at >= enabled_at  (local Module 5 will not post to this account)
  6. INSTAGRAM_DAILY_LIMIT >= 1, ALLOWED_POST_TIMES non-empty, fingerprint set
  7. the account and its channel profile match ROUTES exactly

Secrets per account (GitHub Secrets only, passed to the job only while the
repository variable says ENABLED):
  AFFILIATE_<ACCOUNT>_ACCESS_TOKEN  AFFILIATE_<ACCOUNT>_USER_ID  AFFILIATE_<ACCOUNT>_MEDIA_KEY_B64
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from oneframe_cloud.gate import ENABLED, Gate

from . import FOREIGN_ACCOUNTS, ROUTES, SCHEMA_VERSION

ROOT_DIR = "affiliate"
LOCAL_CONFIRMED = "CLOUD_ONLY_CONFIRMED"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Meta error codes that stop the WHOLE account queue (never retried automatically).
# Conservative on purpose: halting on a code that turns out harmless costs one
# manual resume; retrying into a restriction is what must never happen.
AUTH_CODES = {190, 102, 10, 3}                 # invalid/expired token, session, permission
RESTRICTION_CODES = {368}                      # temporarily blocked (policy)
RESTRICTION_SUBCODES = {2207050, 2207051}      # account inactive/checkpointed/restricted; action blocked
RATE_CODES = {4, 17, 32, 613}                  # app / user / page rate limits
RATE_SUBCODES = {2207042}                      # content publishing limit reached


def env_names(account: str) -> dict:
    return {"variable": f"AFFILIATE_CLOUD_{account}",
            "token": f"AFFILIATE_{account}_ACCESS_TOKEN",
            "user_id": f"AFFILIATE_{account}_USER_ID",
            "media_key": f"AFFILIATE_{account}_MEDIA_KEY_B64"}


def fingerprint(ig_user_id: str) -> str:
    """Public, salted fingerprint of the real numeric ig-user-id. Lets the runner
    prove the secret it was handed belongs to THIS account before any Meta call."""
    return hashlib.sha256(f"AFFILIATE-ACCOUNT|{str(ig_user_id).strip()}".encode()).hexdigest()[:16]


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


def root(state_dir) -> Path:
    return Path(state_dir) / ROOT_DIR


def account_dir(state_dir, account: str) -> Path:
    if account not in ROUTES:
        raise ValueError(f"unknown affiliate account {account!r}")
    return root(state_dir) / account


def load_registry(state_dir) -> dict:
    return _read(root(state_dir) / "accounts.json")


def validate_registry(reg: dict) -> list:
    errs = []
    if reg.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"accounts.json schema {reg.get('schema_version')!r}")
    accts = reg.get("accounts")
    if not isinstance(accts, dict):
        return errs + ["accounts.json has no accounts"]
    for a, c in accts.items():
        if a in FOREIGN_ACCOUNTS:
            errs.append(f"{a}: belongs to another scheduler -- never an affiliate account")
            continue
        r = ROUTES.get(a)
        if r is None:
            errs.append(f"{a}: not in the affiliate routing table")
            continue
        for k in ("channel_profile", "username", "language"):
            if c.get(k) != r[k]:
                errs.append(f"{a}: {k} {c.get(k)!r} != route {r[k]!r}")
        try:
            ZoneInfo(c.get("TIMEZONE") or "")
        except Exception:  # noqa: BLE001
            errs.append(f"{a}: bad TIMEZONE {c.get('TIMEZONE')!r}")
        times = c.get("ALLOWED_POST_TIMES")
        if not isinstance(times, list) or any(not _HHMM.match(str(t)) for t in times):
            errs.append(f"{a}: ALLOWED_POST_TIMES must be a list of HH:MM")
        lim = c.get("INSTAGRAM_DAILY_LIMIT")
        if lim is not None and (not isinstance(lim, int) or lim < 1):
            errs.append(f"{a}: INSTAGRAM_DAILY_LIMIT must be null or an int >= 1")
        if float(c.get("min_spacing_hours") or 0) < 4:
            errs.append(f"{a}: min_spacing_hours >= 4 required")
        if not 1 <= int(c.get("max_attempts") or 0) <= 3:
            errs.append(f"{a}: max_attempts must be 1..3")
        if int(c.get("retry_backoff_minutes") or 0) < 30:
            errs.append(f"{a}: retry_backoff_minutes >= 30 required")
        if c.get("cloud_publish") not in (ENABLED, "DISABLED"):
            errs.append(f"{a}: cloud_publish must be ENABLED or DISABLED")
    profiles = [c.get("channel_profile") for c in accts.values()]
    if len(set(profiles)) != len(profiles):
        errs.append("one channel profile routed to two accounts")
    return errs


def halt_info(state_dir, account: str):
    p = account_dir(state_dir, account) / "halt.json"
    return _read(p) or ({"reason": "halt.json unreadable"} if p.exists() else None)


def write_halt(state_dir, account: str, *, reason: str, at: str, item_ref=None, code=None):
    p = account_dir(state_dir, account) / "halt.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"account": account, "halted_at": at, "reason": reason,
                             "item_ref": item_ref, "meta_error_code": code,
                             "resume": "verify the account in the Instagram app, then delete "
                                       "this file in a reviewed commit"}, indent=1) + "\n",
                 encoding="utf-8")


def account_gate(state_dir, account: str, env: dict, reg: dict = None) -> Gate:
    reg = load_registry(state_dir) if reg is None else reg
    c = (reg.get("accounts") or {}).get(account) or {}
    reasons = []
    if account not in ROUTES or account in FOREIGN_ACCOUNTS:
        return Gate(False, [f"{account}: not an affiliate account"])
    if not c:
        return Gate(False, [f"{account}: not in accounts.json"])
    if c.get("cloud_publish") != ENABLED:
        reasons.append("accounts.json cloud_publish != ENABLED")
    enabled_at = _dt(c.get("enabled_at"))
    if enabled_at is None:
        reasons.append("enabled_at not set")
    if (env.get(env_names(account)["variable"]) or "") != ENABLED:
        reasons.append(f"repository variable {env_names(account)['variable']} != ENABLED")
    if c.get("paused") is not False:
        reasons.append("account PAUSED")
    h = halt_info(state_dir, account)
    if h is not None:
        reasons.append(f"account HALTED: {h.get('reason')}")
    rec = _read(account_dir(state_dir, account) / "local_reconcile.json")
    if rec.get("local_publisher") != LOCAL_CONFIRMED:
        reasons.append(f"local Module 5 publisher not confirmed off for this account ({LOCAL_CONFIRMED})")
    gen = _dt(rec.get("generated_at"))
    if gen is None or enabled_at is None or gen < enabled_at:
        reasons.append("local history reconcile missing or older than enabled_at")
    if not isinstance(c.get("INSTAGRAM_DAILY_LIMIT"), int) or c["INSTAGRAM_DAILY_LIMIT"] < 1:
        reasons.append("INSTAGRAM_DAILY_LIMIT not configured")
    if not c.get("ALLOWED_POST_TIMES"):
        reasons.append("ALLOWED_POST_TIMES not configured")
    if not re.fullmatch(r"[0-9a-f]{16}", str(c.get("account_fingerprint") or "")):
        reasons.append("account_fingerprint not set")
    r = ROUTES[account]
    if c.get("channel_profile") != r["channel_profile"] or c.get("username") != r["username"]:
        reasons.append("account routing != ROUTES")
    return Gate(not reasons, reasons)


def classify_meta_error(exc) -> dict:
    """-> {"kind": AUTH|RESTRICTION|RATE|IDENTITY|AMBIGUOUS|OTHER, "code", "subcode"}.
    MetaError messages carry the Graph error JSON; parse code/subcode from it."""
    msg = str(exc)
    m = re.search(r'"code"\s*:\s*(\d+)', msg)
    s = re.search(r'"error_subcode"\s*:\s*(\d+)', msg)
    code, sub = (int(m.group(1)) if m else None), (int(s.group(1)) if s else None)
    phase = getattr(exc, "phase", "")
    if phase in ("identity", "config"):
        kind = "IDENTITY"
    elif code in AUTH_CODES or (code is not None and 200 <= code <= 299) or re.search(r"HTTP 401\b", msg):
        kind = "AUTH"
    elif code in RESTRICTION_CODES or sub in RESTRICTION_SUBCODES:
        kind = "RESTRICTION"
    elif code in RATE_CODES or sub in RATE_SUBCODES:
        kind = "RATE"
    elif getattr(exc, "ambiguous", False):
        kind = "AMBIGUOUS"
    else:
        kind = "OTHER"
    return {"kind": kind, "code": code, "subcode": sub}


# every kind except OTHER stops the account's whole queue until a human resumes it
HALTING_KINDS = ("AUTH", "RESTRICTION", "RATE", "IDENTITY", "AMBIGUOUS")
