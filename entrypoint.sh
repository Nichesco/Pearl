#!/usr/bin/env bash
set -uo pipefail
export LC_ALL=C

if [ -z "${WALLET:-}" ]; then
  echo "[error] WALLET environment variable is required."
  sleep 30
  exit 1
fi

POOL="${POOL:-stratum+ssl://fr.pearl.gfwroute.com:1200}"
MINER=$(echo "${MINER:-onezerominer}" | tr '[:upper:]' '[:lower:]')
SHORT_HOST="${HOSTNAME:-salad}"
SHORT_HOST="${SHORT_HOST:0:8}"
if [ -n "${WORKER_PREFIX:-}" ]; then
  WORKER="${WORKER_PREFIX}-${SHORT_HOST}"
else
  WORKER="${WORKER:-${HOSTNAME:-salad}}"
fi
PASS="${PASS:-x}"
MIN_THS="${MIN_THS:-0}"
HOURLY_COST="${HOURLY_COST:-0}"
MIN_MARGIN="${MIN_MARGIN:-1.10}"
if [ -z "${FEE_PCT:-}" ]; then
  if [ "$MINER" = "wildrig" ]; then
    FEE_PCT="1.0"
  else
    FEE_PCT="2.0"
  fi
fi
WARMUP_SEC="${WARMUP_SEC:-90}"
REJECT_5090D="${REJECT_5090D:-1}"

DYN_MIN_THS="0"

reallocate() {
  local reason="$1"
  echo "[watchdog] Reallocating node: ${reason}"
  local payload
  payload=$(jq -nc --arg r "$reason" '{reason: $r}')
  curl -fsS -X POST \
    -H "Metadata: true" \
    -H "Content-Type: application/json" \
    --noproxy "*" \
    -d "$payload" \
    http://169.254.169.254/v1/reallocate || true
  sleep 30
  exit 1
}

fetch_price_usd() {
  local p=""
  p=$(curl -fsS --max-time 8 "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=PEARL_USDT" 2>/dev/null \
    | jq -r '.[0].last // empty' 2>/dev/null || true)
  if [ -z "$p" ]; then
    p=$(curl -fsS --max-time 8 "https://api.kraken.com/0/public/Ticker?pair=PEARLUSD" 2>/dev/null \
      | jq -r '(.result // {} | to_entries[0].value.c[0]) // empty' 2>/dev/null || true)
  fi
  if [ -z "$p" ]; then
    p=$(curl -fsS --max-time 8 "https://api.coingecko.com/api/v3/simple/price?ids=pearl-2&vs_currencies=usd" 2>/dev/null \
      | jq -r '."pearl-2".usd // empty' 2>/dev/null || true)
  fi
  echo "$p"
}

refresh_dynamic_min_ths() {
  local enabled
  enabled=$(awk -v c="$HOURLY_COST" 'BEGIN { print (c + 0 > 0) ? 1 : 0 }')
  [ "$enabled" -eq 1 ] || return 0

  local price net_line net_h reward btime
  price=$(fetch_price_usd)
  net_line=$(curl -fsS --max-time 8 "https://pool.kryptex.com/prl/api/v1/pool/stats" 2>/dev/null \
    | jq -r 'select(.net_hashrate and .block_reward and .block_time) | "\(.net_hashrate) \(.block_reward) \(.block_time)"' 2>/dev/null || true)

  if [ -z "$net_line" ]; then
    # Fallback to HeroMiners network difficulty (4294967296 hashes per diff unit)
    net_line=$(curl -fsS --max-time 8 "https://pearl.herominers.com/api/stats" 2>/dev/null \
      | jq -r 'select(.network.difficulty and .lastblock.reward and .config.coinUnits and .config.coinDifficultyTarget) | "\((.network.difficulty * 4294967296) / .config.coinDifficultyTarget) \(.lastblock.reward / .config.coinUnits) \(.config.coinDifficultyTarget)"' 2>/dev/null || true)
  fi

  if [ -z "$price" ] || [ -z "$net_line" ]; then
    echo "[pricing] Failed to fetch live PEARL price/network; keeping previous dyn_min_ths=${DYN_MIN_THS}"
    return 0
  fi

  read -r net_h reward btime <<< "$net_line"
  local calc
  calc=$(awk -v p="$price" -v nh="$net_h" -v rw="$reward" -v bt="$btime" \
             -v hc="$HOURLY_COST" -v mg="$MIN_MARGIN" -v fp="$FEE_PCT" '
    BEGIN {
      if (p+0 <= 0 || nh+0 < 1e17 || rw+0 <= 0 || bt+0 <= 0) { print "ERR"; exit }
      daily_prl = (86400.0 / bt) * rw
      net_th = nh / 1e12
      usd_per_th_day = (daily_prl / net_th) * p * (1.0 - (fp / 100.0))
      if (usd_per_th_day <= 0) { print "ERR"; exit }
      dyn = ((hc * 24.0) / usd_per_th_day) * mg
      if (dyn <= 0 || dyn > 10000) { print "ERR"; exit }
      printf "%.2f %.2f", dyn, (nh / 1e18)
    }')

  if [ "$calc" = "ERR" ] || [ -z "$calc" ]; then
    echo "[pricing] Invalid live stats (price=${price} net=${net_line}); keeping dyn_min_ths=${DYN_MIN_THS}"
    return 0
  fi

  local net_eh
  read -r DYN_MIN_THS net_eh <<< "$calc"
  echo "[pricing] PEARL=\$${price} net=${net_eh} EH/s | cost=\$${HOURLY_COST}/hr margin=${MIN_MARGIN}x fee=${FEE_PCT}% -> dyn_min_ths=${DYN_MIN_THS}"
}

if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_INFO=$(nvidia-smi --query-gpu=name,driver_version,power.limit --format=csv,noheader | head -n1 || true)
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1 || true)
  DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 | tr -d '[:space:]' || true)
  echo "[boot] Host GPU: ${GPU_INFO}"

  if [ "$REJECT_5090D" = "1" ] && echo "$GPU_NAME" | grep -qi "5090 D"; then
    reallocate "RTX 5090 D detected"
  fi

  # OneZeroMiner v1.8.6 requires NVIDIA driver v580+ on Blackwell/Hopper GPUs
  if [ "$MINER" = "onezerominer" ]; then
    DRIVER_MAJOR="${DRIVER%%.*}"
    if echo "$GPU_NAME" | grep -Eqi "5090|5080|5070|5060|Blackwell|H100|H200"; then
      if [[ "$DRIVER_MAJOR" =~ ^[0-9]+$ ]] && [ "$DRIVER_MAJOR" -lt 580 ]; then
        reallocate "NVIDIA driver ${DRIVER} < 580 required by OneZeroMiner on Blackwell"
      fi
    fi
  fi
fi

refresh_dynamic_min_ths

echo "[boot] Starting miner=${MINER} -> pool=${POOL} worker=${WORKER} static_min=${MIN_THS} hourly_cost=${HOURLY_COST} margin=${MIN_MARGIN} fee=${FEE_PCT}% warmup=${WARMUP_SEC}s"
if [ "$MINER" = "wildrig" ]; then
  /miner/wildrig-multi \
    --algo pearlhash \
    --url "$POOL" \
    --user "$WALLET" \
    --pass "$PASS" \
    --worker "$WORKER" \
    --api-port 3001 \
    --print-time 30 &
  MINER_PID=$!
elif [ "$MINER" = "onezerominer" ]; then
  /miner/onezerominer \
    --disable-amd \
    -a pearlhash \
    -o "$POOL" \
    -w "$WALLET" \
    -p "$PASS" \
    --worker "$WORKER" \
    --api-host 127.0.0.1 \
    --api-port 3001 \
    --disable-telemetry &
  MINER_PID=$!
else
  echo "[error] Unsupported MINER='${MINER}'. Use 'onezerominer' or 'wildrig'."
  sleep 30
  exit 1
fi

trap 'kill "$MINER_PID" 2>/dev/null || true; exit 0' TERM INT

ELAPSED=0
while [ "$ELAPSED" -lt "$WARMUP_SEC" ]; do
  if ! kill -0 "$MINER_PID" 2>/dev/null; then
    reallocate "Miner (${MINER}) crashed during warmup"
  fi
  sleep 10
  ELAPSED=$((ELAPSED + 10))
done

ZERO_STREAK=0
LOW_STREAK=0
TICKS=0
while true; do
  if ! kill -0 "$MINER_PID" 2>/dev/null; then
    reallocate "Miner (${MINER}) process exited unexpectedly"
  fi

  if [ "$TICKS" -gt 0 ] && [ $((TICKS % 15)) -eq 0 ]; then
    refresh_dynamic_min_ths
  fi
  TICKS=$((TICKS + 1))

  RAW_HR=$(curl -fsS --max-time 5 --noproxy "*" http://127.0.0.1:3001/ 2>/dev/null \
    | jq -r '(.algos[0].total_hashrate? // .total_hashrate? // .hashrate.total[0]? // .hashrate.total[1]? // .hashrate.total[2]? // 0)' 2>/dev/null || echo "0")
  RAW_HR="${RAW_HR:-0}"

  THS=$(awk -v hr="$RAW_HR" 'BEGIN { printf "%.2f", (hr + 0) / 1e12 }')
  EFF_MIN=$(awk -v s="$MIN_THS" -v d="$DYN_MIN_THS" 'BEGIN { printf "%.2f", (d + 0 > s + 0) ? d + 0 : s + 0 }')
  echo "[watchdog] miner=${MINER} raw_hr=${RAW_HR} (~${THS} TH/s) | eff_min=${EFF_MIN} TH/s (static=${MIN_THS} dyn=${DYN_MIN_THS})"

  IS_ZERO=$(awk -v th="$THS" 'BEGIN { print (th + 0 <= 0.01) ? 1 : 0 }')
  if [ "$IS_ZERO" -eq 1 ]; then
    ZERO_STREAK=$((ZERO_STREAK + 1))
    if [ "$ZERO_STREAK" -ge 2 ]; then
      kill "$MINER_PID" 2>/dev/null || true
      reallocate "Dead/firewalled node: 0.00 TH/s for 2 checks"
    fi
  else
    ZERO_STREAK=0
  fi

  IS_LOW=$(awk -v th="$THS" -v min="$EFF_MIN" 'BEGIN { print (min + 0 > 0 && th + 0 < min + 0) ? 1 : 0 }')
  if [ "$IS_LOW" -eq 1 ]; then
    LOW_STREAK=$((LOW_STREAK + 1))
    if [ "$LOW_STREAK" -ge 3 ]; then
      kill "$MINER_PID" 2>/dev/null || true
      reallocate "Sustained low hashrate: ${THS} TH/s < ${EFF_MIN} TH/s"
    fi
  else
    LOW_STREAK=0
  fi

  sleep 60
done
