# Two Lakes Edge unit watcher

Polls the property's SightMap availability API and emails you when a watched
unit becomes available, changes price or move-in date, or drops off the list.

One HTTP request per check. No browser, no scraping, no HTML parsing.

## Watchlist

| Tier | Units |
|---|---|
| **TOP PICK** | 381, 481, 581, 681, 781, 881 |
| **GREAT** | 301, 401, 501, 601, 701, 801, 313, 413, 513, 613, 713, 813 |
| **WOULD SETTLE** | 331, 431, 531, 631, 731, 831, 335, 435, 535, 635, 735, 835 |

Edit `config.json` → `tiers`. Tier affects sort order and subject line only;
every listed unit is watched equally.

## How it works

`https://sightmap.com/app/api/v1/1ywymd4gwq0/sightmaps/109010` returns the
complete availability feed for the property — every listed unit with its floor,
floor plan, square footage, price, and move-in date. The watcher pulls that,
keeps only unit numbers on the watchlist, and compares against `state.json`
from the previous run.

Three things trigger an email:

- **AVAILABLE** — a watched unit appears in the feed
- **CHANGED** — its price or move-in date moved
- **GONE** — it dropped off (usually means someone leased it)

Matching is on the API's `unit_number` field, so there are no false positives
from square footage or prices that happen to contain your unit numbers.

## Setup

1. In `config.json`, replace `REPLACE_WITH_YOUR_EMAIL`.
2. Repo → Settings → Secrets and variables → Actions → add `RESEND_API_KEY`.
3. Repo → Settings → Actions → General → Workflow permissions → **Read and
   write** (the watcher commits `state.json` back each run).
4. Actions tab → Run workflow → mode `debug`. Confirms the API is reachable and
   prints everything currently available.
5. Actions tab → Run workflow → mode `seed`. Records the current state silently.
6. Done. The schedule takes over.

Skipping the seed step means your first scheduled run emails you about every
watched unit already listed.

## Cost

GitHub bills Actions in whole minutes, rounded up, so each run costs 1 minute
even though it finishes in ~20 seconds. At every 15 minutes that's about
**2,880 minutes/month**.

| Setup | Free minutes | Works at 15 min? |
|---|---|---|
| **Public repo** | unlimited | Yes |
| Private + GitHub Free | 2,000/mo | No — use `*/30` |
| Private + GitHub Pro | 3,000/mo | Yes |

If you exceed the quota the workflow silently stops running and you get no
alerts. Either make the repo public (there's nothing sensitive in it) or change
the cron in `.github/workflows/watch.yml` to `*/30 * * * *`.

## Health checks

The dangerous failure is silent: if the API moves, "no watched units" looks
exactly like "nothing available." So the watcher sends a one-time **"watcher is
broken"** email if the request fails, returns a non-200, or comes back without
a `units` array — and also if a floor your watchlist depends on vanishes from
the feed. You won't get the warning again until it recovers and breaks again.

If the endpoint ever changes, run in `debug` mode, download the artifact, and
update `sightmap_api` in `config.json`. The ID in that URL is tied to the
property's SightMap instance and would only change if they rebuild it.

## Notes

- **Faster alerts.** Email push can lag. Add your carrier's SMS gateway as a
  second recipient in `config.json` → `email.to` (e.g. `5125551234@vtext.com`
  for Verizon, `@txt.att.net` for AT&T) to get a text within seconds.
- **Disappearance alerts** are on by default. Set `notify_on_disappear` to
  `false` if they're noisy.
- **Adding units later** just means editing `tiers`. Required-floor checks are
  derived from the unit numbers automatically.
