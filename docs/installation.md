# Installation

This page describes the current Community Edition (CE) local setup. It is a
developer setup and a first pass at an installation contract; native Windows,
macOS, and Linux installers are not part of this release groundwork.

## Requirements

- Python 3.13 or newer
- A local Python virtual environment
- Network access to install the Python dependencies

The current distribution name in `pyproject.toml` is `millionaire`. The
repository owner may choose a different repository or distribution name later;
the commands below intentionally use the current tree and do not predict that
rename.

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

The equivalent package extra is `ml30m` (`lightgbm` and `baostock`). With an
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
.venv/bin/python scripts/serve.py --open
```

On Windows, use `.venv\Scripts\python.exe scripts\serve.py --open`.
The `--open` hook opens the local URL in the default browser after Uvicorn
starts. It is opt-in; the equivalent environment hook is
`QUANTIDE_OPEN_BROWSER=1`. Use `--no-browser` to override that environment
setting.

Development stub mode is available for a local run without a real gateway or
live data provider:

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

The bundled launch paths bind to `127.0.0.1` by default. Set `--host` or
`QUANTIDE_HOST` explicitly to expose the UI on another interface. A persisted
non-localhost `app_host` remains honored by settings consumers; it is not
silently rewritten to localhost.

## Service-manager hook

The supported service-manager boundary is the same foreground command used
above. A service manager should invoke the environment's Python executable,
set its working directory to the checkout, and use a persistent data/config
directory appropriate for the host. For example, a systemd unit can use:

```ini
[Service]
WorkingDirectory=/srv/millionaire
ExecStart=/srv/millionaire/.venv/bin/python /srv/millionaire/scripts/serve.py
Restart=on-failure
Environment=QUANTIDE_HOST=127.0.0.1
Environment=QUANTIDE_PORT=8000
```

Windows Task Scheduler, launchd, and other supervisors can use the analogous
`.venv` Python command. No OS-specific service registration or native
installer is claimed by this repository yet.

## Release artifact check

Build and inspect both release formats before publishing:

```bash
poetry build
python3 scripts/validate_release_artifacts.py dist
```

The validation rejects the private risk source names and known private risk
IDs. Repository-only tests and notes are intentionally excluded from the CE
sdist; they remain available in the checkout for development.
