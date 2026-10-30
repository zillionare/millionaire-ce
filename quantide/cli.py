"""Run the installed Millionaire CE web application."""

from __future__ import annotations

import argparse
import os
import threading
import time
import webbrowser

import uvicorn


def _truthy(value: str | None) -> bool:
    """Return whether an environment flag is enabled."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _open_browser(url: str) -> None:
    """Open the local app after the server has had time to bind."""
    time.sleep(1.0)
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    """Start Uvicorn and optionally open the local app in a browser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("QUANTIDE_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("QUANTIDE_PORT", "8000")),
    )
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--stub",
        action="store_true",
        help="enable development stubs (requires a source checkout)",
    )
    parser.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="open the local app in the default browser after startup",
    )
    parser.add_argument(
        "--no-browser",
        dest="open_browser",
        action="store_false",
        help="disable QUANTIDE_OPEN_BROWSER for this run",
    )
    parser.set_defaults(open_browser=_truthy(os.getenv("QUANTIDE_OPEN_BROWSER")))
    args = parser.parse_args(argv)

    if args.stub:
        os.environ["QUANTIDE_ENABLE_DEV_STUBS"] = "1"

    url = f"http://{args.host}:{args.port}"
    if args.open_browser:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    uvicorn.run(
        "quantide.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
