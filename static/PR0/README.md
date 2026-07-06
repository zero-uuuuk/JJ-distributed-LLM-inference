# PR0 APC ON Baseline Run

PR0에서는 static quota를 켜지 않고, `quota_mode=off` 상태에서 APC ON baseline과 eviction log가 정상적으로 남는지 확인한다.

Smoke test는 실행하지 않는다. 같은 trace로 smoke를 먼저 돌리면 prefix cache가 데워져 본 실험의 초반 cache hit이 오염될 수 있다.

## 1. 공통 준비

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

pip install -r requirements.txt
mkdir -p static/raw_results
mkdir -p static/eviction_logs
```

## 2. Workload 생성

Chat workload가 이미 있으면 생략한다.

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/workloads/sharegpt

python build_sharegpt_workload.py \
  --num-conversations 100 \
  --min-turns 10 \
  --max-turns 10 \
  --order turn-major \
  --output sharegpt_victim_100conv_10turn.jsonl \
  --tokenizer meta-llama/Llama-3.2-3B-Instruct \
  --max-output-tokens 691
```

Longctx workload:

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/workloads/hotpotqa

python build_hotpotqa_workload.py \
  --output hotpotqa_longctx_2000_4000.jsonl \
  --num-requests 1000 \
  --min-prompt-tokens 2000 \
  --max-prompt-tokens 4000 \
  --tokenizer meta-llama/Llama-3.2-3B-Instruct \
  --max-output-tokens 41
```

Agent workload가 필요하면 생성한다.

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/workloads/traj

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

```bash
wc -l traj_agent_100session_10step.jsonl
```

정상이라면 `1000`줄이 나온다.

## 3. Chat + Longctx 서버 실행

서버 터미널에서 실행한다.

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
VLLM_EVICTION_LOG=/home/ubuntu/JJ-distributed-LLM-inference/static/eviction_logs/pr0_eviction_chat_longctx_apc_on_len8192.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --generation-config vllm \
  --port 8000
```

## 4. 서버 확인

새 터미널에서 실행한다. `/v1/models` 확인은 prefix cache를 데우지 않는다.

```bash
curl http://127.0.0.1:8000/v1/models
```

## 5. Chat + Longctx 본 실행

클라이언트 터미널에서 실행한다.

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

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
  --output static/raw_results/pr0_chat_longctx_apc_on_len8192.jsonl
```

## 6. Chat + Longctx eviction log flush

본 실행이 끝난 뒤 vLLM 서버가 살아있는 상태에서 실행한다.

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```

`num_flushed`가 0보다 크면 pending eviction event가 파일로 기록된 것이다.

```bash
grep '"reused_later": false' /home/ubuntu/JJ-distributed-LLM-inference/static/eviction_logs/pr0_eviction_chat_longctx_apc_on_len8192.jsonl | head
```

## 7. Chat + Longctx 산출물

```text
/home/ubuntu/JJ-distributed-LLM-inference/static/raw_results/pr0_chat_longctx_apc_on_len8192.jsonl
/home/ubuntu/JJ-distributed-LLM-inference/static/raw_results/pr0_chat_longctx_apc_on_len8192_summary.json
/home/ubuntu/JJ-distributed-LLM-inference/static/eviction_logs/pr0_eviction_chat_longctx_apc_on_len8192.jsonl
```

Chat+Longctx는 PR0 필수 baseline이다.

## 8. Chat + Agent 서버 실행

Chat+Longctx 실험이 끝난 뒤 vLLM 서버를 `Ctrl+C`로 종료하고 새로 실행한다. 기존 cache 상태와 eviction log가 섞이면 안 되므로 eviction log path도 Chat+Agent용으로 바꾼다.

서버 터미널에서 실행한다.

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
VLLM_EVICTION_LOG=/home/ubuntu/JJ-distributed-LLM-inference/static/eviction_logs/pr0_eviction_chat_agent_apc_on_len8192.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --generation-config vllm \
  --port 8000
```

새 터미널에서 서버를 확인한다.

```bash
curl http://127.0.0.1:8000/v1/models
```

## 9. Chat + Agent 본 실행

클라이언트 터미널에서 실행한다.

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

python static/run_mixed_agent_c2.py \
  --quota-mode off \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --agent-target-rps 5.0 \
  --agent-steps-per-session 10.0 \
  --phase chat_agent_exponential \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 200 \
  --output static/raw_results/pr0_chat_agent_apc_on_len8192.jsonl
```

## 10. Chat + Agent eviction log flush

본 실행이 끝난 뒤 vLLM 서버가 살아있는 상태에서 실행한다.

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```

```bash
grep '"reused_later": false' /home/ubuntu/JJ-distributed-LLM-inference/static/eviction_logs/pr0_eviction_chat_agent_apc_on_len8192.jsonl | head
```

## 11. Chat + Agent 산출물

```text
/home/ubuntu/JJ-distributed-LLM-inference/static/raw_results/pr0_chat_agent_apc_on_len8192.jsonl
/home/ubuntu/JJ-distributed-LLM-inference/static/raw_results/pr0_chat_agent_apc_on_len8192_summary.json
/home/ubuntu/JJ-distributed-LLM-inference/static/eviction_logs/pr0_eviction_chat_agent_apc_on_len8192.jsonl
```

Chat+Agent는 warm antagonist까지 확인하기 위한 추가 baseline이다.

## 12. 주의 사항

```text
1. PR0에서는 QUOTA_SERVE_CONFIG, QUOTA_SERVE_MODE=static을 사용하지 않는다.
2. Chat+Longctx와 Chat+Agent 사이에는 vLLM 서버를 재시작한다.
3. 서버를 끄기 전에 반드시 /flush_eviction_log를 호출한다.
4. 결과 분석 수치는 PR0_validation.md 또는 QUOTASERVE_PR0_BASELINE_OBSERVATION.md에 기록한다.
```
