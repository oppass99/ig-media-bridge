"""
oneframe_cloud/meta.py  -- OFFICIAL Instagram Graph API (Content Publishing) only.

The same call sequence Module 5 uses locally (src/platforms/instagram.py):
  GET  /me?fields=user_id,username            identity check (must be the target account)
  POST /{ig-user-id}/media  media_type=REELS  container from a public video_url
  GET  /{creation-id}?fields=status_code      poll until FINISHED / ERROR
  POST /{ig-user-id}/media_publish            the single publish call
  GET  /{media-id}?fields=permalink

Host allowlist: graph.instagram.com only (Instagram Login token). No browser
automation, no private/mobile API, no password or cookie. Every method that
creates or publishes calls `gate.assert_open()` first, and the client cannot
even be constructed with a closed gate.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .redact import register, scrub

GRAPH = "https://graph.instagram.com/v21.0"
ALLOWED_HOST = "graph.instagram.com"


class MetaError(Exception):
    def __init__(self, msg, *, phase, ambiguous=False):
        super().__init__(scrub(msg))
        self.phase, self.ambiguous = phase, ambiguous


def _default_transport(method, url, *, body=None, timeout=120):
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class MetaClient:
    def __init__(self, *, token: str, ig_user_id: str, gate, transport=None, sleep=time.sleep,
                 poll_interval=10, poll_max=36):
        gate.assert_open("constructing the Instagram publish client")
        if not token or not ig_user_id:
            raise MetaError("Instagram secrets missing", phase="config")
        register(token, ig_user_id)
        self.token, self.ig_user_id, self.gate = token, ig_user_id, gate
        self.t = transport or _default_transport
        self.sleep, self.poll_interval, self.poll_max = sleep, poll_interval, poll_max
        self.calls = []  # endpoint names only, never URLs with tokens

    def _call(self, method, path, params, *, phase, label):
        url = f"{GRAPH}/{path}"
        if urllib.parse.urlparse(url).hostname != ALLOWED_HOST:
            raise MetaError("host not allowed", phase=phase)
        params = dict(params, access_token=self.token)
        self.calls.append(label)
        try:
            if method == "GET":
                st, raw = self.t("GET", f"{url}?{urllib.parse.urlencode(params)}")
            else:
                st, raw = self.t("POST", url, body=urllib.parse.urlencode(params).encode())
        except Exception as e:  # noqa: BLE001 -- network failure
            raise MetaError(f"{label} transport error: {e}", phase=phase,
                            ambiguous=(label == "media_publish")) from None
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        if st != 200:
            raise MetaError(f"{label} HTTP {st}: {json.dumps(data)[:300]}", phase=phase,
                            ambiguous=(label == "media_publish" and st >= 500))
        return data

    def identity(self, expected_username: str) -> dict:
        me = self._call("GET", "me", {"fields": "user_id,username"}, phase="identity",
                        label="me")
        uid = str(me.get("user_id") or me.get("id") or "")
        if uid != str(self.ig_user_id) or (expected_username and
                                           me.get("username") != expected_username):
            raise MetaError("token does not belong to the target Instagram account",
                            phase="identity")
        return {"username": me.get("username")}

    def create_reel(self, video_url: str, caption: str, share_to_feed=True) -> str:
        self.gate.assert_open("Instagram /media container creation")
        d = self._call("POST", f"{self.ig_user_id}/media", {
            "media_type": "REELS", "video_url": video_url, "caption": caption,
            "share_to_feed": "true" if share_to_feed else "false"},
            phase="create", label="media")
        if not d.get("id"):
            raise MetaError("media create returned no id", phase="create")
        return str(d["id"])

    def wait_finished(self, creation_id: str):
        for _ in range(self.poll_max):
            d = self._call("GET", creation_id, {"fields": "status_code"}, phase="ingest",
                           label="status")
            code = d.get("status_code")
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise MetaError(f"ingest {code}", phase="ingest")
            self.sleep(self.poll_interval)
        raise MetaError("ingest never reached FINISHED", phase="ingest")

    def publish(self, creation_id: str) -> str:
        self.gate.assert_open("Instagram /media_publish")
        d = self._call("POST", f"{self.ig_user_id}/media_publish", {"creation_id": creation_id},
                       phase="publish", label="media_publish")
        if not d.get("id"):
            raise MetaError("media_publish returned no id", phase="publish", ambiguous=True)
        return str(d["id"])

    def permalink(self, media_id: str) -> str:
        try:
            d = self._call("GET", media_id, {"fields": "permalink"}, phase="permalink",
                           label="permalink")
            return d.get("permalink") or f"https://www.instagram.com/reel/{media_id}/"
        except MetaError:
            return f"https://www.instagram.com/reel/{media_id}/"
