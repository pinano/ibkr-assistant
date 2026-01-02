# IBKR Portfolio Manager & Monitor

A robust monitoring and management stack for Interactive Brokers (IBKR). This project provides a companion API for IBKR Gateway, a Telegram bot for real-time monitoring, and automated database logging for account history and currency balances.

## 🚀 Key Features

*   **IBKR REST API**: A FastAPI wrapper utilizing `ib_async` to provide endpoints for account summaries, positions, and option Greeks.
*   **Persistent Storage**: MariaDB database to store historical account balances and portfolio performance.
*   **Telegram Bot**:
    *   **Multi-User Security**: Configurable allow-list (`TELEGRAM_ALLOWED_IDS`) to support multiple admins.
    *   **Real-time Alerts**: Automatically notifies about significant balance changes in EUR, USD, and GBP/CHF/SEK.
    *   **Interactive Commands**: Check NAV, positions, options, and historical highs.
    *   **Flex Query Management**: Scheduled and on-demand generation of official IBKR reports.
*   **Flex Query Data Architecture**:
    *   **Automated Scheduling**: Configurable cron-based schedule (default 07:30 Tue-Sat).
    *   **Robust Archiving**: All XML reports are downloaded and archived to `./flex_queries`.
    *   **Local Reprocessing**: Ability to re-parse and re-report on any archived XML file via the bot.
    *   **Email Reports**: Automated HTML email delivery of the reports.

## 🏗 Architecture

The stack consists of 4 Docker services:
1.  `ibkr-gateway`: Runs the official IBKR Gateway (headless) for market connection.
2.  `ibkr-db`: Persists account history (`mariadb_data` volume).
3.  `ibkr-api`: FastAPI service interacting with the Gateway.
4.  `ibkr-bot`: Telegram bot logic, scheduler, and Flex Query engine.

## 🛠 Setup & Configuration

### 1. Prerequisites
- Docker & Docker Compose (`docker compose` v2+)
- An IBKR Account (Live or Paper)
- A Telegram Bot Token (@BotFather) & your Telegram User ID (@userinfobot)

### 2. Initialization
Run the initialization script to generate your environment configuration. This script handles sensitive secret generation securely.

```bash
./initialize-env.sh
```

You will be prompted to enter:
- **Telegram Token**: Your bot token.
- **Allowed Telegram IDs**: Comma-separated list of user IDs allowed to interact with the bot.
- **IBKR Credentials**: User/Pass (set these in the generated `.env` if using a real gateway, though often managed via the image env vars).
- **Flex Query Token & Query ID**: For downloading reports.

### 3. Key Environment Variables
Configuration is managed in `.env` (generated from `.env.dist`). Key variables to note:

*   `IB_FLEX_SCHEDULE_TIME`: Time to run the daily Flex Query (e.g., `07:30`).
*   `IB_FLEX_DAILY_QUERY_ID`: Your daily Flex Query ID.
*   `IB_FLEX_MONTHLY_QUERY_ID`: Your monthly Flex Query ID (runs on the 1st of each month at 12:00).
*   `CASH_DIFFERENCE_CHECK_INTERVAL`: Frequency (in seconds) to check for cash balance changes and send alerts. Default: `300` (5 minutes). Database records are only inserted when changes are detected.
*   `DB_INSERT_INTERVAL`: Frequency (in seconds) for periodic database snapshots. Default: `1800` (30 minutes). This ensures historical data is captured even without cash changes.
*   `TELEGRAM_ALLOWED_IDS`: Authorization list for bot commands.
*   `TRAEFIK_...`: If running behind a Traefik proxy.

## 🕹 Operation

### Management Scripts
The project includes convenience scripts for lifecycle management:

*   **Start Stack**:
    ```bash
    ./start.sh
    ```
    *Builds images (if needed), ensures directories exist, and starts services detached.*

*   **Stop Stack**:
    ```bash
    ./stop.sh
    ```
    *Stops and removes containers.*

### 🤖 Telegram Bot Commands

| Command | Description |
| :--- | :--- |
| `/nav` | **Net Asset Value**: Shows current Net Liquidity, P&L, Cushion, and Margin usage. |
| `/pos` | **Positions**: Real-time table of all open positions (Stock & Options). |
| `/orders` | **Orders**: Show active open orders. |
| `/trades` | **Trades**: Show executions from the current session. |
| `/quote <SMBL>` | **Quote**: Get real-time price snapshot for any symbol. |
| `/contract <SMBL>` | **Contract**: Search for contract details (ConID, Exchange). |
| `/options` | **Options Dashboard**: Interactive list of option positions grouped by expiry. Click details to see **Greeks** (Δ, Θ, etc.). |
| `/max` | **All-Time High**: Compares current NAV against the historical maximum recorded in the DB. |
| `/today` | **Daily Range**: Show today's Min, Max, and Current NAV. |
| `/flex` | **Daily Flex Report**: Manually trigger the daily Flex Query report immediately. |
| `/flex monthly` | **Monthly Flex Report**: Manually trigger the monthly Flex Query report. |
| `/flex YYYYMMDD` | **Local Flex Report**: Reprocess a previously archived XML file (e.g., `/flex 20251225`). |
| `/help` | Show the list of available commands. |

*Note: The `/risk` command has been deprecated and removed in favor of the interactive `/options` dashboard which provides more accurate, on-demand Greek calculations.*

### 🔌 REST API Endpoints
The `ibkr-api` service exposes the following endpoints (protected by `X-API-Key`):

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/account/summary` | Returns comprehensive account metrics (NAV, Cushion, P&L, etc.). |
| `GET` | `/account/positions` | Returns a list of all open positions. |
| `GET` | `/account/currencies` | Returns cash balances for all held currencies. |
| `GET` | `/option/risk/{symbol}` | Calculates real-time Greeks and market data for a specific option symbol (OSI format). |
| `GET` | `/account/orders` | (NEW) Returns active open orders. |
| `GET` | `/account/trades` | (NEW) Returns executions (trades) from the current session. |
| `GET` | `/contract/search` | (NEW) Search for contract details by symbol (query param: `symbol`, optional `secType`). |
| `GET` | `/market/snapshot/{symbol}` | (NEW) Get a real-time price snapshot for any symbol. |
| `GET` | `/options/chain/{symbol}` | (NEW) Get option expirations and strikes for a symbol. |

## 🌍 International Stocks

Use suffix notation to query non-US stocks:

| Market | Suffix | Example | Description |
|--------|--------|---------|-------------|
| 🇺🇸 USA | (none) | `AAPL` | Default |
| 🇬🇧 UK | `.L` | `BATS.L` | London Stock Exchange |
| 🇩🇪 Germany | `.DE` | `SAP.DE` | Xetra |
| 🇫🇷 France | `.PA` | `RMS.PA` | Euronext Paris |
| 🇳🇱 Netherlands | `.AS` | `ASML.AS` | Euronext Amsterdam |
| 🇨🇭 Switzerland | `.SW` | `NESN.SW` | SIX Swiss Exchange |
| 🇪🇸 Spain | `.MC` | `SAN.MC` | Bolsa de Madrid |
| 🇮🇹 Italy | `.MI` | `ENI.MI` | Borsa Italiana |

**Bot Examples:**
- `/quote BATS.L` → British American Tobacco (GBP)
- `/contract RMS.PA` → Hermès details
- `/chain ASML.AS` → ASML option chain

## 📋 Running Multiple Instances

You can run multiple independent instances of this stack on the same machine (e.g., for different IBKR accounts or domains). 

1.  **Clone or Copy the project** into a new directory (e.g., `ibkr-instance-2`).
2.  **Run `./initialize-env.sh`**.
3.  **Set a unique `PROJECT_ID`** (e.g., `ibkr-personal`, `ibkr-trading`).
4.  **Set a unique `MARIADB_HOST_PORT`** (e.g., `3307`, `3308`) to avoid port conflicts.
5.  **Configure unique credentials and domain**.
6.  **Start with `./start.sh`**.

Each instance will have its own isolated database, containers, and Traefik routing rules.

## 📂 Data & Archiving

*   **Database**: Data is stored in `./mariadb_data` (mapped volume).
*   **Flex Queries**: XML reports are archived in `./flex_queries`. This allows for auditing and re-running reports without hitting IBKR limits.

## ⚠️ Important Notes
*   **Market Data**: For Option Greeks to work in `/options`, you must have the appropriate market data subscriptions in IBKR.
*   **Gateway Login**: The `ibkr-gateway` container may require 2FA authentication on first launch or periodically. Check container logs if it fails to connect.

## 📄 License
MIT
