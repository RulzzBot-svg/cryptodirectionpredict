# Weather Kalshi Paper Bot

Estimates probabilities for **Kalshi daily high-temperature** brackets (starting
with NYC / `KXHIGHNY`) from an Open-Meteo **ensemble** forecast, compares them
to live market mids, and papers YES when edge ≥ `MIN_EDGE`.

This lives under `weather/` so it stays separate from the BTC 15m bot. You can
later move the folder into its own repo without changes.

## Quick start

```bash
cd weather
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py --once
```

Loop every minute:

```bash
python main.py
```

## What it does

1. Resolves `TARGET_DATE` (`today` / `tomorrow` / `YYYY-MM-DD`) in the city TZ
2. Pulls Open-Meteo ensemble daily max temps (°F)
3. Builds an integer °F probability mass function (ensemble + light Normal blend)
4. Loads open Kalshi contracts for `KALSHI_SERIES` on that date
5. Scores each contract’s model P(YES) vs market mid
6. Papers YES on the best edge if `AUTO_BET=true` and edge ≥ `MIN_EDGE`

Example status:

```text
NYC Central Park | high 2026-08-05 | ensemble mean 85.6°F [81.5, 88.7] n=31
Kalshi open contracts: 6 (KXHIGHNY)
  82° to 83°       model  28.4%  mkt  44.0%  edge -15.6¢
  80° to 81°       model  18.1%  mkt  34.5%  edge -16.4¢
Advice: SKIP — best edge ...
```

## Settlement rules (important)

Kalshi NYC highs settle on the **NWS CLI integer high** at Central Park:

| strike_type | YES if |
|-------------|--------|
| `between`   | floor ≤ temp ≤ cap |
| `greater`   | temp > floor (ticker T87 ⇒ 88°+) |
| `less`      | temp < cap (ticker T80 ⇒ 79°−) |

## Config

See `.env.example`. Useful knobs:

| Variable | Default | Meaning |
|----------|---------|---------|
| `CITY` | `NYC` | `NYC` / `CHI` / `MIA` / `LAX` |
| `KALSHI_SERIES` | city default | e.g. `KXHIGHNY` |
| `TARGET_DATE` | `tomorrow` | `today` / `tomorrow` / ISO date |
| `MIN_EDGE` | `0.08` | 8¢ model − market |
| `STAKE_NOTIONAL` | `10` | paper dollars per fill |
| `AUTO_BET` | `true` | paper fills when edged |

## Sanity check

```bash
python -m py_compile main.py config.py forecast.py markets.py probability.py advisor.py paper_book.py
python main.py --once
```

## Not included yet

- Live Kalshi order placement (paper only)
- Fee / ask-aware entry
- Multi-city parallel scans
- Historical calibration / CLV tracking
- NWS CLI settlement auto-resolver

Those are the natural next steps once paper stats look sane.
