# MacTrack

A personal, once-a-day price tracker for **one** Mac on Apple India's **Education** store:
MacBook Pro 14" M5 (10C CPU / 10C GPU), 24GB, 1TB, Silver, standard display, plus its AppleCare+.

- Dashboard: https://bahniman14.github.io/MacTrack/
- Data: [`docs/data/prices.csv`](docs/data/prices.csv). This is an append-only history; the bot adds one row per item per day.
- Alerts: Telegram only. You get a message for a price drop, a new all-time low, a check that keeps failing, recovery, and a weekly summary.

> **Configuration caveat (70W adapter):** the tracked price is for the configuration the product
> URL returns by default, which has the **70W adapter** (sku option `CK5H`). Choosing the 96W adapter
> on Apple's page adds about **INR 3,100**, and that amount is *not* included. The device and AppleCare+ are tracked
> as separate rows. Apple's headline price (device + AppleCare+) is never recorded.

## How it works

1. **Schedule:** GitHub Actions runs [`.github/workflows/check-price.yml`](.github/workflows/check-price.yml) daily at
   **12:00 IST** (`30 6 * * *` UTC). Scheduled runs can start 5-30 minutes late. You can also start
   a run by hand under *Actions → check-price → Run workflow*. Ticking *force_summary* sends the
   weekly summary immediately.
2. **Tests first:** the workflow runs `pytest` and the dashboard's Node tests. If any test fails, the job stops and a
   "MacTrack workflow failed" message is sent.
3. **Price check:** `check_price.py` fetches the page once, reads the price from the page's structured data, and
   appends one row per item to the CSV:
   - **Device price:** from the Product JSON-LD. It must match the part number *and* the full sku in `config.json`.
   - **AppleCare+ price:** from the full-price plan whose part number matches.
4. **Commit:** if the CSV changed, the workflow commits it back to `master` as `mactrack-bot`.
5. **Pages:** GitHub Pages serves `docs/` as a static dashboard that reads the CSV in the browser.

### Row statuses

| status | meaning | used as a price? |
|---|---|---|
| `ok` | price found and plausible | yes |
| `no_match` | the page doesn't offer the configured part/sku (page or configuration changed) | no |
| `blocked` | HTTP 403/429 or a bot-check page. Not retried. | no |
| `failed` | fetch/parse error, missing/foreign currency, or ambiguous duplicate prices | no |
| `suspect` | parsed, but outside `min_price_inr`/`max_price_inr` or more than 25% away from the previous ok price | no |

Every status except `ok` counts toward the failure streak.

### Alerts (Telegram)

Thresholds are in `config.json` under `"alerts"`. Each item (device / AppleCare+) is evaluated separately:

- **Price drop:** at least `drop_inr` (5000) below the previous ok price.
- **New all-time low:** strictly lower than every earlier ok price. The first-ever run sends nothing.
  If a drop and a new low happen in the same run, you get one combined message.
- **Failure:** the alert fires when the item's streak of non-ok runs reaches exactly `failure_streak` (2).
  A reminder follows at exactly `failure_reminder_streak` (7).
  For `no_match`, the message says "page or configuration changed (no_match)".
- **Recovery:** the first ok run after a failure alert sends a "working again" message.
- **Weekly summary:** sent on Sundays (IST), or whenever `FORCE_SUMMARY=1` is set.

`check_price.py` exit codes:

| code | meaning |
|---|---|
| 0 | run completed. Individual items may still have failed. |
| 3 | alert processing crashed |
| 4 | Telegram credentials were set but a message could not be delivered |

With codes 3 and 4 the CSV rows are still written and committed. The job is then marked failed, so GitHub's own
failure email also reaches you. That matters when Telegram itself is the problem.

## Files

| path | purpose |
|---|---|
| `check_price.py` | entry point: fetch → parse → sanity check → append CSV → alerts |
| `parser.py` | pure HTML → price parser (no network) |
| `sanity.py` | price bounds and the 25% jump rule (→ `suspect`) |
| `storage.py` | append-only CSV writer/reader and IST timestamps |
| `alerts.py` | alert rules, weekly summary, Telegram sender |
| `config.json` | tracked items (URL, part number, expected sku, price bounds) and alert thresholds |
| `docs/` | the dashboard (`index.html`, `app.js`, `csv.js`, `stats.js`, `styles.css`) and `data/prices.csv` |
| `tests/` | pytest suites; `tests/js/` holds the Node tests for the dashboard; `tests/fixtures/` holds saved pages (redacted) |

To track another model, add an item to `config.json`. No code changes are needed.

## Setup

### 1. Telegram secrets (GitHub)

1. Create a bot with @BotFather and note its token.
2. Send the bot a message, then read your chat id from `https://api.telegram.org/bot<token>/getUpdates`.
3. In the repo, go to *Settings → Secrets and variables → Actions → New repository secret* and add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Optional backup channel: under *GitHub Settings → Notifications → Actions*, turn on notifications for failed workflows.

Secrets are read only from environment variables. They are never written to files or logs. See `.env.example` for the names.

### 2. GitHub Pages

Go to *Settings → Pages → Build and deployment → Deploy from a branch* and choose **`master`** with the **`/docs`** folder.
The dashboard is then at https://bahniman14.github.io/MacTrack/.

### 3. Local environment (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt   # requirements.txt (pinned, used by CI) + ruff + actionlint
```

## Running locally

> **Warning:** the bot commits new rows to `docs/data/prices.csv` every day. Don't let local runs change that file.
> - Always pass `--csv <scratch file>` (or at least `--no-alerts`) when running locally.
> - Always run `git pull` before committing, so your commit doesn't conflict with the bot's.

```powershell
Remove-Item Env:TELEGRAM_* -ErrorAction SilentlyContinue           # make sure nothing is sent
Copy-Item docs\data\prices.csv $env:TEMP\prices_scratch.csv
python check_price.py --csv $env:TEMP\prices_scratch.csv --alerts-dry-run   # print alerts instead of sending
python check_price.py --csv $env:TEMP\prices_scratch.csv --no-alerts        # skip alerts entirely
```

To send yourself the weekly summary:

```powershell
$env:TELEGRAM_BOT_TOKEN = "<token>"; $env:TELEGRAM_CHAT_ID = "<chat id>"; $env:FORCE_SUMMARY = "1"
python check_price.py --csv $env:TEMP\prices_scratch.csv
Remove-Item Env:TELEGRAM_BOT_TOKEN, Env:TELEGRAM_CHAT_ID, Env:FORCE_SUMMARY
```

To preview the dashboard:

```powershell
python -m http.server 8000 --directory docs
```

Then open http://localhost:8000/. If port 8000 is refused, use another port such as 8765. Opening `index.html` directly
(`file://`) doesn't work, because the browser can't fetch the CSV that way.

## Tests

```powershell
python -m pytest -q
node --test tests/js/csv.test.mjs tests/js/stats.test.mjs
python -m ruff check .
actionlint .github/workflows/check-price.yml
```

All of these run offline, with the network mocked. CI runs the pytest and Node tests before every daily check.
