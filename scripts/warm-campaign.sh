#!/usr/bin/env zsh
# Warm-cache data campaign: 4 sequential builds per fleet (medium ∥ large),
# all TheBudget @ main. Sequential within a fleet so each run warms the next,
# exercising the size-scoped S3 key + git-SHA skip-save gate. Invokes the
# Lambda directly so compute_size routes large to the large fleet (the gateway
# MCP schema doesn't expose compute_size yet).
set -u
PROFILE=tycenj-prod
REGION=us-east-1
BUCKET=ios-agent-test-artifacts-838829463875
FN=codebuild-ios-mcp
OUT=/Users/tycenj/Desktop/codebuild-ios-mcp-agentcore-hub/data/raw-runs/warm-campaign-$(date +%Y%m%d-%H%M).log

start() {  # size -> build_id
  local size=$1 payload tmp bid
  payload=$(printf '{"tool":"ios_test","arguments":{"branch":"main","scheme":"TheBudget","repo":"https://github.com/tycenjmccann/TheBudget","project_dir":"ios","compute_size":"%s"}}' "$size")
  tmp=$(mktemp)
  AWS_PROFILE=$PROFILE aws lambda invoke --function-name $FN --region $REGION \
    --cli-binary-format raw-in-base64-out --payload "$payload" "$tmp" >/dev/null 2>&1
  bid=$(python3 -c "import json,sys
try: print(json.load(open('$tmp')).get('build_id',''))
except: pass")
  rm -f "$tmp"
  echo "$bid"
}

wait_done() {  # build_id
  local bid=$1 s
  while :; do
    s=$(AWS_PROFILE=$PROFILE aws codebuild batch-get-builds --ids "$bid" --region $REGION \
        --query 'builds[0].buildStatus' --output text 2>/dev/null)
    [ "$s" != "IN_PROGRESS" ] && { echo "$s"; return; }
    sleep 30
  done
}

probe() {  # size label build_id status  -> append one summary line
  local size=$1 label=$2 bid=$3 st=$4
  local log m line
  log=$(AWS_PROFILE=$PROFILE aws s3 cp "s3://$BUCKET/builds/$bid/build_output.log" - --region $REGION 2>/dev/null)
  m=$(AWS_PROFILE=$PROFILE aws s3 cp "s3://$BUCKET/builds/$bid/metrics.json" - --region $REGION 2>/dev/null)
  line=$(printf '%s' "$log" | grep -iE "Saving warm cache|Skipping warm cache" | tail -1)
  echo "[$size $label] $bid status=$st | $line" >> "$OUT"
  echo "    metrics: $m" >> "$OUT"
}

track() {  # size  -- 4 sequential builds
  local size=$1 i bid st
  for i in 1 2 3 4; do
    bid=$(start "$size")
    echo "$(date +%H:%M:%S) [$size run$i] started $bid" >> "$OUT"
    [ -z "$bid" ] && { echo "[$size run$i] FAILED TO START" >> "$OUT"; continue; }
    st=$(wait_done "$bid")
    probe "$size" "run$i" "$bid" "$st"
  done
  echo "$(date +%H:%M:%S) [$size] TRACK COMPLETE" >> "$OUT"
}

echo "warm-campaign start $(date)" > "$OUT"
echo "OUT=$OUT"
track medium &
MPID=$!
track large &
LPID=$!
wait $MPID $LPID
echo "warm-campaign done $(date)" >> "$OUT"
echo "ALL DONE -> $OUT"
