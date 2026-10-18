"""
affiliate_cloud/  -- per-account affiliate Instagram Reel queues on the SAME free
GitHub Actions scheduler as ONEFRAME (oneframe_cloud). Nothing here replaces or
edits oneframe_cloud; it reuses its parts:

  oneframe_cloud.bundle   AES-256-GCM bundles + manifests (same format)
  oneframe_cloud.ghapi    GitHub Releases (staging + the shared media-bridge release)
  oneframe_cloud.meta     official Instagram Graph API client (same call sequence)
  oneframe_cloud.queue    `decide()` -- the same due / daily-limit / spacing /
                          duplicate / retry-cap / POSTING-halt logic
  oneframe_cloud.gate     `Gate` -- the object MetaClient refuses to run without

New here:
  accounts.py  account registry (state/affiliate/accounts.json), per-account gate,
               PAUSE kill switch, halt file, routing table, identity fingerprint
  aqueue.py    affiliate queue index schema + validation + public-copy lint
  tick.py      one affiliate pass: at most ONE affiliate publish per run

Namespace (one directory per Instagram account, never shared):
  state/affiliate/accounts.json
  state/affiliate/<ACCOUNT>/queues/<afq-ref>.json + .manifest.enc
  state/affiliate/<ACCOUNT>/receipts/<afi-ref>.json
  state/affiliate/<ACCOUNT>/local_reconcile.json
  state/affiliate/<ACCOUNT>/halt.json           (written on auth/restriction errors)

Default for every account: cloud_publish DISABLED, queues DRAFT. Nothing publishes.
"""

SCHEMA_VERSION = "affiliate-cloud-1.0"

# The only allowed (content profile -> Instagram account -> username) routes.
# Mirrors config/instagram_accounts.json content_mappings; checked on every run.
ROUTES = {
    "IG_SIDE_B99": {"channel_profile": "COUPANG_KR", "username": "side_b99", "language": "KO",
                    "program": "COUPANG"},
    "IG_ABLECREW": {"channel_profile": "ALI_EN", "username": "ablecrew", "language": "EN",
                    "program": "ALIEXPRESS"},
}
# accounts that belong to another scheduler and may never appear here
FOREIGN_ACCOUNTS = ("IG_ONEFRAME_999",)

STAGING_TAG = "affiliate-cloud-staging"
TMP_PREFIX = "afc-tmp-"
