# AGENTS.md

## Cursor Cloud specific instructions

Python 3.12 bot that estimates 15-minute BTC above/below probabilities
(prediction-market style) and papers contracts when edge is large enough.

### Environment
- Use `.venv/bin/python` (created by the update script / README).
- Copy `.env.example` → `.env`. Prefer `DATA_PROVIDER=coinbase` and
  `SYMBOL=BTC/USD` in this environment (Binance often returns HTTP 451).

### Run
```bash
.venv/bin/python main.py
```

### Lint / test / build
No dedicated lint/test runner yet. Sanity-check with:

```bash
.venv/bin/python -m py_compile main.py prediction/*.py execution/*.py models/*.py
.venv/bin/python - <<'PY'
from prediction import WindowManager, PredictionAdvisor
print('prediction package ok')
PY
```
