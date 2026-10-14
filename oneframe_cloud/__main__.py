"""
python -m oneframe_cloud tick     [--state state] [--persist git|none]
python -m oneframe_cloud validate [--state state]

`tick` prints ONE JSON line (secrets scrubbed). With the cutover gate DISABLED
(default) it reads the queue, schedule and staged assets and returns
CUTOVER_DISABLED / NOOP without constructing an Instagram client.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from . import ghapi
from . import queue as Q
from .redact import register, scrub
from .tick import tick


def git_commit(state_dir):
    def run(msg) -> bool:
        def g(*a):
            return subprocess.run(["git", *a], capture_output=True, text=True)
        g("add", "--", state_dir)
        if g("diff", "--cached", "--quiet").returncode == 0:
            return True  # nothing changed
        if g("-c", "user.name=oneframe-cloud[bot]",
             "-c", "user.email=oneframe-cloud@users.noreply.github.com",
             "commit", "-q", "-m", msg).returncode != 0:
            return False
        p = g("push", "-q", "origin", "HEAD:main")
        if p.returncode != 0:
            print(json.dumps({"push_error": scrub(p.stderr)[-300:]}))
            return False
        return True
    return run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m oneframe_cloud")
    ap.add_argument("cmd", choices=["tick", "validate"])
    ap.add_argument("--state", default="state")
    ap.add_argument("--persist", choices=["git", "none"], default="none")
    a = ap.parse_args(argv)
    env = dict(os.environ)
    register(env.get("ONEFRAME_IG_ACCESS_TOKEN"), env.get("ONEFRAME_IG_USER_ID"),
             env.get("ONEFRAME_MEDIA_KEY_B64"), env.get("GITHUB_TOKEN"))
    if a.cmd == "validate":
        errs = Q.validate_all(Q.load_queues(a.state))
        print(json.dumps({"valid": not errs, "errors": errs}, ensure_ascii=False))
        return 1 if errs else 0

    repo = ghapi.env_repo()
    gh = ghapi.GitHub(repo, env.get("GITHUB_TOKEN", "")) if repo else None

    def meta_factory(gate):
        from .meta import MetaClient
        return MetaClient(token=env.get("ONEFRAME_IG_ACCESS_TOKEN", ""),
                          ig_user_id=env.get("ONEFRAME_IG_USER_ID", ""), gate=gate)

    res = tick(a.state, now=datetime.now(timezone.utc), env=env, gh=gh,
               meta_factory=meta_factory,
               commit=git_commit(a.state) if a.persist == "git" else None,
               workdir=env.get("RUNNER_TEMP") or None)
    if gh is not None:
        res["github_calls"] = len(gh.calls)
    print(scrub(json.dumps(res, ensure_ascii=False, default=str)))
    return 1 if res.get("result") == "INVALID_STATE" else 0


if __name__ == "__main__":
    sys.exit(main())
