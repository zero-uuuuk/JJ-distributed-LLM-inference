#!/usr/bin/env bash
# QuotaServe static — vLLM 서버 런처 (plan §8.4)
#
# QUOTA_SERVE_MODE만 바꿔 baseline(off) / static을 같은 명령으로 띄운다.
# 서버는 foreground 장기 실행이므로, 이 스크립트로 서버를 띄운 뒤 다른 터미널에서
# run_static.sh로 클라이언트를 실행한다.
#
# 사용:
#   ./server_static.sh <mix> <mode>
#     mix  : longctx | agent   (기본 longctx)   — context length가 달라짐
#     mode : off | static      (기본 static)
#
# 환경 변수(override 가능):
#   VLLM_DIR           vLLM fork 경로            (기본 ~/vllm)
#   QUOTA_SERVE_CONFIG quota_serve.yaml 경로     (기본 $VLLM_DIR/vllm/quota_serve/quota_serve.yaml)
#   MODEL              모델 id
set -euo pipefail

MIX="${1:-longctx}"
MODE="${2:-static}"

STATIC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VLLM_DIR="${VLLM_DIR:-$HOME/vllm}"
MODEL="${MODEL:-meta-llama/Llama-3.2-3B-Instruct}"
QUOTA_SERVE_CONFIG="${QUOTA_SERVE_CONFIG:-$VLLM_DIR/vllm/quota_serve/quota_serve.yaml}"

case "$MIX" in
  longctx) MAXLEN=8192 ;;    # §2 고정값
  agent)   MAXLEN=12288 ;;   # §8.4.2: agent trajectory는 context가 길다
  *) echo "unknown mix: $MIX (longctx|agent)"; exit 1 ;;
esac

LOG_DIR="$STATIC_DIR/eviction_logs"
mkdir -p "$LOG_DIR"
EVICTION_LOG="$LOG_DIR/eviction_${MODE}_chat_${MIX}.jsonl"
QUOTA_LOG="$LOG_DIR/quota_${MODE}_chat_${MIX}.jsonl"

echo "mix=$MIX mode=$MODE max_model_len=$MAXLEN"
echo "config=$QUOTA_SERVE_CONFIG"
echo "eviction_log=$EVICTION_LOG"
echo "quota_log=$QUOTA_LOG"

cd "$VLLM_DIR"

VLLM_SERVER_DEV_MODE=1 \
VLLM_EVICTION_LOG="$EVICTION_LOG" \
QUOTA_SERVE_MODE="$MODE" \
QUOTA_SERVE_CONFIG="$QUOTA_SERVE_CONFIG" \
QUOTA_SERVE_LOG="$QUOTA_LOG" \
vllm serve "$MODEL" \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len "$MAXLEN" \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
