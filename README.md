# MacTrack

Personal price tracker for one Mac on Apple Store India's **education** store.
Phase 1 is the script core: fetch → parse → append to `docs/data/prices.csv`.

Device and AppleCare+ are recorded as **separate rows**. The page's headline price is
the two of them combined, so it is never recorded.

The tracked device config is the one the URL returns by default, with the **70W adapter** (sku option CK5H). Per Apple's page, the 96W adapter option costs **+INR 3100**.

## Setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python check_price.py                      # appends one row per enabled item
Get-Content docs\data\prices.csv -Tail 4
```

Each run makes one request per unique URL and waits 3–8 s between different URLs.
A 403 or 429 response, or a CAPTCHA/robot page, is recorded as `blocked` and not retried.
The script exits 0 even when individual items fail.

## Test / lint (offline)

```powershell
python -m pytest -q
python -m ruff check .
```

## Config (`config.json`)

| field | meaning |
|---|---|
| `line_item` | `device` (Product JSON-LD price) or `applecare` (`APPLECARE_BOOTSTRAP` plan price) |
| `part_number` | device: part-number prefix (e.g. `Z1KM`); applecare: plan part (e.g. `SXKK2HN/A`) |
| `expected_sku` | device only: the full configured sku must equal this, or the row is `no_match` |
| `mrp_title` | title of the item in the page's "MRP details" block |
| `enabled` | include in runs |

To track another model (e.g. an M6), add an item to this file. No code changes are needed.

## CSV statuses

`ok` · `no_match` (the page doesn't have the configured part/sku) · `blocked` · `failed` (fetch or parse error).
Empty cells mean the value was not found. Values are never guessed.
