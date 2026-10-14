"""
oneframe_cloud/  -- ONEFRAME Instagram Reel scheduler for a FREE GitHub Actions runner.

  bundle.py   AES-256-GCM sealed media bundles + encrypted queue manifests
  gate.py     global cutover kill switch (DISABLED by default; two independent switches)
  queue.py    public minimal queue index: schema, slot rules, next-due selection
  ghapi.py    GitHub Releases REST (temporary plaintext asset + orphan cleanup)
  meta.py     official Instagram Graph API (Content Publishing) client only
  tick.py     one scheduler pass: at most ONE publish, then exit
  redact.py   secret masking for logs

Runs on the standard GitHub-hosted runner of a PUBLIC repository: $0.
Nothing here publishes unless the cutover gate is ENABLED in BOTH places.
"""

SCHEMA_VERSION = "oneframe-cloud-1.0"
