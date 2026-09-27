"""Node integration checks with no network, GPU, downloads, or child processes."""

import concurrent.futures
import contextlib
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import signal
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent
PACKAGE = "doge_node_integration_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(PACKAGE + ".nodes", ROOT / "nodes.py")
nodes = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = nodes
spec.loader.exec_module(nodes)

WALLET = "D596YFweJQuHY1BbjazZYmAbt8jJPbKehC"
GPU = {"index": 0, "uuid": "GPU-11111111-2222-3333-4444-555555555555", "name": "Mock GPU"}


class Interrupted(BaseException):
    pass


class ImmediateExecutor:
    def __init__(self, **kwargs):
        self.closed = False

    def submit(self, function, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(function(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, **kwargs):
        self.closed = True


class FakeGuardian:
    def __init__(self):
        self.returncode = None
        self.signals = []
        self.killed = False

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout):
        self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL


class NodeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="doge-node-tests-"))
        self.root = Path(self.temp)
        self.interrupted = False
        self.cancel_after_launch = False
        self.guardian = FakeGuardian()
        self.snap = {
            "wallet": WALLET, "uuid": "synthetic-account", "balance": "2.5", "paid": "0",
            "payment_threshold": "30", "at": "synthetic-time",
        }
        comfy = types.ModuleType("comfy")
        management = types.ModuleType("comfy.model_management")
        management.InterruptProcessingException = Interrupted
        management.throw_exception_if_processing_interrupted = self.check_interrupt
        comfy.model_management = management
        folders = types.ModuleType("folder_paths")
        folders.get_user_directory = lambda: str(self.root)
        self.stack.enter_context(mock.patch.dict(sys.modules, {
            "comfy": comfy, "comfy.model_management": management, "folder_paths": folders,
        }))
        self.stack.enter_context(mock.patch.object(nodes, "environment", return_value=GPU))
        self.pool = self.stack.enter_context(mock.patch.object(nodes, "snapshot", return_value=dict(self.snap)))
        self.install = self.stack.enter_context(mock.patch.object(nodes, "install_miner", return_value=self.root / "lolMiner"))
        self.launch = self.stack.enter_context(mock.patch.object(nodes.subprocess, "Popen", return_value=self.guardian))
        self.stack.enter_context(mock.patch.object(nodes.concurrent.futures, "ThreadPoolExecutor", ImmediateExecutor))
        self.stack.enter_context(mock.patch.object(nodes.time, "sleep", return_value=None))
        self.args = {
            "mode": "mine", "wallet": WALLET, "target_doge": 10.0,
            "campaign_id": "test-campaign", "gpu_index": 0, "max_hours": 1.0,
            "shared_baseline": "",
        }
        self.node = nodes.DOGEMiningSession()

    def check_interrupt(self):
        if self.interrupted or (self.cancel_after_launch and self.launch.called):
            raise Interrupted("synthetic ComfyUI cancellation")

    @property
    def directory(self):
        key = hashlib.sha256((WALLET + ":test-campaign").encode()).hexdigest()[:24]
        return self.root / "doge-mining" / "sessions" / key

    def read(self, filename):
        return json.loads((self.directory / filename).read_text())

    def run_node(self, **overrides):
        return self.node.run(**{**self.args, **overrides})

    def assert_guardian_stopped(self):
        self.assertEqual(self.guardian.signals, [signal.SIGTERM])
        self.assertEqual(self.guardian.returncode, 0)
        self.assertTrue((self.directory / "STOP").exists())

    def test_cancel_baseexception_propagates_and_stops_guardian(self):
        self.cancel_after_launch = True
        with self.assertRaises(Interrupted):
            self.run_node()
        self.launch.assert_called_once()
        self.assert_guardian_stopped()
        self.assertEqual(self.read("status.json")["phase"], "cancelled")
        self.assertFalse(self.read("status.json")["miner_running"])
        self.assertEqual(self.read("campaign.json")["initial_balance"], "2.5")

    def test_target_stops_and_persisted_latch_prevents_restart_after_payout(self):
        self.pool.side_effect = [dict(self.snap), dict(self.snap), {**self.snap, "balance": "12.5"}]
        result = self.run_node()
        self.assertEqual(json.loads(result["result"][0])["phase"], "target_reached")
        self.assert_guardian_stopped()
        self.assertTrue(self.read("campaign.json")["target_reached"])
        self.pool.side_effect = None
        self.pool.return_value = {**self.snap, "balance": "0", "paid": "30"}
        result = self.run_node()
        self.assertEqual(json.loads(result["result"][0])["earned_doge"], "10.0")
        self.launch.assert_called_once()
        self.install.assert_called_once()

    def test_same_wallet_active_campaign_blocks_before_pool_or_launch(self):
        locks = self.root / "doge-mining" / "locks"
        locks.mkdir(parents=True)
        filename = hashlib.sha256(WALLET.encode()).hexdigest()[:24] + ".lock"
        with (locks / filename).open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "already active"):
                self.run_node()
        self.pool.assert_not_called()
        self.launch.assert_not_called()

    def test_changed_target_preserves_campaign_and_never_launches(self):
        self.run_node(mode="check_only")
        baseline = (self.directory / "campaign.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "another wallet/target/account"):
            self.run_node(target_doge=20.0)
        self.assertEqual((self.directory / "campaign.json").read_bytes(), baseline)
        self.launch.assert_not_called()

    def test_shared_baseline_mismatch_cannot_reset_stored_campaign(self):
        checked = json.loads(self.run_node(mode="check_only")["result"][0])
        baseline = json.loads(checked["shared_baseline"])
        baseline["initial_balance"] = "1"
        with self.assertRaisesRegex(ValueError, "baseline differs"):
            self.run_node(shared_baseline=json.dumps(baseline))
        self.assertEqual(self.read("campaign.json")["initial_balance"], "2.5")
        self.launch.assert_not_called()

    def test_monitor_pool_error_stops_without_resetting_baseline(self):
        self.pool.side_effect = [dict(self.snap), dict(self.snap), TimeoutError("mock pool failure")]
        with self.assertRaisesRegex(TimeoutError, "mock pool failure"):
            self.run_node()
        self.assert_guardian_stopped()
        self.assertEqual(self.read("status.json")["phase"], "error")
        self.assertEqual(self.read("campaign.json")["initial_balance"], "2.5")
        self.assertFalse(self.read("campaign.json")["target_reached"])

    def test_target_reached_during_download_is_persisted_without_launch(self):
        self.pool.side_effect = [dict(self.snap), {**self.snap, "balance": "12.5"}]
        result = self.run_node()
        self.assertEqual(json.loads(result["result"][0])["phase"], "target_reached")
        self.assertTrue(self.read("campaign.json")["target_reached"])
        self.assertEqual(self.read("status.json")["phase"], "target_reached")
        self.assertEqual(self.read("status.json")["pool_balance"], "12.5")
        self.assertFalse(self.read("status.json")["miner_running"])
        self.launch.assert_not_called()

    def test_workflow_widgets_match_node_contract(self):
        workflow = json.loads((ROOT / "examples" / "doge-10.json").read_text())
        node = workflow["nodes"][0]
        self.assertEqual(node["type"], "DOGEMiningSession")
        names = list(nodes.DOGEMiningSession.INPUT_TYPES()["required"])
        self.assertEqual(names, ["mode", "wallet", "target_doge", "campaign_id", "gpu_index", "max_hours", "shared_baseline"])
        values = dict(zip(names, node["widgets_values"]))
        self.assertEqual(len(names), len(node["widgets_values"]))
        self.assertEqual(values, {
            "mode": "mine", "wallet": "ENTER_DOGE_MAINNET_ADDRESS", "target_doge": 10.0,
            "campaign_id": "doge-session-1", "gpu_index": 0, "max_hours": 0.0,
            "shared_baseline": "",
        })


if __name__ == "__main__":
    unittest.main()
