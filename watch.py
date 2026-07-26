#!/usr/bin/env python3
"""
Two Lakes Edge unit watcher.

Loads the residences sitemap in headless Chromium, clicks through every floor,
scans all frames (the unit map is typically an embedded iframe), and emails via
Resend when a watched unit appears, changes, or disappears.

Usage:
    python watch.py --debug        # dump what the scraper sees, send nothing
    python watch.py --seed         # record current state silently (do this once)
    python watch.py                # normal run: compare + email on changes
    python watch.py --test-email   # verify Resend credentials
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_PATH = ROOT / "state.json"
DEBUG_DIR = ROOT / "debug"

TZ = ZoneInfo("America/Chicago")
CONTEXT_CHARS = 140
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def now():
    return datetime.now(TZ).strftime("%Y-%m-%d %I:%M %p %Z")


def watchlist():
    out = {}
    for tier, units in CONFIG["tiers"].items():
        for u in units:
            out.setdefault(str(u), tier)
    return out


def required_floors():
    """Floors we MUST be able to read, derived from the watchlist itself
    (unit 481 lives on floor 4). Adding a 9xx unit later automatically
    requires floor 9, with no config change."""
    override = CONFIG.get("required_floors")
    if override:
        return sorted({int(f) for f in override})
    out = set()
    for u in watchlist():
        if len(u) >= 3 and u[0].isdigit():
            out.add(int(u[0]))
    return sorted(out)


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def capture(page, label, blobs, seen_hashes):
    """Grab text from every frame. Returns True if anything NEW was captured."""
    fresh = False
    for frame in page.frames:
        if frame is page.main_frame:
            lbl = label
        else:
            lbl = f"{label} [iframe: {frame.url[:90]}]"
        try:
            text = frame.inner_text("body")
        except Exception:
            continue
        if not text or not text.strip():
            continue
        h = hashlib.sha1(text.encode()).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        blobs.append((lbl, text))
        fresh = True

        if CONFIG.get("scan_html"):
            try:
                blobs.append((f"{lbl} [html]", frame.content()))
            except Exception:
                pass
    return fresh


def find_floor_control(page, n, fc):
    """Locate a clickable element labeled as floor n, in any frame."""
    pats = [re.compile(p.replace("{n}", str(n)), re.I)
            for p in fc["label_patterns"]]
    for frame in page.frames:
        for sel in fc["candidate_selectors"]:
            try:
                els = frame.query_selector_all(sel)
            except Exception:
                continue
            for el in els[:400]:
                try:
                    if not el.is_visible():
                        continue
                    label = (el.inner_text() or el.text_content() or "").strip()
                except Exception:
                    continue
                if not label or len(label) > 20:
                    continue
                label = re.sub(r"\s+", " ", label)
                if any(p.match(label) for p in pats):
                    return el, label
    return None, None


def scrape(debug=False):
    """Return (blobs, floors_with_new_content, api_payloads)."""
    blobs, api_payloads, seen_hashes = [], [], set()
    floors_effective = set()
    fc = CONFIG.get("floor_cycle", {})

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA,
                                  viewport={"width": 1600, "height": 2200})
        page = ctx.new_page()

        def on_response(resp):
            try:
                if resp.request.resource_type not in ("xhr", "fetch"):
                    return
                if "json" not in resp.headers.get("content-type", "").lower():
                    return
                body = resp.text()
                if 20 < len(body) < 3_000_000:
                    api_payloads.append((resp.url, body))
            except Exception:
                pass

        page.on("response", on_response)

        for url in CONFIG["urls"]:
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
            except Exception as e:
                print(f"  ! skip {url}: {type(e).__name__}", file=sys.stderr)
                continue

            page.wait_for_timeout(CONFIG.get("settle_ms", 4000))

            for sel in CONFIG.get("click_selectors", []):
                try:
                    for el in page.query_selector_all(sel):
                        try:
                            el.click(timeout=1500)
                            page.wait_for_timeout(500)
                        except Exception:
                            pass
                except Exception:
                    pass

            page.wait_for_timeout(1200)
            capture(page, f"{url} [initial]", blobs, seen_hashes)

            # Click each floor in turn. The sitemap only renders one floor at
            # a time, so without this we would only ever see the ground floor.
            if fc.get("enabled"):
                for n in fc.get("floors", []):
                    el, label = find_floor_control(page, n, fc)
                    if el is None:
                        print(f"  floor {n}: no control found")
                        continue
                    try:
                        el.click(timeout=3000)
                    except Exception:
                        try:
                            el.click(timeout=3000, force=True)
                        except Exception:
                            print(f"  floor {n}: found '{label}' but click failed")
                            continue
                    page.wait_for_timeout(fc.get("pause_ms", 1500))
                    if capture(page, f"{url} [floor {n}]", blobs, seen_hashes):
                        floors_effective.add(n)
                        print(f"  floor {n}: clicked '{label}' -> new content")
                    else:
                        print(f"  floor {n}: clicked '{label}' -> no change")

        browser.close()

    for u, body in api_payloads:
        h = hashlib.sha1(body.encode()).hexdigest()
        if h not in seen_hashes:
            seen_hashes.add(h)
            blobs.append((f"[api] {u}", body))

    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        for i, (src, text) in enumerate(blobs):
            safe = re.sub(r"[^a-z0-9]+", "_", src.lower())[:80]
            (DEBUG_DIR / f"{i:02d}_{safe}.txt").write_text(text)
        print(f"\n  {len(blobs)} distinct blob(s) -> {DEBUG_DIR}")
        for src, text in blobs:
            print(f"    - [{len(text):>7} chars] {src}")
        if api_payloads:
            print("  JSON endpoints called:")
            for u, _ in dict.fromkeys(api_payloads):
                print(f"    - {u}")

    return blobs, sorted(floors_effective), api_payloads


def find_units(blobs):
    template = CONFIG["unit_regex"]
    found = {}
    for src, text in blobs:
        flat = re.sub(r"\s+", " ", text)
        for unit in watchlist():
            pattern = template.replace("{unit}", re.escape(unit))
            for m in re.finditer(pattern, flat, re.I):
                a = max(0, m.start() - CONTEXT_CHARS)
                b = min(len(flat), m.end() + CONTEXT_CHARS)
                snippet = flat[a:b].strip()
                bucket = found.setdefault(unit, [])
                if not any(e["snippet"] == snippet for e in bucket):
                    bucket.append({"source": src, "snippet": snippet})
    return found


def digest(entries):
    return hashlib.sha1("|".join(sorted(e["snippet"] for e in entries)).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"units": {}, "last_run": None, "degraded": False}


def save_state(found, degraded):
    prev = load_state()
    units = {}
    for unit, entries in found.items():
        old = prev["units"].get(unit, {})
        units[unit] = {
            "digest": digest(entries),
            "snippets": [e["snippet"] for e in entries][:4],
            "first_seen": old.get("first_seen") or now(),
            "last_seen": now(),
        }
    STATE_PATH.write_text(json.dumps(
        {"units": units, "last_run": now(), "degraded": degraded}, indent=2))


def diff(found):
    prev = load_state()["units"]
    events = []
    for unit, entries in found.items():
        d = digest(entries)
        if unit not in prev:
            events.append({"kind": "NEW", "unit": unit, "entries": entries, "old": None})
        elif prev[unit]["digest"] != d:
            events.append({"kind": "CHANGED", "unit": unit, "entries": entries,
                           "old": prev[unit].get("snippets", [])})
    if CONFIG.get("notify_on_disappear"):
        for unit in prev:
            if unit not in found:
                events.append({"kind": "GONE", "unit": unit, "entries": [],
                               "old": prev[unit].get("snippets", [])})
    order = {"NEW": 0, "CHANGED": 1, "GONE": 2}
    tiers = list(CONFIG["tiers"].keys())
    wl = watchlist()
    events.sort(key=lambda e: (order[e["kind"]],
                               tiers.index(wl.get(e["unit"], tiers[-1])), e["unit"]))
    return events


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def send_email(subject, body_html):
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        print(f"!! RESEND_API_KEY not set — would have sent: {subject}", file=sys.stderr)
        return False
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"from": CONFIG["email"]["from"], "to": CONFIG["email"]["to"],
              "subject": subject, "html": body_html},
        timeout=30)
    if r.status_code >= 300:
        print(f"!! Resend error {r.status_code}: {r.text}", file=sys.stderr)
        return False
    print(f"  email sent: {subject}")
    return True


def build_subject(events):
    wl = watchlist()
    new = [e for e in events if e["kind"] == "NEW"]
    if new:
        top = [e["unit"] for e in new if wl.get(e["unit"]) == "TOP PICK"]
        if top:
            return f"🔥 TOP PICK unit {', '.join(top)} just showed up at {CONFIG['property_name']}"
        return f"🏠 Unit {', '.join(e['unit'] for e in new[:3])} showing at {CONFIG['property_name']}"
    changed = [e["unit"] for e in events if e["kind"] == "CHANGED"]
    if changed:
        return f"📝 Listing changed: unit {', '.join(changed[:3])} at {CONFIG['property_name']}"
    gone = [e["unit"] for e in events if e["kind"] == "GONE"]
    return f"⚠️ Unit {', '.join(gone[:3])} no longer listed at {CONFIG['property_name']}"


def build_body(events, floors):
    wl = watchlist()
    colors = {"NEW": "#0a7c2f", "CHANGED": "#9a6700", "GONE": "#8b1a1a"}
    labels = {"NEW": "NOW LISTED", "CHANGED": "LISTING CHANGED", "GONE": "NO LONGER LISTED"}
    parts = ['<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
             'max-width:640px;color:#1a1a1a">',
             f'<h2 style="margin:0 0 4px">{html.escape(CONFIG["property_name"])} watch</h2>',
             f'<p style="margin:0 0 20px;color:#666;font-size:13px">Checked {now()} · '
             f'floors read: {", ".join(map(str, floors)) or "none"}</p>']
    for e in events:
        c, tier = colors[e["kind"]], wl.get(e["unit"], "—")
        parts.append(
            f'<div style="border-left:4px solid {c};padding:10px 14px;margin:0 0 16px;background:#fafafa">'
            f'<div style="font-size:19px;font-weight:700">Unit {html.escape(e["unit"])}'
            f'<span style="font-size:11px;font-weight:600;color:#fff;background:{c};padding:2px 7px;'
            f'border-radius:3px;margin-left:8px;vertical-align:middle">{labels[e["kind"]]}</span></div>'
            f'<div style="font-size:12px;color:#666;margin:2px 0 8px">{html.escape(tier)}</div>')
        for entry in e["entries"][:3]:
            parts.append(f'<div style="font-size:13px;line-height:1.5;background:#fff;'
                         f'border:1px solid #e5e5e5;padding:8px 10px;margin-bottom:6px;'
                         f'border-radius:3px">…{html.escape(entry["snippet"])}…</div>')
        if e["kind"] == "CHANGED" and e["old"]:
            parts.append(f'<div style="font-size:12px;color:#888;margin-top:6px">Was: '
                         f'{html.escape(e["old"][0][:200])}…</div>')
        parts.append("</div>")
    parts.append(
        f'<p style="margin-top:24px"><a href="{CONFIG["urls"][0]}" style="background:#111;color:#fff;'
        f'padding:11px 20px;text-decoration:none;border-radius:4px;font-weight:600;'
        f'display:inline-block">Open the sitemap →</a></p></div>')
    return "".join(parts)


def health_alert(floors, missing):
    wl = watchlist()
    blind_units = sorted(u for u in wl if int(u[0]) in missing)
    return send_email(
        f"🚨 Watcher is blind on floor {', '.join(map(str, missing))}",
        f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:600px">'
        f'<h2>{html.escape(CONFIG["property_name"])} watcher is degraded</h2>'
        f'<p>The last run could not read floor(s) <b>{", ".join(map(str, missing))}</b>. '
        f'It did read: {", ".join(map(str, floors)) or "none"}.</p>'
        f'<p><b>These units are NOT being monitored right now:</b><br>'
        f'{", ".join(blind_units)}</p>'
        f'<p>The site most likely changed its floor selector markup. Re-run the workflow '
        f'in <code>debug</code> mode and check the <code>floor N:</code> lines in the log.</p>'
        f'<p style="font-size:12px;color:#888">You will not get this warning again until the '
        f'watcher recovers and degrades a second time.</p></div>')


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--test-email", action="store_true")
    args = ap.parse_args()

    if args.test_email:
        ok = send_email(f"Test — {CONFIG['property_name']} watcher is alive",
                        "<p>Resend is wired up correctly.</p>")
        sys.exit(0 if ok else 1)

    print(f"[{now()}] scanning {CONFIG['property_name']}…")
    blobs, floors, _ = scrape(debug=args.debug)
    if not blobs:
        print("!! nothing scraped — every URL failed", file=sys.stderr)
        sys.exit(1)

    need = required_floors()
    missing = [f for f in need if f not in floors]
    degraded = bool(missing)
    print(f"\n  floors with distinct content: {floors or 'none'}")
    print(f"  floors required by watchlist:  {need}")
    if missing:
        print(f"  ** DEGRADED — could not read floor(s): {missing} **")

    found = find_units(blobs)
    print(f"  matched {len(found)} watched unit(s): {', '.join(sorted(found)) or 'none'}")

    if args.debug:
        for unit, entries in sorted(found.items()):
            print(f"\n  --- {unit} ({watchlist()[unit]}) ---")
            for e in entries[:3]:
                print(f"    …{e['snippet']}…")
        return

    if args.seed:
        save_state(found, degraded)
        print("  state seeded; no email sent")
        return

    was_degraded = load_state().get("degraded", False)
    if degraded and not was_degraded:
        health_alert(floors, missing)

    events = diff(found)
    if events:
        send_email(build_subject(events), build_body(events, floors))
    else:
        print("  no changes")

    save_state(found, degraded)


if __name__ == "__main__":
    main()
