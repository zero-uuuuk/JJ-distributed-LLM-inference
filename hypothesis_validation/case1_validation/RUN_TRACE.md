<div align="center">

# Case 1 Validation

**run_trace.py 실행 방법**

_Single workload · APC ON/OFF · Chat-only · RAG-only · Longctx-only · Agent-only_

</div>

---

## 실행 방법

### 1. 공통 준비

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

pip install -r requirements.txt
mkdir -p hypothesis_validation/case1_validation/raw_results
```

### 2. vLLM 서버 실행

<details>
<summary>APC ON</summary>

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

</details>

<details>
<summary>APC OFF</summary>

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --no-enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

</details>

> ON/OFF arm 사이에는 서버를 재시작해 cache/queue 상태를 초기화합니다.

### 3. Chat-only 실행

<details>
<summary>APC ON 서버가 떠 있을 때</summary>

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

python case1_validation/run_trace.py \
  --trace ../workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --api chat \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --workload-tag chat \
  --qps 5.0 \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 400 \
  --output case1_validation/raw_results/chat_only_apc_on.jsonl
```

</details>

<details>
<summary>APC OFF 서버로 재시작한 뒤</summary>

```bash
python case1_validation/run_trace.py \
  --trace ../workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --api chat \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --workload-tag chat \
  --qps 5.0 \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 400 \
  --output case1_validation/raw_results/chat_only_apc_off.jsonl
```

</details>

### 4. RAG-only 실행

<details>
<summary>APC ON 서버가 떠 있을 때</summary>

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

python case1_validation/run_trace.py \
  --trace ../workloads/msmarco/msmarco_v21_validation.jsonl \
  --api chat \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --workload-tag rag \
  --qps 5.0 \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 400 \
  --output case1_validation/raw_results/rag_only_apc_on.jsonl
```

</details>

<details>
<summary>APC OFF 서버로 재시작한 뒤</summary>

```bash
python case1_validation/run_trace.py \
  --trace ../workloads/msmarco/msmarco_v21_validation.jsonl \
  --api chat \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --workload-tag rag \
  --qps 5.0 \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 400 \
  --output case1_validation/raw_results/rag_only_apc_off.jsonl
```

</details>

### 5. Longctx-only 실행

<details>
<summary>APC ON 서버가 떠 있을 때</summary>

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

python case1_validation/run_trace.py \
  --trace ../workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl \
  --api chat \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --workload-tag longctx \
  --qps 5.0 \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 7700 \
  --output case1_validation/raw_results/longctx_only_apc_on.jsonl
```

</details>

<details>
<summary>APC OFF 서버로 재시작한 뒤</summary>

```bash
python case1_validation/run_trace.py \
  --trace ../workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl \
  --api chat \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --workload-tag longctx \
  --qps 5.0 \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 7700 \
  --output case1_validation/raw_results/longctx_only_apc_off.jsonl
```

</details>

### 6. Agent-only 실행

<details>
<summary>APC ON 서버가 떠 있을 때</summary>

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

python case1_validation/run_trace.py \
  --trace ../workloads/traj/traj_agent_100session_10step.jsonl \
  --api chat \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --workload-tag agent \
  --scheduler agent-session \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode exponential \
  --agent-tool-gap-mean 2 \
  --agent-tool-gap-max 20 \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 10000 \
  --output case1_validation/raw_results/agent_only_exp2_cap20_apc_on_len8192.jsonl
```

</details>

<details>
<summary>APC OFF 서버로 재시작한 뒤</summary>

```bash
python case1_validation/run_trace.py \
  --trace ../workloads/traj/traj_agent_100session_10step.jsonl \
  --api chat \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --workload-tag agent \
  --scheduler agent-session \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode exponential \
  --agent-tool-gap-mean 2 \
  --agent-tool-gap-max 20 \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 10000 \
  --output case1_validation/raw_results/agent_only_exp2_cap20_apc_off_len8192.jsonl
```

</details>

### 7. 측정값

결과 해석은 요청별 raw 결과보다 `*_summary.json` 기준으로 보는 것을 권장합니다.

| 항목 | 의미 |
|---|---|
| `n_total`, `n_ok`, `n_failed` | 전체/성공/실패 요청 수 |
| `throughput_req_per_s`, `throughput_tok_per_s` | 요청/토큰 처리량 |
| `slo_attainment` | `TTFT <= slo_ms` 비율 |
| `hit_rate_mean`, `hit_rate_p50` | cache hit rate 집계 |
| `ttft_ms_*` | TTFT mean/p50/p95/p99 |
| `tpot_ms_*` | TPOT mean/p50/p95/p99 |
| `itl_ms_*` | ITL mean/p50/p95/p99 |
| `e2e_ms_*` | E2E mean/p50/p95/p99 |

---

<div align="center">
<sub>Case 1 Validation · run_trace.py · JJ Distributed LLM Inference</sub>
</div>
