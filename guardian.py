"""Foreground guardian for the explicitly queued DOGE mining session.

Usage: python guardian.py /absolute/path/to/config.json
No downloads, background restarts, or shell commands are performed here.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time


HEARTBEAT_TIMEOUT = 15.0
CHECK_INTERVAL = 0.5
TERMINATE_TIMEOUT = 5.0
NO_ACCEPTED_SHARES_TIMEOUT = 1800.0
STATUS_INTERVAL = 10.0
POOL = "stratum+ssl://octopus.unmineable.com:4444"
_STOP_SIGNAL = None
_ACCEPTED_SHARE = re.compile(
    r"(?:^|\s)GPU\s+\d+:\s*Share\s+accepted\s*\(\s*\d+(?:\.\d+)?\s*ms\s*\)",
    re.IGNORECASE,
)


def _load_config(filename):
    with open(filename, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    for key in ("binary", "gpu_lock", "heartbeat_file", "status_file", "log_file", "stop_file"):
        value = config.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"{key} must be an absolute path")
    if not re.fullmatch(r"D[1-9A-HJ-NP-Za-km-z]{33}", str(config.get("wallet", ""))):
        raise ValueError("wallet must be a Dogecoin mainnet address")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(config.get("worker", ""))):
        raise ValueError("worker must contain 1-64 letters, digits, underscores or hyphens")
    for key in ("gpu_index", "parent_pid"):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
    if config["gpu_index"] < 0 or config["parent_pid"] < 1:
        raise ValueError("Invalid GPU index or parent PID")
    if not re.fullmatch(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", str(config.get("gpu_uuid", ""))):
        raise ValueError("gpu_uuid must be a complete NVIDIA GPU UUID")
    hours = config.get("max_hours", 0)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        raise ValueError("max_hours must be a nonnegative number")
    if not math.isfinite(hours) or hours < 0:
        raise ValueError("max_hours must be a finite nonnegative number")
    config["max_hours"] = float(hours)
    binary = Path(config["binary"])
    if binary.name != "lolMiner":
        raise ValueError("The binary must be named lolMiner")
    mode = binary.stat().st_mode
    if not stat.S_ISREG(mode) or mode & (stat.S_ISUID | stat.S_ISGID):
        raise ValueError("lolMiner must be a regular executable without setuid/setgid")
    if not os.access(binary, os.X_OK):
        raise ValueError("lolMiner is not executable")
    return config


def _miner_argv(config):
    return [
        config["binary"],
        "--algo", "OCTOPUS",
        "--pool", POOL,
        "--user", f"DOGE:{config['wallet']}.{config['worker']}",
        "--devices", "0",
        "--watchdog", "exit",
        "--nocolor", "on",
        "--compactaccept", "off",
        "--apiport", "0",
    ]


def _exec_miner(config_path, expected_guardian_pid):
    """Set the parent-death signal in a fresh interpreter before execv."""
    if sys.platform != "linux":
        raise RuntimeError("The mining guardian requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    # PR_SET_PDEATHSIG = 1. Never run the miner if the guardian already died.
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != expected_guardian_pid:
        raise RuntimeError("Guardian exited before the miner could start")
    config = _load_config(config_path)
    if os.getppid() != expected_guardian_pid:
        raise RuntimeError("Guardian exited while loading the miner configuration")
    os.execv(config["binary"], _miner_argv(config))


def _write_status(path, phase, reason, miner_pid=None, **extra):
    payload = {
        "phase": phase,
        "reason": reason,
        "guardian_pid": os.getpid(),
        "miner_pid": miner_pid,
        "updated_at": time.time(),
        **extra,
    }
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _logger_for(path):
    logger = logging.getLogger(f"doge-guardian-{os.getpid()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _signal_handler(signum, _frame):
    global _STOP_SIGNAL
    _STOP_SIGNAL = signum


def _parent_start_time(pid):
    # The part after the final ')' begins at /proc stat field 3. Field 22 is
    # starttime, which also distinguishes a reused PID from our real parent.
    contents = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return contents.rsplit(")", 1)[1].split()[19]


def _stop_reason(config, parent_start_time, started_monotonic):
    if _STOP_SIGNAL is not None:
        return f"signal_{signal.Signals(_STOP_SIGNAL).name.lower()}"
    if Path(config["stop_file"]).exists():
        return "stop_requested"
    if os.getppid() != config["parent_pid"]:
        return "parent_changed"
    try:
        if _parent_start_time(config["parent_pid"]) != parent_start_time:
            return "parent_changed"
        os.kill(config["parent_pid"], 0)
    except (ProcessLookupError, FileNotFoundError):
        return "parent_exited"
    try:
        heartbeat_age = time.time() - Path(config["heartbeat_file"]).stat().st_mtime
    except FileNotFoundError:
        return "heartbeat_missing"
    # Future timestamps are not proof that the owner is still alive.
    if heartbeat_age > HEARTBEAT_TIMEOUT or heartbeat_age < -HEARTBEAT_TIMEOUT:
        return "heartbeat_expired"
    if config["max_hours"] > 0 and time.monotonic() - started_monotonic >= config["max_hours"] * 3600:
        return "time_limit_reached"
    return None


class ShareTracker:
    """Bounded log parser for individual accepted-share events only."""

    def __init__(self, started_monotonic):
        self.started_monotonic = started_monotonic
        self.last_share_monotonic = None
        self.accepted_shares = 0
        self.pending = ""
        self.lock = threading.Lock()

    def _line(self, line, now):
        # Stats tables such as "Accepted 0" or "Accepted shares: 12" are
        # deliberately excluded: only an explicit per-share response counts.
        if _ACCEPTED_SHARE.search(line):
            with self.lock:
                self.accepted_shares += 1
                self.last_share_monotonic = now

    def feed(self, text, now=None):
        now = time.monotonic() if now is None else now
        lines = re.split(r"[\r\n]", self.pending + text)
        self.pending = lines.pop()[-8192:]
        for line in lines:
            self._line(line, now)

    def finish(self, now=None):
        self._line(self.pending, time.monotonic() if now is None else now)
        self.pending = ""

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            reference = self.started_monotonic if self.last_share_monotonic is None else self.last_share_monotonic
            return {"accepted_shares": self.accepted_shares, "last_share_age": max(0.0, now - reference)}

    def expired(self, now=None):
        return self.snapshot(now)["last_share_age"] >= NO_ACCEPTED_SHARES_TIMEOUT


def _copy_miner_output(pipe, logger, tracker):
    try:
        while True:
            # Read fixed-size chunks so an endless line cannot grow RAM usage.
            chunk = os.read(pipe.fileno(), 4096)
            if not chunk:
                tracker.finish()
                return
            text = chunk.decode("utf-8", errors="replace")
            tracker.feed(text)
            logger.info("miner: %s", text.rstrip())
    except (OSError, ValueError):
        return


def _group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _terminate_miner(process, logger):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + TERMINATE_TIMEOUT
    while time.monotonic() < deadline:
        process.poll()  # Reap the leader so it does not keep the group alive.
        if not _group_exists(process.pid):
            break
        time.sleep(0.1)
    if _group_exists(process.pid):
        logger.warning("Miner process group did not stop after 5 seconds; sending SIGKILL")
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def _run(config_path):
    global _STOP_SIGNAL
    _STOP_SIGNAL = None
    if sys.platform != "linux":
        raise RuntimeError("The mining guardian requires Linux")
    config = _load_config(config_path)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _signal_handler)
    logger = _logger_for(config["log_file"])
    lock_handle = None
    process = None
    reader = None
    phase, reason, exit_code = "error", "guardian_error", 1
    started_at = time.time()
    started_monotonic = time.monotonic()
    tracker = ShareTracker(started_monotonic)
    try:
        _write_status(config["status_file"], "starting", "acquiring_gpu_lock", started_at=started_at, **tracker.snapshot())
        lock_handle = open(config["gpu_lock"], "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            phase, reason, exit_code = "error", "gpu_lock_busy", 2
            return exit_code
        parent_start_time = _parent_start_time(config["parent_pid"])
        reason = _stop_reason(config, parent_start_time, started_monotonic)
        if reason is not None:
            phase, exit_code = "stopped", 0
            return exit_code
        logger.info("Starting Octopus miner on GPU %s; guardian PID %s", config["gpu_index"], os.getpid())
        miner_environment = os.environ.copy()
        miner_environment["CUDA_VISIBLE_DEVICES"] = config["gpu_uuid"]
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--exec-miner", config_path, str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=str(Path(config["binary"]).parent),
            env=miner_environment,
        )
        reader = threading.Thread(target=_copy_miner_output, args=(process.stdout, logger, tracker), daemon=True)
        reader.start()
        _write_status(config["status_file"], "running", "mining", process.pid, started_at=started_at, **tracker.snapshot())
        last_status_write = time.monotonic()
        while True:
            reason = _stop_reason(config, parent_start_time, started_monotonic)
            if reason is not None:
                phase, exit_code = "stopped", 0
                break
            return_code = process.poll()
            if return_code is not None:
                phase, reason, exit_code = "error", f"miner_exited_{return_code}", 1
                break
            if tracker.expired():
                phase, reason, exit_code = "error", "no_accepted_shares", 1
                break
            if time.monotonic() - last_status_write >= STATUS_INTERVAL:
                _write_status(config["status_file"], "running", "mining", process.pid, started_at=started_at, **tracker.snapshot())
                last_status_write = time.monotonic()
            time.sleep(CHECK_INTERVAL)
    except BaseException as exc:
        phase, reason, exit_code = "error", f"guardian_error: {type(exc).__name__}: {exc}", 1
        logger.exception("Guardian stopped because of an error")
    finally:
        try:
            _terminate_miner(process, logger)
        except BaseException as exc:
            phase, reason, exit_code = "error", f"miner_cleanup_error: {type(exc).__name__}: {exc}", 1
            logger.exception("Miner cleanup failed")
        if reader is not None:
            reader.join(timeout=1)
        if process is not None and process.stdout is not None:
            process.stdout.close()
        logger.info("Stopped: %s", reason)
        try:
            _write_status(
                config["status_file"], phase, reason,
                process.pid if process is not None else None,
                started_at=started_at,
                ended_at=time.time(),
                miner_returncode=process.returncode if process is not None else None,
                **tracker.snapshot(),
            )
        finally:
            if lock_handle is not None:
                lock_handle.close()
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
    return exit_code


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == "--exec-miner":
        _exec_miner(str(Path(argv[1]).resolve()), int(argv[2]))
        return 1  # execv returns only by raising.
    if len(argv) != 1:
        print("Usage: python guardian.py CONFIG_JSON", file=sys.stderr)
        return 2
    return _run(str(Path(argv[0]).resolve()))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"DOGE guardian error: {exc}", file=sys.stderr)
        raise SystemExit(2)
