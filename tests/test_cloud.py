"""
cloud/tests/test_cloud.py  -- offline tests for oneframe_cloud (no network, no Meta, no GitHub).

  python3 -m unittest discover -s cloud/tests -t cloud     (from the Module 5 root)
  python3 -m unittest discover -s tests                    (from the cloud repo root)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oneframe_cloud import SCHEMA_VERSION  # noqa: E402
from oneframe_cloud import bundle as B  # noqa: E402
from oneframe_cloud import ghapi, redact  # noqa: E402
from oneframe_cloud import queue as Q  # noqa: E402
from oneframe_cloud.gate import read_gate  # noqa: E402
from oneframe_cloud.meta import MetaClient  # noqa: E402
from oneframe_cloud.tick import tick  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
KEY = os.urandom(32)
KEY_B64 = base64.b64encode(KEY).decode()
TOKEN = "IGQWRtestTOKENvalue_ABCDEFG1234567890"
IGUID = "17841400000000000"


def kst(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=KST)


def slots(start_day, n):
    out, d = [], start_day
    while len(out) < n:
        for h in (12, 18):
            if len(out) < n:
                out.append(datetime.combine(d, datetime.min.time(), tzinfo=KST).replace(hour=h))
        d += timedelta(days=1)
    return out


class FakeGitHub:
    def __init__(self):
        self.token = "ghs_faketoken"
        self.rel = {ghapi.STAGING_TAG: {"id": 1, "tag_name": ghapi.STAGING_TAG, "assets": {}},
                    ghapi.BRIDGE_TAG: {"id": 2, "tag_name": ghapi.BRIDGE_TAG, "assets": {}}}
        self.calls, self.blobs, self.next_id, self.deleted = [], {}, 100, []
        self.fail_delete = False

    def release(self, tag):
        r = self.rel.get(tag)
        return {"id": r["id"], "tag_name": tag} if r else None

    def _by_id(self, rid):
        return next(r for r in self.rel.values() if r["id"] == rid)

    def assets(self, rid):
        return list(self._by_id(rid)["assets"].values())

    def add(self, tag, name, data, created_at=None):
        self.next_id += 1
        a = {"id": self.next_id, "name": name, "size": len(data),
             "created_at": (created_at or datetime.now(timezone.utc)).isoformat(),
             "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{name}"}
        self.rel[tag]["assets"][name] = a
        self.blobs[a["browser_download_url"]] = data
        return a

    def download_public(self, url):
        self.calls.append("download")
        return self.blobs[url]

    def upload_asset(self, release, name, data, content_type="application/octet-stream"):
        self.calls.append(f"upload {name}")
        tag = release["tag_name"]
        return self.add(tag, name, data)

    def verify_public(self, url, size):
        assert len(self.blobs[url]) == size
        return {"http": 206}

    def delete_asset(self, aid):
        if self.fail_delete:
            raise ghapi.GitHubError("HTTP 500")
        for r in self.rel.values():
            for n, a in list(r["assets"].items()):
                if a["id"] == aid:
                    del r["assets"][n]
                    self.deleted.append(n)
        return True


class FakeMeta:
    """Transport for MetaClient: records every endpoint; configurable failures."""

    def __init__(self, *, fail_at=None, uid=IGUID, username="oneframe_999"):
        self.log, self.fail_at, self.uid, self.username = [], fail_at, uid, username
        self.media_ids = iter(range(18000000000000001, 18000000000099999))

    def __call__(self, method, url, body=None, timeout=120):
        path = url.split("/v21.0/")[1].split("?")[0]
        ep = ("me" if path == "me" else "media_publish" if path.endswith("/media_publish")
              else "media" if path.endswith("/media") else "status" if "fields=status_code" in url
              else "permalink")
        self.log.append(ep)
        if self.fail_at == ep:
            return 500, b'{"error":{"message":"boom"}}'
        if ep == "me":
            return 200, json.dumps({"user_id": self.uid, "username": self.username}).encode()
        if ep == "media":
            return 200, b'{"id":"cid-1"}'
        if ep == "status":
            return 200, b'{"status_code":"FINISHED"}'
        if ep == "media_publish":
            return 200, json.dumps({"id": str(next(self.media_ids))}).encode()
        return 200, b'{"permalink":"https://www.instagram.com/reel/XYZ/"}'


def build_state(tmp: Path, gh: FakeGitHub, songs, *, key=KEY):
    """songs: [(song_seq, state, [slot datetimes])] -> writes state/ like the local stager."""
    sd = tmp / "state"
    (sd / "queues").mkdir(parents=True)
    (sd / "receipts").mkdir()
    (sd / "control.json").write_text(json.dumps({"ONEFRAME_CLOUD_CUTOVER": "DISABLED"}))
    media = {}
    for seq, state, sl in songs:
        qref = f"ofq-s{seq:02d}-v1-test{seq}"
        items, mitems = [], []
        for pos, t in enumerate(sl, 1):
            mp4 = f"song{seq}-short{pos}-".encode() * 50
            sha = hashlib.sha256(mp4).hexdigest()
            iref = "ofi-" + hashlib.sha256(f"IG_ONEFRAME_999|{sha}".encode()).hexdigest()[:20]
            ident = {"PROJECT_ID": f"P{seq}", "JOB_ID": f"P{seq}-SHORTS-{pos:02d}",
                     "SHORTS_ID": f"SHORTS_{pos:02d}"}
            blob = B.seal_media(key, item_ref=iref, source_sha256=sha, mp4=mp4, identity=ident)
            name = f"ofb-{iref[4:]}-{B.sha256_bytes(blob)[:12]}.bin"
            gh.add(ghapi.STAGING_TAG, name, blob)
            media[iref] = mp4
            items.append({"position": pos, "item_ref": iref, "shorts_seq": pos, "source_sha256": sha,
                          "bundle_asset": name, "bundle_sha256": B.sha256_bytes(blob),
                          "bundle_bytes": len(blob), "target_at": t.isoformat(), "status": "QUEUED",
                          "attempt_count": 0, "media_id": None, "post_url": None,
                          "published_at": None, "error": None, "last_attempt_at": None})
            mitems.append({**ident, "item_ref": iref, "position": pos, "shorts_seq": pos,
                           "source_sha256": sha, "SOURCE_SHA256": sha, "target_at": t.isoformat(),
                           "final_caption": f"caption {seq}/{pos}\n\n#tag{seq}"})
        man = {"queue_ref": qref, "account": "IG_ONEFRAME_999", "username": "oneframe_999",
               "items": mitems}
        mraw = B.seal_json(key, queue_ref=qref, obj=man)
        (sd / "queues" / f"{qref}.manifest.enc").write_bytes(mraw)
        q = {"schema_version": SCHEMA_VERSION, "queue_ref": qref, "queue_version": 1, "song_seq": seq,
             "state": state, "account": "IG_ONEFRAME_999", "timezone": "Asia/Seoul",
             "daily_limit": 2, "times": ["12:00", "18:00"], "min_spacing_hours": 4,
             "max_attempts": 3, "retry_backoff_minutes": 30, "key_id": B.key_id(key),
             "manifest_file": f"{qref}.manifest.enc", "manifest_sha256": B.sha256_bytes(mraw),
             "items": items}
        (sd / "queues" / f"{qref}.json").write_text(json.dumps(q, indent=1))
    return sd, media


def enable(sd, at):
    (sd / "control.json").write_text(json.dumps({"ONEFRAME_CLOUD_CUTOVER": "ENABLED",
                                                 "cutover_enabled_at": at.isoformat()}))
    (sd / "local_reconcile.json").write_text(json.dumps({
        "local_scheduler": "DISABLED_CONFIRMED", "generated_at": (at + timedelta(minutes=1)).isoformat(),
        "posts": []}))


ENV_ON = {"ONEFRAME_CLOUD_CUTOVER": "ENABLED", "ONEFRAME_MEDIA_KEY_B64": KEY_B64,
          "ONEFRAME_IG_ACCESS_TOKEN": TOKEN, "ONEFRAME_IG_USER_ID": IGUID}


class Base(unittest.TestCase):
    def setUp(self):
        redact.reset()
        self.tmp = Path(tempfile.mkdtemp())
        self.gh = FakeGitHub()
        self.meta = FakeMeta()
        self.commits = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def factory(self, gate):
        return MetaClient(token=TOKEN, ig_user_id=IGUID, gate=gate, transport=self.meta,
                          sleep=lambda s: None)

    def commit(self, msg):
        self.commits.append(msg)
        return True

    def run_tick(self, sd, now, env=None):
        return tick(sd, now=now, env=ENV_ON if env is None else env, gh=self.gh,
                    meta_factory=self.factory, commit=self.commit, workdir=str(self.tmp),
                    clock=lambda: now)

    def two_songs(self, s1="APPROVED", s2="DRAFT"):
        return build_state(self.tmp, self.gh, [
            (1, s1, slots(datetime(2026, 10, 15).date(), 10)),
            (2, s2, slots(datetime(2026, 10, 20).date(), 10))])


class QueueRules(Base):
    def test_parse_and_validate_two_songs(self):
        sd, _ = self.two_songs()
        qs = Q.load_queues(sd)
        self.assertEqual(len(qs), 2)
        self.assertEqual(Q.validate_all(qs), [])

    def test_timezone_and_slot_times(self):
        sd, _ = self.two_songs()
        q = Q.load_queues(sd)[0]
        q["items"][0]["target_at"] = kst(2026, 10, 15, 13).isoformat()
        self.assertTrue(any("not a configured slot" in e for e in Q.validate_queue(q)))
        q["items"][0]["target_at"] = "2026-10-15T12:00:00"  # naive -> rejected
        self.assertTrue(any("timezone" in e for e in Q.validate_queue(q)))
        # the same instant expressed in UTC is fine (12:00 KST == 03:00Z)
        q["items"][0]["target_at"] = "2026-10-15T03:00:00+00:00"
        self.assertEqual(Q.validate_queue(q), [])

    def test_two_per_day_and_spacing(self):
        sd, _ = self.two_songs()
        q = Q.load_queues(sd)[0]
        q["times"] = ["12:00", "18:00", "23:00"]
        q["items"][2]["target_at"] = kst(2026, 10, 15, 23).isoformat()
        errs = Q.validate_queue(q)
        self.assertTrue(any("daily_limit" in e for e in errs), errs)
        q2 = Q.load_queues(sd)[0]
        q2["times"] = ["12:00", "14:00", "18:00"]
        q2["items"][1]["target_at"] = kst(2026, 10, 15, 14).isoformat()
        self.assertTrue(any("< 4" in e for e in Q.validate_queue(q2)))
        q3 = Q.load_queues(sd)[0]
        q3["min_spacing_hours"] = 3
        self.assertTrue(Q.validate_queue(q3))

    def test_order_and_cross_queue_collision(self):
        sd, _ = self.two_songs()
        qs = Q.load_queues(sd)
        qs[0]["items"][1]["shorts_seq"] = 9
        self.assertTrue(any("order" in e for e in Q.validate_queue(qs[0])))
        qs = Q.load_queues(sd)
        qs[1]["items"][0]["target_at"] = qs[0]["items"][0]["target_at"]
        qs[1]["items"] = qs[1]["items"][:1]
        self.assertTrue(any("SLOT_COLLISION" in e for e in Q.validate_all(qs)))

    def test_multi_song_sequencing(self):
        sd, _ = self.two_songs(s1="APPROVED", s2="APPROVED")
        qs = Q.load_queues(sd)
        for i in qs[0]["items"]:
            i.update(status="POSTED", media_id="m" + i["item_ref"], published_at=i["target_at"])
        d = Q.decide(qs, now=kst(2026, 10, 20, 12, 5), receipts=[], local_posts=[])
        self.assertEqual(d["action"], "PUBLISH")
        self.assertEqual(d["queue_ref"], qs[1]["queue_ref"])
        self.assertEqual(d["item"]["position"], 1)
        # song 2 is not due before its own first slot
        d = Q.decide(qs, now=kst(2026, 10, 20, 11, 50), receipts=[], local_posts=[])
        self.assertEqual(d["action"], "NOOP")

    def test_non_approved_states_never_publish(self):
        sd, _ = self.two_songs(s1="MIGRATION_PENDING", s2="DRAFT")
        d = Q.decide(Q.load_queues(sd), now=kst(2026, 10, 25, 12), receipts=[], local_posts=[])
        self.assertEqual(d["action"], "NOOP")

    def test_daily_limit_and_spacing_across_local_and_cloud(self):
        sd, _ = self.two_songs()
        qs = Q.load_queues(sd)
        now = kst(2026, 10, 15, 18, 5)
        local = [{"account": "IG_ONEFRAME_999", "published_at": kst(2026, 10, 15, 9).isoformat(),
                  "source_sha256": "a" * 64, "media_id": "L1"},
                 {"account": "IG_ONEFRAME_999", "published_at": kst(2026, 10, 15, 13).isoformat(),
                  "source_sha256": "b" * 64, "media_id": "L2"}]
        self.assertEqual(Q.decide(qs, now=now, receipts=[], local_posts=local)["action"],
                         "WAIT_DAILY_LIMIT")
        local = local[1:]
        local[0]["published_at"] = kst(2026, 10, 15, 16).isoformat()
        self.assertEqual(Q.decide(qs, now=now, receipts=[], local_posts=local)["action"],
                         "WAIT_SPACING")


class Gate(Base):
    def test_default_disabled_noop_zero_meta_calls(self):
        sd, _ = self.two_songs()
        built = []
        res = tick(sd, now=kst(2026, 10, 15, 12, 10), env={}, gh=self.gh,
                   meta_factory=lambda g: built.append(1), commit=self.commit)
        self.assertEqual(res["result"], "CUTOVER_DISABLED")
        self.assertEqual(res["would_decide"]["action"], "PUBLISH")
        self.assertEqual((built, self.meta.log, self.commits, res["publish_calls"]), ([], [], [], 0))
        self.assertEqual(res["staged"]["present"], 20)

    def test_each_switch_alone_is_not_enough(self):
        sd, _ = self.two_songs()
        now = kst(2026, 10, 14, 23)
        enable(sd, now)
        self.assertTrue(read_gate(sd, {"ONEFRAME_CLOUD_CUTOVER": "ENABLED"}).enabled)
        self.assertFalse(read_gate(sd, {}).enabled)
        self.assertFalse(read_gate(sd, {"ONEFRAME_CLOUD_CUTOVER": "enabled"}).enabled)
        (sd / "local_reconcile.json").unlink()
        self.assertFalse(read_gate(sd, {"ONEFRAME_CLOUD_CUTOVER": "ENABLED"}).enabled)
        enable(sd, now)
        rec = json.loads((sd / "local_reconcile.json").read_text())
        rec["generated_at"] = (now - timedelta(hours=1)).isoformat()  # stale export
        (sd / "local_reconcile.json").write_text(json.dumps(rec))
        self.assertFalse(read_gate(sd, {"ONEFRAME_CLOUD_CUTOVER": "ENABLED"}).enabled)

    def test_meta_client_refuses_closed_gate(self):
        sd, _ = self.two_songs()
        with self.assertRaises(PermissionError):
            MetaClient(token=TOKEN, ig_user_id=IGUID, gate=read_gate(sd, {}), transport=self.meta)
        self.assertEqual(self.meta.log, [])


class Publish(Base):
    def setUp(self):
        super().setUp()
        self.sd, self.media = self.two_songs()
        enable(self.sd, kst(2026, 10, 15, 0))

    def test_one_post_per_tick_and_receipt(self):
        # both song-1 slot 1 and 2 overdue -> still ONE publish
        res = self.run_tick(self.sd, kst(2026, 10, 15, 19))
        self.assertEqual(res["result"], "POSTED", res)
        self.assertEqual(self.meta.log.count("media_publish"), 1)
        self.assertEqual(self.meta.log, ["me", "media", "status", "media_publish", "permalink"])
        q = Q.load_queues(self.sd)[0]
        self.assertEqual([i["status"] for i in q["items"][:2]], ["POSTED", "QUEUED"])
        rc = Q.load_receipts(self.sd)
        self.assertEqual(len(rc), 1)
        self.assertEqual(rc[0]["source_sha256"], q["items"][0]["source_sha256"])
        ident = B.open_json(KEY, base64.b64decode(rc[0]["identity_enc"]), queue_ref=rc[0]["item_ref"])
        self.assertEqual(ident["SHORTS_ID"], "SHORTS_01")
        self.assertEqual(self.commits[0][:20], "oneframe: POSTING of")
        # temp plaintext removed from the bridge, nothing left on the runner
        self.assertEqual(self.gh.rel[ghapi.BRIDGE_TAG]["assets"], {})
        self.assertEqual(list(self.tmp.glob("*.mp4")), [])
        self.assertTrue(res["temp_asset_deleted"])
        # next tick: spacing (posted at ~now) blocks the second item
        res2 = self.run_tick(self.sd, kst(2026, 10, 15, 19, 15))
        self.assertEqual(res2["result"], "WAIT_SPACING")
        self.assertEqual(self.meta.log.count("media_publish"), 1)

    def test_temp_upload_is_plaintext_of_source(self):
        uploaded = {}
        orig = self.gh.upload_asset

        def spy(release, name, data, ct="application/octet-stream"):
            uploaded[name] = (release["tag_name"], data)
            return orig(release, name, data, ct)
        self.gh.upload_asset = spy
        self.run_tick(self.sd, kst(2026, 10, 15, 12, 10))
        (name, (tag, data)), = uploaded.items()
        self.assertRegex(name, ghapi.TMP_RE)
        self.assertEqual(tag, ghapi.BRIDGE_TAG)
        self.assertIn(data, self.media.values())

    def test_failed_publish_does_not_advance(self):
        self.meta.fail_at = "media"
        res = self.run_tick(self.sd, kst(2026, 10, 15, 12, 10))
        self.assertEqual(res["result"], "FAILED")
        q = Q.load_queues(self.sd)[0]
        self.assertEqual(q["items"][0]["status"], "FAILED")
        self.assertEqual(q["items"][0]["attempt_count"], 1)
        self.assertEqual(q["items"][1]["status"], "QUEUED")
        self.assertNotIn("media_publish", self.meta.log)
        self.assertEqual(self.gh.rel[ghapi.BRIDGE_TAG]["assets"], {})  # temp deleted on failure too
        # backoff: no retry inside 30 min, and item 2 never jumps ahead (even when due)
        self.meta.fail_at = None
        res = self.run_tick(self.sd, kst(2026, 10, 15, 18, 1))
        self.assertEqual(res["result"], "POSTED")
        q = Q.load_queues(self.sd)[0]
        self.assertEqual([i["status"] for i in q["items"][:2]], ["POSTED", "QUEUED"])

    def test_retry_cap_halts(self):
        self.meta.fail_at = "media"
        t = kst(2026, 10, 15, 12, 10)
        for k in range(3):
            self.run_tick(self.sd, t + timedelta(minutes=31 * k))
        res = self.run_tick(self.sd, t + timedelta(hours=5))
        self.assertEqual(res["result"], "HALTED")
        self.assertEqual(self.meta.log.count("media"), 3)

    def test_ambiguous_publish_blocks(self):
        self.meta.fail_at = "media_publish"
        res = self.run_tick(self.sd, kst(2026, 10, 15, 12, 10))
        self.assertEqual(res["result"], "BLOCKED")
        self.assertIn("verify the account manually",
                      Q.load_queues(self.sd)[0]["items"][0]["error"])

    def test_interrupted_posting_halts_never_reposts(self):
        q = Q.load_queues(self.sd)[0]
        q["items"][0]["status"] = "POSTING"
        Path(q["_path"]).write_text(json.dumps({k: v for k, v in q.items() if k != "_path"}))
        res = self.run_tick(self.sd, kst(2026, 10, 15, 12, 30))
        self.assertEqual(res["result"], "HALTED")
        self.assertEqual(self.meta.log, [])

    def test_not_persisted_means_not_published(self):
        res = tick(self.sd, now=kst(2026, 10, 15, 12, 10), env=ENV_ON, gh=self.gh,
                   meta_factory=self.factory, commit=lambda m: False)
        self.assertEqual(res["result"], "ABORTED_STATE_NOT_PERSISTED")
        self.assertEqual(self.meta.log, [])

    def test_duplicate_receipt_never_reposted(self):
        q = Q.load_queues(self.sd)[0]
        (self.sd / "receipts" / "x.json").write_text(json.dumps({
            "account": "IG_ONEFRAME_999", "item_ref": "other",
            "source_sha256": q["items"][0]["source_sha256"], "media_id": "M0",
            "published_at": kst(2026, 10, 14, 12).isoformat()}))
        res = self.run_tick(self.sd, kst(2026, 10, 15, 12, 10))
        self.assertEqual(res["result"], "ALREADY_POSTED")
        self.assertEqual(self.meta.log, [])
        it = Q.load_queues(self.sd)[0]["items"][0]
        self.assertEqual((it["status"], it["media_id"]), ("POSTED", "M0"))
        # a receipt without proof (no media id) can only block, never count as posted
        (self.sd / "receipts" / "y.json").write_text(json.dumps({
            "account": "IG_ONEFRAME_999", "item_ref": q["items"][1]["item_ref"],
            "source_sha256": q["items"][1]["source_sha256"], "media_id": None}))
        res = self.run_tick(self.sd, kst(2026, 10, 15, 18, 10))
        self.assertEqual(res["result"], "BLOCKED_DUPLICATE")
        self.assertEqual(self.meta.log, [])

    def test_local_history_reconcile_never_reposts(self):
        """Items the local scheduler already posted before cutover: recorded with the
        local proof, one per tick, the queue keeps its order, no Meta call."""
        q = Q.load_queues(self.sd)[0]
        rec = json.loads((self.sd / "local_reconcile.json").read_text())
        rec["posts"] = [{"account": "IG_ONEFRAME_999", "source_sha256": q["items"][k]["source_sha256"],
                         "media_id": f"LOCAL{k}", "published_at": q["items"][k]["target_at"]}
                        for k in (0, 1)]
        (self.sd / "local_reconcile.json").write_text(json.dumps(rec))
        now = kst(2026, 10, 16, 12, 10)
        self.assertEqual(self.run_tick(self.sd, now)["result"], "ALREADY_POSTED")
        self.assertEqual(self.run_tick(self.sd, now)["result"], "ALREADY_POSTED")
        res = self.run_tick(self.sd, now)
        self.assertEqual(res["result"], "POSTED")
        self.assertEqual(self.meta.log.count("media_publish"), 1)
        st = [(i["status"], i["media_id"]) for i in Q.load_queues(self.sd)[0]["items"][:3]]
        self.assertEqual(st[:2], [("POSTED", "LOCAL0"), ("POSTED", "LOCAL1")])
        self.assertEqual(st[2][0], "POSTED")

    def test_wrong_key_blocks(self):
        env = dict(ENV_ON, ONEFRAME_MEDIA_KEY_B64=base64.b64encode(os.urandom(32)).decode())
        res = self.run_tick(self.sd, kst(2026, 10, 15, 12, 10), env=env)
        self.assertEqual(res["result"], "BLOCKED")
        self.assertIn("WRONG_KEY", Q.load_queues(self.sd)[0]["items"][0]["error"])
        self.assertNotIn("media", self.meta.log)

    def test_wrong_sha_blocks(self):
        q = Q.load_queues(self.sd)[0]
        name = q["items"][0]["bundle_asset"]
        a = self.gh.rel[ghapi.STAGING_TAG]["assets"][name]
        blob = bytearray(self.gh.blobs[a["browser_download_url"]])
        blob[-1] ^= 1
        self.gh.blobs[a["browser_download_url"]] = bytes(blob)
        res = self.run_tick(self.sd, kst(2026, 10, 15, 12, 10))
        self.assertEqual(res["result"], "BLOCKED")
        self.assertNotIn("media", self.meta.log)

    def test_wrong_account_identity_blocks(self):
        self.meta.uid = "999"
        res = self.run_tick(self.sd, kst(2026, 10, 15, 12, 10))
        self.assertEqual(res["result"], "BLOCKED")
        self.assertEqual(self.meta.log, ["me"])
        self.assertEqual(self.gh.rel[ghapi.BRIDGE_TAG]["assets"], {})

    def test_secret_redaction_in_errors_and_state(self):
        self.meta.fail_at = "media"
        res = self.run_tick(self.sd, kst(2026, 10, 15, 12, 10))
        blob = json.dumps(res) + "".join(p.read_text() for p in (self.sd).rglob("*.json"))
        for s in (TOKEN, KEY_B64, IGUID):
            self.assertNotIn(s, blob)
        self.assertEqual(redact.scrub(f"x?access_token={TOKEN}&a=1"), "x?access_token=***&a=1")


class Bundles(Base):
    def test_roundtrip_and_failures(self):
        mp4 = b"\x00\x00\x00\x18ftypmp42" + os.urandom(5000)
        sha = hashlib.sha256(mp4).hexdigest()
        blob = B.seal_media(KEY, item_ref="ofi-a", source_sha256=sha, mp4=mp4, identity={"X": 1})
        self.assertNotIn(mp4[:2000], blob)
        ident, out = B.open_media(KEY, blob, item_ref="ofi-a", source_sha256=sha)
        self.assertEqual((out, ident["X"]), (mp4, 1))
        with self.assertRaises(B.BundleError) as e:
            B.open_media(os.urandom(32), blob, item_ref="ofi-a", source_sha256=sha)
        self.assertEqual(e.exception.code, "WRONG_KEY_OR_TAMPERED")
        with self.assertRaises(B.BundleError):  # bundle moved onto another item
            B.open_media(KEY, blob, item_ref="ofi-b", source_sha256=sha)
        with self.assertRaises(B.BundleError) as e:
            B.seal_media(KEY, item_ref="ofi-a", source_sha256="0" * 64, mp4=mp4, identity={})
        self.assertEqual(e.exception.code, "SOURCE_SHA256_MISMATCH")
        with self.assertRaises(B.BundleError):
            B.key_from_b64(base64.b64encode(b"short").decode())
        n1 = B.seal_media(KEY, item_ref="ofi-a", source_sha256=sha, mp4=mp4, identity={})[5:17]
        n2 = B.seal_media(KEY, item_ref="ofi-a", source_sha256=sha, mp4=mp4, identity={})[5:17]
        self.assertNotEqual(n1, n2)


class Cleanup(Base):
    def test_orphans_only_prefixed_and_old(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=3)
        self.gh.add(ghapi.BRIDGE_TAG, "ofc-tmp-" + "a" * 32 + ".mp4", b"x", old)
        self.gh.add(ghapi.BRIDGE_TAG, "ofc-tmp-" + "b" * 32 + ".mp4", b"x", now)  # in flight
        self.gh.add(ghapi.BRIDGE_TAG, "sha256-abc.mp4", b"x", old)               # Module 5's own
        self.gh.add(ghapi.BRIDGE_TAG, "ofc-tmp-evil.mp4", b"x", old)             # wrong pattern
        self.gh.add(ghapi.STAGING_TAG, "ofb-x.bin", b"x", old)
        deleted = ghapi.cleanup_orphans(self.gh, self.gh.release(ghapi.BRIDGE_TAG), now=now)
        self.assertEqual(deleted, ["ofc-tmp-" + "a" * 32 + ".mp4"])
        self.assertEqual(len(self.gh.rel[ghapi.BRIDGE_TAG]["assets"]), 3)
        self.assertEqual(len(self.gh.rel[ghapi.STAGING_TAG]["assets"]), 1)

    def test_failed_temp_delete_left_for_orphan_cleanup(self):
        sd, _ = self.two_songs()
        enable(sd, kst(2026, 10, 15, 0))
        self.gh.fail_delete = True
        res = self.run_tick(sd, kst(2026, 10, 15, 12, 10))
        self.assertEqual(res["result"], "POSTED")
        self.assertFalse(res["temp_asset_deleted"])
        self.gh.fail_delete = False
        later = datetime.now(timezone.utc) + timedelta(hours=3)
        self.assertEqual(len(ghapi.cleanup_orphans(self.gh, self.gh.release(ghapi.BRIDGE_TAG),
                                                   now=later)), 1)


class ZeroCostGuard(unittest.TestCase):
    """The workflow must stay on free primitives only (public repo, standard runner)."""

    def setUp(self):
        self.wf = (ROOT / ".github" / "workflows" / "oneframe-scheduler.yml").read_text()
        self.body = "\n".join(ln.split("#", 1)[0] for ln in self.wf.splitlines())

    def test_triggers(self):
        self.assertNotIn("pull_request", self.body)
        self.assertIn("schedule:", self.body)
        self.assertIn("workflow_dispatch:", self.body)
        self.assertNotRegex(self.body, r"\bpush:")

    def test_runner_and_paid_features(self):
        runs_on = re.findall(r"runs-on:\s*(\S+)", self.body)
        self.assertEqual(runs_on, ["ubuntu-24.04"])
        for bad in ("larger", "self-hosted", "upload-artifact", "actions/cache", "lfs", "packages:",
                    "ghcr.io", "docker", "aws", "gcloud", "azure", "cloudflare", "r2.", "s3"):
            self.assertNotIn(bad, self.body.lower(), bad)

    def test_actions_pinned_or_absent_and_permissions_minimal(self):
        for use in re.findall(r"uses:\s*(\S+)", self.body):
            self.assertRegex(use, r"@[0-9a-f]{40}$")
        self.assertIn("permissions: {}", self.body)
        perms = re.findall(r"^\s+(\w[\w-]*):\s*(read|write)\b", self.body, re.M)
        self.assertEqual(perms, [("contents", "write")])
        self.assertIn("concurrency:", self.body)
        self.assertIn("cancel-in-progress: false", self.body)
        self.assertEqual(self.body.count("set -euo pipefail"), 3)

    def test_secrets_only_when_enabled(self):
        for name in ("ONEFRAME_IG_ACCESS_TOKEN", "ONEFRAME_IG_USER_ID", "ONEFRAME_MEDIA_KEY_B64"):
            line = next(ln for ln in self.body.splitlines() if f"secrets.{name}" in ln)
            self.assertIn("vars.ONEFRAME_CLOUD_CUTOVER == 'ENABLED' &&", line)

    def test_requirements_hash_pinned(self):
        req = (ROOT / "requirements-cloud.txt").read_text()
        pkgs = re.findall(r"^([a-z0-9_-]+)==([0-9][^ ]*) \\", req, re.M)
        self.assertEqual({p for p, _ in pkgs}, {"cryptography", "cffi", "pycparser"})
        self.assertGreaterEqual(req.count("--hash=sha256:"), 3)

    def test_control_default_disabled(self):
        c = json.loads((ROOT / "state" / "control.json").read_text())
        self.assertEqual(c["ONEFRAME_CLOUD_CUTOVER"], "DISABLED")


if __name__ == "__main__":
    unittest.main()
