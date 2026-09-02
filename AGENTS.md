# Agent Guidelines

## Language

All natural language output produced by agents must be in **English**.
This applies to:

- Shell commands and their inline comments
- Code comments and docstrings
- Log messages and error strings
- Commit messages
- Documentation, markdown files, and READMEs
- Any user-facing text in the bot (Telegram messages, labels, headers, etc.)
- Configuration file comments

> The only exception is content explicitly requested by the user in another language.

---

## Project Overview

This is a **Dockerized IBKR (Interactive Brokers) portfolio monitoring stack** composed of four services:

| Service | Container name | Role |
|---|---|---|
| `ibkr-gateway` | `${PROJECT_NAME}-gateway` | Official IBKR Gateway (headless) |
| `ibkr-db` | `${PROJECT_NAME}-db` | MariaDB — account history and options cache |
| `ibkr-api` | `${PROJECT_NAME}-api` | FastAPI wrapper over `ib_async` |
| `ibkr-bot` | `${PROJECT_NAME}-bot` | Telegram bot, scheduler, Flex report engine |

The API exposes endpoints protected by `X-API-Key`. The bot communicates exclusively with the API (never directly with IBKR) over the internal `ibkr` Docker network.

---

## Architecture Constraints

- **Bot → API only**: The Telegram bot must never talk directly to IBKR Gateway. All market data goes through the internal `ibkr-api` service (`http://{PROJECT_NAME}-api:8000`).
- **API → Gateway**: The API talks to IBKR Gateway via `ib_async` on port `IB_PORT` (default 4003). The host is derived dynamically: `{PROJECT_NAME}-gateway`.
- **No direct DB access from the API in bot calls**: The bot reads data via the API's REST endpoints, not by querying the DB directly (with the exception of the balance/NAV logic which uses SQLAlchemy models directly in `bot.py`).
- **Caching strategy**: Greeks are cached in `option_snapshots` DB table.
  - Market open (Mon–Fri 09:00–23:00 local TZ): serve from DB if < 60 min old.
  - Market closed: always serve from DB regardless of age.
  - `force_refresh=true` bypasses the cache.
- **Concurrency**: Parallel calls to IBKR are rate-limited with `asyncio.Semaphore(3)` in the bot and `asyncio.Semaphore(5)` in the background refresh job.
- **24h Session Token Handling & Gateway Restarts**: IBKR server resets invalidate session tokens every 24 hours. IBC in-memory soft restarts (`AUTO_RESTART_TIME`, `TWS_COLD_RESTART`) must remain disabled in `.env.dist`. Daily cold restarts are handled externally at the host/container level via `scripts/ibkr-gateway-restart.sh` (scheduled in host cron at 05:00 local time).

---

## Code Style & Conventions

### Python

- **Python 3.11+** is the minimum version.
- Use `async/await` throughout — the bot is fully async (`asyncio`), the API is FastAPI async.
- Use `httpx.AsyncClient` for all outbound HTTP calls from the bot. Never use `requests` in async code.
- Use `sqlalchemy` ORM with **context managers** (`with SessionLocal() as session:`) — never manage sessions manually.
- Use `pydantic-settings` (`BaseSettings`) for all configuration. Never read `os.environ` directly.
- Prefer `logger.debug(...)` for per-request noise; use `logger.info(...)` for lifecycle events; `logger.error(..., exc_info=True)` for exceptions.
- Use `datetime.now()` — never `datetime.utcnow()`.
- Define `safe_float(val)` locally where needed (return 0.0 for `None` or `NaN`).

### Telegram bot (aiogram 3.x)

- All bot messages are sent in **English**.
- Use HTML parse mode (`parse_mode="HTML"`) for structured output with `<code>`, `<b>`, etc.
- Use Markdown only in commands that were already using it (e.g. `/nav`, `/week`).
- Monospaced output for aligned data uses `<code>...</code>` blocks.
- Emoji conventions: 🔴 = warning/above threshold, 🟢 = OK/below threshold, ⚪ = no data, ⚠️ = alert.
- Do not add commands to `/help` unless explicitly asked.

### API (FastAPI)

- All routes are in `src/api/routes/`. Add new routes in the appropriate existing file or create a new one for a new domain.
- Always use `Depends(verify_key)` for authentication on non-public endpoints.
- Always use `Depends(get_db)` for DB-dependent endpoints.
- Response models must match the Pydantic models in `src/models.py`.
- Do not change existing `OptionGreeks` model fields without checking all callers (bot, monitor, Google Sheets scripts).

---

## Environment & Configuration

- All configuration lives in `.env` (generated from `.env.dist`). Never hardcode secrets.
- `.env.dist` is the **template** — it must stay clean, complete, and up to date. Any new env variable must be added there first.
- When adding a new env variable:
  1. Add it to `.env.dist` with a safe placeholder value and a comment.
  2. Add it to `docker-compose.yaml` in the relevant service's `environment:` block.
  3. Add it to `src/config.py` (`Settings` class).
  4. Update `README.md` key variable table if user-facing.
- `sync_env.py` / `make init` handles interactive synchronization of `.env` with `.env.dist` — the process must remain non-breaking when new variables are added.

---

## Database

- ORM models are defined in `src/models.py`. The DB schema is auto-created from `Base.metadata.create_all()` at startup — no migration files.
- Tables: `balances` (CashBalance), `option_snapshots` (OptionSnapshot), `market_cache` (MarketCache).
- `OptionSnapshot` is an UPSERT table (one row per option symbol). The `symbol` column is unique.
- Do not add new DB tables without also adding their pruning logic in `monitor.py`.
- Retention: option_snapshots ≥ 30 days, market_cache ≥ 7 days.

---

## Versioning & Releases

- The project uses **CalVer**: `vYYYY.MM.DD` (with `.1`, `.2` suffixes for same-day releases).
- `make release` runs `scripts/release.sh`, which:
  - Requires clean working tree and new commits since last tag.
  - Auto-generates CHANGELOG.md entries from `git log`.
  - Updates `VERSION` file.
  - Creates the git tag.
- Do **not** manually edit `CHANGELOG.md` or `VERSION`.
- `make update` / `make rollback` are the user-facing tools for updating/rolling back production.

---

## Makefile Usage

The `Makefile` is the primary interface for all lifecycle operations. Key targets:

| Target | Description |
|---|---|
| `make start` | Sync `.env`, build images, start stack |
| `make stop` | Stop and remove containers |
| `make restart [service]` | Full restart or specific service |
| `make rebuild [service]` | Rebuild image(s) from scratch |
| `make logs [service]` | Follow logs |
| `make db` | Open MariaDB console |
| `make release` | Create a new CalVer release |
| `make check-updates` | Audit Docker image versions |

- The `Makefile` must not export env variables globally (CRLF/import bugs in child processes).
- New make targets must follow the `##@` group + `##` description convention for `make help`.

---

## Deployment & Infrastructure

- The API is exposed to external traffic via **Traefik** reverse proxy (external network `traefik`).
- The internal services communicate over the `ibkr` bridge network only.
- `DB_BIND_IP` controls which host IP binds the MariaDB port (default `172.17.0.1` — Docker bridge, not public).
- The stack supports running **multiple independent instances** on the same host by setting a unique `PROJECT_NAME` and `PROJECT_ID`.
- `PROJECT_ID` is used to derive the MariaDB host port: `33${PROJECT_ID}` (e.g. `PROJECT_ID=001` → port `33001`).

---

## Parsing & Symbol Conventions

- International stock symbols use dot-suffix notation: `RMS.PA`, `ASML.AS`, `BATS.L`.
- Option symbols are either OSI format (US: `AAPL260117C00185000`) or IBKR European localSymbol format (`P HMI  20260220 1900 M`).
- All parsing logic lives in `src/parsing.py` — this module has **no dependencies on `src.api.*`** so it can be safely imported in unit tests.
- Exchange prefix mapping (Google Finance style) is also defined in `src/parsing.py` as `EXCHANGE_PREFIXES`.

---

## Testing

### Test suite overview

The test suite lives in `tests/` and is designed to run **without a live IBKR connection, database, or Telegram bot**. All tests exercise pure business logic extracted from the production modules.

| File | Tests | What is covered |
|---|---|---|
| `test_parsing.py` | 40 | `parse_symbol`, `parse_osi_symbol`, `parse_european_symbol`, `greeks_are_valid`, `snap_is_valid` |
| `test_helpers.py` | 25 | `_is_market_open()` weekday/hour logic; CBOE option ID string construction; `val_or_zero()` coercion |
| `test_account.py` | 22 | Exchange→prefix resolution for EUR/GBP/CHF options; `get_val()` tag/currency/BASE fallback logic |
| `test_monitor.py` | 39 | Intrinsic/time-value math for puts and calls; expiry date formatting; delta alert filtering, throttling, and cache pruning |
| `test_rich_helpers.py` | 50 | All Telegram Bot API 10.1 rich message block builders (`text_*`, `html_to_rich`, `cell`, `block_table`, `block_details`, `block_thinking`) |
| `test_models.py` | 28 | Pydantic model validation for all API response types; trade `execId` deduplication; `localSymbol or symbol` fallback |

**Total: 204 tests.**

### How to run (host / local development)

The venv at `.venv/` is a thin overlay on top of the system Python installation. The main dependencies (pydantic, sqlalchemy, aiogram, etc.) are installed as **system packages** inside Docker — they are not present in the venv. The venv only adds matplotlib/numpy for chart generation, plus `pytest`.

The venv must have `include-system-site-packages = true` in `.venv/pyvenv.cfg` for the tests to find `pydantic`. This is already the case after initial setup. If you ever recreate the venv and tests fail with `ModuleNotFoundError: No module named 'pydantic'`, run:

```bash
sed -i 's/include-system-site-packages = false/include-system-site-packages = true/' .venv/pyvenv.cfg
```

Then run the tests:

```bash
# Run the full suite (204 tests, ~0.2 s)
.venv/bin/pytest tests/

# Run with verbose output
.venv/bin/pytest tests/ -v

# Run a single file
.venv/bin/pytest tests/test_parsing.py -v

# Run a specific test class or case
.venv/bin/pytest tests/test_monitor.py::TestIntrinsicAndTimeValue -v
```

No Docker stack is required. Tests do not make network requests.

### What is NOT tested here (requires Docker)

The following components involve live IBKR/DB/Telegram interactions and are outside the scope of the offline suite:

- FastAPI route handlers (`src/api/routes/*.py`) — require a running `ibkr-api` container.
- Database read/write operations — require a running `ibkr-db` container.
- Bot command handlers that send Telegram messages — require a running `ibkr-bot` container.
- The `FlexReporter` and IBKR Flex XML parsing — require valid credentials.

### Agent rules for testing

**Before any code change:**
1. Run `pytest tests/` and confirm all 204 tests pass. If any are failing before your change, document why before proceeding.

**After any code change** touching these areas, **always re-run the suite** and confirm it is still green:

- `src/parsing.py` (any change)
- `src/api/helpers.py` (`_is_market_open`, `val_or_zero`, CBOE ID logic)
- `src/api/routes/account.py` (prefix resolution, `get_val`)
- `src/monitor.py` (alert filtering, intrinsic/TV math, date formatting)
- `src/bot.py` (rich message helpers: `text_*`, `block_*`, `cell`, `html_to_rich`)
- `src/models.py` (Pydantic model fields)
- `src/api/routes/orders.py` (trade deduplication, symbol fallback)

**When adding new business logic** (a new helper, a new filtering rule, a new formatter), add corresponding tests in the relevant `tests/test_*.py` file. Tests must:
- Be importable without Docker (no `src.api.*`, no `src.models`, no live connections).
- Replicate any logic that depends on unavailable packages as an **inline pure function** in the test file — not imported from the production module.
- Cover at least: the happy path, a boundary/edge condition, and one invalid/empty input.

**Syntax verification** after editing `src/bot.py` or any API file:

```bash
.venv/bin/python -m py_compile src/bot.py src/api/routes/orders.py src/api/routes/options.py
```

---

## Common Mistakes to Avoid

- **Do not** change `IB_CLIENT_ID` in `config.py` — it must default to `1` only if IBKR Gateway doesn't already assign a client ID; the production value is set via `.env`.
- **Do not** remove the `requests` library from `requirements.txt` — it is used by `ibflex` internally.
- **Do not** add hyphens to expiry date strings when storing or comparing — use `YYYYMMDD` format (normalize with `.replace("-", "")`).
- **Do not** use `logger.error(...)` for recoverable IBKR data gaps — use `logger.warning(...)` or `logger.debug(...)` to avoid log spam during market close.
- **Do not** strip single quotes from env variables like `IB_FLEX_TOKEN_EXPIRY` — they are required for correct shell parsing.
- **Do not** alter CHANGELOG.md or VERSION manually.
