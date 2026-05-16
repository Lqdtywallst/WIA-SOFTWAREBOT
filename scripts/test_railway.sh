#!/bin/sh
# Quick check: Railway bot health + sample webhook (paper).
set -e
BASE="${1:-https://wia-softwarebot-production.up.railway.app}"
echo "=== GET $BASE/health ==="
curl -sS "$BASE/health" | python3 -m json.tool 2>/dev/null || curl -sS "$BASE/health"
echo ""
echo "=== POST $BASE/webhook ==="
curl -sS -X POST "$BASE/webhook" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"ZECUSDT","side":"short","price":489,"bias":"BAJISTA","state":"TENDENCIA","long_score":2,"short_score":8,"dist_vwap_atr":2,"timeframe":"5"}' \
  | python3 -m json.tool 2>/dev/null || true
echo ""
