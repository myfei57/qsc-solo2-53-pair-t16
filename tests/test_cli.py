"""命令行入口。"""

from __future__ import annotations

import json

from coke.cli import main


def test_demo_runs_full_flow(tmp_path, capsys):
    code = main(["demo", "--fast", "--rounds", "4", "--storage-root", str(tmp_path / "state")])
    out = capsys.readouterr().out
    assert code == 0
    report = json.loads(out)
    assert report["charge"]["ok"] is True
    assert report["push"]["ok"] is True
    assert report["quench"]["ok"] is True
    assert report["cleaning"]["ok"] is True
    assert report["concurrency"]["winner"] == "manual_console"
    assert all(item["ok"] for item in report["probe"].values())


def test_status_and_config_and_reset(tmp_path, capsys):
    storage = str(tmp_path / "state")
    assert main(["--storage-root", storage, "config"]) == 0
    assert main(["--storage-root", storage, "status"]) == 0
    assert main(["--storage-root", storage, "reset"]) == 0
    out = capsys.readouterr().out
    assert "storage_root" in out


def test_serve_with_probe(tmp_path, capsys):
    code = main(
        [
            "--config",
            "config/coke.json",
            "--storage-root",
            str(tmp_path / "state"),
            "serve",
            "--port",
            "0",
            "--duration",
            "0.2",
            "--probe",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["listening"].startswith("http://")
    assert payload["probe"]["/api/status"]["ok"] is True


def test_recover_command_inspect_and_resume(tmp_path, capsys):
    from coke.app import build_platform
    from coke.clock import ManualClock
    from coke.config import default_config

    storage = str(tmp_path / "state")
    clock = ManualClock()
    first = build_platform(default_config(), clock=clock, storage_root=storage)
    first.start()
    first.crash_drill(operation="push", phase="extend")

    code = main(["--storage-root", storage, "recover", "--resume"])
    assert code == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["ok"] is True
    assert resumed["resumed"] is True
