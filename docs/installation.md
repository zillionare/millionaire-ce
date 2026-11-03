# 源码安装（开发者）

End users should use the [native installers and service controller](native-installation.md).
This page describes source-checkout installation for contributors.

## Requirements

- Python 3.13 (the supported development and CI version)
- A local Python virtual environment
- Network access to install the Python dependencies

The distribution name in `pyproject.toml` is `millionaire-ce`, and the
runtime edition defaults to `millionaire-ce`.

## Cross-platform local setup

The Python installer creates `.venv` and installs the checkout in editable
mode on Windows, macOS, and Linux:

```bash
python3 scripts/install.py
```

On Windows, use the Python launcher if that is how Python 3.13 is installed:

```powershell
py -3.13 scripts\install.py
```

The optional 30-minute ML example dependencies are not installed by default.
Install them explicitly when needed:

```bash
python3 scripts/install.py --ml30m
```

The equivalent package extra is `ml30m` (`lightgbm` and `scikit-learn`). With an
already active environment, the package form is:

```bash
python -m pip install -e ".[ml30m]"
```

Documentation and test tooling are Poetry dependency groups, rather than
runtime extras:

```bash
poetry install --with doc,test,dev
```

## Start CE in paper/simulation mode

After installation:

```bash
.venv/bin/millionaire-ce --open
```

The console command accepts `--host`, `--port`, `--reload`, `--stub`,
`--open`, and `--no-browser`. The source-tree form remains available:

```bash
.venv/bin/python scripts/serve.py --open
```

On Windows, use `.venv\Scripts\millionaire-ce.exe --open` or
`.venv\Scripts\python.exe scripts\serve.py --open`.
The `--open` hook opens the local URL in the default browser after Uvicorn
starts. It is opt-in; the equivalent environment hook is
`QUANTIDE_OPEN_BROWSER=1`. Use `--no-browser` to override that environment
setting.

Development stub mode is available for a source checkout without a real
gateway or live data provider. It depends on repository test fixtures and is
not included in the installed wheel:

```bash
.venv/bin/python scripts/serve.py --stub --open
```

The existing Unix helper remains available after setup:

```bash
./start.sh --stub
```

It expects `.venv` to already exist; it does not install dependencies.

## First-run security defaults

The first launch is uninitialized and opens the init wizard. CE does not create
an administrator account or a default password at startup; the wizard's
administrator-password step creates or updates the `admin` account. Until that
step is completed, login is unavailable, and no `admin`/`admin123` account is
created by default.

The listener defaults to `127.0.0.1:8130`. The wizard's saved address and port
take effect when the service restarts. Administrators can change them later at
System Maintenance → System Settings → Service Access. Explicit `--host` and
`--port` arguments or `QUANTIDE_HOST` and `QUANTIDE_PORT` override saved values.

## Background service

Standalone installations use native launchd, systemd or Windows services and a separate
foreground controller. See [native installation](native-installation.md) for installation,
upgrade, data locations and service commands. Source-checkout development runs in the foreground.

## Release artifact check

Build and inspect both release formats before publishing:

```bash
poetry build
python -m scripts.validate_release_artifacts
```

The validation rejects the private risk source names (including the
triple-barrier, pullback-sell, and cost-stop modules) and known private risk ID
markers in both the wheel and the sdist. Repository-only tests and notes are
intentionally excluded from the CE sdist; they remain available in the checkout
for development. The CE wheel/sdist contain the generic risk-program framework
only, and CE live trading stays hidden and unavailable.
