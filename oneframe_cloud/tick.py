"""
oneframe_cloud/tick.py  -- ONE scheduler pass on the GitHub runner, then exit.

  load state -> validate -> orphan cleanup -> staged-asset check
  -> CUTOVER gate (DISABLED => NOOP, no Instagram call of any kind)
  -> decide (APPROVED queues only; due in Asia/Seoul; daily limit; spacing;
     duplicate vs cloud receipts AND the local Module 5 history export)
  -> persist POSTING (pushed BEFORE any Meta call: an interrupted run can
     never lead to a second post -- the next run sees POSTING and halts)
  -> decrypt + verify SOURCE_SHA256 -> temporary random-named plaintext asset
  -> /media -> FINISHED -> /media_publish (once) -> delete temp asset + file
  -> persist POSTED + receipt, or FAILED/BLOCKED (never the next Short)

At most ONE publish per tick.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from . import bundle as B
from . import ghapi
from . import queue as Q
from .gate import read_gate
from .redact import scrub


class Blocked(Exception):
    """Non-retryable: the item is BLOCKED and its queue halts."""


def save_queue(q: dict):
    data = {k: v for k, v in q.items() if not k.startswith("_")}
    Path(q["_path"]).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                encoding="utf-8")


def staged_check(gh, queues) -> dict:
    rel = gh.release(ghapi.STAGING_TAG)
    if rel is None:
        return {"release": None, "present": 0, "expected": sum(len(q["items"]) for q in queues),
                "missing": "staging release absent"}
    have = {a["name"]: a for a in gh.assets(rel["id"])}
    exp, missing, bad = 0, [], []
    for q in queues:
        for i in q["items"]:
            exp += 1
            a = have.get(i["bundle_asset"])
            if a is None:
                missing.append(f"{q['queue_ref']}#{i['position']}")
            elif int(a.get("size") or -1) != int(i["bundle_bytes"]):
                bad.append(f"{q['queue_ref']}#{i['position']}")
    plaintext = [n for n in have if n.lower().endswith((".mp4", ".mov"))]
    return {"release": rel["tag_name"], "expected": exp, "present": exp - len(missing),
            "missing": missing, "size_mismatch": bad, "plaintext_assets": plaintext}


def tick(state_dir, *, now: datetime, env: dict, gh=None, meta_factory=None, commit=None,
         workdir=None, clock=None) -> dict:
    sd = Path(state_dir)
    gate = read_gate(sd, env)
    queues = Q.load_queues(sd)
    receipts = Q.load_receipts(sd)
    local_posts = Q.load_local_posts(sd)
    res = {"schema_version": SCHEMA_VERSION, "tick_at": now.isoformat(), "gate": gate.to_dict(),
           "schedule": Q.schedule_view(queues, now=now), "meta_calls": [],
           "publish_calls": 0}
    errs = Q.validate_all(queues)
    if gh is not None:
        try:
            res["staged"] = staged_check(gh, queues)
        except ghapi.GitHubError as e:
            res["staged"] = {"error": scrub(e)}
        if gh.token:
            try:
                bridge = gh.release(ghapi.BRIDGE_TAG)
                res["orphans_deleted"] = ghapi.cleanup_orphans(gh, bridge, now=now) if bridge else []
            except ghapi.GitHubError as e:
                res["orphans_deleted"] = {"error": scrub(e)}
    if errs:
        res.update(result="INVALID_STATE", action="NOOP", errors=errs)
        return res
    decision = Q.decide(queues, now=now, receipts=receipts, local_posts=local_posts)
    summary = {k: v for k, v in decision.items() if k not in ("item", "notes")}
    if decision.get("item"):
        summary["item"] = {k: decision["item"][k] for k in ("position", "item_ref", "shorts_seq",
                                                            "target_at", "status")}
    if not gate.enabled:
        res.update(result="CUTOVER_DISABLED", action="NOOP", would_decide=summary)
        return res
    res["decision"] = summary
    if decision["action"] == "BLOCK_DUPLICATE":
        q = next(x for x in queues if x["queue_ref"] == decision["queue_ref"])
        it = next(i for i in q["items"] if i["item_ref"] == decision["item"]["item_ref"])
        it.update(status="BLOCKED", error=f"duplicate: {decision['detail']}")
        save_queue(q)
        _commit(commit, f"oneframe: BLOCK duplicate {q['queue_ref']}#{it['position']}")
        res.update(result="BLOCKED_DUPLICATE", action="NOOP")
        return res
    if decision["action"] != "PUBLISH":
        res.update(result=decision["action"], action="NOOP")
        return res

    q = next(x for x in queues if x["queue_ref"] == decision["queue_ref"])
    it = next(i for i in q["items"] if i["item_ref"] == decision["item"]["item_ref"])
    it.update(status="POSTING", attempt_count=int(it["attempt_count"]) + 1,
              last_attempt_at=now.isoformat(), error=None)
    save_queue(q)
    if not _commit(commit, f"oneframe: POSTING {q['queue_ref']}#{it['position']}"):
        res.update(result="ABORTED_STATE_NOT_PERSISTED", action="NOOP")
        return res  # nothing was sent to Meta

    meta = None
    try:
        meta = meta_factory(gate)
        out = _publish_one(q, it, sd, env=env, gh=gh, meta=meta, workdir=workdir,
                           clock=clock or (lambda: datetime.now(timezone.utc)))
    except Blocked as e:
        it.update(status="BLOCKED", error=scrub(e)[:500])
        res.update(result="BLOCKED", action="PUBLISH_ATTEMPTED")
    except Exception as e:  # noqa: BLE001 -- never a fake success
        ambiguous = getattr(e, "ambiguous", False)
        phase = getattr(e, "phase", "")
        if ambiguous or phase in ("identity", "config"):
            it.update(status="BLOCKED", error=scrub(f"{phase or 'error'}: {e}")[:500]
                      + (" -- publish outcome unknown: verify the account manually" if ambiguous else ""))
            res.update(result="BLOCKED", action="PUBLISH_ATTEMPTED")
        else:
            it.update(status="FAILED", error=scrub(f"{phase or 'error'}: {e}")[:500])
            res.update(result="FAILED", action="PUBLISH_ATTEMPTED")
    else:
        it.update(status="POSTED", media_id=out["media_id"], post_url=out["post_url"],
                  published_at=out["published_at"], error=None)
        receipt = _receipt(q, it, out, env)
        rp = sd / "receipts" / f"{it['item_ref']}.json"
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
    if all(i["status"] in Q.DONE_ITEM_STATES for i in q["items"]):
        q["state"] = "COMPLETED"
    save_queue(q)
    res["persisted"] = _commit(commit, f"oneframe: {res['result']} {q['queue_ref']}#{it['position']}")
    return res


def _commit(commit, msg) -> bool:
    return True if commit is None else bool(commit(msg))


def _manifest(q, sd, key):
    p = sd / "queues" / q["manifest_file"]
    raw = p.read_bytes()
    if B.sha256_bytes(raw) != q["manifest_sha256"]:
        raise Blocked("manifest file sha256 != index")
    return B.open_json(key, raw, queue_ref=q["queue_ref"])


def _publish_one(q, it, sd, *, env, gh, meta, workdir, clock):
    try:
        key = B.key_from_b64(env.get("ONEFRAME_MEDIA_KEY_B64", ""))
    except B.BundleError as e:
        raise Blocked(str(e)) from None
    if B.key_id(key) != q["key_id"]:
        raise Blocked("WRONG_KEY: media key id != queue key id")
    try:
        man = _manifest(q, sd, key)
    except B.BundleError as e:
        raise Blocked(str(e)) from None
    mi = next((m for m in man["items"] if m["item_ref"] == it["item_ref"]), None)
    if mi is None:
        raise Blocked("item not in encrypted manifest")
    for k in ("position", "source_sha256", "target_at", "shorts_seq"):
        if str(mi[k]) != str(it[k]):
            raise Blocked(f"index/manifest mismatch on {k}")
    if man.get("account") != q["account"]:
        raise Blocked("manifest account != queue account")

    rel = gh.release(ghapi.STAGING_TAG)
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
    for k in ("PROJECT_ID", "JOB_ID", "SHORTS_ID"):
        if ident.get(k) != mi.get(k):
            raise Blocked(f"bundle identity {k} != manifest")

    meta.identity(man.get("username") or "")
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
        tmp_asset = gh.upload_asset(bridge, ghapi.temp_name(), data, "video/mp4")
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
                deleted = False  # orphan cleanup removes it on a later tick
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return {"media_id": mid, "post_url": link, "published_at": published_at,
            "creation_id": cid, "temp_asset_deleted": bool(deleted),
            "identity_enc": base64.b64encode(B.seal_json(
                key, queue_ref=it["item_ref"],
                obj={k: mi.get(k) for k in ("QUEUE_ID", "QUEUE_VERSION", "PROJECT_ID", "JOB_ID",
                                            "SONG_ID", "SHORTS_ID", "SOURCE_SHA256",
                                            "account_profile", "channel_profile")})).decode()}


def _receipt(q, it, out, env) -> dict:
    return {"schema_version": SCHEMA_VERSION, "status": "POSTED", "item_ref": it["item_ref"],
            "queue_ref": q["queue_ref"], "position": it["position"], "shorts_seq": it["shorts_seq"],
            "account": q["account"], "source_sha256": it["source_sha256"],
            "media_id": out["media_id"], "post_url": out["post_url"],
            "published_at": out["published_at"], "creation_id": out["creation_id"],
            "run_id": env.get("GITHUB_RUN_ID", ""), "identity_enc": out["identity_enc"]}
