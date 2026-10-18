# Affiliate cloud queues

Extends the ONEFRAME cloud scheduler (`ONEFRAME_CLOUD.md`) to the affiliate Instagram
accounts. It uses the same workflow job, runner, Releases bridge, crypto, Meta client
and `decide()` logic. `oneframe_cloud/` is not modified.

**Current state: every account DISABLED (ALI also PAUSED); one ALI queue in DRAFT (unscheduled, encrypted bundle staged); nothing publishes.**

| Content profile | Account | Username | Default |
|---|---|---|---|
| `COUPANG_KR` | `IG_SIDE_B99` | @side_b99 | DISABLED · 1/day · 18:00 `Asia/Seoul` |
| `ALI_EN` | `IG_ABLECREW` | @ablecrew | DISABLED + PAUSED · 1/day · 16:00 `America/New_York` |

Slot times are stored in each account's own timezone. ALI follows US daylight saving
automatically, so its Korea-time equivalent moves by one hour (for example, 05:00 KST
in summer and 06:00 KST in winter). No fixed KST conversion is stored anywhere.

## Daily limit by intended slot date

GitHub cron runs late, often by hours. The daily limit is therefore counted per
**intended slot date** in the account's timezone, not by the date the post happened to
go out:

- A late post keeps its own slot date. An 18:00 item that posts at 01:00 does not use
  the next day's quota.
- If a slot date's quota is already used, for example by a manual post, the head
  moves to the next free slot. It is recorded as `slot_at`, nothing is skipped, and
  later items shift by one.
- `catchup_max_posts_24h` (2) caps real posts in any rolling 24 h. After a long
  outage, the backlog drains at most 2 per day instead of bursting.
- Catch-up works and no item is skipped, but exact-time posting is not guaranteed.

## How a run works

The workflow runs the ONEFRAME `Tick` step first, unchanged. The `Affiliate tick` step
runs after it in the same job, so the two can never race. Each run makes at most ONE
affiliate publish.

An account reaches Meta only if every one of these holds:

1. `state/affiliate/accounts.json`: `cloud_publish` is `ENABLED` and `enabled_at` is set.
2. The repository variable `AFFILIATE_CLOUD_<ACCOUNT>` is `ENABLED`. Until then the
   account's secrets are not passed to the job.
3. `paused` is `false`. This is the per-account kill switch.
4. There is no `state/affiliate/<ACCOUNT>/halt.json`.
5. `<ACCOUNT>/local_reconcile.json` says `CLOUD_ONLY_CONFIRMED`, and it was generated after `enabled_at`.
6. `INSTAGRAM_DAILY_LIMIT` and `ALLOWED_POST_TIMES` are set by the operator.
7. The secret user id matches `account_fingerprint`, before any Meta call.
8. `/me` returns the same user id and username.
9. The queue is `APPROVED`.

Account-halting errors stop that account's queue and write `halt.json`:

- auth: 190, 102, 10, 3, 200–299
- restriction: 368, subcodes 2207050 and 2207051
- rate: 4, 17, 32, 613, subcode 2207042
- identity mismatch
- an ambiguous publish

Other failures retry at 30, then 60, then 120 minutes, with at most 3 attempts. A
failed item never lets the next one go ahead.

## Secrets (GitHub Secrets only)

For each account:

- `AFFILIATE_<ACCOUNT>_ACCESS_TOKEN`
- `AFFILIATE_<ACCOUNT>_USER_ID`
- `AFFILIATE_<ACCOUNT>_MEDIA_KEY_B64`

The media key is generated locally into `.env` by the first `draft`.

## Local CLI (Module 5 root)

```
python3 -m src.affiliate_cloud dry-run | candidates
python3 -m src.affiliate_cloud draft    --account IG_SIDE_B99 --job <JOB_ID>
python3 -m src.affiliate_cloud schedule --account IG_SIDE_B99 --queue-ref afq-...
python3 -m src.affiliate_cloud approve  --account IG_SIDE_B99 --queue-ref afq-... --confirm "APPROVE afq-..."
python3 -m src.affiliate_cloud export-local --account IG_SIDE_B99 --publisher-state CLOUD_ONLY_CONFIRMED
```

The bundle upload and the deploy are separate steps, and each needs the operator's approval.
