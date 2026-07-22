# AGENTS.md

## Cursor Cloud specific instructions

This is a Python 3.12 scaffold for a BTC/USD paper-trading bot. The module
directories (`config/`, `data/`, `models/`, `strategies/`, `execution/`) currently
contain only empty `__init__.py` files — the application logic is not implemented
yet. There is no lint config, no test suite, and no runnable entrypoint script.

### Environment
- Dependencies live in a virtualenv at `.venv/` (gitignored). The update script
  creates it and installs `requirements.txt`. Run Python via `.venv/bin/python`.
- Setup requires the `python3.12-venv` system package (already present in the VM
  snapshot); it is a system dependency, so it is intentionally not in the update script.
- Copy `.env.example` to `.env` for config (`PAPER_INITIAL_BALANCE`, `SYMBOL`,
  `DATA_PROVIDER`). `.env` is gitignored.

### Running / market data (gotcha)
- Market data comes from `ccxt`. The default `DATA_PROVIDER=binance` returns
  HTTP 451 (geo-restricted) from Cursor Cloud VMs. Use a non-restricted exchange
  such as `kraken` (`BTC/USDT`), `coinbase` or `bitstamp` (`BTC/USD`) when fetching
  live data in this environment.

### Lint / test / build
- No linter, tests, or build step are configured yet. When adding code, verify it
  with `.venv/bin/python`.
