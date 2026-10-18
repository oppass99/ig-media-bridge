"""
cloud/tests/test_affiliate_cloud.py  -- offline tests for affiliate_cloud (no network,
no Meta, no GitHub). Reuses the ONEFRAME fakes from test_cloud.py.

  python3 -m unittest discover -s tests          (from cloud/)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from affiliate_cloud import ROUTES, SCHEMA_VERSION, STAGING_TAG  # noqa: E402
from affiliate_cloud import accounts as A  # noqa: E402
from affiliate_cloud import aqueue as AQ  # noqa: E402
from affiliate_cloud import tick as T  # noqa: E402
from oneframe_cloud import bundle as B  # noqa: E402
from oneframe_cloud import ghapi, redact  # noqa: E402
from oneframe_cloud.meta import MetaClient  # noqa: E402
from oneframe_cloud.tick import tick as oneframe_tick  # noqa: E402
from test_cloud import (ENV_ON, FakeGitHub, FakeMeta, build_state, enable, kst,  # noqa: E402
                        slots)

UID = {"IG_SIDE_B99": "17841400000000001", "IG_ABLECREW": "17841400000000002"}
TOK = {"IG_SIDE_B99": "IGAAsideTOKENvalue_1234567890abcdef", "IG_ABLECREW": "IGAAableTOKENvalue_1234567890abcdef"}
KEYS = {a: os.urandom(32) for a in ROUTES}
CAPTION = {
    "IG_SIDE_B99": "정수리가 답답한 거, 은근 스트레스더라고요.\n\n궁금하시면 프로필 링크에서 '쿨링 샴푸 {n}' "
                   "검색해 보세요.\n\n#쿨링샴푸{n}\n\n이 게시물은 쿠팡 파트너스 활동의 일환으로, 이에 따른 "
                   "일정액의 수수료를 제공받습니다.",
    "IG_ABLECREW": "Closet packed and clothes slipping?\n\nIf you're interested, check my profile link "
                   "and search for \"Velvet Hangers {n}\".\n\n#hangers{n}\n\nAs an AliExpress affiliate I "
                   "may earn a small commission from qualifying purchases, at no extra cost to you.",
}
T0 = kst(2026, 11, 2, 12, 5)
ONEFRAME_WORKFLOW_SHA256 = "c56d093671f210d7504133dca4dbbde654806af91fde53e9747725ebc1f1b6a3"


def cfg_for(account, **kw):
    r = ROUTES[account]
    c = {"channel_profile": r["channel_profile"], "username": r["username"], "language": r["language"],
         "account_fingerprint": A.fingerprint(UID[account]), "cloud_publish": "ENABLED",
         "enabled_at": "2026-10-20T00:00:00+00:00", "paused": False, "INSTAGRAM_DAILY_LIMIT": 2,
         "ALLOWED_POST_TIMES": ["12:00", "18:00"], "TIMEZONE": "Asia/Seoul", "min_spacing_hours": 4,
         "max_attempts": 3, "retry_backoff_minutes": 30, "retry_backoff_cap_minutes": 240}
    c.update(kw)
    return c


def env_for(*accounts, var=True):
    e = {"GITHUB_RUN_ID": "1"}
    for a in accounts:
        n = A.env_names(a)
        if var:
            e[n["variable"]] = "ENABLED"
        e[n["token"]] = TOK[a]
        e[n["user_id"]] = UID[a]
        e[n["media_key"]] = base64.b64encode(KEYS[a]).decode()
    return e


def build_aff(sd: Path, gh: FakeGitHub, spec: dict, *, cfgs=None, qstate="APPROVED", tag="q1",
              captions=None, manifest_over=None, sha_over=None):
    """spec: {account: [slot datetimes or None]} -> affiliate namespace under sd."""
    root = sd / "affiliate"
    root.mkdir(parents=True, exist_ok=True)
    regp = root / "accounts.json"
    reg = json.loads(regp.read_text()) if regp.exists() else {"schema_version": SCHEMA_VERSION,
                                                              "accounts": {}}
    for a in spec:
        reg["accounts"].setdefault(a, (cfgs or {}).get(a) or cfg_for(a))
    regp.write_text(json.dumps(reg, ensure_ascii=False))
    media = {}
    for a, sl in spec.items():
        ad = root / a
        (ad / "queues").mkdir(parents=True, exist_ok=True)
        (ad / "receipts").mkdir(exist_ok=True)
        if not (ad / "local_reconcile.json").exists():
            (ad / "local_reconcile.json").write_text(json.dumps({
                "generated_at": "2026-10-21T00:00:00+00:00", "local_publisher": A.LOCAL_CONFIRMED,
                "account": a, "posts": []}))
        qref = f"afq-{a.lower()[3:8]}-v1-{tag}"
        items, mitems = [], []
        for n, t in enumerate(sl, 1):
            mp4 = f"{a}-{tag}-product{n}-".encode() * 64
            sha = (sha_over or {}).get(n) or hashlib.sha256(mp4).hexdigest()
            if sha_over and n in sha_over:
                mp4 = sha_over["_bytes"][n]
            iref = "afi-" + hashlib.sha256(f"{a}|{sha}".encode()).hexdigest()[:20]
            ident = {"PROJECT_ID": f"{a}-P{n}", "JOB_ID": f"JOB-{a[3:]}-{tag}-{n}", "PRODUCT_ID": f"9{n}",
                     "INSTAGRAM_PROFILE": a}
            blob = B.seal_media(KEYS[a], item_ref=iref, source_sha256=sha, mp4=mp4, identity=ident)
            name = f"afb-{iref[4:]}-{B.sha256_bytes(blob)[:12]}.bin"
            gh.add(STAGING_TAG, name, blob)
            media[iref] = mp4
            ts = t.isoformat() if t else None
            items.append({"position": n, "item_ref": iref, "item_seq": n, "source_sha256": sha,
                          "bundle_asset": name, "bundle_sha256": B.sha256_bytes(blob),
                          "bundle_bytes": len(blob), "target_at": ts,
                          "status": "READY" if t else "QUEUED", "attempt_count": 0,
                          "last_attempt_at": None, "media_id": None, "post_url": None,
                          "published_at": None, "error": None})
            cap = (captions or {}).get(n) or CAPTION[a].format(n=n)
            mitems.append({**ident, "item_ref": iref, "position": n, "item_seq": n,
                           "source_sha256": sha, "SOURCE_SHA256": sha, "target_at": ts,
                           "CHANNEL_PROFILE": ROUTES[a]["channel_profile"],
                           "INSTAGRAM_ACCOUNT": ROUTES[a]["username"], "final_caption": cap})
        man = {"queue_ref": qref, "account": a, "INSTAGRAM_PROFILE": a,
               "CHANNEL_PROFILE": ROUTES[a]["channel_profile"],
               "INSTAGRAM_ACCOUNT": ROUTES[a]["username"], "items": mitems}
        man.update((manifest_over or {}).get(a) or {})
        mraw = B.seal_json(KEYS[a], queue_ref=qref, obj=man)
        (ad / "queues" / f"{qref}.manifest.enc").write_bytes(mraw)
        q = {"schema_version": SCHEMA_VERSION, "queue_ref": qref, "queue_version": 1,
             "state": qstate, "account": a, "channel_profile": ROUTES[a]["channel_profile"],
             "key_id": B.key_id(KEYS[a]), "manifest_file": f"{qref}.manifest.enc",
             "manifest_sha256": B.sha256_bytes(mraw), "items": items}
        (ad / "queues" / f"{qref}.json").write_text(json.dumps(q, indent=1))
    return media


class Base(unittest.TestCase):
    def setUp(self):
        redact.reset()
        self.tmp = Path(tempfile.mkdtemp())
        self.sd = self.tmp / "state"
        (self.sd / "queues").mkdir(parents=True)
        (self.sd / "receipts").mkdir()
        self.gh = FakeGitHub()
        self.gh.rel[STAGING_TAG] = {"id": 3, "tag_name": STAGING_TAG, "assets": {}}
        self.metas = {a: FakeMeta(uid=UID[a], username=ROUTES[a]["username"]) for a in ROUTES}
        self.built = []
        self.commits = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def factory(self, gate, account, token, uid):
        self.built.append((account, token, uid))
        return MetaClient(token=token, ig_user_id=uid, gate=gate, transport=self.metas[account],
                          sleep=lambda s: None)

    def run_tick(self, now, env, commit=None):
        return T.tick(self.sd, now=now.astimezone(timezone.utc), env=env, gh=self.gh,
                      meta_factory=self.factory, commit=commit, workdir=str(self.tmp),
                      clock=lambda: now.astimezone(timezone.utc))

    def queue(self, account, tag="q1"):
        p = next((self.sd / "affiliate" / account / "queues").glob(f"*{tag}.json"))
        return json.loads(p.read_text())

    def bridge_tmp(self):
        return [n for n in self.gh.rel[ghapi.BRIDGE_TAG]["assets"] if n.startswith("afc-tmp-")]


class Routing(Base):
    def test_side_b99_routing(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 2)})
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual((r["result"], r["account"], r["publish_calls"]), ("POSTED", "IG_SIDE_B99", 1))
        self.assertEqual(self.built, [("IG_SIDE_B99", TOK["IG_SIDE_B99"], UID["IG_SIDE_B99"])])
        self.assertEqual(self.metas["IG_ABLECREW"].log, [])
        rec = list((self.sd / "affiliate" / "IG_SIDE_B99" / "receipts").glob("*.json"))
        self.assertEqual(len(rec), 1)
        self.assertEqual(json.loads(rec[0].read_text())["channel_profile"], "COUPANG_KR")

    def test_ablecrew_routing(self):
        build_aff(self.sd, self.gh, {"IG_ABLECREW": slots(T0.date(), 1)})
        r = self.run_tick(T0, env_for("IG_ABLECREW"))
        self.assertEqual((r["result"], r["account"]), ("POSTED", "IG_ABLECREW"))
        self.assertEqual(self.metas["IG_SIDE_B99"].log, [])
        self.assertEqual(self.metas["IG_ABLECREW"].log,
                         ["me", "media", "status", "media_publish", "permalink"])

    def test_wrong_account_queue_in_namespace_rejected(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1),
                                     "IG_ABLECREW": slots(T0.date(), 1)})
        p = next((self.sd / "affiliate" / "IG_SIDE_B99" / "queues").glob("*.json"))
        q = json.loads(p.read_text())
        q["account"] = "IG_ABLECREW"
        p.write_text(json.dumps(q))
        r = self.run_tick(T0, env_for("IG_SIDE_B99", "IG_ABLECREW"))
        self.assertFalse(r["accounts"]["IG_SIDE_B99"]["valid"])
        self.assertIn("WRONG_ACCOUNT", " ".join(r["accounts"]["IG_SIDE_B99"]["errors"]))
        self.assertEqual(r["account"], "IG_ABLECREW")  # the other account is unaffected
        self.assertEqual(self.metas["IG_SIDE_B99"].log, [])

    def test_wrong_channel_profile_rejected(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        p = next((self.sd / "affiliate" / "IG_SIDE_B99" / "queues").glob("*.json"))
        q = json.loads(p.read_text())
        q["channel_profile"] = "ALI_EN"
        p.write_text(json.dumps(q))
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertIn("WRONG_ROUTE", " ".join(r["accounts"]["IG_SIDE_B99"]["errors"]))
        self.assertEqual(self.built, [])

    def test_manifest_routing_mismatch_blocks_before_meta(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)},
                  manifest_over={"IG_SIDE_B99": {"INSTAGRAM_ACCOUNT": "ablecrew"}})
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual(r["result"], "BLOCKED")
        self.assertEqual(self.metas["IG_SIDE_B99"].log, [])
        self.assertIn("WRONG_ACCOUNT", self.queue("IG_SIDE_B99")["items"][0]["error"])

    def test_registry_refuses_oneframe_account(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        p = self.sd / "affiliate" / "accounts.json"
        reg = json.loads(p.read_text())
        reg["accounts"]["IG_ONEFRAME_999"] = cfg_for("IG_SIDE_B99")
        p.write_text(json.dumps(reg))
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual(r["result"], "INVALID_REGISTRY")
        self.assertEqual(self.built, [])

    def test_registry_refuses_swapped_route(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)},
                  cfgs={"IG_SIDE_B99": cfg_for("IG_SIDE_B99", channel_profile="ALI_EN")})
        self.assertEqual(self.run_tick(T0, env_for("IG_SIDE_B99"))["result"], "INVALID_REGISTRY")


class Identity(Base):
    def test_secret_fingerprint_mismatch_blocks_with_zero_meta_calls(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 2)})
        env = env_for("IG_SIDE_B99")
        env[A.env_names("IG_SIDE_B99")["user_id"]] = UID["IG_ABLECREW"]  # swapped secret
        r = self.run_tick(T0, env)
        self.assertEqual((r["result"], r.get("account_halted")), ("BLOCKED", True))
        self.assertEqual(self.built, [])
        self.assertTrue((self.sd / "affiliate" / "IG_SIDE_B99" / "halt.json").exists())
        r2 = self.run_tick(T0 + timedelta(hours=6), env_for("IG_SIDE_B99"))
        self.assertIn("HALTED", " ".join(r2["accounts"]["IG_SIDE_B99"]["reasons"]))

    def test_token_of_other_account_fails_me_check(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        self.metas["IG_SIDE_B99"] = FakeMeta(uid=UID["IG_SIDE_B99"], username="ablecrew")
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual((r["result"], r["error_kind"]), ("BLOCKED", "IDENTITY"))
        self.assertEqual(self.metas["IG_SIDE_B99"].log, ["me"])  # never reached /media
        self.assertTrue((self.sd / "affiliate" / "IG_SIDE_B99" / "halt.json").exists())


class Limits(Base):
    def test_separate_daily_limits(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": [kst(2026, 11, 2, 18)],
                                     "IG_ABLECREW": [kst(2026, 11, 2, 18)]},
                  cfgs={"IG_SIDE_B99": cfg_for("IG_SIDE_B99", INSTAGRAM_DAILY_LIMIT=1)})
        rec = self.sd / "affiliate" / "IG_SIDE_B99" / "local_reconcile.json"
        d = json.loads(rec.read_text())
        d["posts"] = [{"account": "IG_SIDE_B99", "source_sha256": "f" * 64, "media_id": "1",
                       "published_at": kst(2026, 11, 2, 9).isoformat()}]
        rec.write_text(json.dumps(d))
        r = self.run_tick(kst(2026, 11, 2, 18, 5), env_for("IG_SIDE_B99", "IG_ABLECREW"))
        self.assertEqual(r["account"], "IG_ABLECREW")  # ablecrew limit 2: not affected
        r2 = self.run_tick(kst(2026, 11, 2, 18, 20), env_for("IG_SIDE_B99", "IG_ABLECREW"))
        self.assertEqual(r2["result"], "WAIT_DAILY_LIMIT")  # side_b99 limit 1: already used
        self.assertEqual(self.metas["IG_SIDE_B99"].log, [])

    def test_separate_schedules(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": [kst(2026, 11, 2, 9)],
                                     "IG_ABLECREW": [kst(2026, 11, 2, 9)]},
                  cfgs={"IG_SIDE_B99": cfg_for("IG_SIDE_B99", ALLOWED_POST_TIMES=["09:00", "19:00"])})
        r = self.run_tick(kst(2026, 11, 2, 9, 5), env_for("IG_SIDE_B99", "IG_ABLECREW"))
        self.assertNotIn("valid", r["accounts"]["IG_SIDE_B99"])
        self.assertFalse(r["accounts"]["IG_ABLECREW"]["valid"])  # 09:00 is not an ablecrew time
        self.assertEqual((r["result"], r["account"]), ("POSTED", "IG_SIDE_B99"))

    def test_spacing_enforced_per_account(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 2)})
        self.run_tick(T0, env_for("IG_SIDE_B99"))
        # 18:00 slot is due, but pretend the 12:00 post happened late at 15:00
        q = self.queue("IG_SIDE_B99")
        p = next((self.sd / "affiliate" / "IG_SIDE_B99" / "queues").glob("*q1.json"))
        q["items"][0]["published_at"] = kst(2026, 11, 2, 15).isoformat()
        p.write_text(json.dumps(q))
        for f in (self.sd / "affiliate" / "IG_SIDE_B99" / "receipts").glob("*.json"):
            f.unlink()
        r = self.run_tick(kst(2026, 11, 2, 18, 5), env_for("IG_SIDE_B99"))
        self.assertEqual(r["result"], "WAIT_SPACING")

    def test_bad_registry_limits_rejected(self):
        reg = {"schema_version": SCHEMA_VERSION,
               "accounts": {"IG_SIDE_B99": cfg_for("IG_SIDE_B99", min_spacing_hours=1, max_attempts=9)}}
        errs = A.validate_registry(reg)
        self.assertTrue(any("min_spacing" in e for e in errs) and any("max_attempts" in e for e in errs))


class Duplicates(Base):
    def test_same_sha_already_posted_locally_is_never_reposted(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        sha = self.queue("IG_SIDE_B99")["items"][0]["source_sha256"]
        rec = self.sd / "affiliate" / "IG_SIDE_B99" / "local_reconcile.json"
        d = json.loads(rec.read_text())
        d["posts"] = [{"account": "IG_SIDE_B99", "source_sha256": sha, "media_id": "18006119090968765",
                       "post_url": "https://www.instagram.com/reel/X/",
                       "published_at": "2026-09-09T11:01:21+00:00"}]
        rec.write_text(json.dumps(d))
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual(r["result"], "ALREADY_POSTED")
        self.assertEqual(self.built, [])
        self.assertEqual(self.queue("IG_SIDE_B99")["state"], "COMPLETED")

    def test_duplicate_sha_inside_account_invalid(self):
        mp4 = b"same-bytes" * 99
        sha = hashlib.sha256(mp4).hexdigest()
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 2)},
                  sha_over={1: sha, 2: sha, "_bytes": {1: mp4, 2: mp4}})
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertIn("DUPLICATE", " ".join(r["accounts"]["IG_SIDE_B99"]["errors"]))
        self.assertEqual(self.built, [])

    def test_same_video_on_two_accounts_stops_both(self):
        mp4 = b"cross-account" * 99
        sha = hashlib.sha256(mp4).hexdigest()
        over = {1: sha, "_bytes": {1: mp4}}
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)}, sha_over=over)
        build_aff(self.sd, self.gh, {"IG_ABLECREW": slots(T0.date(), 1)}, sha_over=over)
        r = self.run_tick(T0, env_for("IG_SIDE_B99", "IG_ABLECREW"))
        self.assertFalse(r["accounts"]["IG_SIDE_B99"]["valid"])
        self.assertFalse(r["accounts"]["IG_ABLECREW"]["valid"])
        self.assertEqual(self.built, [])

    def test_posted_receipt_blocks_second_publish(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        self.run_tick(T0, env_for("IG_SIDE_B99"))
        r = self.run_tick(T0 + timedelta(days=1), env_for("IG_SIDE_B99"))
        self.assertEqual(r["result"], "NOOP")
        self.assertEqual(self.metas["IG_SIDE_B99"].log.count("media_publish"), 1)


class Failures(Base):
    def test_failed_publish_capped_exponential_retry_and_no_advance(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 2)})
        self.metas["IG_SIDE_B99"] = FakeMeta(uid=UID["IG_SIDE_B99"], username="side_b99",
                                             fail_at="media")
        env = env_for("IG_SIDE_B99")
        t = T0
        self.assertEqual(self.run_tick(t, env)["result"], "FAILED")
        self.assertEqual(self.run_tick(t + timedelta(minutes=29), env)["result"], "WAIT_BACKOFF")
        self.assertEqual(self.run_tick(t + timedelta(minutes=31), env)["result"], "FAILED")
        t2 = t + timedelta(minutes=31)
        # 2nd backoff is 60 min (exponential)
        self.assertEqual(self.run_tick(t2 + timedelta(minutes=45), env)["result"], "WAIT_BACKOFF")
        self.assertEqual(self.run_tick(t2 + timedelta(minutes=61), env)["result"], "FAILED")
        r = self.run_tick(t2 + timedelta(hours=8), env)
        self.assertEqual(r["result"], "HALTED")
        q = self.queue("IG_SIDE_B99")
        self.assertEqual([i["status"] for i in q["items"]], ["FAILED", "READY"])  # no advance
        self.assertEqual(q["items"][0]["attempt_count"], 3)
        self.assertEqual(self.metas["IG_SIDE_B99"].log.count("media_publish"), 0)
        self.assertEqual(self.bridge_tmp(), [])  # temp asset deleted after every failure

    def test_backoff_formula(self):
        c = cfg_for("IG_SIDE_B99")
        self.assertEqual([AQ.backoff_minutes(c, n) for n in (1, 2, 3, 4, 5, 6)],
                         [30, 60, 120, 240, 240, 240])

    def _meta_error(self, body):
        class M(FakeMeta):
            def __call__(s, method, url, body_=None, timeout=120, **kw):
                if url.endswith("/media"):
                    s.log.append("media")
                    return 400, body
                return FakeMeta.__call__(s, method, url, kw.get("body"), timeout)
        return M(uid=UID["IG_SIDE_B99"], username="side_b99")

    def test_auth_error_halts_account_and_not_the_other(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 2),
                                     "IG_ABLECREW": [kst(2026, 11, 2, 18)]})
        self.metas["IG_SIDE_B99"] = self._meta_error(
            b'{"error":{"message":"Error validating access token","type":"OAuthException","code":190}}')
        env = env_for("IG_SIDE_B99", "IG_ABLECREW")
        r = self.run_tick(T0, env)
        self.assertEqual((r["result"], r["error_kind"], r["account_halted"]), ("BLOCKED", "AUTH", True))
        halt = json.loads((self.sd / "affiliate" / "IG_SIDE_B99" / "halt.json").read_text())
        self.assertEqual(halt["meta_error_code"], 190)
        r2 = self.run_tick(kst(2026, 11, 2, 18, 5), env)
        self.assertEqual((r2["result"], r2["account"]), ("POSTED", "IG_ABLECREW"))
        self.assertEqual(self.metas["IG_SIDE_B99"].log.count("media"), 1)
        self.assertTrue(TOK["IG_SIDE_B99"] not in json.dumps(r) + json.dumps(halt))

    def test_restriction_subcode_halts_account(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        self.metas["IG_SIDE_B99"] = self._meta_error(
            b'{"error":{"message":"restricted","code":9004,"error_subcode":2207050}}')
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual((r["error_kind"], r["account_halted"]), ("RESTRICTION", True))

    def test_rate_limit_halts_account(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        self.metas["IG_SIDE_B99"] = self._meta_error(
            b'{"error":{"message":"limit","code":9,"error_subcode":2207042}}')
        self.assertEqual(self.run_tick(T0, env_for("IG_SIDE_B99"))["error_kind"], "RATE")

    def test_ambiguous_publish_blocks_and_halts(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        self.metas["IG_SIDE_B99"] = FakeMeta(uid=UID["IG_SIDE_B99"], username="side_b99",
                                             fail_at="media_publish")
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual((r["result"], r["error_kind"]), ("BLOCKED", "AMBIGUOUS"))
        self.assertIn("verify the account manually", self.queue("IG_SIDE_B99")["items"][0]["error"])
        self.assertEqual(self.bridge_tmp(), [])

    def test_classifier(self):
        class E(Exception):
            phase, ambiguous = "create", False
        self.assertEqual(A.classify_meta_error(E('HTTP 400: {"error": {"code": 368}}'))["kind"], "RESTRICTION")
        self.assertEqual(A.classify_meta_error(E('HTTP 400: {"error": {"code": 200}}'))["kind"], "AUTH")
        self.assertEqual(A.classify_meta_error(E('HTTP 400: {"error": {"code": 4}}'))["kind"], "RATE")
        self.assertEqual(A.classify_meta_error(E('HTTP 500: {"error": {"message": "x"}}'))["kind"], "OTHER")


class Gates(Base):
    def test_disabled_by_default(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)},
                  cfgs={"IG_SIDE_B99": cfg_for("IG_SIDE_B99", cloud_publish="DISABLED", enabled_at=None)})
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual(r["result"], "NO_OPEN_ACCOUNT")
        self.assertEqual(self.built, [])

    def test_pause_kill_switch(self):
        build_aff(self.sd, self.gh, {"IG_ABLECREW": slots(T0.date(), 1)},
                  cfgs={"IG_ABLECREW": cfg_for("IG_ABLECREW", paused=True)})
        r = self.run_tick(T0, env_for("IG_ABLECREW"))
        self.assertIn("account PAUSED", r["accounts"]["IG_ABLECREW"]["reasons"])
        self.assertEqual(self.built, [])

    def test_repository_variable_required(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        r = self.run_tick(T0, env_for("IG_SIDE_B99", var=False))
        self.assertEqual(r["result"], "NO_OPEN_ACCOUNT")

    def test_unconfigured_times_or_limit_closes_gate(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": [None]}, qstate="DRAFT",
                  cfgs={"IG_SIDE_B99": cfg_for("IG_SIDE_B99", INSTAGRAM_DAILY_LIMIT=None,
                                               ALLOWED_POST_TIMES=[])})
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        reasons = " ".join(r["accounts"]["IG_SIDE_B99"]["reasons"])
        self.assertIn("INSTAGRAM_DAILY_LIMIT not configured", reasons)
        self.assertIn("ALLOWED_POST_TIMES not configured", reasons)

    def test_stale_local_reconcile_closes_gate(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)},
                  cfgs={"IG_SIDE_B99": cfg_for("IG_SIDE_B99", enabled_at="2026-10-25T00:00:00+00:00")})
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertIn("older than enabled_at", " ".join(r["accounts"]["IG_SIDE_B99"]["reasons"]))

    def test_draft_cannot_publish(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)}, qstate="DRAFT")
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual((r["result"], r["publish_calls"]), ("NOOP", 0))
        self.assertEqual(self.built, [])

    def test_approved_can_publish(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        self.assertEqual(self.run_tick(T0, env_for("IG_SIDE_B99"))["result"], "POSTED")

    def test_unscheduled_item_in_approved_queue_invalid(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": [None]})
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertIn("unscheduled", " ".join(r["accounts"]["IG_SIDE_B99"]["errors"]))

    def test_meta_client_refuses_closed_gate(self):
        g = A.account_gate(self.sd, "IG_SIDE_B99", {})
        with self.assertRaises(PermissionError):
            MetaClient(token="x" * 10, ig_user_id="1", gate=g)


class Concurrency(Base):
    def test_one_item_per_tick_across_accounts(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1),
                                     "IG_ABLECREW": slots(T0.date(), 1)})
        env = env_for("IG_SIDE_B99", "IG_ABLECREW")
        r1 = self.run_tick(T0, env)
        self.assertEqual(r1["publish_calls"], 1)
        r2 = self.run_tick(T0 + timedelta(minutes=15), env)
        self.assertEqual(r2["publish_calls"], 1)
        self.assertNotEqual(r1["account"], r2["account"])
        self.assertEqual(self.run_tick(T0 + timedelta(minutes=30), env)["publish_calls"], 0)

    def test_interrupted_posting_halts_without_meta(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 2)})
        p = next((self.sd / "affiliate" / "IG_SIDE_B99" / "queues").glob("*.json"))
        q = json.loads(p.read_text())
        q["items"][0].update(status="POSTING", attempt_count=1)
        p.write_text(json.dumps(q))
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual(r["result"], "HALTED")
        self.assertEqual(self.built, [])

    def test_posting_not_persisted_means_no_meta_call(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        r = self.run_tick(T0, env_for("IG_SIDE_B99"), commit=lambda msg: False)
        self.assertEqual(r["result"], "ABORTED_STATE_NOT_PERSISTED")
        self.assertEqual(self.built, [])

    def test_posting_committed_before_meta(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        seen = []

        def commit(msg):
            seen.append((msg, list(self.metas["IG_SIDE_B99"].log)))
            return True
        self.run_tick(T0, env_for("IG_SIDE_B99"), commit=commit)
        self.assertTrue(seen[0][0].startswith("affiliate: POSTING"))
        self.assertEqual(seen[0][1], [])
        self.assertTrue(seen[-1][0].startswith("affiliate: POSTED"))


class Staging(Base):
    def test_tampered_bundle_blocked(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        a = next(iter(self.gh.rel[STAGING_TAG]["assets"].values()))
        self.gh.blobs[a["browser_download_url"]] = b"x" * a["size"]
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertEqual(r["result"], "BLOCKED")
        self.assertNotIn("media", self.metas["IG_SIDE_B99"].log)

    def test_wrong_media_key_blocked(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        env = env_for("IG_SIDE_B99")
        env[A.env_names("IG_SIDE_B99")["media_key"]] = base64.b64encode(KEYS["IG_ABLECREW"]).decode()
        r = self.run_tick(T0, env)
        self.assertEqual(r["result"], "BLOCKED")
        self.assertIn("WRONG_KEY", self.queue("IG_SIDE_B99")["items"][0]["error"])

    def test_bundle_swapped_between_accounts_fails_aead(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1),
                                     "IG_ABLECREW": [kst(2026, 11, 2, 18)]})
        side = self.queue("IG_SIDE_B99")["items"][0]["bundle_asset"]
        able = self.queue("IG_ABLECREW")["items"][0]["bundle_asset"]
        assets = self.gh.rel[STAGING_TAG]["assets"]
        self.gh.blobs[assets[side]["browser_download_url"]] = self.gh.blobs[assets[able]["browser_download_url"]]
        r = self.run_tick(T0, env_for("IG_SIDE_B99", "IG_ABLECREW"))
        self.assertEqual(r["result"], "BLOCKED")

    def test_temp_asset_deleted_and_random_named(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertTrue(r["temp_asset_deleted"])
        self.assertEqual(self.bridge_tmp(), [])
        self.assertRegex(self.gh.deleted[0], r"^afc-tmp-[0-9a-f]{32}\.mp4$")
        self.assertEqual(r["staged"]["plaintext_assets"], [])

    def test_orphan_cleanup_touches_only_old_affiliate_temps(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)},
                  cfgs={"IG_SIDE_B99": cfg_for("IG_SIDE_B99", cloud_publish="DISABLED")})
        old = T0.astimezone(timezone.utc) - timedelta(hours=3)
        self.gh.add(ghapi.BRIDGE_TAG, "afc-tmp-" + "a" * 32 + ".mp4", b"x", created_at=old)
        self.gh.add(ghapi.BRIDGE_TAG, "afc-tmp-" + "b" * 32 + ".mp4", b"x",
                    created_at=T0.astimezone(timezone.utc))
        self.gh.add(ghapi.BRIDGE_TAG, "ofc-tmp-" + "c" * 32 + ".mp4", b"x", created_at=old)
        self.gh.add(ghapi.BRIDGE_TAG, "iglive_keep.mp4", b"x", created_at=old)
        r = self.run_tick(T0, {})
        self.assertEqual(r["orphans_deleted"], ["afc-tmp-" + "a" * 32 + ".mp4"])
        left = set(self.gh.rel[ghapi.BRIDGE_TAG]["assets"])
        self.assertTrue({"ofc-tmp-" + "c" * 32 + ".mp4", "iglive_keep.mp4"} <= left)


class Receipts(Base):
    def test_receipt_carries_feedback_linkage(self):
        build_aff(self.sd, self.gh, {"IG_ABLECREW": slots(T0.date(), 1)})
        self.run_tick(T0, env_for("IG_ABLECREW"))
        rp = next((self.sd / "affiliate" / "IG_ABLECREW" / "receipts").glob("*.json"))
        r = json.loads(rp.read_text())
        for k in ("item_ref", "queue_ref", "account", "channel_profile", "source_sha256",
                  "media_id", "post_url", "published_at", "creation_id", "run_id"):
            self.assertTrue(r.get(k), k)
        ident = B.open_json(KEYS["IG_ABLECREW"], base64.b64decode(r["identity_enc"]),
                            queue_ref=r["item_ref"])
        self.assertEqual((ident["JOB_ID"], ident["INSTAGRAM_PROFILE"], ident["SOURCE_SHA256"]),
                         ("JOB-ABLECREW-q1-1", "IG_ABLECREW", r["source_sha256"]))
        self.assertNotIn("JOB-ABLECREW", rp.read_text())  # identity only encrypted
        q = self.queue("IG_ABLECREW")
        self.assertEqual((q["items"][0]["status"], q["state"]), ("POSTED", "COMPLETED"))

    def test_never_posted_without_proof(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        p = next((self.sd / "affiliate" / "IG_SIDE_B99" / "queues").glob("*.json"))
        q = json.loads(p.read_text())
        q["items"][0]["status"] = "POSTED"
        p.write_text(json.dumps(q))
        r = self.run_tick(T0, env_for("IG_SIDE_B99"))
        self.assertIn("without media_id", " ".join(r["accounts"]["IG_SIDE_B99"]["errors"]))


class CopyLint(Base):
    def test_internal_wording_blocks_publish(self):
        build_aff(self.sd, self.gh, {"IG_ABLECREW": slots(T0.date(), 1)},
                  captions={1: CAPTION["IG_ABLECREW"].format(n=1) + " #SunoAI made by automation"})
        r = self.run_tick(T0, env_for("IG_ABLECREW"))
        self.assertEqual(r["result"], "BLOCKED")
        self.assertNotIn("media", self.metas["IG_ABLECREW"].log)

    def test_lint_rules(self):
        ok = AQ.lint_copy(CAPTION["IG_SIDE_B99"].format(n=1), language="KO", program="COUPANG")
        self.assertEqual(ok["errors"], [])
        bad = AQ.lint_copy("Great product #ad", language="KO", program="COUPANG",
                           internal_ids=("COUPANG-X-001",))
        self.assertTrue(any("CTA" in e for e in bad["errors"]))
        self.assertTrue(any("Korean" in e for e in bad["errors"]))
        self.assertTrue(any("disclosure" in e for e in bad["errors"]))
        leak = AQ.lint_copy(CAPTION["IG_ABLECREW"].format(n=1) + " JOB ALIEXPRESS-X-001 ClickBank",
                            language="EN", program="ALIEXPRESS", internal_ids=("ALIEXPRESS-X-001",))
        self.assertTrue(any("internal id" in e for e in leak["errors"]))
        self.assertTrue(any("ClickBank" in e for e in leak["errors"]))

    def test_repeated_caption_is_error(self):
        c = CAPTION["IG_ABLECREW"].format(n=1)
        rep = AQ.lint_queue_copy([c, c])
        self.assertTrue(rep["errors"])
        rep2 = AQ.lint_queue_copy([c, CAPTION["IG_ABLECREW"].format(n=1).replace("Closet", "Rack")])
        self.assertTrue(rep2["warnings"])  # same hashtag block


def kst_days(start_day, n, hour=18):
    return [kst(2026, 11, start_day + k, hour) for k in range(n)]


NY = ZoneInfo("America/New_York")


def ny(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


COUPANG_CFG = dict(INSTAGRAM_DAILY_LIMIT=1, ALLOWED_POST_TIMES=["18:00"], TIMEZONE="Asia/Seoul",
                   catchup_max_posts_24h=2)
ALI_CFG = dict(INSTAGRAM_DAILY_LIMIT=1, ALLOWED_POST_TIMES=["16:00"], TIMEZONE="America/New_York",
               catchup_max_posts_24h=2)


class SlotDateAccounting(Base):
    """COUPANG midnight drift: the daily limit is counted per INTENDED slot date."""

    def side(self, n, start=2, **kw):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": kst_days(start, n)},
                  cfgs={"IG_SIDE_B99": cfg_for("IG_SIDE_B99", **dict(COUPANG_CFG, **kw))})
        return env_for("IG_SIDE_B99")

    def statuses(self):
        return [i["status"] for i in self.queue("IG_SIDE_B99")["items"]]

    def published(self):
        return self.metas["IG_SIDE_B99"].log.count("media_publish")

    def test_1_due_1800_run_before_midnight(self):
        env = self.side(2)
        r = self.run_tick(kst(2026, 11, 2, 23, 30), env)
        self.assertEqual((r["result"], r["decision"]["item"]["position"]), ("POSTED", 1))

    def test_2_due_1800_run_after_midnight_keeps_next_day_quota(self):
        env = self.side(2)
        self.assertEqual(self.run_tick(kst(2026, 11, 3, 1, 0), env)["result"], "POSTED")  # 11/2 item
        r = self.run_tick(kst(2026, 11, 3, 18, 5), env)
        self.assertEqual((r["result"], r["decision"]["item"]["position"]), ("POSTED", 2))
        self.assertEqual(self.statuses(), ["POSTED", "POSTED"])

    def test_3_significantly_late_next_day_catches_up_with_spacing(self):
        env = self.side(2)
        self.assertEqual(self.run_tick(kst(2026, 11, 3, 16, 0), env)["result"], "POSTED")
        self.assertEqual(self.run_tick(kst(2026, 11, 3, 18, 5), env)["result"], "WAIT_SPACING")
        self.assertEqual(self.run_tick(kst(2026, 11, 3, 20, 1), env)["result"], "POSTED")
        self.assertEqual((self.statuses(), self.published()), (["POSTED", "POSTED"], 2))

    def test_4_multi_day_outage_drains_in_order_without_burst_or_skip(self):
        env = self.side(4)  # slots 11/2..11/5 18:00, runner down until 11/5 10:00
        seq = []
        t = kst(2026, 11, 5, 10, 0)
        end = kst(2026, 11, 8, 0, 0)
        while t < end:
            r = self.run_tick(t, env)
            if r["result"] == "POSTED":
                seq.append((t, r["decision"]["item"]["position"]))
            t += timedelta(minutes=15)
        self.assertEqual([p for _, p in seq], [1, 2, 3, 4])  # strict order, nothing skipped
        self.assertEqual(self.published(), 4)                # nothing posted twice
        for k in range(len(seq) - 2):                       # never 3 real posts within 24 h
            self.assertGreaterEqual(seq[k + 2][0] - seq[k][0], timedelta(hours=24))
        for a, b in zip(seq, seq[1:]):
            self.assertGreaterEqual(b[0] - a[0], timedelta(hours=4))

    def test_5_next_days_legitimate_item_on_time(self):
        env = self.side(2)
        self.run_tick(kst(2026, 11, 2, 18, 5), env)
        r = self.run_tick(kst(2026, 11, 3, 18, 5), env)
        self.assertEqual((r["result"], r["decision"]["item"]["position"]), ("POSTED", 2))
        self.assertEqual(self.run_tick(kst(2026, 11, 3, 23, 0), env)["result"], "NOOP")

    def test_6_delayed_previous_day_plus_manual_post_on_next_day(self):
        env = self.side(2)
        self.assertEqual(self.run_tick(kst(2026, 11, 3, 0, 40), env)["result"], "POSTED")
        rec = self.sd / "affiliate" / "IG_SIDE_B99" / "local_reconcile.json"
        d = json.loads(rec.read_text())
        d["posts"] = [{"account": "IG_SIDE_B99", "source_sha256": "e" * 64, "media_id": "777",
                       "published_at": kst(2026, 11, 3, 9).isoformat()}]  # manual post 11/3
        rec.write_text(json.dumps(d))
        r = self.run_tick(kst(2026, 11, 3, 18, 5), env)
        self.assertEqual(r["result"], "WAIT_DAILY_LIMIT")  # 11/3 used by the manual post, not by the late one
        self.assertIn("next free slot 2026-11-04T18:00:00+09:00", r["decision"]["detail"])
        self.assertEqual(self.statuses(), ["POSTED", "READY"])
        r = self.run_tick(kst(2026, 11, 4, 18, 5), env)  # rolled one day, not skipped
        self.assertEqual((r["result"], r["decision"]["item"]["position"]), ("POSTED", 2))
        self.assertEqual(self.queue("IG_SIDE_B99")["items"][1]["slot_at"], kst(2026, 11, 4, 18).isoformat())

    def test_7_retry_after_midnight_stays_on_its_slot_date(self):
        env = self.side(2)
        self.metas["IG_SIDE_B99"] = FakeMeta(uid=UID["IG_SIDE_B99"], username="side_b99",
                                             fail_at="media")
        self.assertEqual(self.run_tick(kst(2026, 11, 2, 23, 50), env)["result"], "FAILED")
        self.metas["IG_SIDE_B99"].fail_at = None
        self.assertEqual(self.run_tick(kst(2026, 11, 3, 0, 25), env)["result"], "POSTED")
        self.assertEqual(self.run_tick(kst(2026, 11, 3, 18, 5), env)["result"], "POSTED")
        self.assertEqual(self.run_tick(kst(2026, 11, 4, 18, 5), env)["result"], "NOOP")
        self.assertEqual(self.published(), 2)
        rec = sorted(json.loads(p.read_text())["target_at"] for p in
                     (self.sd / "affiliate" / "IG_SIDE_B99" / "receipts").glob("*.json"))
        self.assertEqual(rec, [kst(2026, 11, 2, 18).isoformat(), kst(2026, 11, 3, 18).isoformat()])

    def test_8_limit_is_one_per_intended_date_both_directions(self):
        env = self.side(1)
        rec = self.sd / "affiliate" / "IG_SIDE_B99" / "local_reconcile.json"
        d = json.loads(rec.read_text())
        d["posts"] = [{"account": "IG_SIDE_B99", "source_sha256": "e" * 64, "media_id": "778",
                       "published_at": kst(2026, 11, 2, 10).isoformat()}]
        rec.write_text(json.dumps(d))
        self.assertEqual(self.run_tick(kst(2026, 11, 2, 18, 5), env)["result"], "WAIT_DAILY_LIMIT")
        # still the 11/2 slot after midnight: its quota is 11/2's, which is used
        self.assertEqual(self.run_tick(kst(2026, 11, 3, 0, 30), env)["result"], "WAIT_DAILY_LIMIT")
        self.assertEqual(self.built, [])
        self.assertEqual(self.run_tick(kst(2026, 11, 3, 18, 5), env)["result"], "POSTED")
        errs = AQ.validate_queue({**self.queue("IG_SIDE_B99"), "items": self.queue("IG_SIDE_B99")["items"] * 2},
                                 "IG_SIDE_B99", cfg_for("IG_SIDE_B99", **COUPANG_CFG))
        self.assertTrue(any("INSTAGRAM_DAILY_LIMIT 1" in e or "after previous" in e for e in errs))

    def test_catchup_cap_validation(self):
        reg = {"schema_version": SCHEMA_VERSION, "accounts": {
            "IG_SIDE_B99": cfg_for("IG_SIDE_B99", **dict(COUPANG_CFG, catchup_max_posts_24h=5))}}
        self.assertTrue(any("catchup" in e for e in A.validate_registry(reg)))


class AliEasternTime(Base):
    """ALI_EN: 16:00 America/New_York is authoritative; KST/UTC follow US DST."""

    def ali(self, slots_, **kw):
        build_aff(self.sd, self.gh, {"IG_ABLECREW": slots_},
                  cfgs={"IG_ABLECREW": cfg_for("IG_ABLECREW", **dict(ALI_CFG, **kw))})
        return env_for("IG_ABLECREW")

    def test_dst_transition_slots_are_valid_local_1600(self):
        sl = [ny(2026, 10, 31, 16), ny(2026, 11, 1, 16), ny(2026, 11, 2, 16)]
        self.assertEqual([s.utcoffset() for s in sl],
                         [timedelta(hours=-4), timedelta(hours=-5), timedelta(hours=-5)])
        self.assertEqual([s.astimezone(timezone.utc).hour for s in sl], [20, 21, 21])
        self.assertEqual([s.astimezone(kst(2026, 1, 1, 0).tzinfo).strftime("%m-%d %H:%M") for s in sl],
                         ["11-01 05:00", "11-02 06:00", "11-03 06:00"])  # KST equivalent shifts by 1 h
        env = self.ali(sl)
        r = self.run_tick(sl[0] + timedelta(minutes=5), env)
        self.assertNotIn("valid", r["accounts"]["IG_ABLECREW"])
        self.assertEqual(r["result"], "POSTED")

    def test_fixed_kst_conversion_is_rejected(self):
        # 05:00 KST == 16:00 EDT before the change, but == 15:00 EST after it
        bad = [datetime(2026, 11, 2, 5, tzinfo=kst(2026, 1, 1, 0).tzinfo)]
        env = self.ali(bad)
        r = self.run_tick(kst(2026, 11, 2, 6), env)
        self.assertIn("15:00 not an allowed post time", " ".join(r["accounts"]["IG_ABLECREW"]["errors"]))

    def test_spring_forward_too(self):
        sl = [ny(2027, 3, 13, 16), ny(2027, 3, 14, 16)]
        self.assertEqual([s.astimezone(timezone.utc).hour for s in sl], [21, 20])
        env = self.ali(sl)
        self.assertEqual(self.run_tick(sl[0] + timedelta(minutes=1), env)["result"], "POSTED")
        self.assertEqual(self.run_tick(sl[1] + timedelta(minutes=1), env)["result"], "POSTED")

    def test_delayed_past_eastern_midnight_keeps_next_day_quota(self):
        sl = [ny(2026, 11, 2, 16), ny(2026, 11, 3, 16)]
        env = self.ali(sl)
        self.assertEqual(self.run_tick(ny(2026, 11, 3, 1, 0), env)["result"], "POSTED")
        r = self.run_tick(ny(2026, 11, 3, 16, 5), env)
        self.assertEqual((r["result"], r["decision"]["item"]["position"]), ("POSTED", 2))

    def test_ali_limit_uses_eastern_date_not_korean_date(self):
        # manual post at 11/3 09:00 KST == 11/2 19:00 EST -> uses ALI's 11/2 quota
        env = self.ali([ny(2026, 11, 2, 16), ny(2026, 11, 3, 16)])
        rec = self.sd / "affiliate" / "IG_ABLECREW" / "local_reconcile.json"
        d = json.loads(rec.read_text())
        d["posts"] = [{"account": "IG_ABLECREW", "source_sha256": "d" * 64, "media_id": "9",
                       "published_at": kst(2026, 11, 3, 9).isoformat()}]
        rec.write_text(json.dumps(d))
        r = self.run_tick(ny(2026, 11, 2, 20), env)
        self.assertEqual(r["result"], "WAIT_DAILY_LIMIT")
        self.assertIn("2026-11-02 (America/New_York)", r["decision"]["detail"])
        self.assertEqual(self.run_tick(ny(2026, 11, 3, 1), env)["result"], "WAIT_DAILY_LIMIT")
        self.assertEqual(self.run_tick(ny(2026, 11, 3, 16, 5), env)["result"], "POSTED")  # #1 rolled to 11/3
        q = self.queue("IG_ABLECREW")
        self.assertEqual((q["items"][0]["status"], q["items"][0]["slot_at"]),
                         ("POSTED", ny(2026, 11, 3, 16).isoformat()))
        self.assertEqual(self.run_tick(ny(2026, 11, 3, 23), env)["result"], "WAIT_DAILY_LIMIT")
        self.assertEqual(self.run_tick(ny(2026, 11, 4, 16, 5), env)["result"], "POSTED")  # #2 rolled to 11/4

    def test_coupang_and_ali_isolated_in_their_own_zones(self):
        build_aff(self.sd, self.gh, {"IG_SIDE_B99": [kst(2026, 11, 3, 18)],
                                     "IG_ABLECREW": [ny(2026, 11, 2, 16)]},
                  cfgs={"IG_SIDE_B99": cfg_for("IG_SIDE_B99", **COUPANG_CFG),
                        "IG_ABLECREW": cfg_for("IG_ABLECREW", **ALI_CFG)})
        env = env_for("IG_SIDE_B99", "IG_ABLECREW")
        r1 = self.run_tick(kst(2026, 11, 3, 7), env)   # ALI 11/2 16:00 EST == 11/3 06:00 KST
        self.assertEqual((r1["result"], r1["account"]), ("POSTED", "IG_ABLECREW"))
        r2 = self.run_tick(kst(2026, 11, 3, 18, 5), env)
        self.assertEqual((r2["result"], r2["account"]), ("POSTED", "IG_SIDE_B99"))


class OneframeCodePinned(unittest.TestCase):
    """oneframe_cloud/ is byte-identical to production (remote 1e82933 / 709a5e1)."""
    PINNED = {
        "__init__.py": "1d55ea6eb006514e43691068339365d0b39d60de2af956021d394d1df81dd6eb",
        "__main__.py": "605dc48c57dd4e28bf074a8580115a025907d064875679904e7a98a4854941b6",
        "bundle.py": "33ad3a02c3b41ef6f2bf6c67840fe429b675542f621de8ca697083af8194170b",
        "gate.py": "4523ff04c80edde87ef95eb9c18a5375296676d70023ea5949990bbc0b7e9bd2",
        "ghapi.py": "b36208b50bb7f754708d86c1c6011cf5183cab2164af82930eeafca4e9597b7e",
        "meta.py": "e5b96ca2217d95f16f2212725dc036161b0cf2a7cb2c99d62b633aefc6a3c541",
        "queue.py": "5d66d4f5aa0ae8623cd92480c2904f7551f1f984db8543b0450ac48454ab15bb",
        "redact.py": "0f32c9000f90bb37eacbfda4c2c7b8d3f8023bd50af987bbac4686b7355863ec",
        "tick.py": "7316e872a342899631a6ca47040fc9505d31f79a658f51c0d2dbad9a683bc847",
    }

    def test_oneframe_code_unchanged(self):
        d = ROOT / "oneframe_cloud"
        got = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.glob("*.py"))}
        self.assertEqual(got, self.PINNED)


class OneframeRegression(Base):
    """The ONEFRAME tick must behave identically with the affiliate namespace present,
    and the affiliate tick must never touch ONEFRAME state."""

    def _oneframe(self):
        gh = FakeGitHub()
        sd, _ = build_state(self.tmp / "of", gh, [(1, "APPROVED", slots(T0.date(), 2))])
        enable(sd, datetime(2026, 10, 15, 10, tzinfo=timezone.utc))
        return sd, gh

    def _snapshot(self, sd):
        return {str(p.relative_to(sd)): p.read_bytes() for p in sorted(sd.rglob("*"))
                if p.is_file() and "affiliate" not in p.parts}

    def test_oneframe_tick_unchanged_with_affiliate_state(self):
        sd, gh = self._oneframe()
        from test_cloud import IGUID, TOKEN
        meta = FakeMeta()
        fac = (lambda g: MetaClient(token=TOKEN, ig_user_id=IGUID, gate=g, transport=meta,
                                    sleep=lambda s: None))
        before = oneframe_tick(sd, now=T0.astimezone(timezone.utc) - timedelta(days=30), env=ENV_ON,
                               gh=gh, meta_factory=fac)
        gh.rel[STAGING_TAG] = {"id": 3, "tag_name": STAGING_TAG, "assets": {}}
        build_aff(sd, gh, {"IG_SIDE_B99": slots(T0.date(), 1), "IG_ABLECREW": slots(T0.date(), 1)})
        after = oneframe_tick(sd, now=T0.astimezone(timezone.utc) - timedelta(days=30), env=ENV_ON,
                              gh=gh, meta_factory=fac)
        drop = ("tick_at", "staged", "orphans_deleted")
        self.assertEqual({k: v for k, v in before.items() if k not in drop},
                         {k: v for k, v in after.items() if k not in drop})
        r = oneframe_tick(sd, now=T0.astimezone(timezone.utc), env=ENV_ON, gh=gh, meta_factory=fac)
        self.assertEqual(r["result"], "POSTED")  # ONEFRAME still publishes its own head
        self.assertEqual(meta.log.count("media_publish"), 1)

    def test_affiliate_tick_never_touches_oneframe_state(self):
        sd, gh = self._oneframe()
        gh.rel[STAGING_TAG] = {"id": 3, "tag_name": STAGING_TAG, "assets": {}}
        build_aff(sd, gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        snap = self._snapshot(sd)
        env = dict(ENV_ON, **env_for("IG_SIDE_B99"))
        r = T.tick(sd, now=T0.astimezone(timezone.utc), env=env, gh=gh,
                   meta_factory=self.factory, workdir=str(self.tmp))
        self.assertEqual((r["result"], r["account"]), ("POSTED", "IG_SIDE_B99"))
        self.assertEqual(snap, self._snapshot(sd))
        self.assertTrue(all(a != "IG_ONEFRAME_999" for a, _, _ in self.built))
        self.assertTrue(any(n.startswith("ofb-") for n in gh.rel[ghapi.STAGING_TAG]["assets"]))

    def test_oneframe_secrets_never_used_by_affiliate(self):
        sd, gh = self._oneframe()
        gh.rel[STAGING_TAG] = {"id": 3, "tag_name": STAGING_TAG, "assets": {}}
        build_aff(sd, gh, {"IG_SIDE_B99": slots(T0.date(), 1)})
        r = T.tick(sd, now=T0.astimezone(timezone.utc), env=dict(ENV_ON), gh=gh,
                   meta_factory=self.factory, workdir=str(self.tmp))
        self.assertEqual(r["result"], "NO_OPEN_ACCOUNT")
        self.assertEqual(self.built, [])

    def test_no_affiliate_registry_is_a_quiet_noop(self):
        sd, gh = self._oneframe()
        r = T.tick(sd, now=T0.astimezone(timezone.utc), env={}, gh=gh, meta_factory=self.factory)
        self.assertEqual(r["result"], "NO_AFFILIATE_ACCOUNTS")

    def test_workflow_oneframe_step_unchanged(self):
        raw = (ROOT / ".github" / "workflows" / "oneframe-scheduler.yml").read_bytes()
        head, _, tail = raw.partition(b"\n\n      # Affiliate Instagram accounts")
        # byte-identical to the deployed ONEFRAME workflow (remote commit aabce30)
        self.assertEqual(hashlib.sha256(head + b"\n").hexdigest(), ONEFRAME_WORKFLOW_SHA256)
        wf, tail = raw.decode(), tail.decode()
        self.assertIn("python -m affiliate_cloud tick", tail)
        self.assertNotIn("ONEFRAME_IG_ACCESS_TOKEN", tail)
        for a in ROUTES:
            self.assertIn(f"vars.AFFILIATE_CLOUD_{a} == 'ENABLED' && secrets.AFFILIATE_{a}_ACCESS_TOKEN", tail)
        code = "\n".join(ln for ln in wf.splitlines() if not ln.lstrip().startswith("#"))
        for bad in ("pull_request", "uses:", "runs-on: macos", "larger", "lfs"):
            self.assertNotIn(bad, code)


if __name__ == "__main__":
    unittest.main()
