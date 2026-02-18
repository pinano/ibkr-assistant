# IBKR Portfolio Manager & Monitor

A robust monitoring and management stack for Interactive Brokers (IBKR). This project provides a companion API for IBKR Gateway, a Telegram bot for real-time monitoring, and automated database logging for account history and currency balances.

## 🚀 Key Features

*   **IBKR REST API**: A FastAPI wrapper utilizing `ib_async` to provide endpoints for account summaries, positions, orders, trades, and option Greeks.
*   **Persistent Storage**: MariaDB database to store historical account balances and portfolio performance.
*   **Telegram Bot**:
    *   **Multi-User Security**: Configurable allow-list (`TELEGRAM_ALLOWED_IDS`) to support multiple admins.
    *   **Real-time Alerts**: Automatically notifies about significant balance changes in EUR, USD, and GBP.
    *   **Delta Monitoring**: Scheduled checks for high-delta short option positions, with consolidated digest notifications and per-contract throttling (4h cooldown).
    *   **Custom Alerts**: User-defined alerts on option Greeks, price, and IV via `/alert`.
    *   **Interactive Commands**: Check NAV, positions, options Greeks, and historical highs.
    *   **Flex Query Management**: Scheduled and on-demand generation of official IBKR reports.
*   **Flex Query Data Architecture**:
    *   **Automated Scheduling**: Configurable cron-based schedule (default 07:30 Tue-Sat).
    *   **Robust Archiving**: All XML reports are downloaded and archived to `./flex_queries`.
    *   **Local Reprocessing**: Ability to re-parse and re-report on any archived XML file via the bot.
    *   **Email Reports**: Automated HTML email delivery of the reports.
*   **Google Sheets Integration**: Custom function (`GETOPTIONDATA`) to fetch option Greeks from CBOE (US) or IBKR API (European) directly into spreadsheets. See `docs/google-sheets-script.js`.

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
Run the initialization command to generate your environment configuration. This script handles sensitive secret generation securely.

```bash
make init
```

You will be prompted to enter:
- **Telegram Token**: Your bot token.
- **Allowed Telegram IDs**: Comma-separated list of user IDs allowed to interact with the bot.
- **IBKR Credentials**: User/Pass for the Gateway.
- **Flex Query Token & Query ID**: For downloading reports.

### 3. Key Environment Variables
Configuration is managed in `.env` (generated from `.env.dist`). Key variables:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PROJECT_ID` | `ib` | Unique prefix for container names |
| `IB_FLEX_SCHEDULE_TIME` | `07:30` | Time to run the daily Flex Query |
| `IB_FLEX_DAILY_QUERY_ID` | — | Your daily Flex Query ID |
| `IB_FLEX_MONTHLY_QUERY_ID` | — | Monthly Flex Query ID (runs 1st of each month at 12:00) |
| `CASH_DIFFERENCE_CHECK_INTERVAL` | `300` | Seconds between cash balance change checks |
| `DB_INSERT_INTERVAL` | `1800` | Seconds between periodic DB snapshots |
| `TELEGRAM_ALLOWED_IDS` | — | Comma-separated authorized Telegram user IDs |
| `DOMAIN` / `CERT_RESOLVER` | — | If running behind a Traefik proxy |

## 🕹 Operation

### Management Commands
The project uses a `Makefile` for lifecycle management:

| Command | Description |
| :--- | :--- |
| `make init` | Initialize `.env` from `.env.dist` (interactive wizard) |
| `make start` | Sync `.env` with `.env.dist`, build images, and start services |
| `make stop` | Stop and remove containers |
| `make rebuild` | Rebuild images from scratch and recreate all containers |
| `make logs` | Tail logs from all containers |
| `make status` | Show container status |

### 🤖 Telegram Bot Commands

| Command | Description |
| :--- | :--- |
| `/nav` | Net Asset Value: NAV, P&L, Cushion, and Margin usage |
| `/pos` | Real-time table of all open positions (Stocks & Options) |
| `/orders` | Active open orders |
| `/trades` | Executions from the current session |
| `/quote <SMBL>` | Real-time price snapshot for any symbol |
| `/contract <SMBL>` | Search contract details (ConID, Exchange, ISIN) |
| `/chain <SMBL>` | Option chain expirations and strikes |
| `/options` | Interactive options dashboard — click to see Greeks (Δ, γ, θ, ν) |
| `/max` | All-Time High NAV vs current drawdown |
| `/today` | Today's NAV Min / Max / Current |
| `/year [YYYY]` | Yearly NAV analysis (Min, Max, Var%) |
| `/delta` | On-demand delta check for all short option positions |
| `/alert` | Manage custom alerts (`add`, `list`, `del`) |
| `/flex` | Manually trigger daily Flex Query report |
| `/flex monthly` | Manually trigger monthly Flex Query report |
| `/flex YYYYMMDD` | Reprocess a previously archived XML file |
| `/help` | Show available commands |

### 🔌 REST API Endpoints
The `ibkr-api` service exposes the following endpoints (protected by `X-API-Key`):

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Liveness probe (no auth required) |
| `GET` | `/account/summary` | Account metrics (NAV, Cushion, P&L, cash balances) |
| `GET` | `/account/positions` | All open positions (stocks and options) |
| `GET` | `/account/currencies` | Cash balances for all held currencies |
| `GET` | `/account/orders` | Active open orders |
| `GET` | `/account/trades` | Executions from the current session |
| `GET` | `/option/greeks` | Greeks for an option (params: `underlying`, `expiry`, `strike`, `right`, `conId`) |
| `GET` | `/option/risk/{symbol}` | Greeks by OSI or IBKR localSymbol format |
| `GET` | `/contract/search` | Search contract details (params: `symbol`, `secType`) |
| `GET` | `/market/snapshot/{symbol}` | Real-time price snapshot |
| `GET` | `/options/chain/{symbol}` | Option expirations and strikes |

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

You can run multiple independent instances on the same machine (e.g., for different IBKR accounts):

1.  Clone or copy the project into a new directory.
2.  Run `make init`.
3.  Set a unique `PROJECT_ID` (e.g., `ib2`).
4.  Set a unique `MARIADB_HOST_PORT` (e.g., `3307`) to avoid port conflicts.
5.  Configure unique credentials and domain.
6.  Start with `make start`.

Each instance will have its own isolated database, containers, and Traefik routing rules.

## 📂 Data & Archiving

*   **Database**: Data is stored in `./mariadb_data` (mapped volume).
*   **Flex Queries**: XML reports are archived in `./flex_queries`.

## ⚠️ Important Notes
*   **Market Data**: Option Greeks require appropriate IBKR market data subscriptions.
*   **Gateway Login**: The `ibkr-gateway` container may require 2FA authentication on first launch or periodically. Check container logs if it fails to connect.

## 📄 License
MIT
