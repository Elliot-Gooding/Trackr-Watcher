#!/usr/bin/env python3
"""
Trackr quant/trading watcher.

Polls The Trackr's programme API, keeps a local snapshot, and pushes a
notification when a quant/trading-relevant programme is newly listed or
flips from "not open yet" to open.

Standard library only. Configured by environment variables (see README).

    python3 watcher.py              # normal run
    python3 watcher.py --discover   # dump the raw shape of the API response
    python3 watcher.py --dry-run    # diff and print, but don't notify or save
    python3 watcher.py --all        # ignore the quant filter, show everything
    python3 watcher.py --from-file sample.json   # run the diff against a local file
"""

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SEASON = os.environ.get("SEASON", "2027")

# The endpoint the Trackr web app calls. If Trackr changes it, override with
# TRACKR_API_URLS (comma-separated full URLs) rather than editing this file.
DEFAULT_URLS = [
    f"https://api.the-trackr.com/programmes?region=UK&industry=Finance&season={SEASON}&type=summer-internships",
]
API_URLS = [u.strip() for u in os.environ.get("TRACKR_API_URLS", ",".join(DEFAULT_URLS)).split(",") if u.strip()]

PUBLIC_PAGE = "https://app.the-trackr.com/uk-finance/summer-internships"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
USER_AGENT = os.environ.get("USER_AGENT", "personal-internship-watcher/1.0")
MIN_EXPECTED_ROWS = int(os.environ.get("MIN_EXPECTED_ROWS", "150"))

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", GMAIL_USER)

# --------------------------------------------------------------------------- #
# What counts as quant/trading
# --------------------------------------------------------------------------- #

# Anything from these firms is relevant whatever the role is called.
QUANT_FIRMS = {
    "jane street", "optiver", "citadel", "citadel securities", "imc", "imc trading",
    "hudson river trading", "hrt", "g-research", "gresearch", "sig", "susquehanna",
    "drw", "jump trading", "squarepoint", "millennium", "de shaw", "d. e. shaw",
    "d.e. shaw", "two sigma", "qube", "qube research", "man group", "ahl",
    "marshall wace", "point72", "cubist", "balyasny", "aqr", "xtx", "xtx markets",
    "flow traders", "da vinci", "davinci", "ims", "maven securities", "akuna",
    "five rings", "tower research", "radix", "quadrature", "wolverine", "belvedere",
    "old mission", "vatic", "headlands", "verition", "schonfeld", "ferrari trading",
    "xantium", "grasshopper", "tibra", "eclipse trading", "virtu", "jhc",
    "capstone", "graviton", "engineers gate", "voleon", "quantbot", "arrowstreet",
    "winton", "brevan howard", "citadel gqs", "ebury", "amber", "trexquant", "synthesis", "wintermute", "shell", "equinor", "rwe", "stonex",
    "bastion trading", "javelin global commodities", "bp", "dare",
    "mako trading", "state street", "bmll", "genesis quant capital",
    "aspect capital", "marketaxess", "silvertide", "liquidnet", "ripple",
    "teza technologies", "clarksons", "galaxy", "exoduspoint",
    "cboe global markets", "gunvor", "e.on", "aquatic capital management",
    "superbet"
}

# Or the role title looks quant/trading regardless of firm.
ROLE_KEYWORDS = [
    "quant", "quantitative", "trading", "trader", "systematic", "algorithmic",
    "market making", "market-making", "execution", "derivatives", "options",
]

# But not these, which match the keywords above for the wrong reasons.
ROLE_EXCLUSIONS = [
]


def norm(s):
    return (s or "").strip().lower()


def row_company(row):
    c = row.get("company")
    if isinstance(c, dict):
        return c.get("name") or c.get("displayName") or "?"
    return c or row.get("companyName") or "?"


def row_title(row):
    return row.get("name") or row.get("title") or row.get("programme") or "?"


def is_quant(row):
    company = norm(row_company(row))
    title = norm(row_title(row))
    if any(f in company for f in QUANT_FIRMS):
        return True
    if any(x in title for x in ROLE_EXCLUSIONS):
        return False
    return any(k in title for k in ROLE_KEYWORDS)


log = logging.getLogger("trackr")

# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def unwrap(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "programmes", "results", "items", "rows"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


def rate_limit_remaining(headers):
    """Lowest remaining count across the RateLimit headers, or None if absent.

    Trackr sends e.g. `Ratelimit: "10-in-1day"; r=0; t=79562` and, once a
    limit is spent, answers 200 with `[]` instead of a 429.
    """
    remaining = None
    for value in headers.get_all("Ratelimit") or []:
        for part in value.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "r" and v.isdigit():
                remaining = int(v) if remaining is None else min(remaining, int(v))
    return remaining


def fetch_url(url):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last = None
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
                remaining = rate_limit_remaining(resp.headers)
                retry_after = resp.headers.get("Retry-After")
            rows = unwrap(json.loads(raw))
            if remaining is not None:
                log.info("Rate limit: %d requests left today.", remaining)
            if not rows and retry_after:
                hours = int(retry_after) / 3600 if retry_after.isdigit() else None
                wait = f" (resets in {hours:.1f}h)" if hours is not None else ""
                raise RuntimeError(
                    f"{url} is rate limited: empty response with Retry-After{wait}. "
                    "Not saving state."
                )
            return rows, len(raw)
        except HTTPError as e:
            last = e
            if e.code in (401, 403, 404):
                raise RuntimeError(
                    f"{url} returned HTTP {e.code}. The endpoint has probably moved. "
                    "Open the tracker in a browser, DevTools > Network > Fetch/XHR, reload, "
                    "copy the request URL that returns the programme list, and set TRACKR_API_URLS to it."
                ) from e
            log.warning("Attempt %d/3 failed: %s", attempt, e)
            time.sleep(2 * attempt)
        except (URLError, json.JSONDecodeError) as e:
            last = e
            log.warning("Attempt %d/3 failed: %s", attempt, e)
            time.sleep(2 * attempt)
    raise RuntimeError(f"All fetch attempts failed for {url}: {last}")


def fetch_all():
    rows, total_bytes = [], 0
    for url in API_URLS:
        r, n = fetch_url(url)
        log.info("%d rows from %s", len(r), url)
        rows.extend(r)
        total_bytes += n
        time.sleep(1)
    return rows, total_bytes


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def row_id(row):
    rid = row.get("id") or row.get("_id") or row.get("uuid")
    if rid:
        return str(rid)
    return f"{norm(row_company(row))}|{norm(row_title(row))}"


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("State unreadable (%s); treating as first run so we don't spam.", e)
        return None


def snapshot(rows):
    return {
        row_id(r): {
            "openingDate": r.get("openingDate"),
            "closingDate": r.get("closingDate"),
            "company": row_company(r),
            "name": row_title(r),
        }
        for r in rows
    }


def save_state(rows, raw_bytes):
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "row_count": len(rows),
        "raw_bytes": raw_bytes,
        "programmes": snapshot(rows),
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)
    log.info("State saved: %d programmes.", len(payload["programmes"]))


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


def diff(prev, rows):
    """Return (newly_listed, now_open)."""
    newly_listed, now_open = [], []
    for r in rows:
        before = prev.get(row_id(r))
        if before is None:
            newly_listed.append(r)
        elif not before.get("openingDate") and r.get("openingDate"):
            now_open.append(r)

    def key(r):
        return r.get("openingDate") or ""

    newly_listed.sort(key=key, reverse=True)
    now_open.sort(key=key, reverse=True)
    return newly_listed, now_open


def fmt_date(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%d %b %Y")
    except (ValueError, AttributeError):
        return str(iso)


def apply_link(row):
    return row.get("url") or row.get("applicationUrl") or PUBLIC_PAGE


def line(row, tag):
    close = "Rolling" if row.get("rolling") else fmt_date(row.get("closingDate"))
    return f"{tag}: {row_company(row)} — {row_title(row)} (closes {close})"


# --------------------------------------------------------------------------- #
# Notify
# --------------------------------------------------------------------------- #


def send_ntfy(newly_listed, now_open):
    if not NTFY_TOPIC:
        log.info("NTFY_TOPIC unset; skipping push.")
        return
    lines = [line(r, "OPEN") for r in now_open] + [line(r, "NEW") for r in newly_listed]
    total = len(lines)
    body = "\n".join(lines[:20])
    if total > 20:
        body += f"\n…and {total - 20} more"
    req = Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": f"{total} quant internship update{'s' if total != 1 else ''}",
            "Tags": "chart_with_upwards_trend",
            "Click": PUBLIC_PAGE,
            "Priority": "high" if now_open else "default",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as resp:
            resp.read()
        log.info("ntfy push sent (%d items).", total)
    except (URLError, HTTPError) as e:
        log.error("ntfy push failed: %s", e)


def build_email_html(newly_listed, now_open):
    def table(heading, rows, accent):
        if not rows:
            return ""
        out = (
            f'<h3 style="font-family:sans-serif;color:{accent};margin:18px 0 6px">'
            f"{heading} ({len(rows)})</h3>"
            '<table style="border-collapse:collapse;width:100%;font-family:sans-serif;font-size:14px">'
            '<tr style="background:#f2f2f2;text-align:left">'
            '<th style="padding:6px 8px">Firm</th><th style="padding:6px 8px">Role</th>'
            '<th style="padding:6px 8px">Opens</th><th style="padding:6px 8px">Closes</th>'
            '<th style="padding:6px 8px"></th></tr>'
        )
        for r in rows:
            close = "Rolling" if r.get("rolling") else fmt_date(r.get("closingDate"))
            out += (
                '<tr style="border-bottom:1px solid #e0e0e0">'
                f'<td style="padding:6px 8px"><b>{row_company(r)}</b></td>'
                f'<td style="padding:6px 8px">{row_title(r)}</td>'
                f'<td style="padding:6px 8px">{fmt_date(r.get("openingDate"))}</td>'
                f'<td style="padding:6px 8px">{close}</td>'
                f'<td style="padding:6px 8px"><a href="{apply_link(r)}">Apply</a></td></tr>'
            )
        return out + "</table>"

    return "\n".join(
        p
        for p in [
            '<div style="max-width:760px;margin:auto">',
            f'<h2 style="font-family:sans-serif">Trackr — quant/trading, Summer {SEASON}</h2>',
            table("Now open", now_open, "#1a7f37"),
            table("Newly listed", newly_listed, "#0969da"),
            f'<p style="font-family:sans-serif;font-size:12px;color:#888;margin-top:20px">'
            f'{datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")} · '
            f'<a href="{PUBLIC_PAGE}">Full tracker</a></p></div>',
        ]
        if p
    )


def send_email(newly_listed, now_open):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        log.info("Gmail creds unset; skipping email.")
        return
    bits = []
    if now_open:
        bits.append(f"{len(now_open)} now open")
    if newly_listed:
        bits.append(f"{len(newly_listed)} newly listed")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Trackr quant: " + ", ".join(bits)
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(build_email_html(newly_listed, now_open), "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, [EMAIL_TO], msg.as_string())
        log.info("Email sent to %s.", EMAIL_TO)
    except (smtplib.SMTPException, OSError) as e:
        log.error("Email send failed: %s", e)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def discover(rows):
    print(f"rows: {len(rows)}")
    if not rows:
        print("No rows — the response shape is probably not what unwrap() expects.")
        return
    print("keys on first row:", sorted(rows[0].keys()))
    print(json.dumps(rows[0], indent=2)[:2000])
    quant = [r for r in rows if is_quant(r)]
    print(f"\nmatched as quant/trading: {len(quant)}")
    for r in quant[:40]:
        print(f"  {row_company(r)} — {row_title(r)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="dump the API shape and the quant matches")
    ap.add_argument("--dry-run", action="store_true", help="print the diff, don't notify or save state")
    ap.add_argument("--all", action="store_true", help="don't apply the quant/trading filter")
    ap.add_argument("--from-file", help="read rows from a local JSON file instead of the API")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            raw = f.read()
        rows, raw_bytes = unwrap(json.loads(raw)), len(raw)
    else:
        rows, raw_bytes = fetch_all()

    log.info("Fetched %d rows (%d bytes).", len(rows), raw_bytes)

    if args.discover:
        discover(rows)
        return

    if not args.from_file and len(rows) < MIN_EXPECTED_ROWS:
        # Don't save a short snapshot: the next good fetch would flag every
        # missing programme as NEW.
        raise RuntimeError(
            f"Only {len(rows)} rows (expected >= {MIN_EXPECTED_ROWS}) — possible API change "
            "or rate limit. Not saving state."
        )

    prev = load_state()
    if prev is not None and not prev.get("programmes"):
        log.warning("Previous snapshot is empty; re-baselining instead of diffing.")
        prev = None
    if prev is None:
        if not args.dry_run:
            save_state(rows, raw_bytes)
        log.info("First run: baseline saved, no notifications.")
        return

    newly_listed, now_open = diff(prev.get("programmes", {}), rows)
    if not args.all:
        newly_listed = [r for r in newly_listed if is_quant(r)]
        now_open = [r for r in now_open if is_quant(r)]

    log.info("Diff: %d newly listed, %d now open.", len(newly_listed), len(now_open))
    for r in now_open:
        log.info("  %s", line(r, "OPEN"))
    for r in newly_listed:
        log.info("  %s", line(r, "NEW"))

    if args.dry_run:
        log.info("Dry run: nothing notified, state untouched.")
        return

    if newly_listed or now_open:
        send_ntfy(newly_listed, now_open)
        send_email(newly_listed, now_open)

    # Saved last, so a notification failure doesn't silently swallow the change.
    save_state(rows, raw_bytes)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log.error("Fatal: %s", e)
        sys.exit(1)
