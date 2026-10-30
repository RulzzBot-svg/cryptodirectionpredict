# AGENTS.md

## Cursor Cloud specific instructions

Python 3.12 **paper BTC spot** bot (`crypto_bot.py`). The Kalshi 15m
predictor (`main.py`) is legacy — keep `LIVE_TRADING=false`.

### Environment
- Use `.venv/bin/python` (created by the update script / README).
- Copy `.env.example` → `.env`. Prefer `DATA_PROVIDER=coinbase` and
 `SYMBOL=BTC/USD` in this environment (Binance often returns HTTP 451).
- Crypto paper DB: `CRYPTO_DATABASE_URL=sqlite:///./crypto_paper.db`
  (on Render: `sqlite:////var/data/crypto_paper.db`). Never `live_trading.db`.

### Run
```bash
.venv/bin/python crypto_bot.py
```

Legacy Kalshi loop (do not arm live):

```bash
.venv/bin/python main.py
```

### Lint / test / build
No dedicated lint/test runner yet. Sanity-check with:

```bash
.venv/bin/python -m py_compile main.py crypto_bot.py prediction/*.py execution/*.py models/*.py
.venv/bin/python tests/test_crypto_paper.py
.venv/bin/python - <<'PY'
from prediction import WindowManager, PredictionAdvisor
print('prediction package ok')
PY
```
