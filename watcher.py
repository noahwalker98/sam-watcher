"""SAM.gov federal lease watcher.

Pulls every notice posted in the last 48 hours under the NAICS codes below,
de-duplicates across codes, enriches each one from the detail endpoint, and
emails an HTML digest. No scoring, no filtering.

Runs on GitHub Actions twice a day. Needs three repository secrets:
GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENTS (comma separated).

Run locally without sending:  python watcher.py --no-send
Write the HTML to a file too:  python watcher.py --no-send --html out.html
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

SEARCH = "https://sam.gov/api/prod/sgs/v1/search/"
DETAIL = "https://sam.gov/api/prod/opps/v2/opportunities/"
NAICS = ["531120", "53112", "531210", "53"]  # narrow codes first, broad last
WINDOW_HOURS = 48
HDRS = {"Accept": "application/hal+json", "User-Agent": "Mozilla/5.0"}
CENTRAL = datetime.timezone(datetime.timedelta(hours=-5))

# Badge colour per notice type. Text, background, border.
TYPE_STYLE = {
    "Solicitation": ("#B42318", "#FEF3F2", "#FECDCA"),
    "Combined Synopsis/Solicitation": ("#B42318", "#FEF3F2", "#FECDCA"),
    "Presolicitation": ("#B54708", "#FFFAEB", "#FEDF89"),
    "Sources Sought": ("#175CD3", "#EFF8FF", "#B2DDFF"),
    "Special Notice": ("#6941C6", "#F9F5FF", "#E9D7FE"),
    "Award Notice": ("#027A48", "#ECFDF3", "#A6F4C5"),
    "Justification": ("#475467", "#F2F4F7", "#D0D5DD"),
}
DEFAULT_STYLE = ("#475467", "#F2F4F7", "#D0D5DD")

SET_ASIDE = {
    "SDVOSBC": "SDVOSB SET-ASIDE",
    "SDVOSBS": "SDVOSB SOLE SOURCE",
    "VSA": "VET-OWNED SET-ASIDE",
    "VSS": "VET-OWNED SOLE SOURCE",
    "SBA": "SMALL BUSINESS SET-ASIDE",
    "SBP": "PARTIAL SMALL BUSINESS",
    "8A": "8(A) SET-ASIDE",
    "8AN": "8(A) SOLE SOURCE",
    "HZC": "HUBZONE SET-ASIDE",
    "HZS": "HUBZONE SOLE SOURCE",
    "WOSB": "WOSB SET-ASIDE",
    "WOSBSS": "WOSB SOLE SOURCE",
    "EDWOSB": "EDWOSB SET-ASIDE",
    "EDWOSBSS": "EDWOSB SOLE SOURCE",
}
# The ones that matter most to Guardian and Direct Point get the loud colour.
VET_SET_ASIDES = {"SDVOSBC", "SDVOSBS", "VSA", "VSS"}


def get_json(url, timeout=60):
    req = urllib.request.Request(url, headers=HDRS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def clean(html):
    t = re.sub(r"<[^>]+>", " ", html or "")
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&ndash;", "-")
    t = t.replace("&rsquo;", "'").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", t).strip()


def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

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
                data = get_json(SEARCH + "?" + urllib.parse.urlencode(params), 90)
            except Exception as exc:
                errors.append("search naics %s page %s: %s" % (code, page, exc))
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
                hits.append({
                    "id": oid,
                    "title": o.get("title") or "(no title)",
                    "noticeType": (o.get("type") or {}).get("value") or "Notice",
                    "solicitationNumber": (o.get("solicitationNumber") or "").strip(),
                    "published": pub,
                    "responseDate": o.get("responseDate"),
                    "department": org[0].get("name") if org else None,
                    "office": org[-1].get("name") if len(org) > 1 else None,
                    "matchedNaics": code,
                    "isCanceled": bool(o.get("isCanceled")),
                    "description": clean((o.get("descriptions") or [{}])[0].get("content"))[:320],
                    "link": "https://sam.gov/opp/%s/view" % oid,
                    "setAside": None, "location": None, "contact": None,
                    "awardAmount": None, "awardee": None,
                })

            page += 1
            if page * 100 >= total:
                break

    hits.sort(key=lambda h: h["published"], reverse=True)
    enrich(hits, errors)
    return hits, errors


def enrich(hits, errors):
    """Second call per notice for set-aside, location, contact and award data."""
    for h in hits:
        try:
            d = (get_json(DETAIL + h["id"], 30) or {}).get("data2") or {}
        except Exception as exc:
            errors.append("detail %s: %s" % (h["id"], exc))
            continue

        sol = d.get("solicitation") or {}
        code = (sol.get("setAside") or "").strip().upper()
        if code and code != "NONE":
            h["setAside"] = (code, SET_ASIDE.get(code, code))

        pop = d.get("placeOfPerformance") or {}
        city = (pop.get("city") or {}).get("name")
        state = (pop.get("state") or {}).get("code")
        h["location"] = ", ".join(x for x in (city, state) if x) or None

        poc = (d.get("pointOfContact") or [{}])[0]
        if poc.get("fullName") or poc.get("email"):
            h["contact"] = (poc.get("fullName"), poc.get("email"))

        award = d.get("award") or {}
        if award.get("amount"):
            try:
                h["awardAmount"] = "$%s" % format(float(award["amount"]), ",.0f")
            except (TypeError, ValueError):
                h["awardAmount"] = str(award["amount"])
        h["awardee"] = ((award.get("awardee") or {}).get("name")) or None

def due_text(h, now):
    if not h["responseDate"]:
        return None
    try:
        d = datetime.datetime.fromisoformat(h["responseDate"])
    except ValueError:
        return h["responseDate"]
    days = (d - now).days
    when = d.strftime("%b %d, %Y")
    if days < 0:
        return "%s (closed)" % when
    return "%s (%d days)" % (when, days)


def fmt_date(dt):
    return dt.astimezone(CENTRAL).strftime("%b %d, %Y at %I:%M %p CT")


def text_card(h, now):
    lines = ["[%s]%s %s" % (h["noticeType"].upper(),
                            " [%s]" % h["setAside"][1] if h["setAside"] else "",
                            h["title"])]
    org = " > ".join(x for x in (h["department"], h["office"]) if x)
    if org:
        lines.append("  " + org)
    lines.append("  Published %s" % fmt_date(h["published"]))
    due = due_text(h, now)
    if due:
        lines.append("  Offers due %s" % due)
    if h["location"]:
        lines.append("  Location %s" % h["location"])
    if h["awardAmount"] or h["awardee"]:
        lines.append("  Award %s to %s" % (h["awardAmount"] or "amount not stated",
                                           h["awardee"] or "awardee not stated"))
    meta = [x for x in (h["solicitationNumber"], "NAICS " + h["matchedNaics"]) if x]
    lines.append("  " + " | ".join(meta))
    lines.append("  " + h["link"])
    return "\n".join(lines)


def build_text(hits, errors, now, recent, older):
    out = []
    if errors:
        out.append("FETCH ERRORS, this list may be incomplete:")
        out.extend("  " + e for e in errors[:10])
        out.append("")
    out.append("%d notices posted in the last 48 hours. %d in the last 12 hours, %d before that."
               % (len(hits), len(recent), len(older)))
    for heading, group in (("POSTED IN THE LAST 12 HOURS", recent),
                           ("POSTED 12 TO 48 HOURS AGO", older)):
        out.append("")
        out.append("%s (%d)" % (heading, len(group)))
        out.append("-" * 60)
        if not group:
            out.append("None.")
        for h in group:
            out.append("")
            out.append(text_card(h, now))
    out.append("")
    out.append("Searched NAICS %s on sam.gov, de-duplicated across codes." % ", ".join(NAICS))
    return "\n".join(out)


def badge(label, fg, bg, border):
    return (
        '<span style="display:inline-block;padding:3px 8px;margin:0 6px 6px 0;'
        'font:600 10px/1.2 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'letter-spacing:.6px;text-transform:uppercase;color:%s;background:%s;'
        'border:1px solid %s;border-radius:4px;">%s</span>' % (fg, bg, border, esc(label)))


def field(label, value, color="#101828"):
    return (
        '<td style="padding:0 22px 0 0;vertical-align:top;">'
        '<div style="font:600 10px/1.4 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'letter-spacing:.6px;text-transform:uppercase;color:#98A2B3;white-space:nowrap;">%s</div>'
        '<div style="font:600 13px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'color:%s;white-space:nowrap;">%s</div></td>' % (esc(label), color, esc(value)))

def html_card(h, now):
    fg, bg, br = TYPE_STYLE.get(h["noticeType"], DEFAULT_STYLE)
    badges = badge(h["noticeType"], fg, bg, br)
    if h["setAside"]:
        code, label = h["setAside"]
        if code in VET_SET_ASIDES:
            badges += badge(label, "#FFFFFF", "#027A48", "#027A48")
        else:
            badges += badge(label, "#475467", "#FFFFFF", "#D0D5DD")
    if h["isCanceled"]:
        badges += badge("Cancelled", "#B42318", "#FFFFFF", "#FECDCA")

    org = " &rsaquo; ".join(esc(x) for x in (h["department"], h["office"]) if x)

    cells = [field("Published", fmt_date(h["published"]))]
    due = due_text(h, now)
    if due:
        cells.append(field("Offers due", due, "#B42318" if "days" in due else "#101828"))
    if h["location"]:
        cells.append(field("Location", h["location"]))
    if h["awardAmount"]:
        cells.append(field("Award amount", h["awardAmount"], "#027A48"))
    if h["awardee"]:
        cells.append(field("Awardee", h["awardee"], "#027A48"))

    meta = [x for x in (h["solicitationNumber"], "NAICS " + h["matchedNaics"]) if x]
    if h["contact"]:
        name, mail = h["contact"]
        meta.append("%s%s" % (name or "", " " + mail if mail else ""))

    desc = ""
    if h["description"]:
        desc = ('<div style="margin:10px 0 0;font:400 13px/1.6 -apple-system,Segoe UI,'
                'Helvetica,Arial,sans-serif;color:#667085;">%s</div>' % esc(h["description"]))

    return (
        '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" '
        'style="border:1px solid #EAECF0;border-radius:8px;background:#FFFFFF;margin:0 0 14px;">'
        '<tr><td style="padding:16px 18px;">'
        '%s'
        '<div style="margin:2px 0 6px;"><a href="%s" style="font:600 16px/1.4 -apple-system,'
        'Segoe UI,Helvetica,Arial,sans-serif;color:#1849A9;text-decoration:none;">%s</a></div>'
        '<div style="margin:0 0 12px;font:600 10px/1.4 -apple-system,Segoe UI,Helvetica,Arial,'
        'sans-serif;letter-spacing:.6px;text-transform:uppercase;color:#98A2B3;">%s</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>%s</tr></table>'
        '%s'
        '<div style="margin:12px 0 0;padding:10px 0 0;border-top:1px solid #F2F4F7;'
        'font:400 12px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#98A2B3;">%s</div>'
        '</td></tr></table>'
        % (badges, esc(h["link"]), esc(h["title"]), org or "Agency not stated",
           "".join(cells), desc, esc(" | ".join(meta))))

def build_html(hits, errors, now, recent, older):
    parts = ['<div style="background:#F9FAFB;padding:24px 12px;">'
             '<div style="max-width:680px;margin:0 auto;">']

    parts.append(
        '<div style="margin:0 0 4px;font:700 22px/1.3 -apple-system,Segoe UI,Helvetica,'
        'Arial,sans-serif;color:#101828;">%d notices in the last 48 hours</div>'
        '<div style="margin:0 0 20px;font:400 14px/1.5 -apple-system,Segoe UI,Helvetica,'
        'Arial,sans-serif;color:#667085;">%d in the last 12 hours, %d before that. '
        'NAICS %s on SAM.gov.</div>' % (len(hits), len(recent), len(older), ", ".join(NAICS)))

    if errors:
        parts.append(
            '<div style="margin:0 0 18px;padding:12px 14px;border:1px solid #FECDCA;'
            'background:#FEF3F2;border-radius:8px;font:400 13px/1.6 -apple-system,Segoe UI,'
            'Helvetica,Arial,sans-serif;color:#B42318;"><strong>Fetch errors, this list may be '
            'incomplete.</strong><br>%s</div>' % "<br>".join(esc(e) for e in errors[:6]))

    for heading, group in (("Posted in the last 12 hours", recent),
                           ("Posted 12 to 48 hours ago", older)):
        parts.append(
            '<div style="margin:22px 0 12px;padding:0 0 8px;border-bottom:2px solid #EAECF0;'
            'font:700 12px/1.4 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'letter-spacing:.8px;text-transform:uppercase;color:#344054;">%s '
            '<span style="color:#98A2B3;">(%d)</span></div>' % (esc(heading), len(group)))
        if not group:
            parts.append(
                '<div style="margin:0 0 14px;font:400 13px/1.6 -apple-system,Segoe UI,'
                'Helvetica,Arial,sans-serif;color:#98A2B3;">None.</div>')
        for h in group:
            parts.append(html_card(h, now))

    parts.append(
        '<div style="margin:24px 0 0;padding:14px 0 0;border-top:1px solid #EAECF0;'
        'font:400 12px/1.6 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#98A2B3;">'
        'Everything posted under NAICS %s in the last 48 hours, de-duplicated across codes. '
        'No filtering applied. Runs at 8am and 4pm Central.</div>' % ", ".join(NAICS))

    parts.append("</div></div>")
    return ('<!DOCTYPE html><html><body style="margin:0;padding:0;background:#F9FAFB;">'
            + "".join(parts) + "</body></html>")


def send(subject, text_body, html_body):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    recipients = [r.strip() for r in os.environ["RECIPIENTS"].split(",") if r.strip()]

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(sender, password)
        s.send_message(msg)
    print("sent to %s" % ", ".join(recipients))


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    hits, errors = fetch()

    recent = [h for h in hits if (now - h["published"]).total_seconds() <= 12 * 3600]
    older = [h for h in hits if (now - h["published"]).total_seconds() > 12 * 3600]

    text_body = build_text(hits, errors, now, recent, older)
    html_body = build_html(hits, errors, now, recent, older)
    print(text_body)

    if "--html" in sys.argv:
        path = sys.argv[sys.argv.index("--html") + 1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_body)
        print("\n[html written to %s]" % path, file=sys.stderr)

    stamp = now.astimezone(CENTRAL)
    subject = "%sSAM.gov leases, %s %s: %d in the last 48 hours" % (
        "[FETCH ERRORS] " if errors else "",
        stamp.strftime("%b %d"),
        "AM" if stamp.hour < 12 else "PM",
        len(hits),
    )

    if "--no-send" in sys.argv:
        print("\n[--no-send set, email skipped]", file=sys.stderr)
        return
    send(subject, text_body, html_body)


if __name__ == "__main__":
    main()
