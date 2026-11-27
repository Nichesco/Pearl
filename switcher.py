#!/usr/bin/env python3
"""SaladCloud Pearl (PRL) ROI switcher, kill-switch, and auto-rollout. Pure stdlib."""

import json
import os
import sys
import time
import urllib.error
import urllib.request

SALAD_API = "https://api.salad.com/api/public"

# Longest/most-specific names first so "RTX 4070 Ti Super" matches before "RTX 4070 Ti"
# and laptop GPUs are explicitly excluded before matching desktop names.
GPU_BENCHMARKS_THS = [
    ("RTX 5090 Laptop", None),
    ("RTX 5090", 435.0),
    ("RTX 4090 Laptop", None),
    ("RTX 4090", 335.0),
    ("RTX 5080 Laptop", None),
    ("RTX 5080", 226.0),
    ("RTX 4080 Laptop", None),
    ("RTX 4080", 165.0),
    ("RTX 5070 Ti Laptop", None),
    ("RTX 5070 Ti", 174.0),
    ("RTX 4070 Ti Super", 159.5),
    ("RTX 4070 Ti", 144.8),
    ("RTX 4070 Laptop", None),
    ("RTX 4070", 128.0),
    ("RTX 5070 Laptop", None),
    ("RTX 5070", 117.8),
    ("RTX 3090 Ti", 141.5),
    ("RTX 3090", 125.1),
    ("RTX 3080 Ti Laptop", None),
    ("RTX 3080 Ti", 128.1),
    ("RTX 3080 Laptop", None),
    ("RTX 3080", 106.9),
    ("RTX 3070 Ti", 75.4),
    ("RTX 3070", 72.0),
    ("RTX 4060 Ti", 92.5),
    ("RTX 5060 Ti", 89.0),
    ("RTX 5060 Laptop", None),
    ("RTX 5060", 74.0),
    ("RTX 4060 Laptop", None),
    ("RTX 4060", 65.2),
    ("RTX 3060 Ti", 59.6),
    ("RTX 3060 Laptop", None),
    ("RTX 3060", 49.0),
    ("RTX 2080 Ti", 92.3),
    ("RTX 2080", 67.6),
    ("RTX 2070", 52.9),
    ("RTX 2060", 41.3),
    ("Stable Diffusion Compatible", 60.0),
]

NEB_BENCHMARKS_GHS = [
    ("RTX 5090 Laptop", None),
    ("RTX 5090", 1.72),
    ("RTX 4090 Laptop", None),
    ("RTX 4090", 1.25),
    ("RTX 5070 Ti Laptop", None),
    ("RTX 5070 Ti", 0.59),
]

ACTIVE_STATUSES = {"running", "deploying", "pending"}


def http_json(url, method="GET", headers=None, payload=None, timeout=12):
    req_headers = {"Accept": "application/json", "User-Agent": "pearl-salad-switcher/1.0"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = (
            "application/merge-patch+json" if method.upper() == "PATCH" else "application/json"
        )
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace").strip()
        return json.loads(body) if body else None


def fetch_pearl_price_usd():
    sources = [
        ("Gate.io", "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=PEARL_USDT", lambda d: float(d[0]["last"])),
        ("Kraken", "https://api.kraken.com/0/public/Ticker?pair=PEARLUSD", lambda d: float(next(iter(d["result"].values()))["c"][0])),
        ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids=pearl-2&vs_currencies=usd", lambda d: float(d["pearl-2"]["usd"])),
    ]
    for name, url, parse in sources:
        try:
            price = parse(http_json(url, timeout=8))
            if 0.01 < price < 10000:
                return price, name
        except Exception:
            continue
    raise RuntimeError("Failed to fetch live PEARL price from all sources")


def fetch_pearl_network():
    try:
        d = http_json("https://pool.kryptex.com/prl/api/v1/pool/stats", timeout=8)
        net_h, rw, bt = float(d["net_hashrate"]), float(d["block_reward"]), float(d["block_time"])
        if net_h >= 1e17 and rw > 0 and bt > 0:
            return net_h, rw, bt, "Kryptex"
    except Exception:
        pass
    try:
        d = http_json("https://pearl.herominers.com/api/stats", timeout=8)
        bt = float(d["config"]["coinDifficultyTarget"])
        net_h = (float(d["network"]["difficulty"]) * 4294967296.0) / bt
        rw = float(d["lastblock"]["reward"]) / float(d["config"]["coinUnits"])
        if net_h >= 1e17 and rw > 0 and bt > 0:
            return net_h, rw, bt, "HeroMiners"
    except Exception:
        pass
    raise RuntimeError("Failed to fetch live Pearl network stats from all sources")


def match_gpu_ths(gpu_name):
    low = (gpu_name or "").lower()
    for pattern, ths in GPU_BENCHMARKS_THS:
        if pattern.lower() in low:
            return ths
    return None


def match_neb_ghs(gpu_name):
    low = (gpu_name or "").lower()
    for pattern, ghs in NEB_BENCHMARKS_GHS:
        if pattern.lower() in low:
            return ghs
    return None


def group_coin(env):
    if env.get("POOL") and env.get("WALLET"):
        return "PRL"
    if (env.get("COIN") or "").upper() == "NEB" and env.get("USERNAME"):
        return "NEB"
    return None


def fetch_neb_usd_per_gh_hr_gross():
    d = http_json("https://api.coingecko.com/api/v3/simple/price?ids=neural-engine-byte&vs_currencies=usd", timeout=8)
    price = float(d["neural-engine-byte"]["usd"])
    if not (0.0001 < price < 10000):
        raise RuntimeError(f"Unexpected NEB price: {price}")
    try:
        s = http_json("https://neb.suprnova.cc/api/pools/neb", timeout=8)
        net_gh = float(s["pool"]["networkStats"]["networkHashrate"]) / 1e9
        if net_gh > 10:
            # shortcut: Suprnova omits blockTime/reward, assumes 60s target and 19 NEB reward; update on halving
            return (1.0 / net_gh) * 60.0 * 19.0 * price, price, net_gh, "Suprnova"
    except Exception:
        pass
    m2 = http_json("https://neb.2miners.com/api/stats", timeout=8)
    nodes = m2.get("nodes")
    node = nodes[0] if isinstance(nodes, list) else nodes
    net_gh = float(node["networkhashps"]) / 1e9
    bt = float(node["avgBlockTime"])
    rw = float(node["blockReward"]) / 1e8
    if net_gh > 10 and bt > 0 and rw > 0:
        return (1.0 / net_gh) * (3600.0 / bt) * rw * price, price, net_gh, "2Miners"
    raise RuntimeError("Failed to fetch live NEB network stats from all sources")


def plan_rollout(groups, target_image):
    """Return list of (group_name, old_image) for Pearl miner groups needing an image update."""
    updates = []
    for g in groups:
        container = g.get("container") or {}
        env = container.get("environment_variables") or {}
        if not env.get("POOL") or not env.get("WALLET"):
            continue
        cur_image = container.get("image") or ""
        if cur_image != target_image:
            updates.append((g.get("name", "?"), cur_image))
    return updates


def rollout_patch_accepted(resp, target_image):
    got_image = ((resp or {}).get("container") or {}).get("image")
    return got_image == target_image or bool((resp or {}).get("pending_change"))


def fetch_gpu_availability(groups, api_key, org):
    """Fetch live SaladCloud GPU availability per group priority; returns {} or {name: int|None}."""
    if not api_key:
        return {}
    headers = {"Salad-Api-Key": api_key}
    avail_url = f"{SALAD_API}/organizations/{org}/availability/sce-gpu-availability"
    cache = {}
    out = {}
    for g in groups:
        name = g.get("name", "?")
        container = g.get("container") or {}
        env = container.get("environment_variables") or {}
        if not group_coin(env):
            continue
        res = container.get("resources") or {}
        gpu_ids = res.get("gpu_classes") or []
        if len(gpu_ids) != 1:
            continue
        cpu = int(res.get("cpu") or 1)
        mem = int(res.get("memory") or 2048)
        key = (gpu_ids[0], cpu, mem)
        if key not in cache:
            try:
                cache[key] = http_json(
                    avail_url,
                    method="POST",
                    headers=headers,
                    payload={"gpu_classes": [gpu_ids[0]], "cpu": cpu, "memory": mem, "storage_amount": 1073741824},
                )
            except Exception:
                cache[key] = None
            time.sleep(0.05)
        data = cache[key]
        prio = (g.get("priority") or "batch").lower()
        val = data.get(f"available_gpu_{prio}") if isinstance(data, dict) else None
        out[name] = int(val) if val is not None else None
    return out


def plan_actions(
    groups,
    gpu_classes,
    usd_per_100th_hr_gross,
    max_daily_usd,
    start_roi_pct,
    stop_roi_pct,
    default_fee_pct,
    low_start_roi_pct=None,
    low_stop_roi_pct=None,
    avail_by_group=None,
    neb_usd_per_gh_hr_gross=None,
):
    """Pure decision planner budgeting by live running/creating GPU spend."""
    class_by_id = {c["id"]: c for c in gpu_classes}
    low_start = start_roi_pct if low_start_roi_pct is None else low_start_roi_pct
    low_stop = stop_roi_pct if low_stop_roi_pct is None else low_stop_roi_pct
    avail_map = avail_by_group or {}
    candidates = []
    skipped = []

    for g in groups:
        name = g.get("name", "?")
        container = g.get("container") or {}
        env = container.get("environment_variables") or {}
        coin = group_coin(env)
        if not coin:
            skipped.append((name, "not a managed miner group (missing POOL/WALLET or COIN=NEB)"))
            continue

        gpu_ids = (container.get("resources") or {}).get("gpu_classes") or []
        if len(gpu_ids) != 1:
            skipped.append((name, f"mixed or zero GPU classes ({len(gpu_ids)}) — keep 1 GPU per group"))
            continue

        gclass = class_by_id.get(gpu_ids[0])
        if not gclass:
            skipped.append((name, f"unknown GPU class id {gpu_ids[0]}"))
            continue

        gpu_name = gclass.get("name", "?")
        if coin == "PRL":
            est_rate = match_gpu_ths(gpu_name)
            if est_rate is None:
                skipped.append((name, f"no TH/s benchmark for '{gpu_name}'"))
                continue
        else:
            if neb_usd_per_gh_hr_gross is None:
                skipped.append((name, "NEB market stats unavailable"))
                continue
            est_rate = match_neb_ghs(gpu_name)
            if est_rate is None:
                skipped.append((name, f"no NEB GH/s benchmark for '{gpu_name}'"))
                continue

        priority = (g.get("priority") or "batch").lower()
        hourly_cost = None
        for p in gclass.get("prices") or []:
            if (p.get("priority") or "").lower() == priority:
                hourly_cost = float(p["price"])
                break
        if hourly_cost is None and env.get("HOURLY_COST"):
            try:
                hourly_cost = float(env["HOURLY_COST"])
            except ValueError:
                hourly_cost = None
        if not hourly_cost or hourly_cost <= 0:
            skipped.append((name, f"could not determine hourly price for {gpu_name} ({priority})"))
            continue

        base_fee = default_fee_pct if coin == "PRL" else 1.0
        try:
            fee_pct = float(env.get("FEE_PCT", base_fee))
        except ValueError:
            fee_pct = base_fee

        replicas = max(1, int(g.get("replicas") or 1))
        state = g.get("current_state") or {}
        status = (state.get("status") or "stopped").lower()
        is_active = status in ACTIVE_STATUSES
        counts = state.get("instance_status_counts") or {}
        running_now = int(counts.get("running_count") or 0) + int(counts.get("creating_count") or 0)
        live_daily_cost = hourly_cost * 24.0 * running_now
        max_daily_cost = hourly_cost * 24.0 * replicas
        start_budget_cost = 0.0 if priority == "batch" else max_daily_cost

        if coin == "PRL":
            rev_hr = (est_rate / 100.0) * usd_per_100th_hr_gross * (1.0 - fee_pct / 100.0)
        else:
            rev_hr = est_rate * neb_usd_per_gh_hr_gross * (1.0 - fee_pct / 100.0)
        roi_pct = ((rev_hr - hourly_cost) / hourly_cost) * 100.0
        daily_profit = (rev_hr - hourly_cost) * 24.0 * running_now

        candidates.append({
            "name": name,
            "coin": coin,
            "gpu": gpu_name,
            "priority": priority,
            "avail": avail_map.get(name),
            "replicas": replicas,
            "running_now": running_now,
            "status": status,
            "is_active": is_active,
            "hourly_cost": hourly_cost,
            "live_daily_cost": live_daily_cost,
            "max_daily_cost": max_daily_cost,
            "start_budget_cost": start_budget_cost,
            "env": env,
            "est_ths": est_rate,
            "roi_pct": roi_pct,
            "daily_profit": daily_profit,
            "action": "KEEP_STOPPED",
            "reason": "",
        })

    candidates.sort(
        key=lambda c: (c["roi_pct"], 1 if c["running_now"] > 0 else 0, 1 if c["is_active"] else 0),
        reverse=True,
    )

    planned_daily_spend = 0.0
    for c in candidates:
        grp_start, grp_stop = (start_roi_pct, stop_roi_pct) if c["priority"] == "batch" else (low_start, low_stop)
        min_needed_roi = grp_stop if c["is_active"] else grp_start
        if c["roi_pct"] < min_needed_roi:
            if c["is_active"]:
                c["action"] = "STOP"
                c["reason"] = f"ROI {c['roi_pct']:+.1f}% < stop floor {grp_stop:.1f}%"
            else:
                c["action"] = "KEEP_STOPPED"
                c["reason"] = f"ROI {c['roi_pct']:+.1f}% < start floor {grp_start:.1f}%"
            continue

        if c["is_active"]:
            if c["running_now"] == 0:
                if planned_daily_spend < max_daily_usd - 1e-6:
                    c["action"] = "KEEP_RUNNING"
                    c["reason"] = "profitable & queued ($0 live burn)"
                else:
                    c["action"] = "STOP"
                    c["reason"] = f"budget full (${planned_daily_spend:.2f}/${max_daily_usd:.2f})"
            elif planned_daily_spend + c["live_daily_cost"] <= max_daily_usd + 1e-6:
                planned_daily_spend += c["live_daily_cost"]
                c["action"] = "KEEP_RUNNING"
                c["reason"] = "profitable & within live budget"
            else:
                c["action"] = "STOP"
                c["reason"] = f"over ${max_daily_usd:.2f}/day live cap (higher-ROI groups take priority)"
        else:
            if c["priority"] != "batch" and c["avail"] == 0:
                c["action"] = "KEEP_STOPPED"
                c["reason"] = f"0 live GPUs available on {c['priority']}"
                continue
            if planned_daily_spend + c["start_budget_cost"] <= max_daily_usd + 1e-6 and (
                c["start_budget_cost"] > 0 or planned_daily_spend < max_daily_usd - 1e-6
            ):
                planned_daily_spend += c["start_budget_cost"]
                c["action"] = "START"
                c["reason"] = "profitable & within budget"
            else:
                c["action"] = "KEEP_STOPPED"
                c["reason"] = f"budget full (${planned_daily_spend:.2f}/${max_daily_usd:.2f})"

    return candidates, skipped, planned_daily_spend


def run_rollout(target_image, api_key, org, project):
    if not api_key:
        print("[info] SALAD_API_KEY secret is not set yet — skipping rollout cleanly.")
        return

    headers = {"Salad-Api-Key": api_key}
    groups = (http_json(f"{SALAD_API}/organizations/{org}/projects/{project}/containers", headers=headers) or {}).get("items", [])
    updates = plan_rollout(groups, target_image)
    if not updates:
        print(f"[ok] All Pearl miner groups are already on {target_image}.")
        return

    failed = False
    for name, old_img in updates:
        url = f"{SALAD_API}/organizations/{org}/projects/{project}/containers/{name}"
        try:
            resp = http_json(url, method="PATCH", headers=headers, payload={"container": {"image": target_image}})
            got_image = ((resp or {}).get("container") or {}).get("image")
            pending = bool((resp or {}).get("pending_change"))
            if not rollout_patch_accepted(resp, target_image):
                raise RuntimeError(f"PATCH returned image '{got_image}' != '{target_image}' (pending_change={pending})")
            suffix = " (pending)" if pending and got_image != target_image else ""
            print(f"[ok] rollout {name}: {old_img} -> {target_image}{suffix}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            print(f"[err] rollout {name} -> HTTP {e.code}: {detail}")
            failed = True
        except Exception as e:
            print(f"[err] rollout {name} -> {e}")
            failed = True
        time.sleep(0.25)

    if failed:
        sys.exit(1)


def run_self_test():
    # 1. Verify benchmark coverage and laptop exclusions
    for model in (
        "RTX 4070", "RTX 3070", "RTX 3060 Ti", "RTX 3080", "RTX 2070",
        "RTX 4070 Ti", "RTX 4070 Ti Super", "RTX 3080 Ti",
        "RTX 5060 Ti", "RTX 5060", "RTX 4060", "RTX 3060", "RTX 2080",
        "Stable Diffusion Compatible",
    ):
        assert match_gpu_ths(f"{model} (12 GB)") is not None, model
    assert match_gpu_ths("RTX 3060 Ti (8 GB)") > match_gpu_ths("RTX 3060 (8 GB)")
    assert match_gpu_ths("RTX 4060 Ti (8 GB)") > match_gpu_ths("RTX 4060 (8 GB)")
    assert match_gpu_ths("RTX 2080 Ti (11 GB)") > match_gpu_ths("RTX 2080 (8 GB)")
    for laptop in (
        "RTX 4090 Laptop (16 GB)", "RTX 5090 Laptop (24 GB)", "RTX 5080 Laptop (16 GB)",
        "RTX 5060 Laptop (8 GB)", "RTX 4060 Laptop (8 GB)", "RTX 3060 Laptop (6 GB)",
    ):
        assert match_gpu_ths(laptop) is None, laptop

    gpu_classes = [
        {"id": "g5090", "name": "RTX 5090 (32 GB)", "prices": [{"priority": "batch", "price": "0.25"}]},
        {"id": "g4090", "name": "RTX 4090 (24 GB)", "prices": [{"priority": "batch", "price": "0.16"}, {"priority": "low", "price": "0.253"}]},
        {"id": "g5080", "name": "RTX 5080 (16 GB)", "prices": [{"priority": "batch", "price": "0.15"}]},
        {"id": "g4070", "name": "RTX 4070 (12 GB)", "prices": [{"priority": "batch", "price": "0.07"}]},
        {"id": "g5070ti", "name": "RTX 5070 Ti (16 GB)", "prices": [{"priority": "batch", "price": "0.11"}]},
        {"id": "g5090lap", "name": "RTX 5090 Laptop (24 GB)", "prices": [{"priority": "batch", "price": "0.10"}]},
    ]
    env_ok = {"POOL": "stratum+ssl://fr.pearl.gfwroute.com:1200", "WALLET": "prl1test"}

    # 2. Idle batch groups (replicas > $50/day on paper, 0 running) must STAY active;
    #    marginal +7% group must STOP at stop_roi_pct=10.0; live spend exceeding $20 stops lowest-ROI runner.
    groups = [
        {"name": "4090-idle", "priority": "batch", "replicas": 5,
         "current_state": {"status": "deploying", "instance_status_counts": {"running_count": 0, "creating_count": 0, "allocating_count": 5}},
         "container": {"image": "ghcr.io/nichesco/pearl:old", "resources": {"gpu_classes": ["g4090"]}, "environment_variables": env_ok}},
        {"name": "4070-idle", "priority": "batch", "replicas": 5,
         "current_state": {"status": "deploying", "instance_status_counts": {"running_count": 0, "creating_count": 0, "allocating_count": 5}},
         "container": {"image": "ghcr.io/nichesco/pearl:new", "resources": {"gpu_classes": ["g4070"]}, "environment_variables": env_ok}},
        {"name": "5090-run2", "priority": "batch", "replicas": 5,
         "current_state": {"status": "running", "instance_status_counts": {"running_count": 2, "creating_count": 0, "allocating_count": 3}},
         "container": {"image": "ghcr.io/nichesco/pearl:old", "resources": {"gpu_classes": ["g5090"]}, "environment_variables": env_ok}},
        {"name": "5070ti-run5", "priority": "batch", "replicas": 5,
         "current_state": {"status": "running", "instance_status_counts": {"running_count": 5, "creating_count": 0, "allocating_count": 0}},
         "container": {"image": "ghcr.io/nichesco/pearl:old", "resources": {"gpu_classes": ["g5070ti"]}, "environment_variables": env_ok}},
        {"name": "4090-low-stop", "priority": "low", "replicas": 4,
         "current_state": {"status": "stopped", "instance_status_counts": {"running_count": 0, "creating_count": 0, "allocating_count": 0}},
         "container": {"image": "ghcr.io/nichesco/pearl:old", "resources": {"gpu_classes": ["g4090"]}, "environment_variables": env_ok}},
        {"name": "5080-marginal", "priority": "batch", "replicas": 2,
         "current_state": {"status": "running", "instance_status_counts": {"running_count": 0, "creating_count": 0, "allocating_count": 2}},
         "container": {"image": "ghcr.io/nichesco/pearl:old", "resources": {"gpu_classes": ["g5080"]}, "environment_variables": env_ok}},
        {"name": "laptop-skip", "priority": "batch", "replicas": 1, "current_state": {"status": "stopped"},
         "container": {"image": "ghcr.io/nichesco/pearl:old", "resources": {"gpu_classes": ["g5090lap"]}, "environment_variables": env_ok}},
        {"name": "non-miner", "priority": "batch", "replicas": 1, "current_state": {"status": "running"},
         "container": {"image": "ghcr.io/nichesco/pearl:old", "resources": {"gpu_classes": ["g5090"]}, "environment_variables": {}}},
    ]
    # At gross=0.0725/100TH & fee=2% -> net=0.07105:
    # 4090 batch (+48.8%), 4070 batch (+29.9%), 5090 batch (+23.6%, $12/d), 5070ti batch (+23.6%, $12/d),
    # 5080 batch (+7.0% -> STOP < 10%), 4090 low (-5.9% -> KEEP_STOPPED)
    cands, skipped, spend = plan_actions(groups, gpu_classes, 0.0725, 20.0, 15.0, 10.0, 2.0)
    by_name = {c["name"]: c["action"] for c in cands}
    assert by_name["4090-idle"] == "KEEP_RUNNING", by_name
    assert by_name["4070-idle"] == "KEEP_RUNNING", by_name
    assert by_name["5090-run2"] == "KEEP_RUNNING", by_name
    assert by_name["5070ti-run5"] == "STOP", by_name
    assert by_name["5080-marginal"] == "STOP", by_name
    assert by_name["4090-low-stop"] == "KEEP_STOPPED", by_name
    assert spend <= 20.0 + 1e-6, spend
    assert {s[0] for s in skipped} == {"laptop-skip", "non-miner"}, skipped

    # 3. Stopped low-priority group reserves full replicas*hourly_cost*24 ($24.29 for 4x 4090 low > $20 cap -> stays stopped)
    low_only = [
        {"name": "4090-low-4x", "priority": "low", "replicas": 4,
         "current_state": {"status": "stopped", "instance_status_counts": {"running_count": 0, "creating_count": 0, "allocating_count": 0}},
         "container": {"resources": {"gpu_classes": ["g4090"]}, "environment_variables": env_ok}},
        {"name": "4090-low-1x", "priority": "low", "replicas": 1,
         "current_state": {"status": "stopped", "instance_status_counts": {"running_count": 0, "creating_count": 0, "allocating_count": 0}},
         "container": {"resources": {"gpu_classes": ["g4090"]}, "environment_variables": env_ok}},
    ]
    cands_low, _, spend_low = plan_actions(low_only, gpu_classes, 0.095, 20.0, 15.0, 10.0, 2.0)
    by_low = {c["name"]: c["action"] for c in cands_low}
    assert by_low["4090-low-4x"] == "KEEP_STOPPED", by_low
    assert by_low["4090-low-1x"] == "START", by_low
    assert abs(spend_low - 6.072) < 0.01, spend_low

    # 4. Rollout planner updates only Pearl groups not on target_image
    roll = [n for n, _ in plan_rollout(groups, "ghcr.io/nichesco/pearl:new")]
    assert "4070-idle" not in roll and "non-miner" not in roll, roll
    assert "4090-idle" in roll and "5090-run2" in roll, roll
    assert rollout_patch_accepted({"container": {"image": "img:new"}, "pending_change": False}, "img:new")
    assert rollout_patch_accepted({"container": {"image": "img:old"}, "pending_change": True}, "img:new")
    assert not rollout_patch_accepted({"container": {"image": "img:old"}, "pending_change": False}, "img:new")

    # 5. Low-tier thresholds (+2% start / <0% stop) + availability gating (0-avail low skipped, 0-avail batch queued)
    mixed = [
        {"name": "4090-low-zero", "priority": "low", "replicas": 3,
         "current_state": {"status": "stopped"},
         "container": {"resources": {"gpu_classes": ["g4090"]}, "environment_variables": env_ok}},
        {"name": "4070-low-avail", "priority": "low", "replicas": 2,
         "current_state": {"status": "stopped"},
         "container": {"resources": {"gpu_classes": ["g4070"]}, "environment_variables": env_ok}},
        {"name": "5090-low-neg", "priority": "low", "replicas": 1,
         "current_state": {"status": "running", "instance_status_counts": {"running_count": 1}},
         "container": {"resources": {"gpu_classes": ["g5090"]}, "environment_variables": env_ok}},
        {"name": "5070ti-batch-zero", "priority": "batch", "replicas": 2,
         "current_state": {"status": "stopped"},
         "container": {"resources": {"gpu_classes": ["g5070ti"]}, "environment_variables": env_ok}},
    ]
    # Note: g4070 low price from fixture prices above needs low tier; add it locally for test
    gpu_classes_ext = list(gpu_classes) + [
        {"id": "g4070low", "name": "RTX 4070 (12 GB)", "prices": [{"priority": "low", "price": "0.102"}]},
        {"id": "g5090low", "name": "RTX 5090 (32 GB)", "prices": [{"priority": "low", "price": "0.367"}]},
    ]
    mixed[1]["container"]["resources"]["gpu_classes"] = ["g4070low"]
    mixed[2]["container"]["resources"]["gpu_classes"] = ["g5090low"]
    avail = {"4090-low-zero": 0, "4070-low-avail": 5, "5090-low-neg": 10, "5070ti-batch-zero": 0}
    cands_mix, _, spend_mix = plan_actions(
        mixed, gpu_classes_ext, 0.084, 20.0, 15.0, 10.0, 2.0,
        low_start_roi_pct=2.0, low_stop_roi_pct=0.0, avail_by_group=avail,
    )
    by_mix = {c["name"]: c["action"] for c in cands_mix}
    assert by_mix["4090-low-zero"] == "KEEP_STOPPED", by_mix
    assert by_mix["4070-low-avail"] == "START", by_mix
    assert by_mix["5090-low-neg"] == "STOP", by_mix
    assert by_mix["5070ti-batch-zero"] == "START", by_mix
    assert abs(spend_mix - (0.102 * 24.0 * 2)) < 0.01, spend_mix

    # 6. NEB group ROI start/stop & rollout exclusion
    neb_env = {"COIN": "NEB", "USERNAME": "neb1test", "POOL_URL": "stratum+ssl://neb.suprnova.cc:7806"}
    neb_groups = [
        {"name": "neb5090batch", "priority": "batch", "replicas": 10,
         "current_state": {"status": "stopped", "instance_status_counts": {"running_count": 0}},
         "container": {"image": "ocminersupr/suprminer-base:1.9.63", "resources": {"gpu_classes": ["g5090"]}, "environment_variables": neb_env}},
    ]
    c_neb_on, _, _ = plan_actions(neb_groups, gpu_classes, 0.06, 200.0, 15.0, 10.0, 2.0, neb_usd_per_gh_hr_gross=0.195)
    assert c_neb_on[0]["action"] == "START", c_neb_on
    neb_groups[0]["current_state"]["status"] = "running"
    c_neb_off, _, _ = plan_actions(neb_groups, gpu_classes, 0.06, 200.0, 15.0, 10.0, 2.0, neb_usd_per_gh_hr_gross=0.095)
    assert c_neb_off[0]["action"] == "STOP", c_neb_off
    assert plan_rollout(neb_groups, "ghcr.io/nichesco/pearl:new") == []

    price, psrc = fetch_pearl_price_usd()
    net_h, rw, bt, nsrc = fetch_pearl_network()
    print(f"SELF-TEST PASSED | Live check: PEARL=${price:.4f} ({psrc}), net={net_h/1e18:.2f} EH/s ({nsrc})")


def main():
    if "--self-test" in sys.argv:
        run_self_test()
        return

    api_key = os.environ.get("SALAD_API_KEY", "").strip()
    org = os.environ.get("SALAD_ORG", "pearl123s").strip()
    project = os.environ.get("SALAD_PROJECT", "pearl-test-4090").strip()

    if "--rollout-image" in sys.argv:
        idx = sys.argv.index("--rollout-image")
        if idx + 1 >= len(sys.argv) or not sys.argv[idx + 1].strip():
            print("[error] --rollout-image requires an image tag argument.", file=sys.stderr)
            sys.exit(2)
        run_rollout(sys.argv[idx + 1].strip(), api_key, org, project)
        return

    if not api_key:
        print("[info] SALAD_API_KEY secret is not set yet — skipping run cleanly.")
        return

    max_daily_usd = float(os.environ.get("MAX_DAILY_USD", "20.0"))
    start_roi_pct = float(os.environ.get("START_ROI_PCT", "15.0"))
    stop_roi_pct = float(os.environ.get("STOP_ROI_PCT", "10.0"))
    low_start_roi_pct = float(os.environ.get("LOW_START_ROI_PCT", "2.0"))
    low_stop_roi_pct = float(os.environ.get("LOW_STOP_ROI_PCT", "0.0"))
    default_fee_pct = float(os.environ.get("FEE_PCT", "2.0"))
    dry_run = os.environ.get("DRY_RUN", "true").strip().lower() != "false"

    price, psrc = fetch_pearl_price_usd()
    net_h, rw, bt, nsrc = fetch_pearl_network()
    daily_prl = (86400.0 / bt) * rw
    usd_per_100th_hr_gross = ((daily_prl / (net_h / 1e12)) * 100.0 * price) / 24.0

    print(f"=== Pearl SaladCloud Switcher ({'DRY-RUN' if dry_run else 'LIVE'}) ===")
    print(f"Org/Project : {org}/{project}")
    print(f"Market      : PEARL=${price:.4f} ({psrc}) | Net={net_h/1e18:.2f} EH/s ({nsrc}) | Gross=${usd_per_100th_hr_gross:.4f}/hr per 100 TH/s")
    print(
        f"Rules       : MAX_DAILY_USD=${max_daily_usd:.2f} | "
        f"BATCH>={start_roi_pct:.1f}%/<{stop_roi_pct:.1f}% | "
        f"LOW>={low_start_roi_pct:.1f}%/<{low_stop_roi_pct:.1f}% | FEE={default_fee_pct:.1f}%\n"
    )

    headers = {"Salad-Api-Key": api_key}
    gpu_classes = (http_json(f"{SALAD_API}/organizations/{org}/gpu-classes", headers=headers) or {}).get("items", [])
    groups = (http_json(f"{SALAD_API}/organizations/{org}/projects/{project}/containers", headers=headers) or {}).get("items", [])
    avail_by_group = fetch_gpu_availability(groups, api_key, org)

    neb_usd_per_gh_hr_gross = None
    if any(group_coin((g.get("container") or {}).get("environment_variables") or {}) == "NEB" for g in groups):
        try:
            neb_usd_per_gh_hr_gross, neb_price, neb_gh, neb_src = fetch_neb_usd_per_gh_hr_gross()
            print(f"NEB Market  : NEB=${neb_price:.4f} (CoinGecko) | Net={neb_gh:.1f} GH/s ({neb_src}) | Gross=${neb_usd_per_gh_hr_gross:.4f}/hr per GH/s\n")
        except Exception as e:
            print(f"[warn] Failed to fetch NEB stats ({e}) — skipping NEB groups.\n")

    candidates, skipped, planned_daily = plan_actions(
        groups,
        gpu_classes,
        usd_per_100th_hr_gross,
        max_daily_usd,
        start_roi_pct,
        stop_roi_pct,
        default_fee_pct,
        low_start_roi_pct=low_start_roi_pct,
        low_stop_roi_pct=low_stop_roi_pct,
        avail_by_group=avail_by_group,
        neb_usd_per_gh_hr_gross=neb_usd_per_gh_hr_gross,
    )

    for name, reason in skipped:
        print(f"[skip] {name}: {reason}")
    if skipped:
        print()

    print(f"{'Group':<28} {'GPU':<22} {'Prio':<6} {'Avl':>4} {'Run/Rep':>7} {'Status':<10} {'Live$/d':>7} {'ROI':>8}  Action")
    print("-" * 120)
    for c in candidates:
        label = c["action"]
        if dry_run and label in ("START", "STOP"):
            label = f"WOULD_{label}"
        run_rep = f"{c['running_now']}/{c['replicas']}"
        avl = str(c["avail"]) if c["avail"] is not None else "?"
        print(
            f"{c['name']:<28} {c['gpu'][:22]:<22} {c['priority']:<6} {avl:>4} {run_rep:>7} "
            f"{c['status']:<10} ${c['live_daily_cost']:>6.2f} {c['roi_pct']:>+7.1f}%  {label} ({c['reason']})"
        )

    print(f"\nPlanned live daily spend (uses existing prepaid Salad credits only): ${planned_daily:.2f} / ${max_daily_usd:.2f}")

    if dry_run:
        print("\n[dry-run] No start/stop/patch API calls sent. Set GitHub repo variable DRY_RUN=false when ready.")
        return

    for c in candidates:
        if c["action"] in ("KEEP_RUNNING", "START") and c["coin"] == "PRL":
            merged_env = dict(c["env"])
            changes = []

            raw_hc = c["env"].get("HOURLY_COST", "")
            try:
                hc_drifted = abs(float(raw_hc) - c["hourly_cost"]) > 0.001
            except (TypeError, ValueError):
                hc_drifted = True
            if hc_drifted:
                new_hc = f"{c['hourly_cost']:.3f}".rstrip("0").rstrip(".")
                merged_env["HOURLY_COST"] = new_hc
                changes.append(f"HOURLY_COST '{raw_hc}'->'{new_hc}'")

            if c["priority"] != "batch":
                target_mm = max(0.85, round(1.0 + (low_stop_roi_pct - 2.0) / 100.0, 2))
                raw_mm = c["env"].get("MIN_MARGIN", "")
                try:
                    mm_drifted = abs(float(raw_mm) - target_mm) > 0.005
                except (TypeError, ValueError):
                    mm_drifted = True
                if mm_drifted:
                    new_mm = f"{target_mm:.2f}"
                    merged_env["MIN_MARGIN"] = new_mm
                    changes.append(f"MIN_MARGIN '{raw_mm}'->'{new_mm}'")

            if changes:
                url = f"{SALAD_API}/organizations/{org}/projects/{project}/containers/{c['name']}"
                try:
                    http_json(url, method="PATCH", headers=headers, payload={"container": {"environment_variables": merged_env}})
                    print(f"[ok] sync env {c['name']}: {', '.join(changes)}")
                except urllib.error.HTTPError as e:
                    detail = e.read().decode("utf-8", errors="replace")[:200]
                    print(f"[err] sync env {c['name']} -> HTTP {e.code}: {detail}")
                time.sleep(0.25)

        if c["action"] not in ("START", "STOP"):
            continue
        endpoint = "start" if c["action"] == "START" else "stop"
        url = f"{SALAD_API}/organizations/{org}/projects/{project}/containers/{c['name']}/{endpoint}"
        try:
            http_json(url, method="POST", headers=headers)
            print(f"[ok] {c['action']} {c['name']}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            print(f"[err] {c['action']} {c['name']} -> HTTP {e.code}: {detail}")
        time.sleep(0.25)


if __name__ == "__main__":
    main()
