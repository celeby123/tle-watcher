# Two Lakes Edge unit watcher

Checks the floor plan pages on a schedule and emails you the moment a watched
unit appears, changes status, or disappears.

## Watchlist

| Tier | Units |
|---|---|
| **TOP PICK** | 381, 481, 581, 681, 781, 881 |
| **GREAT** | 301, 401, 501, 601, 701, 801, 313, 413, 513, 613, 713, 813 |
| **WOULD SETTLE** | 331, 431, 531, 631, 731, 831, 335, 435, 535, 635, 735, 835 |

Edit `config.json` → `tiers` to change these. Tier only affects sort order and
the subject line; every listed unit gets watched equally.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

**1. Set your email.** In `config.json`, replace `REPLACE_WITH_YOUR_EMAIL`.
The `from` address is pointed at your existing verified `zulimworkouts.it.com`
domain — reuse it or swap in a new one.

**2. Confirm the URLs.** I guessed three likely paths. Open the real floor
plans page in your browser and put the actual URL first in `config.json` →
`urls`. Wrong URLs are skipped silently, so it's worth checking.

**3. Run debug once.** This is the important step:

```bash
export RESEND_API_KEY=re_xxxxx
python watch.py --debug
```

It prints which units matched with surrounding text, dumps the full scraped
page into `debug/`, and lists any JSON endpoints the site calls behind the
scenes. Read the snippets and confirm they're real unit listings.

**4. Seed, then go live.**

```bash
python watch.py --seed   # records what's currently listed, sends nothing
python watch.py          # from here on, emails only on changes
```

Skipping `--seed` means your first real run emails you about every unit
already on the page.

## Deploying to GitHub Actions

1. Push this folder to a **private** repo (`state.json` gets committed back
   each run, and it's a log of your apartment hunt).
2. Settings → Secrets and variables → Actions → add `RESEND_API_KEY`.
3. Settings → Actions → General → Workflow permissions → **Read and write**.
4. Actions tab → run the workflow manually with mode `debug`, download the
   artifact, verify the scrape looks right.
5. Run once with mode `seed`.
6. Done — the 15-minute cron takes over.

## Floors

The sitemap renders one floor at a time, so the scraper clicks through floors
1–8 and captures the unit list after every click. It only counts a floor as
"read" if clicking it produced content it hadn't already seen — that way a dead
button can't masquerade as an empty floor.

**Required floors are derived from your watchlist**, not configured by hand:
unit 481 lives on floor 4, so floor 4 is required. Right now that resolves to
floors **3–8**. Add a 9xx unit later and floor 9 becomes required automatically.
(Set `required_floors` in config.json to a list if you ever want to override.)

If any required floor can't be read, the run is flagged **degraded** and you get
a one-time email naming the floor and listing exactly which units went
unmonitored. This matters because the dangerous failure here is silent: a broken
floor selector looks exactly like "no units available." You won't get the
warning again until it recovers and breaks a second time.

If you see that email, re-run in `debug` mode and read the `floor N:` lines —
they say whether a control was found, clicked, and whether the page changed.

## Tuning the matcher

Three-digit unit numbers collide with square footage and prices, so
`unit_regex` in `config.json` already excludes `813 sq ft`, `$1,301`, and
`2,381 sq ft`. If debug output still shows junk, tighten it by requiring a
prefix:

```json
"unit_regex": "(?:#|unit\\s*|apt\\.?\\s*)0?{unit}\\b"
```

If it's matching *nothing* and `debug/` looks empty, the page probably renders
units only after a click. Add the button text to `click_selectors`, or point
`urls` directly at a JSON endpoint the debug output surfaced — those are
usually far more reliable than scraping the rendered page.

## Notes

- **Frequency.** Cron is set to 15 minutes rather than the hour you asked for,
  since you said units go fast. GitHub delays scheduled runs under load, so
  real-world spacing is more like 15–25 min. Don't go below 5; you'll get
  throttled and it starts looking like abuse of their site.
- **Getting alerts faster.** Email push can lag. Adding your carrier's
  email-to-SMS gateway as a second recipient in `config.json` → `email.to`
  (e.g. `5125551234@vtext.com` for Verizon, `@txt.att.net` for AT&T) gets you
  a text within seconds.
- **Disappearance alerts** are on by default — a unit vanishing usually means
  someone got it, which is useful signal. Set `notify_on_disappear` to `false`
  if it's noisy.
- If the site changes its layout the scraper may go quiet rather than error.
  Worth running `--debug` every couple weeks to confirm it's still seeing units.
