"""
affiliate_cloud/tick.py  -- ONE affiliate scheduler pass, after the ONEFRAME tick in
the same workflow job (same concurrency group, so no two runs ever overlap).

  load accounts.json -> validate (routing table, no foreign account)
  -> per account: validate its own namespace (an invalid account stops ONLY itself)
  -> orphan cleanup (afc-tmp-* only) -> staged-bundle check
  -> per-account gate (DISABLED / PAUSED / HALTED => that account never reaches Meta)
  -> oneframe_cloud.queue.decide over the open accounts' APPROVED queues
     (due in Asia/Seoul, per-account daily limit + spacing, duplicates vs this
     account's receipts AND its local Module 5 history, capped exponential retry)
  -> persist POSTING (pushed BEFORE any Meta call)
  -> account fingerprint check of the secret ig-user-id (before any Meta call)
  -> decrypt + verify SOURCE_SHA256 -> temp random-named plaintext asset
  -> /me identity -> /media -> FINISHED -> /media_publish once -> delete temp asset
  -> POSTED + receipt, or FAILED (backoff) / BLOCKED; auth, restriction, rate,
     identity or ambiguous errors also write halt.json: the account stops.

At most ONE affiliate publish per run.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets as _secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oneframe_cloud import bundle as B
from oneframe_cloud import ghapi
from oneframe_cloud import queue as Q
from oneframe_cloud.redact import scrub

from . import ROUTES, SCHEMA_VERSION, STAGING_TAG, TMP_PREFIX
from . import accounts as A
from . import aqueue as AQ

TMP_RE = re.compile(r"^afc-tmp-[0-9a-f]{32}\.mp4$")
ORPHAN_AFTER = timedelta(hours=2)


class Blocked(Exception):
    """Non-retryable: the item is BLOCKED and its queue halts."""


def temp_name() -> str:
    return f"{TMP_PREFIX}{_secrets.token_hex(16)}.mp4"


def save_queue(q: dict):
    data = {k: v for k, v in q.items() if not k.startswith("_")}
    Path(q["_path"]).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                encoding="utf-8")


def cleanup_orphans(gh, release, *, now) -> list:
    """Delete stale AFFILIATE temporary plaintext assets. Never touches other names."""
    deleted = []
    for a in gh.assets(release["id"]):
        if not TMP_RE.match(a.get("name") or ""):
            continue
        created = datetime.fromisoformat(str(a["created_at"]).replace("Z", "+00:00"))
        if now - created >= ORPHAN_AFTER:
            gh.delete_asset(a["id"])
            deleted.append(a["name"])
    return deleted


def staged_check(gh, by_account) -> dict:
    expected = sum(len(q["items"]) for qs in by_account.values() for q in qs)
    rel = gh.release(STAGING_TAG)
    if rel is None:
        return {"release": None, "expected": expected, "present": 0,
                "missing": "staging release absent" if expected else []}
    have = {a["name"]: a for a in gh.assets(rel["id"])}
    missing, bad = [], []
    for qs in by_account.values():
        for q in qs:
            for i in q["items"]:
                a = have.get(i["bundle_asset"])
                if a is None:
                    missing.append(f"{q['queue_ref']}#{i['position']}")
                elif int(a.get("size") or -1) != int(i["bundle_bytes"]):
                    bad.append(f"{q['queue_ref']}#{i['position']}")
    return {"release": rel["tag_name"], "expected": expected, "present": expected - len(missing),
            "missing": missing, "size_mismatch": bad,
            "plaintext_assets": [n for n in have if n.lower().endswith((".mp4", ".mov"))]}


def load_all(sd, reg):
    """-> (by_account queues, account errors, receipts, local posts)"""
    by_account, acct_errs, receipts, local_posts = {}, {}, [], []
    for account, cfg in (reg.get("accounts") or {}).items():
        try:
            qs = AQ.load_queues(sd, account)
            errs = AQ.validate_account(qs, account, cfg)
        except (OSError, ValueError, KeyError, TypeError) as e:
            qs, errs = [], [f"{account}: unreadable state ({e})"]
        by_account[account] = qs
        if errs:
            acct_errs[account] = errs
        receipts += [r for r in AQ.load_receipts(sd, account) if r.get("account") == account]
        local_posts += AQ.load_local_posts(sd, account)
    for e in AQ.validate_cross_account(by_account):
        for account in by_account:  # both accounts involved stop
            if account in e:
                acct_errs.setdefault(account, []).append(e)
    return by_account, acct_errs, receipts, local_posts


def tick(state_dir, *, now: datetime, env: dict, gh=None, meta_factory=None, commit=None,
         workdir=None, clock=None) -> dict:
    sd = Path(state_dir)
    reg = A.load_registry(sd)
    res = {"schema_version": SCHEMA_VERSION, "tick_at": now.isoformat(), "accounts": {},
           "meta_calls": [], "publish_calls": 0}
    if not reg:
        res.update(result="NO_AFFILIATE_ACCOUNTS", action="NOOP")
        return res
    reg_errs = A.validate_registry(reg)
    if reg_errs:
        res.update(result="INVALID_REGISTRY", action="NOOP", errors=reg_errs)
        return res
    by_account, acct_errs, receipts, local_posts = load_all(sd, reg)
    if gh is not None:
        try:
            res["staged"] = staged_check(gh, by_account)
        except ghapi.GitHubError as e:
            res["staged"] = {"error": scrub(e)}
        if gh.token:
            try:
                bridge = gh.release(ghapi.BRIDGE_TAG)
                res["orphans_deleted"] = cleanup_orphans(gh, bridge, now=now) if bridge else []
            except ghapi.GitHubError as e:
                res["orphans_deleted"] = {"error": scrub(e)}

    views, gates = [], {}
    for account, cfg in reg["accounts"].items():
        gate = A.account_gate(sd, account, env, reg)
        gates[account] = gate
        qs = by_account.get(account, [])
        row = {"gate": gate.state, "reasons": gate.reasons,
               "queues": [AQ.account_view(q) for q in qs]}
        if account in acct_errs:
            row.update(valid=False, errors=acct_errs[account][:10])
        res["accounts"][account] = row
        if not gate.enabled or account in acct_errs:
            continue
        mine = [AQ.decide_view(q, cfg) for q in qs if q["state"] == "APPROVED"]
        verrs = Q.validate_all(mine)  # e.g. a cross-queue slot collision: stops this account only
        if verrs:
            row.update(valid=False, errors=verrs[:10])
            continue
        views += mine
    if not views:
        res.update(result="NO_OPEN_ACCOUNT" if not any(g.enabled for g in gates.values())
                   else "NOOP", action="NOOP")
        return res

    decision = Q.decide(views, now=now, receipts=receipts, local_posts=local_posts)
    summary = {k: v for k, v in decision.items() if k not in ("item", "notes")}
    if decision.get("item"):
        summary["item"] = {k: decision["item"][k] for k in ("position", "item_ref", "target_at")}
    res["decision"] = summary
    if decision["action"] not in ("PUBLISH", "BLOCK_DUPLICATE", "ALREADY_POSTED"):
        res.update(result=decision["action"], action="NOOP")
        return res

    view = next(v for v in views if v["queue_ref"] == decision["queue_ref"])
    account = view["account"]
    q = next(x for x in by_account[account] if x["queue_ref"] == view["queue_ref"])
    it = next(i for i in q["items"] if i["item_ref"] == decision["item"]["item_ref"])
    res["account"] = account

    if decision["action"] in ("BLOCK_DUPLICATE", "ALREADY_POSTED"):
        dup = decision["duplicate"]
        if decision["action"] == "ALREADY_POSTED":
            it.update(status="POSTED", media_id=str(dup["media_id"]), post_url=dup.get("post_url"),
                      published_at=dup["published_at"],
                      error=f"already published ({dup['source']}); not re-posted")
            res.update(result="ALREADY_POSTED", action="NOOP")
        else:
            it.update(status="BLOCKED", error=f"duplicate: {decision['detail']}")
            res.update(result="BLOCKED_DUPLICATE", action="NOOP")
        _finish(q)
        res["persisted"] = _commit(commit, f"affiliate: {res['result']} {q['queue_ref']}#{it['position']}")
        return res

    it.update(status="POSTING", attempt_count=int(it["attempt_count"]) + 1,
              last_attempt_at=now.isoformat(), error=None)
    save_queue(q)
    if not _commit(commit, f"affiliate: POSTING {q['queue_ref']}#{it['position']}"):
        res.update(result="ABORTED_STATE_NOT_PERSISTED", action="NOOP")
        return res  # nothing was sent to Meta

    names = A.env_names(account)
    cfg = reg["accounts"][account]
    meta = None
    try:
        uid = env.get(names["user_id"], "")
        if not uid or A.fingerprint(uid) != cfg["account_fingerprint"]:
            raise Blocked("ACCOUNT_IDENTITY_MISMATCH: secret ig-user-id does not match this "
                          "account's fingerprint")
        meta = meta_factory(gates[account], account, env.get(names["token"], ""), uid)
        out = _publish_one(q, it, sd, account=account, cfg=cfg, key_b64=env.get(names["media_key"], ""),
                           gh=gh, meta=meta, workdir=workdir,
                           clock=clock or (lambda: datetime.now(timezone.utc)))
    except Blocked as e:
        it.update(status="BLOCKED", error=scrub(e)[:500])
        res.update(result="BLOCKED", action="PUBLISH_ATTEMPTED")
        if "ACCOUNT_IDENTITY_MISMATCH" in str(e):
            A.write_halt(sd, account, reason="identity: secret does not match account",
                         at=now.isoformat(), item_ref=it["item_ref"])
            res["account_halted"] = True
    except Exception as e:  # noqa: BLE001 -- never a fake success
        c = A.classify_meta_error(e)
        phase = getattr(e, "phase", "") or "error"
        msg = scrub(f"{phase}: {e}")[:500]
        if c["kind"] in A.HALTING_KINDS:
            it.update(status="BLOCKED", error=msg + (" -- publish outcome unknown: verify the "
                                                     "account manually" if c["kind"] == "AMBIGUOUS"
                                                     else ""))
            A.write_halt(sd, account, reason=f"{c['kind']}: {msg[:200]}", at=now.isoformat(),
                         item_ref=it["item_ref"], code=c["code"])
            res.update(result="BLOCKED", action="PUBLISH_ATTEMPTED", account_halted=True,
                       error_kind=c["kind"])
        else:
            it.update(status="FAILED", error=msg)
            res.update(result="FAILED", action="PUBLISH_ATTEMPTED", error_kind=c["kind"])
    else:
        it.update(status="POSTED", media_id=out["media_id"], post_url=out["post_url"],
                  published_at=out["published_at"], error=None)
        receipt = _receipt(q, it, out, env)
        rp = A.account_dir(sd, account) / "receipts" / f"{it['item_ref']}.json"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        res.update(result="POSTED", action="PUBLISH_ATTEMPTED",
                   receipt={k: receipt[k] for k in ("item_ref", "media_id", "post_url",
                                                    "published_at")},
                   temp_asset_deleted=out["temp_asset_deleted"])
    finally:
        if meta is not None:
            res["meta_calls"] = list(meta.calls)
            res["publish_calls"] = meta.calls.count("media_publish")
    _finish(q)
    res["persisted"] = _commit(commit, f"affiliate: {res['result']} {q['queue_ref']}#{it['position']}")
    return res


def _finish(q):
    if all(i["status"] in AQ.DONE_ITEM_STATES for i in q["items"]):
        q["state"] = "COMPLETED"
    save_queue(q)


def _commit(commit, msg) -> bool:
    return True if commit is None else bool(commit(msg))


def _publish_one(q, it, sd, *, account, cfg, key_b64, gh, meta, workdir, clock):
    try:
        key = B.key_from_b64(key_b64)
    except B.BundleError as e:
        raise Blocked(str(e)) from None
    if B.key_id(key) != q["key_id"]:
        raise Blocked("WRONG_KEY: media key id != queue key id")
    p = Path(q["_path"]).parent / q["manifest_file"]
    raw = p.read_bytes()
    if B.sha256_bytes(raw) != q["manifest_sha256"]:
        raise Blocked("manifest file sha256 != index")
    try:
        man = B.open_json(key, raw, queue_ref=q["queue_ref"])
    except B.BundleError as e:
        raise Blocked(str(e)) from None
    route = ROUTES[account]
    if (man.get("INSTAGRAM_PROFILE") != account or man.get("CHANNEL_PROFILE") != route["channel_profile"]
            or man.get("INSTAGRAM_ACCOUNT") != route["username"]):
        raise Blocked("WRONG_ACCOUNT: manifest routing != this account")
    mi = next((m for m in man["items"] if m["item_ref"] == it["item_ref"]), None)
    if mi is None:
        raise Blocked("item not in encrypted manifest")
    for k in ("position", "source_sha256", "target_at", "item_seq"):
        if str(mi.get(k)) != str(it[k]):
            raise Blocked(f"index/manifest mismatch on {k}")
    if mi.get("INSTAGRAM_PROFILE") != account:
        raise Blocked("WRONG_ACCOUNT: item routed to another account")
    lint = AQ.lint_copy(mi["final_caption"], language=route["language"], program=route["program"],
                        internal_ids=(mi.get("JOB_ID"), mi.get("PROJECT_ID")))
    if lint["errors"]:
        raise Blocked("public copy lint: " + "; ".join(lint["errors"][:3]))

    rel = gh.release(STAGING_TAG)
    asset = next((a for a in gh.assets(rel["id"]) if a["name"] == it["bundle_asset"]), None) \
        if rel else None
    if asset is None:
        raise Blocked("encrypted bundle not staged")
    blob = gh.download_public(asset["browser_download_url"])
    if B.sha256_bytes(blob) != it["bundle_sha256"]:
        raise Blocked("bundle sha256 != index")
    try:
        ident, mp4 = B.open_media(key, blob, item_ref=it["item_ref"],
                                  source_sha256=it["source_sha256"])
    except B.BundleError as e:
        raise Blocked(str(e)) from None
    del blob
    for k in ("PROJECT_ID", "JOB_ID", "INSTAGRAM_PROFILE"):
        if ident.get(k) != mi.get(k):
            raise Blocked(f"bundle identity {k} != manifest")

    meta.identity(route["username"])  # /me: token's user_id == secret id AND username
    bridge = gh.release(ghapi.BRIDGE_TAG)
    if bridge is None:
        raise RuntimeError("media bridge release missing")
    fd, tmp = tempfile.mkstemp(suffix=".mp4", dir=workdir)
    tmp_asset, deleted = None, None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(mp4)
        del mp4
        data = Path(tmp).read_bytes()
        if B.sha256_bytes(data) != it["source_sha256"]:
            raise Blocked("SOURCE_SHA256 mismatch on runner file")
        tmp_asset = gh.upload_asset(bridge, temp_name(), data, "video/mp4")
        gh.verify_public(tmp_asset["browser_download_url"], len(data))
        cid = meta.create_reel(tmp_asset["browser_download_url"], mi["final_caption"])
        meta.wait_finished(cid)
        mid = meta.publish(cid)
        published_at = clock().isoformat()
        link = meta.permalink(mid)
    finally:
        if tmp_asset is not None:
            try:
                deleted = gh.delete_asset(tmp_asset["id"])
            except ghapi.GitHubError:
                deleted = False  # orphan cleanup removes it on a later run
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return {"media_id": mid, "post_url": link, "published_at": published_at,
            "creation_id": cid, "temp_asset_deleted": bool(deleted),
            "identity_enc": base64.b64encode(B.seal_json(
                key, queue_ref=it["item_ref"],
                obj={k: mi.get(k) for k in ("QUEUE_ID", "QUEUE_VERSION", "PROJECT_ID", "JOB_ID",
                                            "PRODUCT_ID", "PRODUCT_NAME", "SOURCE_SHA256",
                                            "SOURCE_FILE_PATH", "CHANNEL_PROFILE",
                                            "INSTAGRAM_PROFILE", "INSTAGRAM_ACCOUNT")})).decode()}


def _receipt(q, it, out, env) -> dict:
    return {"schema_version": SCHEMA_VERSION, "status": "POSTED", "item_ref": it["item_ref"],
            "queue_ref": q["queue_ref"], "position": it["position"], "account": q["account"],
            "channel_profile": q["channel_profile"], "source_sha256": it["source_sha256"],
            "media_id": out["media_id"], "post_url": out["post_url"],
            "published_at": out["published_at"], "creation_id": out["creation_id"],
            "run_id": env.get("GITHUB_RUN_ID", ""), "identity_enc": out["identity_enc"]}
