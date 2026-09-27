"""Linux-only guardian integration tests using a harmless sleeping script.

No actual miner, network connection, GPU operation, or user's wallet is involved.
"""

import json
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


GUARDIAN = Path(__file__).with_name("guardian.py").resolve()
# Valid Dogecoin Base58Check encoding of version 30 plus a zeroed 20-byte hash.
# This is synthetic test data, not a user's wallet and never reaches a pool.
TEST_WALLET = "D596YFweJQuHY1BbjazZYmAbt8jJPbKehC"
TEST_GPU_UUID = "GPU-11111111-2222-3333-4444-555555555555"


_SPEC = importlib.util.spec_from_file_location("guardian_under_test", GUARDIAN)
_GUARDIAN_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GUARDIAN_MODULE)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return fields[0], fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def _process_alive(pid):
    identity = _process_identity(pid)
    # SIGKILL may leave a zombie until the container's init reaps it. A zombie
    # is already terminated and cannot keep mining or consume GPU resources.
    return identity is not None and identity[0] not in ("Z", "X")


@unittest.skipUnless(sys.platform == "linux", "guardian integration tests require Linux /proc and prctl")
class GuardianIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="doge-guardian-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = []
        self.miner_identities = {}
        self.addCleanup(self._cleanup_processes)

    def _new_job(self, name, gpu_lock=None, max_hours=0):
        directory = self.root / name
        directory.mkdir()
        binary = directory / "lolMiner"
        binary.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys, time\n"
            "marker = pathlib.Path(__file__).with_name('fake-miner-started.json')\n"
            "marker.write_text(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:], 'cuda_visible_devices': os.environ.get('CUDA_VISIBLE_DEVICES')}))\n"
            "print('Harmless test process ready', flush=True)\n"
            "while True:\n"
            "    time.sleep(0.1)\n",
            encoding="utf-8",
        )
        binary.chmod(0o700)
        heartbeat = directory / "heartbeat"
        heartbeat.touch()
        config = {
            "binary": str(binary),
            "wallet": TEST_WALLET,
            "worker": "guardian_test",
            "gpu_index": 3,
            "gpu_uuid": TEST_GPU_UUID,
            "gpu_lock": str(gpu_lock or self.root / "gpu-0.lock"),
            "parent_pid": os.getpid(),
            "heartbeat_file": str(heartbeat),
            "status_file": str(directory / "status.json"),
            "log_file": str(directory / "guardian.log"),
            "stop_file": str(directory / "STOP"),
            "max_hours": max_hours,
        }
        config_path = directory / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        output = open(directory / "launcher.log", "wb")
        process = subprocess.Popen(
            [sys.executable, str(GUARDIAN), str(config_path)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        job = {
            "process": process, "config": config, "directory": directory,
            "marker": directory / "fake-miner-started.json", "output": output,
        }
        self.jobs.append(job)
        return job

    def _diagnostics(self, job):
        paths = (job["directory"] / "launcher.log", Path(job["config"]["log_file"]))
        messages = []
        for path in paths:
            try:
                messages.append(path.read_text(encoding="utf-8", errors="replace")[-6000:])
            except FileNotFoundError:
                pass
        return "\n".join(messages)

    def _wait_for(self, predicate, timeout=8, detail="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.02)
        self.fail(f"Timed out waiting for {detail}")

    def _wait_running(self, job):
        def running():
            status = _read_json(job["config"]["status_file"])
            marker = _read_json(job["marker"])
            if job["process"].poll() is not None:
                self.fail(f"Guardian exited before the fake miner was running:\n{self._diagnostics(job)}")
            if status and status.get("phase") == "running" and marker:
                self.assertEqual(status["miner_pid"], marker["pid"])
                identity = _process_identity(marker["pid"])
                self.assertIsNotNone(identity)
                self.miner_identities[marker["pid"]] = identity[1]
                return marker
            return None
        return self._wait_for(running, detail="the harmless fake miner to execute")

    def _assert_stopped(self, job, reason, expected_code=0):
        return_code = job["process"].wait(timeout=9)
        self.assertEqual(return_code, expected_code, self._diagnostics(job))
        status = _read_json(job["config"]["status_file"])
        self.assertIsNotNone(status, self._diagnostics(job))
        self.assertEqual(status["phase"], "stopped" if expected_code == 0 else "error")
        self.assertEqual(status["reason"], reason, self._diagnostics(job))
        if status.get("miner_pid") is not None:
            self._wait_for(lambda: not _process_alive(status["miner_pid"]), detail="fake miner termination")
        return status

    def _cleanup_processes(self):
        # First request normal termination, then force termination on failed
        # tests. Only children/unique temporary fake executables are targeted.
        for job in self.jobs:
            Path(job["config"]["stop_file"]).touch()
            if job["process"].poll() is None:
                job["process"].terminate()
        for job in self.jobs:
            process = job["process"]
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            marker = _read_json(job["marker"])
            status = _read_json(job["config"]["status_file"])
            candidates = set()
            if marker:
                candidates.add(marker.get("pid"))
            if status:
                candidates.add(status.get("miner_pid"))
            for pid in candidates - {None}:
                identity = _process_identity(pid)
                if identity is None or identity[0] in ("Z", "X"):
                    continue
                expected = self.miner_identities.get(pid)
                if expected is not None and identity[1] != expected:
                    continue
                if expected is None:
                    try:
                        command = Path(f"/proc/{pid}/cmdline").read_bytes()
                    except FileNotFoundError:
                        continue
                    if os.fsencode(job["config"]["binary"]) not in command:
                        continue
                try:
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            job["output"].close()

    def test_stop_file_terminates_miner(self):
        job = self._new_job("stop")
        marker = self._wait_running(job)
        self.assertEqual(marker["argv"], [
            "--algo", "OCTOPUS", "--pool", "stratum+ssl://octopus.unmineable.com:4444",
            "--user", f"DOGE:{TEST_WALLET}.guardian_test", "--devices", "0",
            "--watchdog", "exit", "--nocolor", "on", "--compactaccept", "off", "--apiport", "0",
        ])
        self.assertEqual(marker["cuda_visible_devices"], TEST_GPU_UUID)
        Path(job["config"]["stop_file"]).touch()
        self._assert_stopped(job, "stop_requested")

    def test_stale_heartbeat_terminates_miner(self):
        job = self._new_job("heartbeat")
        self._wait_running(job)
        expired = time.time() - 60
        os.utime(job["config"]["heartbeat_file"], (expired, expired))
        self._assert_stopped(job, "heartbeat_expired")

    def test_runtime_limit_terminates_miner(self):
        job = self._new_job("runtime", max_hours=1.5 / 3600)
        self._wait_running(job)
        self._assert_stopped(job, "time_limit_reached")

    def test_gpu_lock_conflict_does_not_start_second_miner(self):
        first = self._new_job("lock-first")
        self._wait_running(first)
        second = self._new_job("lock-second")
        status = self._assert_stopped(second, "gpu_lock_busy", expected_code=2)
        self.assertIsNone(status["miner_pid"])
        self.assertFalse(second["marker"].exists(), "Contending miner must never execute")
        self.assertIsNone(first["process"].poll(), "Lock conflict must not interrupt the owner")
        Path(first["config"]["stop_file"]).touch()
        self._assert_stopped(first, "stop_requested")

    def test_guardian_sigkill_triggers_miner_parent_death_signal(self):
        job = self._new_job("parent-death")
        marker = self._wait_running(job)
        self.assertTrue(_process_alive(marker["pid"]))
        job["process"].kill()  # SIGKILL prevents normal finally/cleanup execution.
        self.assertEqual(job["process"].wait(timeout=3), -signal.SIGKILL)
        self._wait_for(
            lambda: not _process_alive(marker["pid"]), timeout=3,
            detail="PR_SET_PDEATHSIG to kill the fake miner after guardian SIGKILL",
        )


class ShareTrackerTests(unittest.TestCase):
    def test_summary_counts_and_rejected_shares_do_not_count(self):
        tracker = _GUARDIAN_MODULE.ShareTracker(10)
        tracker.feed("Accepted 0\nAccepted shares: 12\nGPU 0: Share rejected (16 ms)\n", now=20)
        self.assertEqual(tracker.snapshot(30), {"accepted_shares": 0, "last_share_age": 20})
        self.assertFalse(tracker.expired(1809.9))
        self.assertTrue(tracker.expired(1810))

    def test_accepted_share_across_chunks_resets_timeout(self):
        tracker = _GUARDIAN_MODULE.ShareTracker(10)
        tracker.feed("GPU 0: Share acc", now=20)
        self.assertEqual(tracker.snapshot(20)["accepted_shares"], 0)
        tracker.feed("epted (16 ms)\r\n", now=30)
        self.assertEqual(tracker.snapshot(40), {"accepted_shares": 1, "last_share_age": 10})
        self.assertFalse(tracker.expired(1829.9))
        self.assertTrue(tracker.expired(1830))

    def test_unterminated_output_has_bounded_buffer(self):
        tracker = _GUARDIAN_MODULE.ShareTracker(0)
        for _ in range(20):
            tracker.feed("x" * 4096, now=1)
        self.assertLessEqual(len(tracker.pending), 8192)
        self.assertEqual(tracker.snapshot(2)["accepted_shares"], 0)


if __name__ == "__main__":
    unittest.main()
