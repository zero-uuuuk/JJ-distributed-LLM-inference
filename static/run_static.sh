#!/usr/bin/env bash
# QuotaServe static — 클라이언트 런처 (plan §8.4)
#
# server_static.sh로 서버를 띄운 뒤, 다른 터미널에서 실행한다. 같은 mix/mode를
# 서버와 맞춰서 호출한다. 출력은 static/raw_results/c2_<mode>_chat_<mix>_apc_on.jsonl.
#
# 사용:
#   ./run_static.sh <mix> <mode>
#     mix  : longctx | agent   (기본 longctx)
#     mode : off | static      (기본 static)  — 라벨링용. 실제 정책은 서버 env로 켜짐.
#
# 환경 변수(override 가능):
#   JJ_ROOT  workload trace가 있는 JJ repo 경로 (기본 ~/JJ-distributed-LLM-inference)
#   URL      vLLM chat completions URL
#   PYTHON   python 실행기 (기본 python)
set -euo pipefail

MIX="${1:-longctx}"
MODE="${2:-static}"

STATIC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="${URL:-http://127.0.0.1:8000/v1/chat/completions}"
PYTHON="${PYTHON:-python}"
export JJ_ROOT="${JJ_ROOT:-$HOME/JJ-distributed-LLM-inference}"

echo "mix=$MIX mode=$MODE JJ_ROOT=$JJ_ROOT"

case "$MIX" in
  longctx)
    # §8.4.1 Chat + Longctx (cold antagonist). --longctx-trace로 longctx 선택.
    "$PYTHON" "$STATIC_DIR/run_mixed_c2.py" \
      --quota-mode "$MODE" \
      --longctx-trace \
      --chat-qps 5 \
      --longctx-qps 5 \
      --num-chat-prompts 1000 \
      --num-longctx-prompts 1000 \
      --chat-slo-ms 400 \
      --longctx-slo-ms 7700 \
      --max-concurrency 32 \
      --url "$URL"
    ;;
  agent)
    # §8.4.2 Chat + Agent (warm antagonist, delayed self-reuse).
    "$PYTHON" "$STATIC_DIR/run_mixed_agent_c2.py" \
      --quota-mode "$MODE" \
      --phase chat_agent_exponential \
      --chat-qps 5 \
      --agent-target-rps 5 \
      --agent-steps-per-session 10 \
      --agent-tool-gap-mode exponential \
      --agent-tool-gap-mean 2 \
      --agent-tool-gap-max 20 \
      --num-chat-prompts 1000 \
      --num-agent-prompts 1000 \
      --chat-slo-ms 400 \
      --agent-slo-ms 10000 \
      --max-concurrency 32 \
      --url "$URL"
    ;;
  *)
    echo "unknown mix: $MIX (longctx|agent)"; exit 1 ;;
esac
