# ONEFRAME cloud scheduler

Posts queued ONEFRAME Reels to Instagram from a free GitHub Actions runner, so a
scheduled Reel goes out even when the operator's Mac is off.

**Current state: preparation only. Cutover gate = DISABLED. Nothing publishes.**

## Cost: $0, structurally

| Uses | Does not use |
|---|---|
| this PUBLIC repository | private-repo minutes, larger runners, self-hosted runners |
| standard runner `ubuntu-24.04` | Actions artifacts, cache, Packages, LFS |
| GitHub Releases (encrypted bundles, short-lived temp file) | any cloud provider or paid storage |
| repository Secrets + one repository variable | any card, trial or subscription |
| built-in `GITHUB_TOKEN` (`contents: write` only) | third-party Actions (none at all) |

Standard runners are free for public repositories.

## How one tick works

`.github/workflows/oneframe-scheduler.yml` runs every 15 minutes (plus manual
`workflow_dispatch`; never on pull requests) with `concurrency: oneframe-publisher`.

1. Load `state/` and validate it. Slot times must be 12:00/18:00 Asia/Seoul, max 2 per day,
   ≥ 4 h spacing, strict Shorts order, and no cross-queue collision or duplicate source.
2. Delete stale `ofc-tmp-*.mp4` temp assets (older than 2 h) on the `media-bridge` release. Nothing else is touched.
3. Check the staged encrypted bundles.
4. **Cutover gate.** If it is not ENABLED, return `CUTOVER_DISABLED` and exit. No Instagram call is made, and the
   Instagram token and the media key are not even passed to the job.
5. Pick at most ONE due item. It must be:
   - the head of an `APPROVED` queue
   - due in Asia/Seoul
   - within the daily limit and spacing (counting cloud receipts AND the local history export)
   - not a duplicate
6. Commit and push `POSTING` **before** any Meta call. If the push fails, nothing is sent. If a run dies
   mid-publish, the next run sees `POSTING` and halts (no re-post).
7. Download the bundle, check its sha256, then decrypt with AES-256-GCM. The associated data binds it to
   the item. The decrypted video must match `SOURCE_SHA256`, and its identity must match the manifest.
8. Upload a random-named temp MP4 to `media-bridge` and verify its public URL.
9. Run the official Graph API sequence: `/me` identity, then `/media` (REELS), then poll until FINISHED,
   then `/media_publish` once.
10. In a `finally` block, delete the temp asset and the runner file.
11. Record the outcome:
    - On success: `POSTED` plus a receipt.
    - On failure: `FAILED`. It is retried only after a 30 min backoff and at most 3 attempts in total,
      and the next Short never jumps ahead.
    - If the outcome is ambiguous or identity fails: `BLOCKED`, and the item needs manual verification.

## Files

- `state/control.json`: the global gate (`ONEFRAME_CLOUD_CUTOVER`), default `DISABLED`.
- `state/queues/<ref>.json`: the public minimal index. It holds opaque refs, order, slots, SHA256s,
  bundle identity, status and proof. It contains no captions and no internal ids.
- `state/queues/<ref>.manifest.enc`: the AES-256-GCM manifest. It holds the full identity
  (PROJECT/JOB/SONG/SHORTS ids), captions, hashtags and the exact final caption.
- `state/receipts/<item_ref>.json`: a verified post (media id, URL, time). The identity is encrypted.
- `state/local_reconcile.json`: the local Module 5 Instagram history export (sha256 + media id + time).
- Release `oneframe-cloud-staging`: encrypted bundles only (`ofb-*.bin`).

## Queue states

| State | Meaning |
|---|---|
| `MIGRATION_PENDING` | Mirror of a queue the local scheduler still owns. Never publishes. |
| `DRAFT` | Prepared, not approved. Never publishes. |
| `APPROVED` | The only publishable state. |

## Cutover gate: every condition is required

1. `state/control.json`: `"ONEFRAME_CLOUD_CUTOVER": "ENABLED"` and `cutover_enabled_at`.
2. Repository variable `ONEFRAME_CLOUD_CUTOVER` = `ENABLED`.
3. `state/local_reconcile.json`: `local_scheduler` = `DISABLED_CONFIRMED`, with `generated_at` later than
   `cutover_enabled_at`. The exporter refuses `DISABLED_CONFIRMED` while the local launchd job is loaded.
4. Secrets `ONEFRAME_IG_ACCESS_TOKEN`, `ONEFRAME_IG_USER_ID` and `ONEFRAME_MEDIA_KEY_B64` are set.
5. The queue is `APPROVED`.

## Notes

- GitHub disables scheduled workflows in a repository with no activity for 60 days. Re-enable it in
  the Actions tab if that happens.
- Instagram long-lived tokens last about 60 days. Refresh the secret before it expires.
- Tests: `python3 -m unittest discover -s tests`.
