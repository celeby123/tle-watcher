#!/usr/bin/env python3
"""
Two Lakes Edge unit watcher.

Reads the SightMap availability API directly (one GET, no browser) and emails
via Resend when a watched unit becomes available, changes price/date, or drops
off the list.

    python watch.py --debug        # show everything currently available
    python watch.py --seed         # record current state, send nothing
    python watch.py                # normal run: compare + email on changes
    python watch.py --test-email   # verify Resend credentials
"""

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_PATH = ROOT / "state.json"
DEBUG_DIR = ROOT / "debug"
TZ = ZoneInfo(CONFIG.get("timezone", "America/Chicago"))


def now():
    return datetime.now(TZ).strftime("%Y-%m-%d %I:%M %p %Z")


def watchlist():
    out = {}
    for tier, units in CONFIG["tiers"].items():
        for u in units:
            out.setdefault(str(u), tier)
    return out


def required_floors():
    override = CONFIG.get("required_floors")
    if override:
        return sorted({int(f) for f in override})
    return sorted({int(u[0]) for u in watchlist() if len(u) >= 3 and u[0].isdigit()})


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

class FetchError(Exception):
    pass


def fetch(debug=False):
    """Return (units_by_number, floor_labels, problems).

    Retries transient failures (timeouts, 5xx, rate limiting) before giving
    up. A single slow response is not an outage and should not alarm anyone.
    """
    url = CONFIG["sightmap_api"]
    headers = {
        "Accept": "application/json",
        "Referer": CONFIG["site_url"],
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
    }
    attempts = CONFIG.get("retry_attempts", 3)
    backoff = CONFIG.get("retry_backoff_seconds", [5, 20])
    last = None

    for i in range(1, attempts + 1):
        try:
            r = requests.get(url, headers=headers, timeout=(10, 45))
            if r.status_code == 200:
                break
            last = f"HTTP {r.status_code} from availability API"
            transient = r.status_code in (408, 429, 500, 502, 503, 504)
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            transient = True

        if not transient:
            raise FetchError(last)
        if i < attempts:
            pause = backoff[min(i - 1, len(backoff) - 1)]
            print(f"  attempt {i}/{attempts} failed ({last}) — retrying in {pause}s")
            time.sleep(pause)
        else:
            raise FetchError(f"{attempts} attempts failed — last: {last}")

    try:
        data = r.json()["data"]
    except Exception as e:
        raise FetchError(f"unexpected response shape: {type(e).__name__}")

    if not isinstance(data.get("units"), list):
        raise FetchError("no 'units' array in response — API shape changed")

    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / "sightmap.json").write_text(json.dumps(data, indent=2))

    floors = {f["id"]: f.get("filter_label", "?") for f in data.get("floors", [])}
    plans = {p["id"]: p.get("filter_label", "?") for p in data.get("floor_plans", [])}

    units = {}
    for u in data["units"]:
        n = str(u.get("unit_number", "")).strip()
        if not n:
            continue
        units[n] = {
            "unit": n,
            "floor": floors.get(u.get("floor_id"), "?"),
            "plan": plans.get(u.get("floor_plan_id"), "?"),
            "area": u.get("area"),
            "price": u.get("price"),
            "display_price": u.get("display_price") or "—",
            "available_on": u.get("available_on"),
            "display_available_on": u.get("display_available_on") or "—",
            "lease_term": u.get("display_lease_term") or "",
        }

    # Sanity: every floor our watchlist depends on should exist in the feed.
    seen = {int(lbl.split()[-1]) for lbl in floors.values()
            if lbl.split()[-1].isdigit()}
    problems = [f for f in required_floors() if f not in seen]
    return units, floors, problems


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"units": {}, "last_run": None, "degraded": False}


def save_state(watched, degraded):
    prev = load_state()["units"]
    out = {}
    for n, u in watched.items():
        out[n] = {
            "price": u["price"],
            "available_on": u["available_on"],
            "display_price": u["display_price"],
            "display_available_on": u["display_available_on"],
            "floor": u["floor"],
            "plan": u["plan"],
            "first_seen": prev.get(n, {}).get("first_seen") or now(),
            "last_seen": now(),
        }
    STATE_PATH.write_text(json.dumps(
        {"units": out, "last_run": now(), "degraded": degraded}, indent=2))


def diff(watched):
    prev = load_state()["units"]
    events = []
    for n, u in watched.items():
        old = prev.get(n)
        if not old:
            events.append({"kind": "NEW", "u": u, "old": None})
        elif (old.get("price") != u["price"]
              or old.get("available_on") != u["available_on"]):
            events.append({"kind": "CHANGED", "u": u, "old": old})
    if CONFIG.get("notify_on_disappear"):
        for n, old in prev.items():
            if n not in watched:
                events.append({"kind": "GONE", "u": {"unit": n, **old}, "old": old})
    tiers = list(CONFIG["tiers"].keys())
    wl = watchlist()
    rank = {"NEW": 0, "CHANGED": 1, "GONE": 2}
    events.sort(key=lambda e: (rank[e["kind"]],
                               tiers.index(wl.get(e["u"]["unit"], tiers[-1])),
                               e["u"]["unit"]))
    return events


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def send_email(subject, body_html):
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        print(f"!! RESEND_API_KEY not set — would have sent: {subject}", file=sys.stderr)
        return False
    r = requests.post("https://api.resend.com/emails",
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"},
                      json={"from": CONFIG["email"]["from"],
                            "to": CONFIG["email"]["to"],
                            "subject": subject, "html": body_html},
                      timeout=30)
    if r.status_code >= 300:
        print(f"!! Resend error {r.status_code}: {r.text}", file=sys.stderr)
        return False
    print(f"  email sent: {subject}")
    return True


def build_subject(events):
    wl = watchlist()
    new = [e["u"]["unit"] for e in events if e["kind"] == "NEW"]
    if new:
        top = [n for n in new if wl.get(n) == "TOP PICK"]
        if top:
            return f"🔥 TOP PICK {', '.join(top)} available at {CONFIG['property_name']}"
        return f"🏠 Unit {', '.join(new[:3])} available at {CONFIG['property_name']}"
    ch = [e["u"]["unit"] for e in events if e["kind"] == "CHANGED"]
    if ch:
        return f"📝 Unit {', '.join(ch[:3])} price/date changed at {CONFIG['property_name']}"
    gone = [e["u"]["unit"] for e in events if e["kind"] == "GONE"]
    return f"⚠️ Unit {', '.join(gone[:3])} no longer available at {CONFIG['property_name']}"


def build_body(events, total):
    wl = watchlist()
    color = {"NEW": "#0a7c2f", "CHANGED": "#9a6700", "GONE": "#8b1a1a"}
    tag = {"NEW": "AVAILABLE", "CHANGED": "CHANGED", "GONE": "GONE"}
    p = ['<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
         'max-width:640px;color:#1a1a1a">',
         f'<h2 style="margin:0 0 4px">{html.escape(CONFIG["property_name"])}</h2>',
         f'<p style="margin:0 0 22px;color:#666;font-size:13px">{now()} · '
         f'{total} units listed property-wide</p>']
    for e in events:
        u, old, c = e["u"], e["old"], color[e["kind"]]
        p.append(
            f'<div style="border-left:4px solid {c};padding:12px 16px;margin:0 0 14px;'
            f'background:#fafafa">'
            f'<div style="font-size:21px;font-weight:700">Unit {html.escape(u["unit"])}'
            f'<span style="font-size:11px;font-weight:600;color:#fff;background:{c};'
            f'padding:2px 7px;border-radius:3px;margin-left:9px;vertical-align:middle">'
            f'{tag[e["kind"]]}</span></div>'
            f'<div style="font-size:12px;color:#666;margin:3px 0 10px">'
            f'{html.escape(wl.get(u["unit"], "—"))} · {html.escape(str(u.get("floor","?")))}'
            f' · plan {html.escape(str(u.get("plan","?")))}</div>')
        if e["kind"] != "GONE":
            p.append(
                f'<table style="font-size:14px;border-collapse:collapse">'
                f'<tr><td style="padding:2px 18px 2px 0;color:#666">Price</td>'
                f'<td style="font-weight:600">{html.escape(str(u["display_price"]))}</td></tr>'
                f'<tr><td style="padding:2px 18px 2px 0;color:#666">Available</td>'
                f'<td style="font-weight:600">{html.escape(str(u["display_available_on"]))}</td></tr>'
                f'<tr><td style="padding:2px 18px 2px 0;color:#666">Size</td>'
                f'<td>{html.escape(str(u.get("area","?")))} sq ft</td></tr></table>')
        if e["kind"] == "CHANGED" and old:
            p.append(f'<div style="font-size:12px;color:#888;margin-top:8px">'
                     f'Was {html.escape(str(old.get("display_price")))} · '
                     f'{html.escape(str(old.get("display_available_on")))}</div>')
        p.append("</div>")
    p.append(f'<p style="margin-top:26px"><a href="{CONFIG["site_url"]}" '
             f'style="background:#111;color:#fff;padding:12px 22px;text-decoration:none;'
             f'border-radius:4px;font-weight:600;display:inline-block">'
             f'Open the sitemap →</a></p></div>')
    return "".join(p)


def health_alert(reason):
    return send_email(
        "🚨 Unit watcher is broken",
        f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        f'max-width:600px"><h2>{html.escape(CONFIG["property_name"])} watcher failed</h2>'
        f'<p><code>{html.escape(str(reason))}</code></p>'
        f'<p><b>You are not being alerted about any units right now.</b> The availability '
        f'API likely moved or changed shape. Re-run the workflow in <code>debug</code> mode '
        f'to see the raw response.</p>'
        f'<p style="font-size:12px;color:#888">You will not get this warning again until '
        f'the watcher recovers and breaks a second time.</p></div>')


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--test-email", action="store_true")
    args = ap.parse_args()

    if args.test_email:
        sys.exit(0 if send_email(f"Test — {CONFIG['property_name']} watcher is alive",
                                 "<p>Resend is wired up correctly.</p>") else 1)

    print(f"[{now()}] checking {CONFIG['property_name']}…")
    was_degraded = load_state().get("degraded", False)

    try:
        units, floors, problems = fetch(debug=args.debug)
    except FetchError as e:
        print(f"!! {e}", file=sys.stderr)
        if not args.debug and not args.seed and not was_degraded:
            health_alert(e)
        st = load_state(); st["degraded"] = True
        STATE_PATH.write_text(json.dumps(st, indent=2))
        sys.exit(1)

    wl = watchlist()
    watched = {n: u for n, u in units.items() if n in wl}
    print(f"  {len(units)} units listed across floors {sorted(floors.values())}")
    print(f"  {len(watched)} on your watchlist: {', '.join(sorted(watched)) or 'none'}")
    if problems:
        print(f"  ** floors missing from feed: {problems} **")

    if args.debug:
        print(f"\n  {'UNIT':<7}{'FLOOR':<10}{'PLAN':<8}{'AREA':<8}{'PRICE':<12}AVAILABLE")
        print("  " + "-" * 64)
        for n in sorted(units, key=lambda x: (len(x), x)):
            u = units[n]
            star = "  <<<" if n in wl else ""
            print(f"  {n:<7}{u['floor']:<10}{u['plan']:<8}{str(u['area']):<8}"
                  f"{u['display_price']:<12}{u['display_available_on']}{star}")
        print(f"\n  raw payload -> {DEBUG_DIR / 'sightmap.json'}")
        return

    degraded = bool(problems)
    if args.seed:
        save_state(watched, degraded)
        print("  state seeded; no email sent")
        return

    if degraded and not was_degraded:
        health_alert(f"floors missing from availability feed: {problems}")

    events = diff(watched)
    if events:
        send_email(build_subject(events), build_body(events, len(units)))
    else:
        print("  no changes")
    save_state(watched, degraded)


if __name__ == "__main__":
    main()
