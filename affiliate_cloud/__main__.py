"""
python -m affiliate_cloud tick     [--state state] [--persist git|none]
python -m affiliate_cloud validate [--state state]

`tick` prints ONE JSON line (secrets scrubbed). An account whose gate is closed
(the default: DISABLED) never gets an Instagram client constructed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from oneframe_cloud import ghapi
from oneframe_cloud.__main__ import git_commit
from oneframe_cloud.redact import register, scrub

from . import ROUTES
from . import accounts as A
from .tick import load_all, tick


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m affiliate_cloud")
    ap.add_argument("cmd", choices=["tick", "validate"])
    ap.add_argument("--state", default="state")
    ap.add_argument("--persist", choices=["git", "none"], default="none")
    a = ap.parse_args(argv)
    env = dict(os.environ)
    for account in ROUTES:
        n = A.env_names(account)
        register(env.get(n["token"]), env.get(n["user_id"]), env.get(n["media_key"]))
    register(env.get("GITHUB_TOKEN"))
    reg = A.load_registry(a.state)
    if a.cmd == "validate":
        errs = A.validate_registry(reg) if reg else []
        if reg and not errs:
            _, acct_errs, _, _ = load_all(a.state, reg)
            errs = [e for v in acct_errs.values() for e in v]
        print(json.dumps({"valid": not errs, "errors": errs}, ensure_ascii=False))
        return 1 if errs else 0

    repo = ghapi.env_repo()
    gh = ghapi.GitHub(repo, env.get("GITHUB_TOKEN", "")) if repo else None

    def meta_factory(gate, account, token, uid):
        from oneframe_cloud.meta import MetaClient
        return MetaClient(token=token, ig_user_id=uid, gate=gate)

    res = tick(a.state, now=datetime.now(timezone.utc), env=env, gh=gh,
               meta_factory=meta_factory,
               commit=git_commit(a.state) if a.persist == "git" else None,
               workdir=env.get("RUNNER_TEMP") or None)
    if gh is not None:
        res["github_calls"] = len(gh.calls)
    print(scrub(json.dumps(res, ensure_ascii=False, default=str)))
    return 1 if res.get("result") == "INVALID_REGISTRY" else 0


if __name__ == "__main__":
    sys.exit(main())
