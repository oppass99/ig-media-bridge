"""
oneframe_cloud/queue.py  -- the PUBLIC, minimal cloud queue index and its rules.

state/queues/<queue_ref>.json holds only non-secret operational metadata:
opaque refs, order, SHA256s, encrypted-bundle identity, slot, status, proof.
Real identity (PROJECT_ID / JOB_ID / SONG_ID / SHORTS_ID), captions and
hashtags live only in the encrypted manifest (state/queues/<queue_ref>.manifest.enc)
and inside each encrypted media bundle.

Queue states
  MIGRATION_PENDING  mirror of a queue the LOCAL scheduler still owns -- never publishes
  DRAFT              prepared, not approved -- never publishes
  APPROVED           the only publishable state
  PAUSED COMPLETED CANCELLED
Item states: QUEUED POSTING POSTED FAILED BLOCKED SKIPPED
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import SCHEMA_VERSION

QUEUE_STATES = ("MIGRATION_PENDING", "DRAFT", "APPROVED", "PAUSED", "COMPLETED", "CANCELLED")
PUBLISHABLE_STATES = ("APPROVED",)
RESERVING_STATES = ("MIGRATION_PENDING", "DRAFT", "APPROVED", "PAUSED")
ITEM_STATES = ("QUEUED", "POSTING", "POSTED", "FAILED", "BLOCKED", "SKIPPED")
DONE_ITEM_STATES = ("POSTED", "SKIPPED")
REQUIRED_Q = ("schema_version", "queue_ref", "queue_version", "song_seq", "state", "account",
              "timezone", "daily_limit", "times", "min_spacing_hours", "max_attempts",
              "retry_backoff_minutes", "key_id", "manifest_file", "manifest_sha256", "items")
REQUIRED_I = ("position", "item_ref", "shorts_seq", "source_sha256", "bundle_asset",
              "bundle_sha256", "bundle_bytes", "target_at", "status", "attempt_count")


class QueueStateError(Exception):
    pass


def _dt(s) -> datetime:
    d = datetime.fromisoformat(str(s))
    if d.tzinfo is None:
        raise QueueStateError(f"timestamp without timezone: {s!r}")
    return d


def load_queues(state_dir) -> list:
    out = []
    for p in sorted((Path(state_dir) / "queues").glob("*.json")):
        q = json.loads(p.read_text(encoding="utf-8"))
        q["_path"] = str(p)
        out.append(q)
    return out


def load_receipts(state_dir) -> list:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((Path(state_dir) / "receipts").glob("*.json"))]


def load_local_posts(state_dir) -> list:
    p = Path(state_dir) / "local_reconcile.json"
    if not p.exists():
        return []
    return list(json.loads(p.read_text(encoding="utf-8")).get("posts") or [])


def validate_queue(q: dict) -> list:
    errs = []
    for k in REQUIRED_Q:
        if k not in q:
            errs.append(f"{q.get('queue_ref')}: missing {k}")
    if errs:
        return errs
    ref = q["queue_ref"]
    if q["schema_version"] != SCHEMA_VERSION:
        errs.append(f"{ref}: schema {q['schema_version']!r}")
    if q["state"] not in QUEUE_STATES:
        errs.append(f"{ref}: unknown state {q['state']!r}")
    if int(q["daily_limit"]) < 1 or float(q["min_spacing_hours"]) < 4:
        errs.append(f"{ref}: daily_limit >= 1 and min_spacing_hours >= 4 required")
    try:
        zone = ZoneInfo(q["timezone"])
    except Exception:  # noqa: BLE001
        return errs + [f"{ref}: bad timezone {q['timezone']!r}"]
    times = sorted(q["times"])
    items = q["items"]
    if [i.get("position") for i in items] != list(range(1, len(items) + 1)):
        errs.append(f"{ref}: positions must be 1..n in order")
    seqs = [i.get("shorts_seq") for i in items]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        errs.append(f"{ref}: shorts order not strictly increasing")
    prev, per_day = None, {}
    for i in items:
        miss = [k for k in REQUIRED_I if k not in i]
        if miss:
            errs.append(f"{ref}#{i.get('position')}: missing {miss}")
            continue
        if i["status"] not in ITEM_STATES:
            errs.append(f"{ref}#{i['position']}: unknown status {i['status']!r}")
        if len(str(i["source_sha256"])) != 64 or len(str(i["bundle_sha256"])) != 64:
            errs.append(f"{ref}#{i['position']}: bad sha256")
        if i["status"] == "POSTED" and not (i.get("media_id") and i.get("published_at")):
            errs.append(f"{ref}#{i['position']}: POSTED without media_id/published_at proof")
        try:
            t = _dt(i["target_at"]).astimezone(zone)
        except (QueueStateError, ValueError) as e:
            errs.append(f"{ref}#{i['position']}: {e}")
            continue
        if t.strftime("%H:%M") not in times:
            errs.append(f"{ref}#{i['position']}: {t.strftime('%H:%M')} not a configured slot {times}")
        per_day[t.date()] = per_day.get(t.date(), 0) + 1
        if prev is not None and t - prev < timedelta(hours=float(q["min_spacing_hours"])):
            errs.append(f"{ref}#{i['position']}: < {q['min_spacing_hours']}h after previous item")
        prev = t
    for d, n in per_day.items():
        if n > int(q["daily_limit"]):
            errs.append(f"{ref}: {n} items on {d} > daily_limit {q['daily_limit']}")
    return errs


def validate_all(queues: list) -> list:
    errs = []
    for q in queues:
        errs += validate_queue(q)
    if errs:
        return errs
    refs = [q["queue_ref"] for q in queues]
    if len(set(refs)) != len(refs):
        errs.append("duplicate queue_ref")
    live = [q for q in queues if q["state"] in RESERVING_STATES]
    seen_sha, seen_ref, slots = {}, {}, []
    for q in live:
        for i in q["items"]:
            key = (q["account"], i["source_sha256"])
            if key in seen_sha:
                errs.append(f"DUPLICATE_SOURCE: {q['queue_ref']}#{i['position']} and {seen_sha[key]}")
            seen_sha[key] = f"{q['queue_ref']}#{i['position']}"
            if i["item_ref"] in seen_ref:
                errs.append(f"DUPLICATE_ITEM_REF: {i['item_ref']}")
            seen_ref[i["item_ref"]] = q["queue_ref"]
            if i["status"] not in DONE_ITEM_STATES:
                slots.append((q["account"], _dt(i["target_at"]), q, i))
    # no two unfinished items for one account closer than the spacing (cross-queue)
    slots.sort(key=lambda s: (s[0], s[1]))
    for a, b in zip(slots, slots[1:]):
        if a[0] == b[0] and a[2]["queue_ref"] != b[2]["queue_ref"]:
            gap = timedelta(hours=float(min(a[2]["min_spacing_hours"], b[2]["min_spacing_hours"])))
            if b[1] - a[1] < gap:
                errs.append(f"SLOT_COLLISION: {a[2]['queue_ref']}#{a[3]['position']} "
                            f"{a[1].isoformat()} vs {b[2]['queue_ref']}#{b[3]['position']} {b[1].isoformat()}")
    return errs


def head_item(q: dict):
    pending = [i for i in q["items"] if i["status"] not in DONE_ITEM_STATES]
    return pending[0] if pending else None


def account_posts(account: str, queues: list, receipts: list, local_posts: list) -> list:
    """Every known real post to this account: cloud receipts + POSTED cloud items +
    the local Module 5 history export. [(published_at, source_sha256, media_id)]"""
    out = {}
    for r in receipts:
        if r.get("account") == account and r.get("published_at"):
            out[r["media_id"]] = (_dt(r["published_at"]), r.get("source_sha256"), r["media_id"])
    for q in queues:
        if q["account"] != account:
            continue
        for i in q["items"]:
            if i["status"] == "POSTED" and i.get("media_id"):
                out[i["media_id"]] = (_dt(i["published_at"]), i["source_sha256"], i["media_id"])
    for p in local_posts:
        if p.get("account") == account and p.get("published_at"):
            mid = str(p.get("media_id") or f"local:{p.get('source_sha256')}")
            out.setdefault(mid, (_dt(p["published_at"]), p.get("source_sha256"), mid))
    return sorted(out.values(), key=lambda x: x[0])


def find_duplicate(item: dict, account: str, receipts: list, local_posts: list):
    """The earlier real post of this exact content to this account, or None.
    -> {"source": "cloud"|"local", "media_id", "post_url", "published_at", "same_bytes"}"""
    for r in receipts:
        if r.get("account") == account and (r.get("item_ref") == item["item_ref"]
                                            or r.get("source_sha256") == item["source_sha256"]):
            return {"source": "cloud", "media_id": r.get("media_id"), "post_url": r.get("post_url"),
                    "published_at": r.get("published_at"),
                    "same_bytes": r.get("source_sha256") == item["source_sha256"]}
    for p in local_posts:
        if p.get("account") == account and p.get("source_sha256") == item["source_sha256"]:
            return {"source": "local", "media_id": p.get("media_id"), "post_url": p.get("post_url"),
                    "published_at": p.get("published_at"), "same_bytes": True}
    return None


def is_duplicate(item: dict, account: str, receipts: list, local_posts: list) -> str:
    d = find_duplicate(item, account, receipts, local_posts)
    return f"{d['source']} post {d['media_id']}" if d else ""


def decide(queues: list, *, now: datetime, receipts: list, local_posts: list) -> dict:
    """Pick at most ONE publish candidate across all APPROVED queues.
    Returns {"action": ..., ...}. Pure: no I/O, no state change."""
    errs = validate_all(queues)
    if errs:
        return {"action": "INVALID_STATE", "errors": errs}
    heads = []
    for q in queues:
        if q["state"] not in PUBLISHABLE_STATES:
            continue
        h = head_item(q)
        if h is None:
            heads.append(("COMPLETE", q, None))
            continue
        heads.append(("HEAD", q, h))
    if not any(k == "HEAD" for k, _, _ in heads):
        return {"action": "NOOP", "detail": "no APPROVED queue with pending items"}
    # earliest scheduled head first; a halted head blocks only its own queue
    heads = sorted([x for x in heads if x[0] == "HEAD"], key=lambda x: _dt(x[2]["target_at"]))
    notes = []
    for _, q, h in heads:
        ref = f"{q['queue_ref']}#{h['position']}"
        if h["status"] == "POSTING":
            notes.append({"action": "HALTED", "where": ref,
                          "detail": "left in POSTING by an interrupted run -- verify the account, "
                                    "then re-queue explicitly"})
            continue
        if h["status"] == "BLOCKED":
            notes.append({"action": "HALTED", "where": ref, "detail": h.get("error")})
            continue
        if h["status"] == "FAILED":
            if int(h["attempt_count"]) >= int(q["max_attempts"]):
                notes.append({"action": "HALTED", "where": ref,
                              "detail": f"retry cap {q['max_attempts']} reached"})
                continue
            last = h.get("last_attempt_at")
            if last and now - _dt(last) < timedelta(minutes=int(q["retry_backoff_minutes"])):
                notes.append({"action": "WAIT_BACKOFF", "where": ref})
                continue
        target = _dt(h["target_at"])
        if target > now:
            notes.append({"action": "NOOP", "where": ref, "detail": f"next at {h['target_at']}"})
            continue
        zone = ZoneInfo(q["timezone"])
        posts = account_posts(q["account"], queues, receipts, local_posts)
        today = now.astimezone(zone).date()
        n_today = sum(1 for p in posts if p[0].astimezone(zone).date() == today)
        if n_today >= int(q["daily_limit"]):
            notes.append({"action": "WAIT_DAILY_LIMIT", "where": ref,
                          "detail": f"{n_today}/{q['daily_limit']} today"})
            continue
        if posts and now - posts[-1][0] < timedelta(hours=float(q["min_spacing_hours"])):
            notes.append({"action": "WAIT_SPACING", "where": ref,
                          "detail": f"last post {posts[-1][0].isoformat()}"})
            continue
        dup = find_duplicate(h, q["account"], receipts, local_posts)
        if dup:
            # exact same bytes already live on this account (e.g. posted by the local
            # scheduler before cutover): record it as POSTED with that proof, never re-post
            proven = dup["same_bytes"] and dup.get("media_id") and dup.get("published_at")
            return {"action": "ALREADY_POSTED" if proven else "BLOCK_DUPLICATE",
                    "queue_ref": q["queue_ref"], "item": h, "duplicate": dup,
                    "detail": f"{dup['source']} post {dup.get('media_id')}", "notes": notes}
        return {"action": "PUBLISH", "queue_ref": q["queue_ref"], "item": h, "notes": notes}
    # nothing publishable: report the earliest note
    return dict(notes[0], notes=notes) if notes else {"action": "NOOP"}


def schedule_view(queues: list, *, now: datetime) -> dict:
    """Read-only overview for the dry run: per queue state + next unfinished item,
    and the next item that WOULD be due under the schedule (any reserving state)."""
    rows, nxt = [], None
    for q in queues:
        h = head_item(q)
        rows.append({"queue_ref": q["queue_ref"], "song_seq": q["song_seq"], "state": q["state"],
                     "items": len(q["items"]),
                     "posted": sum(1 for i in q["items"] if i["status"] == "POSTED"),
                     "next": h and {"position": h["position"], "shorts_seq": h["shorts_seq"],
                                    "target_at": h["target_at"], "status": h["status"]},
                     "publishable": q["state"] in PUBLISHABLE_STATES})
        if q["state"] in RESERVING_STATES and h and h["status"] == "QUEUED":
            cand = (_dt(h["target_at"]), q["queue_ref"], h)
            if nxt is None or cand[0] < nxt[0]:
                nxt = cand
    return {"queues": rows, "next_scheduled": nxt and {
        "queue_ref": nxt[1], "position": nxt[2]["position"], "shorts_seq": nxt[2]["shorts_seq"],
        "target_at": nxt[2]["target_at"], "due_now": nxt[0] <= now}}
