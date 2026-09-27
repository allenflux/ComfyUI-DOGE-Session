# ComfyUI DOGE Session

An explicit, foreground ComfyUI node that uses an NVIDIA GPU to mine Octopus/CFX through unMineable, either continuously or until a chosen amount of **new account-wide DOGE rewards**. This is not native Dogecoin mining, and stopping at 10 DOGE does not mean a wallet payout.

Only use on a GPU environment you control and whose provider permits cryptocurrency mining. There is no concealment, auto-start, service installation, or attempt to bypass platform restrictions.

## Install from the ComfyUI webpage

In **Manager → Install via Git URL**, enter:

```text
https://github.com/allenflux/ComfyUI-DOGE-Session
```

Restart ComfyUI after existing jobs finish, refresh the page, then import a workflow from `examples/`. The Python code uses only the standard library; installing/importing the node does not start mining or download a miner. If the platform refuses custom-node installation, ask its administrator to install it; do not weaken the platform's installation controls.

For an authorized terminal inside the ComfyUI container:

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/allenflux/ComfyUI-DOGE-Session.git
```

## Requirements

- Existing **Linux x86_64 Docker/container-based ComfyUI**, with NVIDIA GPU access and `nvidia-smi` available inside the container. The node refuses macOS, non-x86_64 and environments where a container cannot be detected.
- Host NVIDIA driver compatible with the installed GPU. No drivers, clocks, power limits or host settings are modified. H200 compatibility is based on lolMiner's Hopper fixes, not a measured hashrate guarantee.
- Outbound HTTPS to official GitHub releases and `api.unmineable.com`; outbound TLS mining connection to `octopus.unmineable.com:4444`.
- Persistent ComfyUI `user/` storage. Plan for under 100 MB for the cached executable/state and up to about 2 MB of rotated miner logs per campaign. GPU VRAM and electricity are still consumed; this does not guarantee profit.

Kubernetes/containerd Pods are supported, including private cgroup v2 namespaces. Container detection uses kernel mount evidence together with the Kubernetes environment; it never reads service-account tokens. A persistent `models/` volume alone does not preserve this node or its earning baseline. Keep the node checkout and `user/doge-mining/` on storage dedicated to this ComfyUI instance, and restore the custom-node/state links after Pod replacement. No nested Docker daemon is required. Check for other Pods sharing the same physical GPU before starting a miner.

## Run and stop

1. Fill `wallet` with a Dogecoin mainnet address beginning with `D`. Never enter a private key, seed phrase or exchange API key.
2. Set `target_doge=10` for a reward target, or `target_doge=0` for continuous mining. Existing account balance is excluded by a baseline captured on the campaign's first `check_only` or `mine` run.
3. Start with `mode=check_only` to validate container/GPU and the pool account. It does not download or execute a miner.
4. Set `mode=mine`, `gpu_index=0` for a single H200, and click **Run**. Keep batch count at 1. `max_hours=0` removes the time limit; a positive value limits that individual run.
5. Use ComfyUI's native **Cancel/Interrupt** button to stop an active run. A second queued workflow cannot interrupt a node already occupying the queue. Closing the browser tab does **not** stop a healthy server-side run.

The first explicit `mine` run downloads lolMiner **1.98a** from its official release, checks the archive's published SHA256 and extracts only the executable. Subsequent starts verify the cached binary receipt.

For continuous operation, import `examples/doge-continuous.json`: it sets both `target_doge=0` and `max_hours=0`. Use a new campaign ID when switching an existing target campaign to continuous operation; targets cannot be changed within a saved campaign. It continues through normal pool payouts, with no reward or time cutoff. Native Cancel, server shutdown and the failure protections below still stop it; this is not an auto-start or auto-restart service. Updating an installed older node requires a ComfyUI restart before it accepts target zero.

The node takes over the selected GPU for mining. Do not run image generation and another miner on that GPU simultaneously. Normal ComfyUI workflows on the same server execute through its queue, not as independent parallel GPU workers.

## Accounting and multiple instances

The target is an increase from the saved **pool-account baseline**. It includes rewards from every worker using the same receive address, including delayed rewards from earlier workers. It is not a per-worker accounting guarantee. Pool settlement and polling delays can cause an overshoot.

State lives under `ComfyUI/user/doge-mining/` (or the configured ComfyUI user directory). Reusing the same `campaign_id` preserves the original baseline across re-runs and restarts. A completed positive-target campaign is latched and cannot restart mining. A new campaign ID creates a new baseline, so do not change it just to resume.

For separate H200/ComfyUI instances:

1. Run `check_only` on the **first** instance before starting any miner.
2. Copy the returned `shared_baseline` JSON into the corresponding field on **all** instances, and use the same wallet, target and campaign ID.
3. Run one miner per GPU. Each watches the same account threshold and stops independently when it observes the target. Separate containers/servers do not share a filesystem GPU lock.

Within one ComfyUI data directory, wallet/GPU file locks prevent overlapping sessions. Across separate servers, use a shared baseline for a combined target; different baselines or targets do not isolate per-worker earnings. Continuous sessions on separate GPUs may share a receive address without sharing a baseline, because they have no reward cutoff.

For positive targets, if the pool balance decreases or the cumulative paid amount changes, the session conservatively stops for review rather than blindly restarting or double-counting cached balances. The baseline is preserved, including when a delayed payment belongs to a previous session.

Continuous mode (`target_doge=0`) allows normal payout transitions. Its displayed earnings are the current balance plus cumulative paid, minus their saved starting total, floored at zero. Cached or non-simultaneous pool fields can temporarily distort that estimate; it is not used as a stop condition and no high-water estimate is permanently accumulated.

The node only **reads** pool APIs. It never changes payout settings or signs/sends transactions. unMineable's native DOGE payout threshold was 30 DOGE when this node was prepared; the live threshold appears in the output. A 10 DOGE earning target can therefore stop with the reward still in the pool.

## Stop protections

- ComfyUI interruption checked twice per second; cleanup runs in `finally`, including ComfyUI's `BaseException` cancellation.
- A separate guardian stops on stale/missing owner heartbeat (15 seconds), owner exit, a stop marker, signals, optional duration limit, or miner exit.
- Linux parent-death signal kills the miner if the guardian itself crashes. Process groups receive TERM, followed by KILL if needed.
- Pool monitoring failure stops the run. No automatic retry or restart after an error.
- No accepted GPU share for 30 minutes also stops the run, including initial connection failures.
- Logs rotate at 1 MiB with one backup. No Docker socket, wallet secret, public control endpoint or arbitrary shell-code widget is used.

## Verification

`test_core.py` checks baseline accounting, shared-baseline reuse, payout transitions and completion latching. `test_guardian.py` uses a harmless temporary fake process to test stop/heartbeat/time-limit/lock/parent-death behavior. `test_nodes.py` checks ComfyUI cancellation, target completion, wallet locks, baseline validation, API failures and workflow widget order with mocked GPU/pool/process calls. `test_environment.py` verifies container detection for Docker/Kubernetes and rejects host-only evidence. These tests do not mine and do not establish real H200 hashrate or earnings.

```bash
python -m unittest discover -v
```

## Sources

- [lolMiner 1.98a and Hopper fixes](https://github.com/Lolliedieb/lolMiner-releases/releases/tag/1.98a)
- [Official archive SHA256](https://github.com/Lolliedieb/lolMiner-releases/releases/expanded_assets/1.98a)
- [unMineable lolMiner configuration](https://unmineable.com/support/article/how-to-setup-lolminer-for-gpu-mining)
- [DOGE pool and payout threshold](https://unmineable.com/coins/DOGE)

This repository contains original wrapper/node code only. lolMiner is downloaded separately from its publisher and retains its own license and developer-fee terms.
