"""
oneframe_cloud/ghapi.py  -- GitHub Releases REST, the only storage this system uses.

  staging release  (STAGING_TAG)   encrypted bundles only (long-term)
  media bridge     (BRIDGE_TAG)    the existing Module 5 bridge release; the cloud
                                   puts ONE random-named temporary plaintext MP4 here
                                   for the duration of an Instagram ingest, then deletes it

Temporary names are `ofc-tmp-<32 hex>.mp4`. Orphan cleanup deletes ONLY assets
matching that exact pattern and older than a threshold -- never anything else.
Auth: the run's built-in GITHUB_TOKEN (contents: write). Bundle downloads use the
public download URL with no credentials at all.
"""

from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .redact import scrub

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
STAGING_TAG = "oneframe-cloud-staging"
BRIDGE_TAG = "media-bridge"
TMP_RE = re.compile(r"^ofc-tmp-[0-9a-f]{32}\.mp4$")
ORPHAN_AFTER = timedelta(hours=2)


class GitHubError(Exception):
    pass


def _default_transport(method, url, *, headers, body=None, timeout=120):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


class GitHub:
    def __init__(self, repo: str, token: str = "", *, transport=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
            raise GitHubError(f"bad repository slug {repo!r}")
        self.repo, self.token = repo, token or ""
        self.t = transport or _default_transport
        self.calls = []

    def _h(self, auth=True, accept="application/vnd.github+json"):
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "oneframe-cloud"}
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _json(self, method, url, *, body=None, ok=(200,), auth=True, headers=None):
        self.calls.append(f"{method} {url.split('?')[0]}")
        h = self._h(auth)
        if headers:
            h.update(headers)
        st, raw, _ = self.t(method, url, headers=h, body=body)
        if st not in ok:
            raise GitHubError(f"{method} {url.split('?')[0]} -> HTTP {st}: {scrub(raw[:300])}")
        return json.loads(raw) if raw else None

    # ---- releases ----------------------------------------------------------
    def repo_info(self) -> dict:
        return self._json("GET", f"{API}/repos/{self.repo}")

    def release(self, tag: str):
        try:
            return self._json("GET", f"{API}/repos/{self.repo}/releases/tags/{urllib.parse.quote(tag)}")
        except GitHubError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def assets(self, release_id) -> list:
        out, page = [], 1
        while True:
            got = self._json("GET", f"{API}/repos/{self.repo}/releases/{release_id}/assets"
                                    f"?per_page=100&page={page}")
            out += got or []
            if not got or len(got) < 100:
                return out
            page += 1

    def download_public(self, url: str) -> bytes:
        """Public release download URL; NO credentials are sent (redirect host is a CDN)."""
        if not url.startswith(f"https://github.com/{self.repo}/releases/download/"):
            raise GitHubError("refusing to download from a non-release URL")
        self.calls.append("GET release-download")
        st, raw, _ = self.t("GET", url, headers={"User-Agent": "oneframe-cloud"}, timeout=600)
        if st != 200:
            raise GitHubError(f"bundle download HTTP {st}")
        return raw

    def upload_asset(self, release: dict, name: str, data: bytes,
                     content_type="application/octet-stream") -> dict:
        url = (f"{UPLOADS}/repos/{self.repo}/releases/{release['id']}/assets"
               f"?name={urllib.parse.quote(name)}")
        return self._json("POST", url, body=data, ok=(201,),
                          headers={"Content-Type": content_type,
                                   "Content-Length": str(len(data))})

    def delete_asset(self, asset_id) -> bool:
        try:
            self._json("DELETE", f"{API}/repos/{self.repo}/releases/assets/{asset_id}", ok=(204,))
            return True
        except GitHubError as e:
            if "HTTP 404" in str(e):
                return True  # already gone
            raise

    def verify_public(self, url: str, size: int) -> dict:
        """The public URL Instagram will fetch serves exactly `size` bytes (range probe)."""
        self.calls.append("GET range-probe")
        st, raw, hdr = self.t("GET", url, headers={"User-Agent": "oneframe-cloud",
                                                   "Range": "bytes=0-0"}, timeout=60)
        cr = {k.lower(): v for k, v in hdr.items()}.get("content-range", "")
        total = cr.rsplit("/", 1)[-1] if "/" in cr else ""
        ok = st in (200, 206) and (st == 200 and len(raw) == size or total == str(size))
        if not ok:
            raise GitHubError(f"temporary media URL not verified (HTTP {st}, range {cr!r})")
        return {"http": st, "bytes": size}


def temp_name() -> str:
    return f"ofc-tmp-{_secrets.token_hex(16)}.mp4"


def cleanup_orphans(gh: GitHub, release: dict, *, now: datetime, older_than=ORPHAN_AFTER) -> list:
    """Delete stale ONEFRAME temporary plaintext assets. Never touches other names."""
    deleted = []
    for a in gh.assets(release["id"]):
        if not TMP_RE.match(a.get("name") or ""):
            continue
        created = datetime.fromisoformat(str(a["created_at"]).replace("Z", "+00:00"))
        if now - created >= older_than:
            gh.delete_asset(a["id"])
            deleted.append(a["name"])
    return deleted


def env_repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "")
