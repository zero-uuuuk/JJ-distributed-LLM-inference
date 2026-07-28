# PR5 실행 스크립트

## 1. 공통 준비

<details>
<summary>실행 스크립트 보기</summary>

```bash
export VLLM_DIR=/home/ubuntu/vllm
export JJ_ROOT=/home/ubuntu/JJ-distributed-LLM-inference

cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

mkdir -p $JJ_ROOT/static/PR5
```

</details>

워크로드는 PR2에서 생성한 파일을 그대로 사용한다.

```text
$JJ_ROOT/workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl
$JJ_ROOT/workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl
$JJ_ROOT/workloads/traj/traj_agent_100session_10step.jsonl
```

## 2. PR5-5 단위 테스트

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

python -m pytest tests/v1/core/test_quota_serve_pr5_contract.py -q
```

</details>

## 3. Baseline 서버 실행: `mode=off`

이전 서버를 종료한 뒤 실행한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
QUOTA_SERVE_CONFIG=$JJ_ROOT/static/quota_serve.yaml \
QUOTA_SERVE_MODE=off \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR5/pr5_eviction_chat_longctx_off.jsonl \
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

## 4. Baseline Chat+Longctx 실행

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
  --output static/raw_results/pr5_chat_longctx_off.jsonl
```

</details>

## 5. Baseline eviction log flush

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```

</details>

서버 터미널에서 `Ctrl+C`로 종료한다.

## 6. Static 서버 실행: signal log ON

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
QUOTA_SERVE_CONFIG=$JJ_ROOT/static/quota_serve.yaml \
QUOTA_SERVE_MODE=static \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR5/pr5_eviction_chat_longctx_static.jsonl \
QUOTA_SERVE_LOG=$JJ_ROOT/static/PR5/pr5_signal_chat_longctx_static.jsonl \
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

서버 로그에서 다음을 확인한다.

```text
QuotaServe config loaded ... mode=static ... active=True
```

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
  --output static/raw_results/pr5_chat_longctx_static.jsonl
```

</details>

## 8. Static log flush와 signal 확인

실험이 끝난 뒤 서버가 살아 있는 상태에서 실행한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log

grep '"type":"useful_eviction_signal"' \
  $JJ_ROOT/static/PR5/pr5_signal_chat_longctx_static.jsonl | head
```

</details>

## 9. Static Chat+Agent 실행

Chat+Longctx 서버를 종료하고 같은 방식으로 서버를 재실행하되 로그 파일만
Agent용으로 바꾼다.

<details>
<summary>서버 실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
QUOTA_SERVE_CONFIG=$JJ_ROOT/static/quota_serve.yaml \
QUOTA_SERVE_MODE=static \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR5/pr5_eviction_chat_agent_static.jsonl \
QUOTA_SERVE_LOG=$JJ_ROOT/static/PR5/pr5_signal_chat_agent_static.jsonl \
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
<summary>클라이언트 실행 스크립트 보기</summary>

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
  --output static/raw_results/pr5_chat_agent_static.jsonl
```

</details>

## 10. Chat+Agent signal flush와 확인

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log

grep '"type":"useful_eviction_signal"' \
  $JJ_ROOT/static/PR5/pr5_signal_chat_agent_static.jsonl | head
```

</details>
