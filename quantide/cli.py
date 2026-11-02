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
        default=int(os.getenv("QUANTIDE_PORT", "8130")),
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
    commands = parser.add_subparsers(dest="command")
    service = commands.add_parser("service", help="manage the native background service")
    service.add_argument("action", choices=["install", "start", "stop", "status", "uninstall"])
    service.add_argument("--system", action="store_true", help="use a macOS LaunchDaemon")
    service.add_argument("--user", help="non-root account for a macOS system service")
    commands.add_parser("control", help="open the foreground service controller")
    counter = commands.add_parser(
        "simulated-counter", help="run the loopback demonstration counter"
    )
    counter.add_argument("--database", required=True)
    counter.add_argument("--listen-port", type=int, default=8131)
    provider = commands.add_parser(
        "quote-provider", help="enable a persistent user quote extension"
    )
    provider.add_argument("action", choices=["enable", "disable", "status"])
    args = parser.parse_args(argv)

    if args.command == "quote-provider":
        from quantide.providers.extensions import manage_quote_provider

        return manage_quote_provider(args.action)
    if args.command == "service":
        from quantide.service.host_service import manage_service

        return manage_service(args.action, system=args.system, user=args.user)
    if args.command == "control":
        from quantide.service.desktop_control import main as control_main

        return control_main()
    if args.command == "simulated-counter":
        from quantide.service.simulated_counter_api import create_counter_app

        uvicorn.run(create_counter_app(args.database), host="127.0.0.1", port=args.listen_port)
        return 0

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
