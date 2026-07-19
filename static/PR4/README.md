# PR4 실행 스크립트

## 1. 공통 준비

<details>
<summary>실행 스크립트 보기</summary>

```bash
export VLLM_DIR=/home/ubuntu/vllm
export JJ_ROOT=/home/ubuntu/JJ-distributed-LLM-inference

cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

mkdir -p $JJ_ROOT/static/PR4
```
</details>

## 2. Workload 생성

이미 생성된 workload가 있으면 생략한다.

### Chat

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT/workloads/sharegpt

python build_sharegpt_workload.py \
  --num-conversations 100 \
  --min-turns 10 \
  --max-turns 10 \
  --order turn-major \
  --output sharegpt_victim_100conv_10turn.jsonl \
  --tokenizer meta-llama/Llama-3.2-3B-Instruct \
  --max-output-tokens 691
```
</details>

### Longctx

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT/workloads/hotpotqa

python build_hotpotqa_workload.py \
  --output hotpotqa_longctx_2000_4000.jsonl \
  --num-requests 1000 \
  --min-prompt-tokens 2000 \
  --max-prompt-tokens 4000 \
  --tokenizer meta-llama/Llama-3.2-3B-Instruct \
  --max-output-tokens 41
```
</details>

### Agent

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT/workloads/traj

python build_traj_agent_workload.py \
  --dataset-id yoonholee/terminalbench-trajectories \
  --config default \
  --split train \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --max-prompt-chars 24000 \
  --output traj_agent_100session_10step.jsonl \
  --tokenizer meta-llama/Llama-3.2-3B-Instruct \
  --max-output-tokens 1776
```
</details>

## 3. Baseline 서버 실행

각 모드 사이에는 서버를 종료하고 다시 실행한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
QUOTA_SERVE_CONFIG=$JJ_ROOT/static/quota_serve.yaml \
QUOTA_SERVE_MODE=off \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR4/pr4_eviction_chat_longctx_off.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --generation-config vllm \
  --port 8000
```
</details>

## 4. 서버 확인

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl http://127.0.0.1:8000/v1/models
```
</details>

## 5. Baseline Chat+Longctx 실행

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT
source $JJ_ROOT/.venv/bin/activate

python static/run_mixed_c2.py \
  --quota-mode off \
  --longctx-trace workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --longctx-qps 5.0 \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-longctx-prompts 1000 \
  --chat-slo-ms 400 \
  --longctx-slo-ms 7700 \
  --output static/raw_results/pr4_chat_longctx_off.jsonl
```
</details>

실험 후 서버가 살아 있는 상태에서 실행한다.

<details>
<summary>flush 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```
</details>

## 6. Static 서버 실행

Baseline 서버를 `Ctrl+C`로 종료한 뒤 실행한다.

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
QUOTA_SERVE_CONFIG=$JJ_ROOT/static/quota_serve.yaml \
QUOTA_SERVE_MODE=static \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR4/pr4_eviction_chat_longctx_static.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --generation-config vllm \
  --port 8000
```

서버 로그에서 다음과 같이 확인한다.

<details>
<summary>로그 예시 보기</summary>

```text
QuotaServe config loaded ... mode=static ... active=True
```
</details>

## 7. Static Chat+Longctx 실행

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT
source $JJ_ROOT/.venv/bin/activate

python static/run_mixed_c2.py \
  --quota-mode static \
  --longctx-trace workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --longctx-qps 5.0 \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-longctx-prompts 1000 \
  --chat-slo-ms 400 \
  --longctx-slo-ms 7700 \
  --output static/raw_results/pr4_chat_longctx_static.jsonl
```
</details>

<details>
<summary>flush 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```
</details>

## 8. Static eviction log 확인

<details>
<summary>검증 스크립트 보기</summary>

```bash
cd $JJ_ROOT

python - <<'PY'
import json
from collections import Counter
from pathlib import Path

path = Path("static/PR4/pr4_eviction_chat_longctx_static.jsonl")
required = {
    "selection_reason",
    "victim_occupancy",
    "victim_quota",
    "occupancy_snapshot",
    "scan_steps",
    "is_cross_workload",
}
allowed_reasons = {
    "over_quota_selected",
    "fallback_no_over_quota",
    "baseline_lru_off_mode",
    "external_eviction",
}

if not path.exists() or path.stat().st_size == 0:
    raise SystemExit("eviction log is empty")

reasons = Counter()
rows = 0
with path.open("r", encoding="utf-8") as f:
    for line in f:
        row = json.loads(line)
        rows += 1
        missing = required - row.keys()
        assert not missing, ("missing fields", missing)
        reasons[row["selection_reason"]] += 1
        assert row["selection_reason"] in allowed_reasons

print("events:", rows)
print("selection reasons:", dict(reasons))
print("PR4 eviction log schema: PASS")
PY
```
</details>

## 9. Static Chat+Agent 실행

Static 서버를 다시 시작하고, `VLLM_EVICTION_LOG` 경로를 Agent용으로 바꾼다.

<details>
<summary>서버 실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
QUOTA_SERVE_CONFIG=$JJ_ROOT/static/quota_serve.yaml \
QUOTA_SERVE_MODE=static \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR4/pr4_eviction_chat_agent_static.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --generation-config vllm \
  --port 8000
```
</details>

<details>
<summary>부하 실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT
source $JJ_ROOT/.venv/bin/activate

python static/run_mixed_agent_c2.py \
  --quota-mode static \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --phase chat_agent_exponential \
  --chat-qps 5.0 \
  --agent-target-rps 5.0 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode exponential \
  --agent-tool-gap-mean 2 \
  --agent-tool-gap-max 20 \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 200 \
  --output static/raw_results/pr4_chat_agent_static.jsonl
```
</details>

<details>
<summary>flush 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```
</details>
