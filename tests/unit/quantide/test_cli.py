from __future__ import annotations

import quantide.cli as cli


def test_cli_starts_uvicorn_with_requested_bind(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        captured.update({"app": app, **kwargs})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setenv("QUANTIDE_OPEN_BROWSER", "0")

    assert cli.main(["--host", "127.0.0.1", "--port", "9123", "--no-browser"]) == 0
    assert captured == {
        "app": "quantide.app:app",
        "host": "127.0.0.1",
        "port": 9123,
        "reload": False,
    }


def test_cli_stub_mode_sets_environment(monkeypatch) -> None:
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    cli.os.environ.pop("QUANTIDE_ENABLE_DEV_STUBS", None)

    try:
        assert cli.main(["--stub", "--no-browser"]) == 0
        assert cli.os.environ["QUANTIDE_ENABLE_DEV_STUBS"] == "1"
    finally:
        cli.os.environ.pop("QUANTIDE_ENABLE_DEV_STUBS", None)
