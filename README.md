# BTC/USD Paper Trading Bot

Python-based paper-trading bot for Bitcoin (BTC/USDT) using live, free WebSocket/REST market data streams.

## Project Structure

```
.
├── config/          # Configuration loading and settings
├── data/            # Market data clients (REST / WebSocket)
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

## Next Steps

- Implement market data ingestion in `data/`
- Define strategy interfaces in `strategies/`
- Wire paper execution and balance tracking in `execution/`
- Add SQLAlchemy models in `models/`
