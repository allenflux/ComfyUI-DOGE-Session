"""Generate a ComfyUI UI workflow without publishing a user's address."""
import argparse
import json
from pathlib import Path
import uuid


def workflow(wallet="ENTER_DOGE_MAINNET_ADDRESS", mode="mine", campaign="doge-session-1", max_hours=0.0, target_doge=10.0):
    return {
        "id": str(uuid.uuid4()), "revision": 0, "last_node_id": 1, "last_link_id": 0,
        "nodes": [{
            "id": 1, "type": "DOGEMiningSession", "pos": [100, 120], "size": [780, 540],
            "flags": {}, "order": 0, "mode": 0, "inputs": [],
            "outputs": [{"name": "session_status", "type": "STRING", "links": None, "slot_index": 0}],
            "title": ("DOGE: CONTINUOUS | Cancel stops miner" if target_doge == 0
                      else f"DOGE: +{target_doge:g} then STOP | Cancel stops miner | pool payout is separate"),
            "properties": {"Node name for S&R": "DOGEMiningSession"},
            "widgets_values": [mode, wallet, target_doge, campaign, 0, max_hours, ""],
        }],
        "links": [], "groups": [], "config": {},
        "extra": {"ds": {"scale": 1, "offset": [0, 0]}}, "version": 0.4,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--wallet", default="ENTER_DOGE_MAINNET_ADDRESS")
    parser.add_argument("--mode", choices=("mine", "check_only", "status"), default="mine")
    parser.add_argument("--campaign", default="doge-session-1")
    parser.add_argument("--max-hours", type=float, default=0.0)
    parser.add_argument("--target-doge", type=float, default=10.0, help="0 means continuous; positive values set an earning target")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(workflow(args.wallet, args.mode, args.campaign, args.max_hours, args.target_doge), indent=2) + "\n")
