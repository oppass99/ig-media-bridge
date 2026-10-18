"""
affiliate_cloud/aqueue.py  -- the PUBLIC affiliate queue index, its rules, and the
adapter that runs it through oneframe_cloud.queue.decide() unchanged.

state/affiliate/<ACCOUNT>/queues/<afq-ref>.json  (opaque refs only; identity,
product name, captions, hashtags and CTA live in <afq-ref>.manifest.enc)

Queue approval states  DRAFT APPROVED PAUSED COMPLETED CANCELLED   (APPROVED publishes)
Item states            QUEUED  staged, no slot yet (DRAFT only)
                       READY   staged + slot assigned
                       POSTING POSTED FAILED BLOCKED SKIPPED

Rules (daily limit, allowed times, spacing, retries) come from the ACCOUNT entry
in accounts.json, so every account has its own limits and schedule.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from oneframe_cloud import SCHEMA_VERSION as OF_SCHEMA

from . import ROUTES, SCHEMA_VERSION
from .accounts import account_dir

QUEUE_STATES = ("DRAFT", "APPROVED", "PAUSED", "COMPLETED", "CANCELLED")
RESERVING_STATES = ("DRAFT", "APPROVED", "PAUSED")
ITEM_STATES = ("QUEUED", "READY", "POSTING", "POSTED", "FAILED", "BLOCKED", "SKIPPED")
DONE_ITEM_STATES = ("POSTED", "SKIPPED")
REQUIRED_Q = ("schema_version", "queue_ref", "queue_version", "state", "account",
              "channel_profile", "key_id", "manifest_file", "manifest_sha256", "items")
REQUIRED_I = ("position", "item_ref", "item_seq", "source_sha256", "bundle_asset",
              "bundle_sha256", "bundle_bytes", "target_at", "status", "attempt_count")


def _dt(s) -> datetime:
    d = datetime.fromisoformat(str(s))
    if d.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {s!r}")
    return d


def load_queues(state_dir, account: str) -> list:
    out = []
    for p in sorted((account_dir(state_dir, account) / "queues").glob("*.json")):
        q = json.loads(p.read_text(encoding="utf-8"))
        q["_path"] = str(p)
        out.append(q)
    return out


def load_receipts(state_dir, account: str) -> list:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((account_dir(state_dir, account) / "receipts").glob("*.json"))]


def load_local_posts(state_dir, account: str) -> list:
    p = account_dir(state_dir, account) / "local_reconcile.json"
    if not p.exists():
        return []
    posts = json.loads(p.read_text(encoding="utf-8")).get("posts") or []
    return [x for x in posts if x.get("account") == account]


def validate_queue(q: dict, account: str, cfg: dict) -> list:
    errs = [f"{q.get('queue_ref')}: missing {k}" for k in REQUIRED_Q if k not in q]
    if errs:
        return errs
    ref = q["queue_ref"]
    if q["schema_version"] != SCHEMA_VERSION:
        errs.append(f"{ref}: schema {q['schema_version']!r}")
    if not str(ref).startswith("afq-"):
        errs.append(f"{ref}: affiliate queue refs start with afq-")
    if q["state"] not in QUEUE_STATES:
        errs.append(f"{ref}: unknown state {q['state']!r}")
    if q["account"] != account:
        errs.append(f"{ref}: WRONG_ACCOUNT queue account {q['account']!r} in {account} namespace")
    if q["channel_profile"] != ROUTES.get(account, {}).get("channel_profile"):
        errs.append(f"{ref}: WRONG_ROUTE channel_profile {q['channel_profile']!r} for {account}")
    items = q["items"]
    if [i.get("position") for i in items] != list(range(1, len(items) + 1)):
        errs.append(f"{ref}: positions must be 1..n in order")
    seqs = [i.get("item_seq") for i in items]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        errs.append(f"{ref}: item order not strictly increasing")
    zone = ZoneInfo(cfg.get("TIMEZONE") or "Asia/Seoul")
    times = list(cfg.get("ALLOWED_POST_TIMES") or [])
    limit = cfg.get("INSTAGRAM_DAILY_LIMIT")
    spacing = timedelta(hours=float(cfg.get("min_spacing_hours") or 4))
    if q["state"] == "APPROVED" and (not isinstance(limit, int) or limit < 1 or not times):
        errs.append(f"{ref}: APPROVED needs INSTAGRAM_DAILY_LIMIT and ALLOWED_POST_TIMES "
                    f"configured for {account}")
    prev, per_day = None, {}
    for i in items:
        miss = [k for k in REQUIRED_I if k not in i]
        if miss:
            errs.append(f"{ref}#{i.get('position')}: missing {miss}")
            continue
        where = f"{ref}#{i['position']}"
        if i["status"] not in ITEM_STATES:
            errs.append(f"{where}: unknown status {i['status']!r}")
        if not str(i["item_ref"]).startswith("afi-") or not str(i["bundle_asset"]).startswith("afb-"):
            errs.append(f"{where}: affiliate refs must be afi-/afb-")
        if len(str(i["source_sha256"])) != 64 or len(str(i["bundle_sha256"])) != 64:
            errs.append(f"{where}: bad sha256")
        if i["status"] == "POSTED" and not (i.get("media_id") and i.get("published_at")):
            errs.append(f"{where}: POSTED without media_id/published_at proof")
        if i["target_at"] is None:
            if i["status"] not in ("QUEUED", "SKIPPED"):
                errs.append(f"{where}: {i['status']} item needs a target slot")
            elif q["state"] == "APPROVED":
                errs.append(f"{where}: APPROVED queue has an unscheduled item")
            continue
        if i["status"] == "QUEUED":
            errs.append(f"{where}: a scheduled item is READY, not QUEUED")
        try:
            t = _dt(i["target_at"]).astimezone(zone)
        except ValueError as e:
            errs.append(f"{where}: {e}")
            continue
        if t.strftime("%H:%M") not in times:
            errs.append(f"{where}: {t.strftime('%H:%M')} not an allowed post time {times}")
        per_day[t.date()] = per_day.get(t.date(), 0) + 1
        if prev is not None and t - prev < spacing:
            errs.append(f"{where}: < {spacing} after previous item")
        prev = t
    if isinstance(limit, int):
        for d, n in per_day.items():
            if n > limit:
                errs.append(f"{ref}: {n} items on {d} > INSTAGRAM_DAILY_LIMIT {limit}")
    return errs


def validate_account(queues: list, account: str, cfg: dict) -> list:
    errs = []
    for q in queues:
        errs += validate_queue(q, account, cfg)
    refs = [q.get("queue_ref") for q in queues]
    if len(set(refs)) != len(refs):
        errs.append(f"{account}: duplicate queue_ref")
    seen_sha, seen_ref = {}, {}
    for q in queues:
        if q.get("state") not in RESERVING_STATES:
            continue
        for i in q.get("items", []):
            if i.get("source_sha256") in seen_sha:
                errs.append(f"DUPLICATE_SOURCE: {q['queue_ref']}#{i['position']} and "
                            f"{seen_sha[i['source_sha256']]}")
            seen_sha[i.get("source_sha256")] = f"{q['queue_ref']}#{i.get('position')}"
            if i.get("item_ref") in seen_ref:
                errs.append(f"DUPLICATE_ITEM_REF: {i['item_ref']}")
            seen_ref[i.get("item_ref")] = q["queue_ref"]
    return errs


def validate_cross_account(by_account: dict) -> list:
    """The same video bytes queued for two different accounts is a routing error."""
    owner, errs = {}, []
    for account, queues in by_account.items():
        for q in queues:
            if q.get("state") not in RESERVING_STATES:
                continue
            for i in q.get("items", []):
                o = owner.setdefault(i.get("source_sha256"), account)
                if o != account:
                    errs.append(f"CROSS_ACCOUNT_SOURCE: {q['queue_ref']}#{i['position']} "
                                f"({account}) is already queued for {o}")
    return errs


def backoff_minutes(cfg: dict, attempt_count: int) -> int:
    """Exponential, capped: base, 2*base, 4*base ... <= cap (existing 30 min base)."""
    base = int(cfg.get("retry_backoff_minutes") or 30)
    cap = int(cfg.get("retry_backoff_cap_minutes") or 240)
    return min(base * 2 ** max(int(attempt_count) - 1, 0), max(cap, base))


# decide() counts its daily limit by the REAL publish date ("today" in the queue zone).
# With a late runner that drifts: an 18:00 item processed at 01:00 would use the NEXT
# day's quota. Affiliate views therefore switch that counter off and slot_quota()
# counts by the item's INTENDED slot date instead (spacing, duplicates, retry cap,
# POSTING halt and ordering are still decide()'s, unchanged).
CALENDAR_LIMIT_OFF = 10 ** 6


def slot_date(target_at, tz: str):
    return _dt(target_at).astimezone(ZoneInfo(tz)).date()


def catchup_cap(cfg: dict) -> int:
    """Max REAL posts in any rolling 24 h: one slot's post + one late catch-up."""
    return int(cfg.get("catchup_max_posts_24h") or int(cfg["INSTAGRAM_DAILY_LIMIT"]) + 1)


def account_post_slots(account: str, queues: list, receipts: list, local_posts: list,
                       tz: str) -> list:
    """Every known real post -> (published_at, attributed slot date, media_id).
    A cloud post is attributed to the slot it was published FOR (slot_at, else its
    intended target_at). A local Module 5 post has no slot: its real publish date
    in the account zone is used."""
    out = {}
    for q in queues:
        for i in q["items"]:
            if i["status"] == "POSTED" and i.get("media_id") and i.get("target_at"):
                out[str(i["media_id"])] = (_dt(i["published_at"]),
                                           slot_date(i.get("slot_at") or i["target_at"], tz),
                                           str(i["media_id"]))
    for r in receipts:
        if r.get("account") == account and r.get("media_id") and r.get("published_at"):
            slot = r.get("slot_at") or r.get("target_at") or r["published_at"]
            out.setdefault(str(r["media_id"]), (_dt(r["published_at"]), slot_date(slot, tz),
                                                str(r["media_id"])))
    for p in local_posts:
        if p.get("account") == account and p.get("published_at"):
            mid = str(p.get("media_id") or f"local:{p.get('source_sha256')}")
            out.setdefault(mid, (_dt(p["published_at"]), slot_date(p["published_at"], tz), mid))
    return sorted(out.values(), key=lambda x: x[0])


def effective_slot(head: dict, cfg: dict, posts: list) -> datetime:
    """The slot this head publishes FOR: its intended target_at, unless that slot
    date's quota is already used (e.g. by a manual post) -- then the next allowed
    slot on the next date with free quota. Never earlier than intended; the queue
    keeps its order and no item is skipped (later items shift by one)."""
    zone = ZoneInfo(cfg["TIMEZONE"])
    limit = int(cfg["INSTAGRAM_DAILY_LIMIT"])
    used = {}
    for p in posts:
        used[p[1]] = used.get(p[1], 0) + 1
    t = _dt(head["target_at"]).astimezone(zone)
    if used.get(t.date(), 0) < limit:
        return t
    times = sorted(datetime.strptime(x, "%H:%M").time() for x in cfg["ALLOWED_POST_TIMES"])
    d = t.date()
    for _ in range(400):
        d += timedelta(days=1)
        if used.get(d, 0) < limit:
            return datetime.combine(d, times[0], tzinfo=zone)
    raise ValueError("no free slot date within 400 days")


def slot_quota(head: dict, cfg: dict, posts: list, now: datetime):
    """-> (effective slot, None) when the due head may publish now, else
    (effective slot, WAIT note). Daily limit is per INTENDED/attributed slot date
    in the account zone; real posts in any rolling 24 h are capped as well."""
    slot = effective_slot(head, cfg, posts)
    if slot > now:
        return slot, {"action": "WAIT_DAILY_LIMIT",
                      "detail": f"slot date {_dt(head['target_at']).astimezone(ZoneInfo(cfg['TIMEZONE'])).date()} "
                                f"({cfg['TIMEZONE']}) quota used; next free slot {slot.isoformat()}"}
    recent = sum(1 for p in posts if now - p[0] < timedelta(hours=24))
    if recent >= catchup_cap(cfg):
        return slot, {"action": "WAIT_CATCHUP_CAP", "detail": f"{recent} real posts in the last 24 h"}
    return slot, None


def decide_view(q: dict, cfg: dict) -> dict:
    """An APPROVED affiliate queue in the shape oneframe_cloud.queue.decide() reads.
    READY -> QUEUED (decide's 'waiting for its slot'); every other status as-is.
    daily_limit is CALENDAR_LIMIT_OFF: the real limit is slot_quota()'s."""
    pending = [i for i in q["items"] if i["status"] not in DONE_ITEM_STATES]
    head_attempts = int(pending[0]["attempt_count"]) if pending else 0
    return {"schema_version": OF_SCHEMA, "queue_ref": q["queue_ref"],
            "queue_version": q["queue_version"], "song_seq": 0, "state": q["state"],
            "account": q["account"], "timezone": cfg["TIMEZONE"],
            "daily_limit": CALENDAR_LIMIT_OFF,
            "times": list(cfg["ALLOWED_POST_TIMES"]),
            "min_spacing_hours": float(cfg["min_spacing_hours"]),
            "max_attempts": int(cfg["max_attempts"]),
            "retry_backoff_minutes": backoff_minutes(cfg, head_attempts),
            "key_id": q["key_id"], "manifest_file": q["manifest_file"],
            "manifest_sha256": q["manifest_sha256"], "_path": q.get("_path", ""),
            "items": [dict(i, shorts_seq=i["item_seq"],
                           status="QUEUED" if i["status"] == "READY" else i["status"])
                      for i in q["items"]]}


# ---- public-copy lint ----------------------------------------------------------
_FORBIDDEN = [r"\bjob[_ -]?id\b", r"\bautomat(ed|ion|ically)\b", r"\bapi\b", r"\bgraph\b",
              r"\bprovider\b", r"\bprompt\b", r"\bsuno\w*", r"#sunoai", r"\boneframe\b",
              r"\bgithub\b", r"\bpipeline\b", r"\bmodule\s*\d", r"\bdry[- ]?run\b",
              r"\bwebhook\b", r"\bscheduler\b", r"\bbot\b"]
_HANGUL = re.compile(r"[가-힣]")
_CTA = {"KO": (r"프로필\s*링크", r"검색"), "EN": (r"profile link", r"search")}
_DISCLOSURE = {"COUPANG": r"쿠팡\s*파트너스", "ALIEXPRESS": r"aliexpress affiliate"}


def lint_copy(final_caption: str, *, language: str, program: str, internal_ids=()) -> dict:
    """-> {"errors": [...], "warnings": [...]} for ONE public caption."""
    errs, warns = [], []
    text = final_caption or ""
    low = text.lower()
    for i in internal_ids:
        if i and str(i).lower() in low:
            errs.append(f"internal id {i!r} in public copy")
    for pat in _FORBIDDEN:
        m = re.search(pat, low)
        if m:
            errs.append(f"internal/provider wording {m.group(0)!r} in public copy")
    if "clickbank" in low and program != "CLICKBANK":
        errs.append("ClickBank wording in a non-ClickBank queue")
    has_hangul = bool(_HANGUL.search(text))
    if language == "KO" and not has_hangul:
        errs.append("KO channel but caption has no Korean")
    if language == "EN" and has_hangul:
        errs.append("EN channel but caption contains Korean")
    cta = _CTA.get(language)
    if cta and not all(re.search(p, low) for p in cta):
        errs.append("affiliate CTA (profile link + product-name search) missing")
    disc = _DISCLOSURE.get(program)
    if disc and not re.search(disc, low):
        errs.append(f"{program} affiliate disclosure missing")
    tags = re.findall(r"#\w+", text)
    if len(tags) > 30:
        errs.append(f"{len(tags)} hashtags > Instagram limit 30")
    elif len(tags) > 15:
        warns.append(f"{len(tags)} hashtags (heavy block)")
    if len(text) > 2200:
        errs.append(f"caption {len(text)} chars > Instagram limit 2200")
    if re.search(r"https?://", low):
        warns.append("raw URL in caption (not clickable on Instagram)")
    return {"errors": errs, "warnings": warns}


def lint_queue_copy(captions: list) -> dict:
    """Repetition across ONE queue: identical captions are errors (spam pattern);
    an identical hashtag block on consecutive items is a warning."""
    errs, warns, seen = [], [], {}
    prev_tags = None
    for n, c in enumerate(captions, 1):
        key = re.sub(r"\s+", " ", c or "").strip().lower()
        if key in seen:
            errs.append(f"item {n}: caption identical to item {seen[key]}")
        seen.setdefault(key, n)
        tags = tuple(sorted(re.findall(r"#\w+", (c or "").lower())))
        if tags and tags == prev_tags:
            warns.append(f"item {n}: same hashtag block as item {n - 1}")
        prev_tags = tags
    return {"errors": errs, "warnings": warns}


def account_view(q: dict) -> dict:
    """Read-only per-queue overview row for status/dry-run output."""
    pending = [i for i in q["items"] if i["status"] not in DONE_ITEM_STATES]
    h = pending[0] if pending else None
    return {"queue_ref": q["queue_ref"], "state": q["state"], "items": len(q["items"]),
            "posted": sum(1 for i in q["items"] if i["status"] == "POSTED"),
            "unscheduled": sum(1 for i in q["items"] if i["target_at"] is None),
            "next": h and {"position": h["position"], "target_at": h["target_at"],
                           "status": h["status"]},
            "publishable": q["state"] == "APPROVED"}
