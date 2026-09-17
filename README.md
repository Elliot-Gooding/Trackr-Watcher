# Trackr quant/trading watcher

Polls The Trackr's programme API, keeps a snapshot in `state.json`, and pushes a phone
notification when something quant/trading-relevant changes. Standard library only — no pip install.

## What it alerts on

- **NEW** — a programme ID that wasn't in the last snapshot.
- **OPEN** — an existing programme whose `openingDate` went from `null` to a real date. This is
  the signal that actually matters: most rows sit on the tracker for months with a null opening
  date before they go live.

The first run writes a silent baseline so you don't get 400 notifications at once.

## Quant filter

A row passes if the **firm** is on the list in `QUANT_FIRMS` (any role at Jane Street, Optiver,
IMC, HRT, DRW, Xantium, Da Vinci etc.) **or** the **title** contains a quant/trading keyword.
`ROLE_EXCLUSIONS` kills the false positives — "Sales and Trading Summer Analyst" at a bank
matches "trading" but isn't what you want. Edit those three lists at the top of `watcher.py`.

Run `python3 watcher.py --discover` to see exactly what the filter currently matches against the
live board before you trust it.

## First: verify the endpoint

The script defaults to:

```
https://api.the-trackr.com/programmes?region=UK&industry=Finance&season=2027&type=summer-internships
```

This is an internal endpoint the web app calls, not a documented public API, so it can change
without warning. Check it once:

```bash
python3 watcher.py --discover
```

If that 404s or returns nothing useful: open the tracker in a browser, DevTools → Network →
Fetch/XHR → reload the page → find the request that returns the programme list → copy its URL and
set `TRACKR_API_URLS` to it. You can pass several comma-separated URLs to watch more than one
board (e.g. off-cycle internships, or the EU board for Amsterdam firms).

## Setup

**1. Push notifications (ntfy)** — install the ntfy app, pick an unguessable topic name like
`elliot-trackr-8fk2ld`, subscribe to it in the app. Public ntfy.sh topics are unauthenticated, so
anyone who guesses the name can read your alerts; nothing sensitive goes over it, but pick
something random anyway.

**2a. GitHub Actions (recommended)** — push this repo (public, so Actions minutes are free), then
Settings → Secrets and variables → Actions, add `NTFY_TOPIC` and optionally `GMAIL_USER` /
`GMAIL_APP_PASSWORD` / `EMAIL_TO`. Run the workflow manually once to lay the baseline. It commits
`state.json` back after each run — that's the memory, no database needed.

**2b. Locally** — cron it:

```bash
export NTFY_TOPIC="elliot-trackr-8fk2ld"
python3 watcher.py
```

```cron
7 6-21 * * * cd ~/trackr-quant-watcher && NTFY_TOPIC=... /usr/bin/python3 watcher.py >> watcher.log 2>&1
```

Only fires while the machine is awake, which is the main reason to prefer Actions.

## Flags

| Flag | Effect |
| --- | --- |
| `--discover` | Dump row count, keys on the first row, and everything the quant filter matches |
| `--dry-run` | Print the diff, notify nothing, leave `state.json` alone |
| `--all` | Skip the quant filter |
| `--from-file f.json` | Diff against a local file instead of the API (for testing) |

## Env vars

`NTFY_TOPIC`, `NTFY_SERVER`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `EMAIL_TO`, `SEASON` (default
2027), `TRACKR_API_URLS`, `STATE_FILE`, `MIN_EXPECTED_ROWS`, `USER_AGENT`.

## Being a good citizen

The `/programmes` endpoint allows **10 requests per day**. Once that's spent it doesn't return an
error — it answers `200 OK` with `[]` and a `Retry-After` header. The workflow runs every 2 hours
(8 a day) to leave headroom for manual runs; don't run `probe.py` casually, it spends 9.

The script logs how many requests are left, and aborts **without saving state** if the response is
empty with `Retry-After`, or has fewer than `MIN_EXPECTED_ROWS` rows. An empty `state.json` is
treated as no baseline, so a bad snapshot can't trigger a flood of NEW alerts later.
