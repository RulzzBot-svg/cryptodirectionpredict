# BTC/USD Paper Trading Bot

Python-based paper-trading bot for Bitcoin (BTC/USDT) using live, free WebSocket/REST market data streams.

## Project Structure

```
.
├── config/          # Configuration loading and settings
│   └── settings.py
├── data/            # Market data clients (REST / WebSocket)
│   └── feed.py      # Async BTC ticker + OHLCV stream
├── models/          # Data models and database schemas
├── strategies/      # Trading strategy implementations
├── execution/       # Paper order execution and portfolio tracking
├── logs/            # Runtime logs
├── .env.example     # Example environment variables
├── requirements.txt # Python dependencies
└── README.md
```

## Prerequisites

- Python 3.10 or newer
- `pip` and `venv` (included with standard Python installs)

## Virtual Environment Setup

### 1. Clone the repository

```bash
git clone <repository-url>
cd <repository-directory>
```

### 2. Create a virtual environment

**macOS / Linux:**

```bash
python3 -m venv .venv
```

**Windows:**

```bash
python -m venv .venv
```

### 3. Activate the virtual environment

**macOS / Linux:**

```bash
source .venv/bin/activate
```

**Windows (Command Prompt):**

```bash
.venv\Scripts\activate.bat
```

**Windows (PowerShell):**

```bash
.venv\Scripts\Activate.ps1
```

Your shell prompt should show `(.venv)` when the environment is active.

### 4. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

> **Note:** `asyncio` is part of the Python standard library and does not appear as a pip package. It is available automatically once Python is installed.

### 5. Configure environment variables

Copy the example env file and adjust values as needed:

```bash
cp .env.example .env
```

Default settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `PAPER_INITIAL_BALANCE` | `10000` | Starting paper balance (USD) |
| `SYMBOL` | `BTC/USDT` | Trading pair |
| `DATA_PROVIDER` | `binance` | Market data provider (via ccxt) |
| `DATABASE_URL` | `sqlite:///./paper_trading.db` | SQLAlchemy database URL |

### 6. Deactivate when finished

```bash
deactivate
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `ccxt` | Unified exchange REST/WebSocket market data |
| `sqlalchemy` | Persistence for trades and portfolio state |
| `pandas` | Time-series analysis and data handling |
| `ta` | Technical analysis indicators |
| `python-dotenv` | Load configuration from `.env` |
| `asyncio` | Async I/O for concurrent data streams (stdlib) |

## Market Data Feed

`data/feed.py` exposes `stream_btc_data(symbol, timeframe='15m')`, an async generator that:

- Connects to Binance or Coinbase via **ccxt.pro** WebSockets (falls back to async REST polling)
- Yields ticker quotes plus OHLCV candles as a pandas DataFrame
- Adds `ema_9`, `ema_21`, and `rsi_14` columns via the `ta` library
- Reconnects automatically on disconnects and rate limits (exponential backoff)

Example:

```python
import asyncio
from data.feed import stream_btc_data

async def main():
    async for snapshot in stream_btc_data("BTC/USDT", timeframe="15m"):
        print(snapshot["last_price"])
        print(snapshot["candles"][["close", "ema_9", "ema_21", "rsi_14"]].tail(3))
        break  # remove to keep streaming

asyncio.run(main())
```

## Strategies

`strategies/base.py` defines `BaseStrategy`. Swap implementations by depending on that interface.

`EMACrossoverStrategy` in `strategies/momentum_strategy.py`:

- **BUY** when the 9 EMA crosses above the 21 EMA and RSI &lt; 60
- **SELL** when the 9 EMA crosses below the 21 EMA
- **HOLD** otherwise

```python
from strategies import EMACrossoverStrategy

strategy = EMACrossoverStrategy()
signal = strategy.generate_signal(snapshot["candles"])  # 'BUY' | 'SELL' | 'HOLD'
```

## Paper Execution

`execution/paper_engine.py` provides `PaperBroker` backed by SQLAlchemy (`portfolio` + `trades` tables).

```python
from execution import PaperBroker

broker = PaperBroker.from_database_url()  # uses DATABASE_URL / sqlite
broker.execute_order("BUY", current_price=65000.0, position_size_pct=0.25)
broker.execute_order("SELL", current_price=66000.0)
print(broker.get_portfolio())
```

- **BUY** — spend 25% of USD balance on BTC, update balances, insert a trade row
- **SELL** — liquidate all BTC, realize P/L vs average entry, update USD, insert a trade row
- Prints timestamped terminal logs on each filled trade

## Next Steps

- Add a main loop that wires `stream_btc_data` → strategy → `PaperBroker`
