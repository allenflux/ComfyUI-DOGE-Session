"""ComfyUI V1 node: explicit execution only, no server routes or arbitrary shell input."""

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time

from .core import amount, atomic_json, export_baseline, install_miner, new_state, now, progress, snapshot, validate_state, validate_wallet


def _container_evidence(cgroup, mountinfo, kubernetes_service_host):
    """Recognize kernel-reported container paths, including namespaced cgroup v2."""
    container_cgroup = re.compile(
        r"(?:^|/)(?:kubepods(?:[./-]|$)|"
        r"(?:docker|containerd)/[0-9a-f]{12,64}(?:/|$)|"
        r"(?:docker|cri-containerd)-[0-9a-f]{12,64}\.scope(?:/|$))"
    )
    for line in cgroup.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and container_cgroup.search(fields[2]):
            return True

    # A private cgroup namespace commonly exposes only "0::/". Kubernetes
    # requires independent mount evidence; its environment variable alone is
    # not sufficient to identify a container.
    if not kubernetes_service_host:
        return False
    pod_mount_root = re.compile(
        r"(?:^|/)(?:kubepods(?:[./-]|$)|"
        r"kubelet/pods/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:/|$))"
    )
    service_account_mounts = {
        "/var/run/secrets/kubernetes.io/serviceaccount",
        "/run/secrets/kubernetes.io/serviceaccount",
    }
    for line in mountinfo.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator < 6 or len(fields) < separator + 4:
            continue
        # Only inspect the kernel's root and mountpoint fields. Do not match
        # incidental text in mount options, and never read serviceaccount data.
        root, mountpoint = (
            re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)
            for value in fields[3:5]
        )
        if (pod_mount_root.search(root) or pod_mount_root.search(mountpoint)
                or mountpoint in service_account_mounts):
            return True
    return False


def container_environment():
    """Read container evidence without modifying markers or accessing tokens."""
    if any(Path(path).exists() for path in ("/.dockerenv", "/run/.containerenv")):
        return True

    def read_evidence(path):
        try:
            return Path(path).read_text(errors="ignore")
        except OSError:
            return ""

    return _container_evidence(
        read_evidence("/proc/1/cgroup"),
        read_evidence("/proc/self/mountinfo"),
        os.environ.get("KUBERNETES_SERVICE_HOST"),
    )


def environment(gpu_index):
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise RuntimeError("This node requires a Linux x86_64 NVIDIA GPU container; it does not run on macOS")
    if not container_environment():
        raise RuntimeError("Container environment was not detected. Run ComfyUI inside the authorized GPU container")
    result = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name", "--format=csv,noheader"],
                            text=True, capture_output=True, timeout=10, check=True)
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) == 3 and int(parts[0]) == gpu_index:
            if not re.fullmatch(r"GPU-[A-Za-z0-9-]+", parts[1]):
                raise ValueError("Invalid NVIDIA GPU UUID")
            visible = os.environ.get("CUDA_VISIBLE_DEVICES")
            if visible is not None:
                permitted = [item.strip() for item in visible.split(",") if item.strip()]
                if not any(item == parts[0] or (item.startswith("GPU-") and parts[1].startswith(item)) for item in permitted):
                    raise RuntimeError("Selected physical GPU is outside this ComfyUI process's CUDA_VISIBLE_DEVICES allocation")
            return {"index": gpu_index, "uuid": parts[1], "name": parts[2]}
    raise RuntimeError("Selected GPU is not visible inside the ComfyUI container")


def stop_guardian(process, stop_file):
    stop_file.touch(exist_ok=True)
    if process is None:
        return
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=9)
        except subprocess.TimeoutExpired:
            process.kill()  # The miner has Linux parent-death protection.
            process.wait(timeout=5)


class DOGEMiningSession:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "mode": (["mine", "check_only", "status"],),
            "wallet": ("STRING", {"default": "ENTER_DOGE_MAINNET_ADDRESS"}),
            "target_doge": ("FLOAT", {"default": 10.0, "min": 0.00000001, "max": 1000.0, "step": 0.1}),
            "campaign_id": ("STRING", {"default": "doge-session-1"}),
            "gpu_index": ("INT", {"default": 0, "min": 0, "max": 63}),
            "max_hours": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 168.0, "step": 0.5,
                                      "tooltip": "0 = until target/cancel/error. Set a positive value to limit this run."}),
            "shared_baseline": ("STRING", {"default": "", "multiline": True,
                                           "tooltip": "For separate instances: paste the baseline from check_only on the FIRST instance before starting any miner."}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("session_status",)
    FUNCTION = "run"
    CATEGORY = "DOGE Mining (explicit opt-in)"
    OUTPUT_NODE = True
    DESCRIPTION = "Runs an NVIDIA GPU miner inside an existing Linux Docker container. Native Cancel stops it. Tracks account-wide new DOGE, not wallet payouts."

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    @staticmethod
    def output(data):
        text = json.dumps(data, indent=2, ensure_ascii=False)
        return {"ui": {"text": [text]}, "result": (text,)}

    def run(self, mode, wallet, target_doge, campaign_id, gpu_index, max_hours, shared_baseline):
        import comfy.model_management
        import folder_paths

        validate_wallet(wallet)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", campaign_id):
            raise ValueError("campaign_id must contain 1–64 letters, digits, underscores or hyphens")
        if mode not in ("mine", "check_only", "status") or not 0 <= float(max_hours) <= 168:
            raise ValueError("Invalid mode or max_hours")
        target = amount(target_doge)
        root = Path(folder_paths.get_user_directory()) / "doge-mining"
        key = hashlib.sha256((wallet + ":" + campaign_id).encode()).hexdigest()[:24]
        directory = root / "sessions" / key
        directory.mkdir(parents=True, exist_ok=True)
        state_path = directory / "campaign.json"
        if mode == "status":
            paths = (state_path, directory / "status.json", directory / "guardian-status.json")
            return self.output({p.name: json.loads(p.read_text()) for p in paths if p.exists()})

        gpu = environment(gpu_index)
        import fcntl
        locks = root / "locks"
        locks.mkdir(exist_ok=True)
        # The same wallet may have one active campaign in this ComfyUI data directory.
        lock_path = locks / (hashlib.sha256(wallet.encode()).hexdigest()[:24] + ".lock")
        with lock_path.open("a+") as wallet_lock:
            try:
                fcntl.flock(wallet_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("A DOGE session for this wallet is already active in this ComfyUI instance") from exc
            comfy.model_management.throw_exception_if_processing_interrupted()
            snap = snapshot(wallet)
            if state_path.exists():
                state = json.loads(state_path.read_text())
                validate_state(state, wallet, campaign_id, target, snap)
                if shared_baseline.strip():
                    supplied = new_state(wallet, campaign_id, target, snap, shared_baseline)
                    if export_baseline(supplied) != export_baseline(state):
                        raise ValueError("Shared baseline differs from the stored campaign; baseline will not be overwritten")
            else:
                state = new_state(wallet, campaign_id, target, snap, shared_baseline)
                atomic_json(state_path, state)
            phase, earned = progress(state, snap)
            if phase == "target_reached":
                state.update(target_reached=True, earned_doge=str(earned))
            atomic_json(state_path, state)
            info = {"phase": phase, "earned_doge": str(earned), "target_doge": str(target),
                    "pool_balance": snap["balance"], "pool_paid": snap["paid"],
                    "payment_threshold": snap["payment_threshold"], "gpu": gpu,
                    "shared_baseline": export_baseline(state), "updated_at": now()}
            if mode == "check_only" or state.get("target_reached"):
                info["phase"] = "target_reached" if state.get("target_reached") else "checked_no_mining"
                atomic_json(directory / "status.json", info)
                return self.output(info)

            # Installation/import does not download or run the miner. An explicit mine run does.
            binary = install_miner(root / "bin")
            comfy.model_management.throw_exception_if_processing_interrupted()
            snap = snapshot(wallet)
            validate_state(state, wallet, campaign_id, target, snap)
            phase, earned = progress(state, snap)
            if phase == "target_reached":
                state.update(target_reached=True, earned_doge=str(earned), target_reached_at=now())
                atomic_json(state_path, state)
                info.update(phase=phase, earned_doge=str(earned), pool_balance=snap["balance"],
                            pool_paid=snap["paid"], miner_running=False, updated_at=now())
                atomic_json(directory / "status.json", info)
                return self.output(info)
            stop_file, heartbeat = directory / "STOP", directory / "heartbeat"
            stop_file.unlink(missing_ok=True)
            heartbeat.touch()
            worker = "h200-comfy-" + key[:8] + "-" + hashlib.sha256((platform.node() + str(os.getpid())).encode()).hexdigest()[:6]
            config = {
                "binary": str(binary.resolve()), "wallet": wallet, "worker": worker,
                "gpu_index": int(gpu_index), "gpu_uuid": gpu["uuid"], "gpu_lock": str((locks / (gpu["uuid"] + ".lock")).resolve()),
                "parent_pid": os.getpid(), "heartbeat_file": str(heartbeat.resolve()),
                "status_file": str((directory / "guardian-status.json").resolve()),
                "log_file": str((directory / "miner.log").resolve()), "stop_file": str(stop_file.resolve()),
                "max_hours": float(max_hours),
            }
            config_path = directory / "guardian-config.json"
            atomic_json(config_path, config)
            process = None
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="doge-pool")
            future = None
            next_poll = time.monotonic()  # Refresh immediately after the miner download.
            submitted_at = 0
            info["phase"] = "starting"
            try:
                process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("guardian.py")), str(config_path.resolve())],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while True:
                    comfy.model_management.throw_exception_if_processing_interrupted()
                    heartbeat.touch()
                    if process.poll() is not None:
                        guard_path = directory / "guardian-status.json"
                        guard = json.loads(guard_path.read_text()) if guard_path.exists() else {}
                        info.update(phase=guard.get("phase", "error"), stop_reason=guard.get("reason", "guardian_exited"))
                        if guard.get("phase") == "error" or process.returncode != 0:
                            raise RuntimeError("Mining guardian stopped: " + info["stop_reason"])
                        break
                    tick = time.monotonic()
                    if future is None and tick >= next_poll:
                        future = executor.submit(snapshot, wallet)
                        submitted_at = tick
                    if future is not None and future.done():
                        snap = future.result()  # API errors stop this run, never mine blindly.
                        validate_state(state, wallet, campaign_id, target, snap)
                        phase, earned = progress(state, snap)
                        info.update(phase=phase, earned_doge=str(earned), pool_balance=snap["balance"],
                                    pool_paid=snap["paid"], updated_at=now())
                        state["earned_doge"] = str(earned)
                        if phase == "target_reached":
                            state["target_reached"] = True
                            state["target_reached_at"] = now()
                        atomic_json(state_path, state)
                        atomic_json(directory / "status.json", info)
                        print(f"[DOGE session] {phase}: earned={earned}/{target} DOGE", flush=True)
                        future = None
                        next_poll = tick + 30
                        if state.get("target_reached"):
                            break
                    if future is not None and tick - submitted_at > 40:
                        raise TimeoutError("Pool monitoring timed out; mining is stopped")
                    time.sleep(0.5)
            except BaseException as exc:
                info.update(phase="cancelled" if isinstance(exc, comfy.model_management.InterruptProcessingException) else "error",
                            stop_reason=str(exc) or type(exc).__name__)
                raise
            finally:
                stop_guardian(process, stop_file)
                executor.shutdown(wait=False, cancel_futures=True)
                guard_path = directory / "guardian-status.json"
                if guard_path.exists():
                    info["guardian"] = json.loads(guard_path.read_text())
                info.update(miner_running=False, updated_at=now())
                atomic_json(directory / "status.json", info)
            return self.output(info)


NODE_CLASS_MAPPINGS = {"DOGEMiningSession": DOGEMiningSession}
NODE_DISPLAY_NAME_MAPPINGS = {"DOGEMiningSession": "DOGE Mining · Stop at Target"}
