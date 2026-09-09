from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import signal
import threading
from urllib.request import urlopen

import pytest
from sqlalchemy import create_engine, text

from knowledge_platform.catalog.migrations import migrate_to_latest


ROOT = Path(__file__).resolve().parents[2]
PYTHON = os.environ.get("KNOWLEDGE_TEST_PYTHON", sys.executable)


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            migrate_to_latest(connection)
            connection.execute(
                text(
                    """
                    INSERT INTO knowledge_spaces
                        (id, name, description, permissions_json, created_at, updated_at)
                    VALUES ('space_kb_default', 'Supervisor fixture', 'local', '{}', 'now', 'now')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO knowledge_datasets
                        (id, space_id, name, version, kind, description, asset_ids,
                         semantic_asset_ids, capabilities, freshness, permissions_json,
                         manifest_digest, created_at, updated_at)
                    VALUES ('dataset_kb_default', 'space_kb_default', 'Supervisor fixture', '1',
                            'wiki', 'local', '[]', '[]', '[]', '{}', '{}', '', 'now', 'now')
                    """
                )
            )
    finally:
        engine.dispose()


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(home: Path, command: str, *extra: object, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    args = [PYTHON, "-m", "knowledge_platform.local.supervisor", command, "--home", str(home)]
    args.extend(str(value) for value in extra)
    environment = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT / "backend")}
    return subprocess.run(args, cwd=cwd or home.parent, env=environment, capture_output=True, text=True, timeout=45)


def _start(home: Path, catalog: Path, wiki: Path, port: int) -> subprocess.CompletedProcess[str]:
    return _run(
        home,
        "start",
        "--catalog",
        catalog,
        "--wiki-root",
        wiki,
        "--port",
        port,
    )


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    assert len(result.stdout.strip().splitlines()) == 1, result.stdout
    return json.loads(result.stdout)


def test_supervisor_real_start_status_stop_owns_child_and_preserves_source(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    before = hashlib.sha256(catalog.read_bytes()).hexdigest()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("# supervisor\n", encoding="utf-8")
    home = tmp_path / "home"
    port = _port()

    started = _start(home, catalog, wiki, port)
    assert started.returncode == 0, started.stderr
    running = _json(started)
    assert running["format"] == "puddingknowledge-local-supervisor/v1"
    assert running["state"] == "running"
    assert running["active"] is True
    assert running["health"] is True
    assert running["child_alive"] is True
    assert running["ownership_verified"] is True
    assert running["manager_pid"] > 0
    assert running["child_pid"] > 0
    assert Path(running["run_dir"]).parent == home / "run"
    assert (home / "run" / "token").stat().st_mode & 0o077 == 0
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == before

    status = _run(home, "status")
    assert status.returncode == 0
    assert _json(status)["state"] == "running"

    stopped = _run(home, "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert _json(stopped)["state"] == "stopped"
    after = _json(_run(home, "status"))
    assert after["state"] == "stopped"
    assert after["active"] is False
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == before
    assert not (home / "run" / "supervisor.json").exists()


def test_supervisor_rejects_duplicate_start_and_does_not_kill_external_listener(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("# supervisor\n", encoding="utf-8")
    home = tmp_path / "home"
    port = _port()
    first = _start(home, catalog, wiki, port)
    assert first.returncode == 0, first.stderr
    try:
        duplicate = _start(home, catalog, wiki, port)
        assert duplicate.returncode != 0
        assert _json(_run(home, "status"))["state"] == "running"

        external_port = _port()
        with socket.socket() as external:
            external.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            external.bind(("127.0.0.1", external_port))
            external.listen(1)
            other_home = tmp_path / "other-home"
            conflict = _start(other_home, catalog, wiki, external_port)
            assert conflict.returncode != 0
            assert external.fileno() >= 0
    finally:
        _run(home, "stop")


def test_supervisor_does_not_accept_external_valid_spaces_service_as_owned_child(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("# supervisor\n", encoding="utf-8")

    class ExternalSpacesHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            if self.path != "/v1/spaces":
                self.send_error(404)
                return
            body = json.dumps({"status": "ok", "data": {"spaces": []}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    external = ThreadingHTTPServer(("127.0.0.1", 0), ExternalSpacesHandler)
    thread = threading.Thread(target=external.serve_forever, daemon=True)
    thread.start()
    try:
        external_port = int(external.server_address[1])
        with urlopen(f"http://127.0.0.1:{external_port}/v1/spaces", timeout=2) as response:
            assert json.loads(response.read())["status"] == "ok"
        result = _start(tmp_path / "home", catalog, wiki, external_port)
        assert result.returncode != 0
        failure = _json(result)
        assert failure["state"] == "error"
        assert "failed" in failure["error"] or "exited" in failure["error"]
        # The listener that supplied the plausible response remains untouched.
        with urlopen(f"http://127.0.0.1:{external_port}/v1/spaces", timeout=2) as response:
            assert json.loads(response.read())["status"] == "ok"
    finally:
        external.shutdown()
        external.server_close()
        thread.join(timeout=5)


def test_supervisor_rejects_relative_and_symlink_home(tmp_path: Path) -> None:
    relative = subprocess.run(
        [PYTHON, "-m", "knowledge_platform.local.supervisor", "status", "--home", "relative-home"],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT / "backend")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert relative.returncode != 0
    assert _json(relative)["code"] == "supervisor_error"
    real = tmp_path / "real-home"
    real.mkdir()
    linked = tmp_path / "linked-home"
    linked.symlink_to(real, target_is_directory=True)
    result = _run(linked, "status")
    assert result.returncode != 0
    assert _json(result)["code"] == "supervisor_error"


def test_supervisor_start_failure_removes_control_and_keeps_failed_state(tmp_path: Path) -> None:
    catalog = tmp_path / "missing.sqlite3"
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    home = tmp_path / "home"
    result = _start(home, catalog, wiki, _port())
    assert result.returncode != 0
    failure = _json(result)
    assert failure["state"] == "error"
    assert (home / "run" / "state.json").is_file()
    assert json.loads((home / "run" / "state.json").read_text())["state"] == "failed"
    assert not (home / "run" / "supervisor.json").exists()


def test_supervisor_concurrent_start_allows_one_owner(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("# supervisor\n", encoding="utf-8")
    home = tmp_path / "home"
    port = _port()
    environment = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT / "backend")}
    command = [
        PYTHON,
        "-m",
        "knowledge_platform.local.supervisor",
        "start",
        "--home",
        str(home),
        "--catalog",
        str(catalog),
        "--wiki-root",
        str(wiki),
        "--port",
        str(port),
    ]
    with subprocess.Popen(command, cwd=tmp_path, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as first:
        with subprocess.Popen(command, cwd=tmp_path, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as second:
            first_output = first.communicate(timeout=45)
            second_output = second.communicate(timeout=45)
            first_code = first.returncode
            second_code = second.returncode
    results = [subprocess.CompletedProcess(command, first_code, first_output[0], first_output[1]), subprocess.CompletedProcess(command, second_code, second_output[0], second_output[1])]
    assert sum(result.returncode == 0 for result in results) == 1
    _run(home, "stop")


def test_supervisor_ignores_untrusted_or_disconnected_control_connections(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("# supervisor\n", encoding="utf-8")
    home = tmp_path / "home"
    started = _start(home, catalog, wiki, _port())
    assert started.returncode == 0, started.stderr
    try:
        control = json.loads((home / "run" / "supervisor.json").read_text())
        with socket.create_connection((control["host"], control["port"]), timeout=2):
            pass
        with socket.create_connection((control["host"], control["port"]), timeout=2) as connection:
            connection.sendall(b'{"command":"status","nonce":"wrong","mac":"wrong"}\n')
            connection.recv(4096)
        time.sleep(0.3)
        status = _json(_run(home, "status"))
        assert status["state"] == "running"
        assert status["active"] is True
        assert status["ownership_verified"] is True
    finally:
        _run(home, "stop")


def test_supervisor_manager_sigkill_lifeline_stops_runtime(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("# supervisor\n", encoding="utf-8")
    home = tmp_path / "home"
    port = _port()
    started = _start(home, catalog, wiki, port)
    assert started.returncode == 0, started.stderr
    running = _json(started)
    os.kill(int(running["manager_pid"]), signal.SIGKILL)
    for _ in range(60):
        stale = _json(_run(home, "status"))
        if stale.get("state") in {"unknown", "stopped"}:
            break
        time.sleep(0.1)
    assert stale["active"] is False
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                pass
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("runtime listener survived manager SIGKILL")
    restarted = _start(home, catalog, wiki, port)
    assert restarted.returncode == 0, restarted.stderr
    _run(home, "stop")


@pytest.mark.parametrize("special_file", ("state", "control", "token"))
def test_supervisor_control_files_fifo_fail_without_blocking(tmp_path: Path, special_file: str) -> None:
    run_root = tmp_path / "home" / "run"
    run_root.mkdir(parents=True)
    if special_file == "token":
        (run_root / "supervisor.json").write_text(
            json.dumps({"format": "puddingknowledge-local-supervisor/v1", "host": "127.0.0.1", "port": 9}),
            encoding="utf-8",
        )
        (run_root / "supervisor.json").chmod(0o600)
    os.mkfifo(run_root / {"state": "state.json", "control": "supervisor.json", "token": "token"}[special_file])
    result = _run(tmp_path / "home", "status")
    assert result.returncode in {0, 2}
    assert len(result.stdout.strip().splitlines()) == 1
    observed = _json(result)
    assert observed.get("active") is not True
    assert observed.get("state") != "running"


def test_startup_timeout_without_control_reaps_only_its_manager(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from knowledge_platform.local import supervisor

    original_popen = subprocess.Popen
    owned = []
    def stuck_manager(*args, **kwargs):
        process = original_popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        owned.append(process)
        return process

    monkeypatch.setattr(supervisor.subprocess, "Popen", stuck_manager)
    ticks = iter([0, 16])
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(ticks))
    args = SimpleNamespace(home=tmp_path / "home", catalog=tmp_path / "catalog", wiki_root=tmp_path,
                           port=18999, database_config=None, structured_config=None)
    try:
        with pytest.raises(supervisor.SupervisorError, match="startup timed out"):
            supervisor._start(args)
        assert len(owned) == 1
        assert owned[0].poll() is not None
    finally:
        for process in owned:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
