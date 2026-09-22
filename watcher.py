"""SAM.gov federal lease watcher.

Pulls every notice posted in the last 48 hours under the NAICS codes below,
de-duplicates across codes, and emails the list. No scoring, no filtering.

Runs on GitHub Actions twice a day. Needs three repository secrets:
GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENTS (comma separated).

Run locally without sending:  python watcher.py --no-send
"""

import datetime
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from email.message import EmailMessage

BASE = "https://sam.gov/api/prod/sgs/v1/search/"
NAICS = ["531120", "53112", "531210", "53"]  # narrow codes first, broad last
WINDOW_HOURS = 48
HDRS = {"Accept": "application/hal+json", "User-Agent": "Mozilla/5.0"}
CENTRAL = datetime.timezone(datetime.timedelta(hours=-5))


def get(params):
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HDRS)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def fetch():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=WINDOW_HOURS)
    seen, hits, errors = set(), [], []

    for code in NAICS:
        page = 0
        while True:
            params = {"index": "opp", "mode": "search", "qMode": "ALL",
                      "is_active": "true", "naics": code,
                      "size": "100", "page": str(page)}
            try:
                data = get(params)
            except Exception as exc:
                errors.append("naics %s page %s: %s" % (code, page, exc))
                break

            results = (data.get("_embedded") or {}).get("results") or []
            total = (data.get("page") or {}).get("totalElements", 0)
            if page == 0:
                print("naics %s: %s active records" % (code, total), file=sys.stderr)
            if not results:
                break

            for o in results:
                oid = o.get("_id")
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                raw = o.get("publishDate")
                if not raw:
                    continue
                try:
                    pub = datetime.datetime.fromisoformat(raw)
                except ValueError:
                    continue
                if pub < cutoff:
                    continue

                org = o.get("organizationHierarchy") or []
                desc = (o.get("descriptions") or [{}])[0].get("content") or ""
                desc = re.sub(r"<[^>]+>", " ", desc)
                desc = re.sub(r"&nbsp;?", " ", desc)
                desc = re.sub(r"\s+", " ", desc).strip()

                hits.append({
                    "title": o.get("title") or "(no title)",
                    "noticeType": (o.get("type") or {}).get("value") or "unknown",
                    "solicitationNumber": (o.get("solicitationNumber") or "").strip() or "none",
                    "published": pub,
                    "responseDate": o.get("responseDate"),
                    "department": org[0].get("name") if org else None,
                    "office": org[-1].get("name") if org else None,
                    "matchedNaics": code,
                    "isCanceled": bool(o.get("isCanceled")),
                    "description": desc[:400],
                    "link": "https://sam.gov/opp/%s/view" % oid,
                })

            page += 1
            if page * 100 >= total:
                break

    hits.sort(key=lambda h: h["published"], reverse=True)
    return hits, errors

def block(h, now):
    org = " / ".join(x for x in (h["department"], h["office"]) if x) or "not stated"

    due = "none stated"
    if h["responseDate"]:
        try:
            d = datetime.datetime.fromisoformat(h["responseDate"])
            due = "%s (%s days)" % (d.strftime("%b %d %Y"), (d - now).days)
        except ValueError:
            due = h["responseDate"]

    flag = "CANCELLED. " if h["isCanceled"] else ""
    lines = [
        "%s%s" % (flag, h["title"]),
        "  %s" % org,
        "  %s | solicitation %s | NAICS %s" % (
            h["noticeType"], h["solicitationNumber"], h["matchedNaics"]),
        "  Posted %s | Due %s" % (
            h["published"].astimezone(CENTRAL).strftime("%b %d %I:%M %p CT"), due),
        "  %s" % h["link"],
    ]
    if h["description"]:
        lines.append("  %s" % h["description"])
    return "\n".join(lines)


def build_report(hits, errors, now):
    recent = [h for h in hits if (now - h["published"]).total_seconds() <= 12 * 3600]
    older = [h for h in hits if (now - h["published"]).total_seconds() > 12 * 3600]

    out = []
    if errors:
        out.append("FETCH ERRORS, the list below may be incomplete:")
        out.extend("  " + e for e in errors)
        out.append("")

    out.append("%d notices posted in the last 48 hours. %d in the last 12 hours, %d before that."
               % (len(hits), len(recent), len(older)))

    for heading, group in (("POSTED IN THE LAST 12 HOURS", recent),
                           ("POSTED 12 TO 48 HOURS AGO", older)):
        out.append("")
        out.append("=" * 60)
        out.append("%s (%d)" % (heading, len(group)))
        out.append("=" * 60)
        if not group:
            out.append("")
            out.append("None.")
        for h in group:
            out.append("")
            out.append(block(h, now))

    out.append("")
    out.append("Searched NAICS %s on sam.gov, de-duplicated across codes."
               % ", ".join(NAICS))
    return "\n".join(out)


def send(subject, body):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    recipients = [r.strip() for r in os.environ["RECIPIENTS"].split(",") if r.strip()]

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(sender, password)
        s.send_message(msg)
    print("sent to %s" % ", ".join(recipients))


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    hits, errors = fetch()
    body = build_report(hits, errors, now)
    print(body)

    stamp = now.astimezone(CENTRAL)
    subject = "%sSAM.gov leases, %s %s: %d posted in the last 48 hours" % (
        "[FETCH ERRORS] " if errors else "",
        stamp.strftime("%b %d"),
        "AM" if stamp.hour < 12 else "PM",
        len(hits),
    )

    if "--no-send" in sys.argv:
        print("\n[--no-send set, email skipped]", file=sys.stderr)
        return
    send(subject, body)


if __name__ == "__main__":
    main()
