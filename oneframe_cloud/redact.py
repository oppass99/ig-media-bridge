"""
oneframe_cloud/redact.py  -- keep secret values out of every log line / state file.

`register()` takes each secret (and its URL-encoded form), tells the GitHub
runner to mask it (::add-mask::) and scrubs it from any text passed through
`scrub()`. Errors are scrubbed before they are printed or persisted.
"""

from __future__ import annotations

import os
import re
import urllib.parse

_SECRETS: set = set()
_TOKENISH = re.compile(r"(access_token|token|key|signature|sig)=([^&\s\"']+)", re.IGNORECASE)


def register(*values, announce=None):
    announce = (os.environ.get("GITHUB_ACTIONS") == "true") if announce is None else announce
    for v in values:
        v = str(v or "")
        if len(v) < 4:
            continue
        for form in {v, urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v)}:
            if form not in _SECRETS:
                _SECRETS.add(form)
                if announce:
                    print(f"::add-mask::{form}")


def scrub(text) -> str:
    s = str(text if text is not None else "")
    for v in sorted(_SECRETS, key=len, reverse=True):
        s = s.replace(v, "***")
    return _TOKENISH.sub(lambda m: f"{m.group(1)}=***", s)


def reset():  # tests only
    _SECRETS.clear()
