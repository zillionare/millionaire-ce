# Millionaire

Millionaire is Zillionare's flagship quantitative trading platform.

The published package name is `millionaire`. The Python import namespace
is `quantide` — this is an internal name, not a brand; if you fork and
rename, you can rename the import namespace to match.

This repository is in active architectural refactoring. It supports
backtesting, paper trading, simulation, live trading, and the
built-in triple-barrier / pullback / cost-stop risk programs. The future
`millionaire-ce` branch removes private strategies and risk programs and
hides live trading.

Current status:

- the project is not using FastAPI + Vue as its target stack
- the application stack is being consolidated around FastHTML + MonsterUI
- development-stage notes are stored under `.dev/`

Until the refactor settles, treat `.dev/` as draft engineering notes rather than release documentation.

For local setup, optional dependencies, first-run browser opening, and
service-manager hooks, see `docs/installation.md`. For minimum developer-side
acceptance, see `docs/developer-acceptance.md`.

## Local Setup

This project uses a standard Python virtual environment (not conda).

```bash
# Create .venv and install the current checkout
python3 scripts/install.py

# Start paper/simulation mode and open the browser
.venv/bin/python scripts/serve.py --open
```

On Windows, use `py -3.13 scripts\install.py` and
`.venv\Scripts\python.exe scripts\serve.py --open`. The Unix helper is also
available after setup:

```bash
./start.sh
```

The optional ML extra is installed with `python3 scripts/install.py --ml30m`.
See `docs/installation.md` for Poetry groups and service-manager hooks.

The first launch is uninitialized and opens the init wizard. CE does not create
an administrator account or a default password at startup; the wizard's
administrator-password step creates the `admin` account. Until that step is
completed, login is unavailable. Launch paths bind to `127.0.0.1` by default;
set `QUANTIDE_HOST` (or `--host`) explicitly to expose another interface.

## Development Stub Mode

When you want to demo the real UI and runtime flow without connecting a real gateway or real Tushare,
start Millionaire with the environment variable `QUANTIDE_ENABLE_DEV_STUBS=1`.

Example:

```bash
QUANTIDE_ENABLE_DEV_STUBS=1 .venv/bin/python scripts/serve.py --reload
```

Or, with the helper script:

```bash
./start.sh --stub
```

With this switch enabled, the process will automatically:

- start a local `gateway` stub and route effective gateway settings to it
- replace the default `tushare` fetcher with fixture-backed local data
- expose the paper/simulation development path after the app has already completed initialization once

Notes:

- This switch is development-only and has no effect unless `QUANTIDE_ENABLE_DEV_STUBS` is truthy.
- It does not rewrite your persisted database settings; the override only exists in the running process.
- The app still expects initialization to have been completed at least once, because `app_home`, auth, and other base runtime state still come from normal initialization.
- The local gateway stub will be started on a random localhost port and logged at startup.
- If an already-running dev server was started without the switch, its old logs are not evidence against stub mode; restart that process with the switch on the actual startup command.
