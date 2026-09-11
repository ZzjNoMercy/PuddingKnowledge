"""Manage one isolated local Knowledge runtime child.

The supervisor is deliberately a small local control plane.  It owns the child
process it starts, authenticates commands with a Home-scoped random token, and
never terminates a process by looking up a PID or a listening port.
"""

from __future__ import annotations

import argparse
import json
import hmac
import hashlib
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.request import urlopen

try:
    import fcntl
except ImportError:  # pragma: no cover - supported deployment is POSIX
    fcntl = None  # type: ignore[assignment]


_FORMAT = "puddingknowledge-local-supervisor/v1"
_RUN_DIR = "run"
_CONTROL_FILE = "supervisor.json"
_STATE_FILE = "state.json"
_TOKEN_FILE = "token"
_LOCK_FILE = "supervisor.lock"
_MAX_CONTROL_BYTES = 8192


class SupervisorError(RuntimeError):
    """Raised for fail-closed supervisor lifecycle errors."""


def _signature(token: str, value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(token.encode("ascii"), encoded, hashlib.sha256).hexdigest()


def _signed_response(token: str, nonce: str, value: dict[str, Any]) -> dict[str, Any]:
    response = dict(value)
    response["nonce"] = nonce
    response["mac"] = _signature(token, response)
    return response


def _recv_frame(connection: socket.socket) -> bytes:
    data = bytearray()
    while len(data) < _MAX_CONTROL_BYTES:
        chunk = connection.recv(min(1024, _MAX_CONTROL_BYTES - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in data:
            return bytes(data).split(b"\n", 1)[0]
    if len(data) >= _MAX_CONTROL_BYTES:
        raise SupervisorError("supervisor control frame is too large")
    return bytes(data)


def _reject_symlink_ancestors(path: Path) -> None:
    current = path
    components: list[Path] = []
    while current != current.parent:
        components.append(current)
        current = current.parent
    components.append(current)
    for component in reversed(components):
        if component.is_symlink():
            raise SupervisorError(f"supervisor path contains a symlink: {component}")


def _home_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SupervisorError("supervisor Home must be an absolute path")
    path = path.absolute()
    _reject_symlink_ancestors(path)
    return path


def _ensure_home(path: Path) -> None:
    _reject_symlink_ancestors(path)
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise SupervisorError("supervisor Home must be a real directory")
    else:
        path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    _reject_symlink_ancestors(path)


def _secure_file(path: Path) -> None:
    _reject_symlink_ancestors(path)
    if not path.is_file() or path.is_symlink():
        raise SupervisorError(f"supervisor file is invalid: {path.name}")
    if path.stat().st_mode & 0o077:
        raise SupervisorError(f"supervisor file permissions are too broad: {path.name}")


def _read_regular(path: Path, limit: int, *, label: str) -> bytes:
    _reject_symlink_ancestors(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        information = os.fstat(descriptor)
        if (not stat.S_ISREG(information.st_mode) or information.st_size > limit
                or information.st_mode & 0o077):
            raise SupervisorError(f"supervisor {label} is invalid")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        if len(result) > limit:
            raise SupervisorError(f"supervisor {label} is too large")
        return result
    finally:
        os.close(descriptor)


def _write_bytes(path: Path, payload: bytes) -> None:
    _reject_symlink_ancestors(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_bytes(path, (json.dumps(value, sort_keys=True) + "\n").encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_regular(path, _MAX_CONTROL_BYTES, label="state").decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SupervisorError("supervisor state is unreadable") from error
    if not isinstance(value, dict):
        raise SupervisorError("supervisor state is invalid")
    return value


def _write_token(path: Path, token: str) -> None:
    _reject_symlink_ancestors(path.parent)
    if path.exists() or path.is_symlink():
        _secure_file(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", closefd=True) as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
    finally:
        _reject_symlink_ancestors(path)


def _read_token(path: Path) -> str:
    try:
        token = _read_regular(path, 256, label="token").decode("ascii").strip()
    except (OSError, UnicodeError) as error:
        raise SupervisorError("supervisor token is invalid") from error
    if len(token) < 32:
        raise SupervisorError("supervisor token is invalid")
    return token


def _open_lifecycle_lock(home: Path) -> int:
    if fcntl is None:
        raise SupervisorError("supervisor requires file locking")
    run_root = home / _RUN_DIR
    _reject_symlink_ancestors(run_root)
    run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_ancestors(run_root)
    lock = run_root / _LOCK_FILE
    _reject_symlink_ancestors(lock)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SupervisorError("supervisor lock is invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, SupervisorError) as error:
        os.close(descriptor)
        if isinstance(error, SupervisorError):
            raise
        raise SupervisorError("supervisor Home is already owned by a live manager") from error
    return descriptor


def _child_command(
    *,
    catalog: Path | None,
    wiki_root: Path | None,
    port: int,
    run_dir: Path,
    lifeline_fd: int,
    database_config: Path | None,
    structured_config: Path | None,
    state_dir: Path | None = None,
    document_migration: Path | None = None,
    wiki_config: Path | None = None,
    capture_config: Path | None = None,
    feishu_config: Path | None = None,
    file_config: Path | None = None,
    package_config: Path | None = None,
    index_config: Path | None = None,
) -> list[str]:
    runtime = [
        sys.executable,
        "-m",
        "knowledge_platform.local",
        "--temp-dir",
        str(run_dir / "workspace"),
        "--ready-file",
        str(run_dir / "ready.json"),
        "--port",
        str(port),
        "--instance-id",
        run_dir.name,
    ]
    if database_config is not None:
        runtime.extend(("--database-config", str(database_config)))
    if structured_config is not None:
        runtime.extend(("--structured-config", str(structured_config)))
    for name, value in (("catalog", catalog), ("wiki-root", wiki_root), ("document-migration", document_migration), ("state-dir", state_dir), ("wiki-config", wiki_config), ("capture-config", capture_config), ("feishu-config", feishu_config), ("file-config", file_config), ("package-config", package_config), ("index-config", index_config)):
        if value is not None:
            runtime.extend(("--" + name, str(value)))
    return [
        sys.executable,
        "-m",
        "knowledge_platform.local.supervisor",
        "_child",
        "--lifeline-fd",
        str(lifeline_fd),
        "--",
        *runtime,
    ]


def _child_main(argv: list[str]) -> int:
    try:
        fd_index = argv.index("--lifeline-fd")
        lifeline_fd = int(argv[fd_index + 1])
        separator = argv.index("--", fd_index + 2)
    except (ValueError, IndexError):
        return 2
    runtime = list(argv[separator + 1 :])
    if not runtime:
        return 2
    os.set_blocking(lifeline_fd, False)
    stopping = threading.Event()

    def watch_lifeline() -> None:
        try:
            while not stopping.is_set():
                readable, _, _ = select.select([lifeline_fd], [], [], 0.2)
                if readable and not os.read(lifeline_fd, 1):
                    if not stopping.is_set():
                        os.kill(os.getpid(), signal.SIGTERM)
                    return
        except (OSError, ValueError):
            if not stopping.is_set():
                os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=watch_lifeline, name="supervisor-lifeline", daemon=True).start()
    try:
        if runtime[:3] != [sys.executable, "-m", "knowledge_platform.local"]:
            return 2
        sys.argv = ["puddingknowledge-local", *runtime[3:]]
        from knowledge_platform.local.__main__ import main as local_main
        return int(local_main())
    finally:
        stopping.set()
        os.close(lifeline_fd)


def _probe(port: int, expected_instance_id: str, *, timeout: float = 0.4) -> tuple[bool, str | None]:
    try:
        with urlopen(f"http://127.0.0.1:{port}/v1/spaces", timeout=timeout) as response:
            observed_instance_id = response.headers.get("X-PuddingKnowledge-Instance")
            if observed_instance_id != expected_instance_id:
                return False, "spaces endpoint instance identity mismatch"
            payload = json.loads(response.read())
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return False, "spaces endpoint returned a non-ok response"
        return True, None
    except Exception as error:  # health is an observation boundary
        return False, f"spaces endpoint unavailable: {type(error).__name__}"


def _terminate_child(child: subprocess.Popen[bytes] | None) -> None:
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def _manager_state(
    *,
    state: str,
    manager_pid: int,
    child: subprocess.Popen[bytes] | None,
    port: int,
    run_dir: Path,
    error: str | None = None,
    health: bool = False,
    health_error: str | None = None,
    ownership_verified: bool = False,
) -> dict[str, Any]:
    return {
        "format": _FORMAT,
        "state": state,
        "manager_pid": manager_pid,
        "child_pid": child.pid if child is not None else None,
        "child_alive": child is not None and child.poll() is None,
        "child_returncode": child.poll() if child is not None else None,
        "port": port,
        "run_dir": str(run_dir),
        "health": health,
        "health_error": health_error,
        "ownership_verified": ownership_verified,
        "error": error,
        "updated_at": time.time(),
    }


def _manager_main(args: argparse.Namespace) -> int:
    home = _home_path(args.home)
    _ensure_home(home)
    run_dir = Path(args.run_dir)
    _reject_symlink_ancestors(run_dir)
    run_root = home / _RUN_DIR
    _reject_symlink_ancestors(run_root)
    state_path = run_root / _STATE_FILE
    control_path = run_root / _CONTROL_FILE
    token_path = run_root / _TOKEN_FILE
    lock_fd = int(args.lock_fd)
    listener: socket.socket | None = None
    child: subprocess.Popen[bytes] | None = None
    lifeline_write: int | None = None
    stopping = False
    result = 1

    def on_signal(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.2)
        control = {"format": _FORMAT, "host": "127.0.0.1", "port": listener.getsockname()[1], "manager_pid": os.getpid()}
        _write_json(control_path, control)
        _write_json(state_path, _manager_state(state="starting", manager_pid=os.getpid(), child=None, port=args.port, run_dir=run_dir))
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = run_dir / "child.log"
        lifeline_read, lifeline_write = os.pipe()
        with log_path.open("ab") as log:
            child = subprocess.Popen(
                _child_command(
                    catalog=Path(args.catalog) if args.catalog else None, wiki_root=Path(args.wiki_root) if args.wiki_root else None,
                    document_migration=Path(args.document_migration) if args.document_migration else None, port=args.port,
                    run_dir=run_dir, lifeline_fd=lifeline_read,
                    database_config=Path(args.database_config) if args.database_config else None,
                    structured_config=Path(args.structured_config) if args.structured_config else None,
                    state_dir=Path(args.state_dir) if args.state_dir else None,
                    wiki_config=Path(args.wiki_config) if args.wiki_config else None,
                    capture_config=Path(args.capture_config) if args.capture_config else None,
                    feishu_config=Path(args.feishu_config) if args.feishu_config else None,
                    file_config=Path(args.file_config) if args.file_config else None,
                    package_config=Path(args.package_config) if args.package_config else None,
                    index_config=Path(args.index_config) if args.index_config else None,
                ),
                cwd=home,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                pass_fds=(lifeline_read,),
                start_new_session=True,
            )
        os.close(lifeline_read)
        _write_json(state_path, _manager_state(state="running", manager_pid=os.getpid(), child=child, port=args.port, run_dir=run_dir))
        while not stopping:
            if child.poll() is not None:
                _write_json(state_path, _manager_state(state="failed", manager_pid=os.getpid(), child=child, port=args.port, run_dir=run_dir, error="child exited before stop"))
                result = 1
                break
            try:
                readable, _, _ = select.select([listener], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    data = _recv_frame(connection)
                    if not data:
                        continue
                    token: str | None = None
                    nonce = ""
                    try:
                        request = json.loads(data.decode("utf-8"))
                        if not isinstance(request, dict):
                            raise SupervisorError("invalid supervisor request")
                        token = _read_token(token_path)
                        nonce = str(request.get("nonce", ""))
                        proof = {"command": str(request.get("command", "")), "nonce": nonce}
                        if not nonce or not secrets.compare_digest(str(request.get("mac", "")), _signature(token, proof)):
                            raise SupervisorError("invalid supervisor token")
                        command = request.get("command")
                        if command == "status":
                            healthy, health_error = _probe(args.port, run_dir.name)
                            alive = child is not None and child.poll() is None
                            ready = (run_dir / "ready.json").is_file()
                            response = _manager_state(state=("running" if alive and ready and healthy else "starting" if alive and not ready else "unhealthy" if alive else "failed"), manager_pid=os.getpid(), child=child, port=args.port, run_dir=run_dir, health=healthy, health_error=health_error, ownership_verified=True)
                        elif command == "stop":
                            _terminate_child(child)
                            _write_json(state_path, _manager_state(state="stopped", manager_pid=os.getpid(), child=child, port=args.port, run_dir=run_dir))
                            response = {"format": _FORMAT, "state": "stopped", "stopped": True}
                            result = 0
                            response = _signed_response(token, nonce, response)
                            connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
                            break
                        else:
                            raise SupervisorError("unknown supervisor command")
                        response = _signed_response(token, nonce, response)
                    except Exception as error:
                        response = {"format": _FORMAT, "state": "error", "error": str(error)}
                        if token is not None and nonce:
                            response = _signed_response(token, nonce, response)
                    connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
            except (OSError, socket.timeout):
                continue
        if stopping:
            _terminate_child(child)
            _write_json(state_path, _manager_state(state="stopped", manager_pid=os.getpid(), child=child, port=args.port, run_dir=run_dir))
            result = 0
    except Exception as error:
        _terminate_child(child)
        try:
            _write_json(state_path, _manager_state(state="failed", manager_pid=os.getpid(), child=child, port=args.port, run_dir=run_dir, error=str(error)))
        except Exception:
            pass
        result = 1
    finally:
        if listener is not None:
            listener.close()
        try:
            control_path.unlink()
        except FileNotFoundError:
            pass
        if lifeline_write is not None:
            os.close(lifeline_write)
        os.close(lock_fd)
    return result


def _send(home: Path, command: str) -> dict[str, Any]:
    run_root = home / _RUN_DIR
    control = _read_json(run_root / _CONTROL_FILE)
    if control.get("format") != _FORMAT or control.get("host") != "127.0.0.1":
        raise SupervisorError("supervisor control metadata is invalid")
    port = int(control.get("port", 0))
    token = _read_token(run_root / _TOKEN_FILE)
    nonce = secrets.token_urlsafe(24)
    request = {"command": command, "nonce": nonce}
    request["mac"] = _signature(token, request)
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall((json.dumps(request) + "\n").encode("utf-8"))
        data = _recv_frame(connection)
    response = json.loads(data.decode("utf-8"))
    if not isinstance(response, dict):
        raise SupervisorError("supervisor response is invalid")
    response_nonce = str(response.pop("nonce", ""))
    response_mac = str(response.pop("mac", ""))
    signed = dict(response)
    signed["nonce"] = response_nonce
    if response_nonce != nonce or not secrets.compare_digest(response_mac, _signature(token, signed)):
        raise SupervisorError("supervisor response authentication failed")
    return response


def _status(home: Path) -> dict[str, Any]:
    run_root = home / _RUN_DIR
    control = run_root / _CONTROL_FILE
    if not control.exists():
        if (run_root / _STATE_FILE).exists():
            state = _read_json(run_root / _STATE_FILE)
            if state.get("state") == "running":
                state["state"] = "unknown"
            state["active"] = False
            return state
        return {"format": _FORMAT, "state": "stopped", "active": False}
    try:
        response = _send(home, "status")
        response["active"] = bool(response.get("state") == "running" and response.get("child_alive") and response.get("health") and response.get("ownership_verified"))
        return response
    except Exception as error:
        state = _read_json(run_root / _STATE_FILE) if (run_root / _STATE_FILE).exists() else {"format": _FORMAT, "state": "unknown"}
        if state.get("state") == "running":
            state["state"] = "unknown"
        state.update({"active": False, "control_unreachable": True, "error": str(error)})
        return state


def _start(args: argparse.Namespace) -> dict[str, Any]:
    home = _home_path(args.home)
    _ensure_home(home)
    lock_fd = _open_lifecycle_lock(home)
    manager: subprocess.Popen[bytes] | None = None
    started = False
    failure = "local runtime failed before readiness"
    try:
        run_dir = home / _RUN_DIR / secrets.token_hex(16)
        run_dir.mkdir(parents=True, mode=0o700)
        _write_token(home / _RUN_DIR / _TOKEN_FILE, secrets.token_urlsafe(48))
        _write_json(home / _RUN_DIR / _STATE_FILE, {
            "format": _FORMAT, "state": "starting", "run_dir": str(run_dir), "error": None,
        })
        command = [
            sys.executable, "-m", "knowledge_platform.local.supervisor", "_manager",
            "--home", str(home),
            "--port", str(args.port), "--run-dir", str(run_dir), "--lock-fd", str(lock_fd),
        ]
        if args.database_config:
            command.extend(("--database-config", str(Path(args.database_config))))
        for name in ("catalog", "wiki_root", "document_migration", "state_dir", "wiki_config", "capture_config", "feishu_config", "file_config", "package_config", "index_config"):
            value = getattr(args, name, None)
            if value is not None:
                command.extend(("--" + name.replace("_", "-"), str(value)))
        if args.structured_config:
            command.extend(("--structured-config", str(Path(args.structured_config))))
        with (run_dir / "manager.log").open("ab") as log:
            manager = subprocess.Popen(
                command, cwd=home, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                pass_fds=(lock_fd,), start_new_session=True,
            )
        os.close(lock_fd)
        lock_fd = -1
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if manager.poll() is not None:
                raise SupervisorError("local runtime manager exited before readiness")
            if (home / _RUN_DIR / _CONTROL_FILE).exists():
                response = _status(home)
                if response.get("state") == "failed":
                    raise SupervisorError(str(response.get("error") or "local runtime failed to start"))
                if response.get("active") and response.get("health"):
                    started = True
                    return response
            state = _read_json(home / _RUN_DIR / _STATE_FILE)
            if state.get("state") == "failed":
                raise SupervisorError(str(state.get("error") or "local runtime failed to start"))
            time.sleep(0.1)
        raise SupervisorError("local runtime startup timed out")
    except Exception as error:
        failure = str(error)
        raise
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        # A missing/broken control file must not bypass startup cleanup. Only
        # this invocation's actual Popen handle is eligible for termination.
        if not started:
            _terminate_child(manager)
            # Terminating our failed startup is cleanup, not a successful stop.
            try:
                _write_json(home / _RUN_DIR / _STATE_FILE, {
                    "format": _FORMAT, "state": "failed", "active": False, "error": failure,
                })
            except (OSError, SupervisorError):
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "status", "stop"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--home", type=Path, required=True)
        if name == "start":
            sub.add_argument("--catalog", type=Path)
            sub.add_argument("--wiki-root", type=Path)
            sub.add_argument("--document-migration", type=Path)
            sub.add_argument("--port", type=int, required=True)
            sub.add_argument("--database-config", type=Path)
            sub.add_argument("--structured-config", type=Path)
            sub.add_argument("--state-dir", type=Path)
            sub.add_argument("--wiki-config", type=Path)
            sub.add_argument("--capture-config", type=Path)
            sub.add_argument("--feishu-config", type=Path)
            sub.add_argument("--file-config", type=Path)
            sub.add_argument("--package-config", type=Path)
            sub.add_argument("--index-config", type=Path)
    manager = subparsers.add_parser("_manager")
    for name in ("home", "run-dir"):
        manager.add_argument("--" + name, required=True)
    for name in ("catalog", "wiki-root", "document-migration"):
        manager.add_argument("--" + name)
    manager.add_argument("--port", type=int, required=True)
    manager.add_argument("--lock-fd", required=True)
    manager.add_argument("--database-config")
    manager.add_argument("--structured-config")
    manager.add_argument("--state-dir")
    manager.add_argument("--wiki-config")
    manager.add_argument("--capture-config")
    manager.add_argument("--feishu-config")
    manager.add_argument("--file-config")
    manager.add_argument("--package-config")
    manager.add_argument("--index-config")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "_child":
        return _child_main(raw_argv[1:])
    parser = _parser()
    args = parser.parse_args(raw_argv)
    try:
        if args.command == "_manager":
            return _manager_main(args)
        home = _home_path(args.home)
        if args.command == "start":
            if args.document_migration and (not args.state_dir or args.catalog or args.wiki_root):
                raise SupervisorError("document-migration requires state-dir without Catalog/Wiki inputs")
            if not args.state_dir and (not args.catalog or not args.wiki_root):
                raise SupervisorError("catalog and wiki-root are required without state-dir")
            if not 1 <= args.port <= 65535:
                raise SupervisorError("port must be in 1..65535")
            result = _start(args)
        elif args.command == "status":
            result = _status(home)
        else:
            result = _send(home, "stop") if (home / _RUN_DIR / _CONTROL_FILE).exists() else _status(home)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (SupervisorError, OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"format": _FORMAT, "state": "error", "code": "supervisor_error", "error": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
