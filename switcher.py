#!/usr/bin/env python3
"""SaladCloud Pearl (PRL) ROI switcher and kill-switch. Pure stdlib."""

import json
import os
import sys
import urllib.error
import urllib.request

SALAD_API = "https://api.salad.com/api/public"

# Longest/most-specific names first so "RTX 4070 Ti Super" matches before "RTX 4070 Ti"
# and "RTX 5090 Laptop" is explicitly excluded before "RTX 5090".
GPU_BENCHMARKS_THS = [
    ("RTX 5090 Laptop", None),
    ("RTX 5090", 410.0),
    ("RTX 4090", 345.0),
    ("RTX 5080", 242.0),
    ("RTX 4080", 185.0),
    ("RTX 5070 Ti", 160.0),
    ("RTX 4070 Ti Super", 155.0),
    ("RTX 4070 Ti", 135.0),
    ("RTX 3090 Ti", 130.0),
    ("RTX 3090", 120.0),
    ("RTX 3080 Ti", 115.0),
    ("RTX 3080", 95.0),
]

ACTIVE_STATUSES = {"running", "deploying", "pending"}


def http_json(url, method="GET", headers=None, timeout=12):
    req_headers = {"Accept": "application/json", "User-Agent": "pearl-salad-switcher/1.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, method=method, headers=req_headers)
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


def plan_actions(groups, gpu_classes, usd_per_100th_hr_gross, max_daily_usd, start_roi_pct, stop_roi_pct, default_fee_pct):
    """Pure decision planner (easy to test without hitting Salad API)."""
    class_by_id = {c["id"]: c for c in gpu_classes}
    candidates = []
    skipped = []

    for g in groups:
        name = g.get("name", "?")
        container = g.get("container") or {}
        env = container.get("environment_variables") or {}
        if not env.get("POOL") or not env.get("WALLET"):
            skipped.append((name, "not a Pearl miner group (missing POOL/WALLET)"))
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
        est_ths = match_gpu_ths(gpu_name)
        if est_ths is None:
            skipped.append((name, f"no TH/s benchmark for '{gpu_name}'"))
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

        try:
            fee_pct = float(env.get("FEE_PCT", default_fee_pct))
        except ValueError:
            fee_pct = default_fee_pct

        replicas = max(1, int(g.get("replicas") or 1))
        status = ((g.get("current_state") or {}).get("status") or "stopped").lower()
        is_active = status in ACTIVE_STATUSES

        rev_hr = (est_ths / 100.0) * usd_per_100th_hr_gross * (1.0 - fee_pct / 100.0)
        roi_pct = ((rev_hr - hourly_cost) / hourly_cost) * 100.0
        daily_cost = hourly_cost * 24.0 * replicas
        daily_profit = (rev_hr - hourly_cost) * 24.0 * replicas

        candidates.append({
            "name": name,
            "gpu": gpu_name,
            "priority": priority,
            "replicas": replicas,
            "status": status,
            "is_active": is_active,
            "hourly_cost": hourly_cost,
            "daily_cost": daily_cost,
            "est_ths": est_ths,
            "roi_pct": roi_pct,
            "daily_profit": daily_profit,
            "action": "KEEP_STOPPED",
            "reason": "",
        })

    candidates.sort(key=lambda c: (c["roi_pct"], 1 if c["is_active"] else 0), reverse=True)

    planned_daily_spend = 0.0
    for c in candidates:
        min_needed_roi = stop_roi_pct if c["is_active"] else start_roi_pct
        if c["roi_pct"] < min_needed_roi:
            if c["is_active"]:
                c["action"] = "STOP"
                c["reason"] = f"ROI {c['roi_pct']:+.1f}% < stop floor {stop_roi_pct:.1f}%"
            else:
                c["action"] = "KEEP_STOPPED"
                c["reason"] = f"ROI {c['roi_pct']:+.1f}% < start floor {start_roi_pct:.1f}%"
            continue

        if planned_daily_spend + c["daily_cost"] <= max_daily_usd + 1e-6:
            planned_daily_spend += c["daily_cost"]
            if c["is_active"]:
                c["action"] = "KEEP_RUNNING"
                c["reason"] = "profitable & within budget"
            else:
                c["action"] = "START"
                c["reason"] = "profitable & within budget"
        else:
            if c["is_active"]:
                c["action"] = "STOP"
                c["reason"] = f"over ${max_daily_usd:.2f}/day cap (higher-ROI groups take priority)"
            else:
                c["action"] = "KEEP_STOPPED"
                c["reason"] = f"budget full (${planned_daily_spend:.2f}/${max_daily_usd:.2f})"

    return candidates, skipped, planned_daily_spend


def run_self_test():
    gpu_classes = [
        {"id": "g5090", "name": "RTX 5090 (32 GB)", "prices": [{"priority": "batch", "price": "0.25"}]},
        {"id": "g5090lap", "name": "RTX 5090 Laptop (24 GB)", "prices": [{"priority": "batch", "price": "0.10"}]},
        {"id": "g4090", "name": "RTX 4090 (24 GB)", "prices": [{"priority": "batch", "price": "0.16"}, {"priority": "low", "price": "0.253"}]},
        {"id": "g5080", "name": "RTX 5080 (16 GB)", "prices": [{"priority": "batch", "price": "0.15"}]},
        {"id": "g5070ti", "name": "RTX 5070 Ti (16 GB)", "prices": [{"priority": "batch", "price": "0.10"}]},
    ]
    env_ok = {"POOL": "de.pearl.herominers.com:1200", "WALLET": "prl1test"}
    groups = [
        {"name": "4090-low", "priority": "low", "replicas": 1, "current_state": {"status": "running"},
         "container": {"resources": {"gpu_classes": ["g4090"]}, "environment_variables": env_ok}},
        {"name": "5090-batch", "priority": "batch", "replicas": 1, "current_state": {"status": "running"},
         "container": {"resources": {"gpu_classes": ["g5090"]}, "environment_variables": env_ok}},
        {"name": "5080-batch", "priority": "batch", "replicas": 2, "current_state": {"status": "stopped"},
         "container": {"resources": {"gpu_classes": ["g5080"]}, "environment_variables": env_ok}},
        {"name": "5070ti-batch", "priority": "batch", "replicas": 3, "current_state": {"status": "running"},
         "container": {"resources": {"gpu_classes": ["g5070ti"]}, "environment_variables": env_ok}},
        {"name": "laptop-skip", "priority": "batch", "replicas": 1, "current_state": {"status": "stopped"},
         "container": {"resources": {"gpu_classes": ["g5090lap"]}, "environment_variables": env_ok}},
        {"name": "non-miner", "priority": "batch", "replicas": 1, "current_state": {"status": "running"},
         "container": {"resources": {"gpu_classes": ["g5090"]}, "environment_variables": {}}},
    ]
    cands, skipped, spend = plan_actions(groups, gpu_classes, 0.080, 20.0, 15.0, 5.0, 2.0)
    by_name = {c["name"]: c["action"] for c in cands}
    assert by_name["5090-batch"] == "KEEP_RUNNING", by_name
    assert by_name["5080-batch"] == "START", by_name
    assert by_name["5070ti-batch"] == "STOP", by_name
    assert by_name["4090-low"] == "KEEP_RUNNING", by_name
    assert spend <= 20.0, spend
    skip_names = {s[0] for s in skipped}
    assert skip_names == {"laptop-skip", "non-miner"}, skip_names

    cands2, _, _ = plan_actions(groups, gpu_classes, 0.070, 20.0, 15.0, 5.0, 2.0)
    by_name2 = {c["name"]: c["action"] for c in cands2}
    assert by_name2["4090-low"] == "STOP", by_name2

    price, psrc = fetch_pearl_price_usd()
    net_h, rw, bt, nsrc = fetch_pearl_network()
    print(f"SELF-TEST PASSED | Live check: PEARL=${price:.4f} ({psrc}), net={net_h/1e18:.2f} EH/s ({nsrc})")


def main():
    if "--self-test" in sys.argv:
        run_self_test()
        return

    api_key = os.environ.get("SALAD_API_KEY", "").strip()
    if not api_key:
        print("[info] SALAD_API_KEY secret is not set yet — skipping run cleanly.")
        return

    org = os.environ.get("SALAD_ORG", "pearl123s").strip()
    project = os.environ.get("SALAD_PROJECT", "pearl-test-4090").strip()
    max_daily_usd = float(os.environ.get("MAX_DAILY_USD", "20.0"))
    start_roi_pct = float(os.environ.get("START_ROI_PCT", "15.0"))
    stop_roi_pct = float(os.environ.get("STOP_ROI_PCT", "5.0"))
    default_fee_pct = float(os.environ.get("FEE_PCT", "2.0"))
    dry_run = os.environ.get("DRY_RUN", "true").strip().lower() != "false"

    price, psrc = fetch_pearl_price_usd()
    net_h, rw, bt, nsrc = fetch_pearl_network()
    daily_prl = (86400.0 / bt) * rw
    usd_per_100th_hr_gross = ((daily_prl / (net_h / 1e12)) * 100.0 * price) / 24.0

    print(f"=== Pearl SaladCloud Switcher ({'DRY-RUN' if dry_run else 'LIVE'}) ===")
    print(f"Org/Project : {org}/{project}")
    print(f"Market      : PEARL=${price:.4f} ({psrc}) | Net={net_h/1e18:.2f} EH/s ({nsrc}) | Gross=${usd_per_100th_hr_gross:.4f}/hr per 100 TH/s")
    print(f"Rules       : MAX_DAILY_USD=${max_daily_usd:.2f} | START_ROI>={start_roi_pct:.1f}% | STOP_ROI<{stop_roi_pct:.1f}% | FEE={default_fee_pct:.1f}%\n")

    headers = {"Salad-Api-Key": api_key}
    gpu_classes = (http_json(f"{SALAD_API}/organizations/{org}/gpu-classes", headers=headers) or {}).get("items", [])
    groups = (http_json(f"{SALAD_API}/organizations/{org}/projects/{project}/containers", headers=headers) or {}).get("items", [])

    candidates, skipped, planned_daily = plan_actions(
        groups, gpu_classes, usd_per_100th_hr_gross, max_daily_usd, start_roi_pct, stop_roi_pct, default_fee_pct
    )

    for name, reason in skipped:
        print(f"[skip] {name}: {reason}")
    if skipped:
        print()

    print(f"{'Group':<28} {'GPU':<20} {'Prio':<6} {'Rep':>3} {'Status':<10} {'$/day':>7} {'ROI':>8}  Action")
    print("-" * 106)
    for c in candidates:
        label = c["action"]
        if dry_run and label in ("START", "STOP"):
            label = f"WOULD_{label}"
        print(
            f"{c['name']:<28} {c['gpu'][:20]:<20} {c['priority']:<6} {c['replicas']:>3} "
            f"{c['status']:<10} ${c['daily_cost']:>6.2f} {c['roi_pct']:>+7.1f}%  {label} ({c['reason']})"
        )

    print(f"\nPlanned max daily spend (uses existing prepaid Salad credits only): ${planned_daily:.2f} / ${max_daily_usd:.2f}")

    if dry_run:
        print("\n[dry-run] No start/stop API calls sent. Set GitHub repo variable DRY_RUN=false when ready.")
        return

    for c in candidates:
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


if __name__ == "__main__":
    main()
