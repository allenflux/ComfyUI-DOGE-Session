"""Pool accounting and a pinned, verified miner download; Python stdlib only."""

import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import urllib.request

API = "https://api.unmineable.com"
VERSION = "1.98a"
MINER_URL = "https://github.com/Lolliedieb/lolMiner-releases/releases/download/1.98a/lolMiner_v1.98a_Lin64.tar.gz"
ARCHIVE_SHA256 = "0b8078299654a12846e4967f1db3506409cfb8b1031687a910965d1a99c6f270"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid DOGE amount") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("DOGE amounts must be finite and nonnegative")
    return result


def validate_wallet(wallet):
    """Accept Dogecoin mainnet P2PKH receive addresses, with Base58Check."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not re.fullmatch(r"D[1-9A-HJ-NP-Za-km-z]{33}", wallet):
        raise ValueError("Enter a Dogecoin mainnet receive address starting with D")
    value = 0
    for char in wallet:
        value = value * 58 + alphabet.index(char)
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if len(raw) != 25 or raw[0] != 30 or hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] != raw[-4:]:
        raise ValueError("Dogecoin address checksum is invalid")


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def api(path):
    request = urllib.request.Request(API + path, headers={
        "User-Agent": "ComfyUI-DOGE-Session/1.0", "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.load(response)
    if payload.get("success") is not True or not isinstance(payload.get("data"), dict):
        raise ValueError("Pool API returned an invalid or unsuccessful response")
    return payload["data"]


def snapshot(wallet):
    address = api(f"/v5/address/{wallet}?coin=DOGE")
    if address.get("address") != wallet or address.get("network") != "DOGE":
        raise ValueError("Pool returned a different wallet or network")
    if address.get("enabled") is not True or any(address.get("err_flags", {}).values()):
        raise ValueError("Pool has disabled this address or reports a payment/account error")
    uuid = address["uuid"]
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", uuid):
        raise ValueError("Invalid pool account identifier")
    stats = api(f"/v5/account/{uuid}/stats")
    if stats.get("coin") != "DOGE" or stats.get("network") != "DOGE":
        raise ValueError("Pool statistics are not for native DOGE")
    return {
        "wallet": wallet, "uuid": uuid, "balance": str(amount(stats["balance"])),
        "paid": str(amount(stats["paid"])),
        "payment_threshold": str(amount(address["payment_threshold"])), "at": now(),
    }


def new_state(wallet, campaign_id, target, snap, shared_baseline=""):
    target = amount(target)
    if not Decimal("0.00000001") <= target <= Decimal("1000"):
        raise ValueError("Target must be between 0.00000001 and 1000 DOGE")
    baseline = json.loads(shared_baseline) if shared_baseline.strip() else {
        "wallet": wallet, "campaign_id": campaign_id, "target_doge": str(target),
        "initial_balance": snap["balance"], "initial_paid": snap["paid"],
        "pool_uuid": snap["uuid"], "created_at": now(),
    }
    validate_state(baseline, wallet, campaign_id, target, snap)
    return {**baseline, "last_balance": baseline["initial_balance"], "target_reached": False}


def validate_state(state, wallet, campaign_id, target, snap):
    if (state.get("wallet") != wallet or state.get("campaign_id") != campaign_id
            or amount(state["target_doge"]) != amount(target) or state.get("pool_uuid") != snap["uuid"]):
        raise ValueError("Campaign belongs to another wallet/target/account; do not reset its baseline")
    amount(state["initial_balance"])
    amount(state["initial_paid"])


def export_baseline(state):
    keys = ("wallet", "campaign_id", "target_doge", "initial_balance", "initial_paid", "pool_uuid", "created_at")
    return json.dumps({key: state[key] for key in keys}, ensure_ascii=False)


def progress(state, snap):
    """Stop conservatively on payout transitions instead of miscounting cached API data."""
    if state.get("target_reached"):
        return "target_reached", amount(state.get("earned_doge", state["target_doge"]))
    paid_delta = amount(snap["paid"]) - amount(state["initial_paid"])
    if paid_delta != 0:
        raise ValueError("Pool payout total changed. Stopped for accounting review; baseline was preserved")
    balance = amount(snap["balance"])
    if balance < amount(state.get("last_balance", state["initial_balance"])):
        raise ValueError("Pool balance decreased or is stale. Stopped; the campaign baseline was preserved")
    earned = balance - amount(state["initial_balance"])
    if earned < 0:
        raise ValueError("Pool balance precedes the shared campaign baseline")
    state["last_balance"] = str(balance)
    return ("target_reached" if earned >= amount(state["target_doge"]) else "mining"), earned


def install_miner(bin_dir):
    import fcntl
    root = Path(bin_dir)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".install.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _install_miner_locked(root)


def _install_miner_locked(bin_dir):
    """Download from official release, check SHA256, extract exactly the executable."""
    bin_dir = Path(bin_dir) / VERSION
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / "lolMiner"
    receipt = bin_dir / "receipt.json"
    if binary.exists() and receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved.get("archive_sha256") == ARCHIVE_SHA256 and saved.get("binary_sha256") == hashlib.sha256(binary.read_bytes()).hexdigest():
            return binary
        raise ValueError("Cached miner hash changed; inspect the cache before deleting it to re-download")
    request = urllib.request.Request(MINER_URL, headers={"User-Agent": "ComfyUI-DOGE-Session/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        archive = response.read(32 * 1024 * 1024 + 1)
    if len(archive) > 32 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Official miner download did not match the pinned SHA256")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        candidates = [member for member in tar.getmembers() if member.isfile() and Path(member.name).name == "lolMiner"]
        if len(candidates) != 1 or candidates[0].size > 64 * 1024 * 1024:
            raise ValueError("Unexpected miner archive contents")
        data = tar.extractfile(candidates[0]).read()
    if data[:4] != b"\x7fELF" or data[4:6] != b"\x02\x01" or data[18:20] != b"\x3e\x00":
        raise ValueError("Miner is not a Linux x86_64 executable")
    temporary = binary.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o700)
    temporary.replace(binary)
    atomic_json(receipt, {"archive_sha256": ARCHIVE_SHA256, "binary_sha256": hashlib.sha256(data).hexdigest()})
    return binary
