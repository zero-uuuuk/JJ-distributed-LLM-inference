<div align="center">

# Case 1 Validation

**run_mixed.py 실행 방법**

_Mixed workload · APC ON/OFF · Chat + RAG/Longctx · Shared cache_

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

VLLM_EVICTION_LOG=/home/ubuntu/vllm/eviction_logs/mixed_chat5_rag5_apc_on_len8192.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

> [!WARNING]
> `VLLM_EVICTION_LOG`를 켠 경우, 실험이 끝난 뒤 vLLM 서버는 반드시 `Ctrl+C`로 종료하고 잠시 기다려야 합니다.
> vLLM은 pending eviction 이벤트를 메모리에 들고 있다가 종료 시점에 `reused_later=false` 이벤트를 파일로 flush합니다.
> 종료 후에는 eviction log에 `"reused_later": false`가 기록되었는지 반드시 확인합니다.
> 바로 세션을 끊거나 강제 종료하면 eviction log가 완전히 집계되지 않을 수 있습니다.

```bash
grep '"reused_later": false' /home/ubuntu/vllm/eviction_logs/mixed_chat5_rag5_apc_on_len8192.jsonl | head
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

### 3. Chat + RAG Mixed 실행

<details>
<summary>APC ON 서버가 떠 있을 때</summary>

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

python case1_validation/run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --rag-trace ../workloads/msmarco/msmarco_v21_validation.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --rag-qps 5.0 \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-rag-prompts 1000 \
  --chat-slo-ms 400 \
  --rag-slo-ms 400 \
  --output case1_validation/raw_results/mixed_chat5_rag5_apc_on_len8192.jsonl
```

</details>

<details>
<summary>APC OFF 서버로 재시작한 뒤</summary>

```bash
python case1_validation/run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --rag-trace ../workloads/msmarco/msmarco_v21_validation.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --rag-qps 5.0 \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-rag-prompts 1000 \
  --chat-slo-ms 400 \
  --rag-slo-ms 400 \
  --output case1_validation/raw_results/mixed_chat5_rag5_apc_off_len8192.jsonl
```

</details>

### 4. Chat + Longctx Mixed 실행

`--longctx-trace`를 지정하면 `run_mixed.py`는 RAG 대신 Longctx를 두 번째 workload로 사용합니다. 이때 요청의 `user` 필드는 `chat` 또는 `longctx`로 들어갑니다.

<details>
<summary>APC ON 서버가 떠 있을 때</summary>

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

python case1_validation/run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --longctx-trace ../workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --longctx-qps 5.0 \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-longctx-prompts 1000 \
  --chat-slo-ms 400 \
  --longctx-slo-ms 400 \
  --output case1_validation/raw_results/mixed_chat5_longctx5_apc_on_len8192.jsonl
```

</details>

<details>
<summary>APC OFF 서버로 재시작한 뒤</summary>

```bash
python case1_validation/run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --longctx-trace ../workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --longctx-qps 5.0 \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-longctx-prompts 1000 \
  --chat-slo-ms 400 \
  --longctx-slo-ms 400 \
  --output case1_validation/raw_results/mixed_chat5_longctx5_apc_off_len8192.jsonl
```

</details>

### 5. 측정값

Mixed 결과 해석은 콘솔 출력보다 `*_summary.json` 위주로 보는 것을 권장합니다.

특히 아래 값을 먼저 확인하면 됩니다.

| 항목 | 의미 |
|---|---|
| `chat.ttft_ms_p50/p95/p99` | Chat 요청의 TTFT 분포 |
| `rag.ttft_ms_p50/p95/p99` | RAG 요청의 TTFT 분포 |
| `longctx.ttft_ms_p50/p95/p99` | Longctx 요청의 TTFT 분포 |
| `chat.slo_attainment` | `TTFT <= chat_slo_ms` 비율 |
| `rag.slo_attainment` | `TTFT <= rag_slo_ms` 비율 |
| `longctx.slo_attainment` | `TTFT <= longctx_slo_ms` 비율 |
| `chat.hit_rate_mean` | Chat prefix cache hit rate 평균 |
| `rag.hit_rate_mean` | RAG prefix cache hit rate 평균 |
| `longctx.hit_rate_mean` | Longctx prefix cache hit rate 평균 |
| `chat.tpot_ms_mean` | Chat decode 구간 평균 token latency |
| `rag.tpot_ms_mean` | RAG decode 구간 평균 token latency |
| `longctx.tpot_ms_mean` | Longctx decode 구간 평균 token latency |

### 6. vLLM eviction 계측

`VLLM_EVICTION_LOG=/path/to/file.jsonl`를 주면 vLLM이 eviction 이벤트를 JSONL로 기록합니다.

`run_mixed.py`는 요청의 `user` 필드에 `chat`과 antagonist workload 태그(`rag` 또는 `longctx`)를 넣고, vLLM은 이를 내부 `workload_tag`로 사용합니다.

따라서 eviction 로그에서는 아래를 보면 됩니다.

| 항목 | 의미 |
|---|---|
| `evicted_workload="chat"` and `trigger_workload="rag"` | RAG가 Chat cached prefix를 밀어낸 경우 |
| `evicted_workload="chat"` and `trigger_workload="longctx"` | Longctx가 Chat cached prefix를 밀어낸 경우 |
| `reused_later=true` | evict된 block이 나중에 다시 재사용된 경우 |
| `time_until_next_reuse` | eviction 이후 다음 재사용까지 걸린 시간 |

---

<div align="center">
<sub>Case 1 Validation · run_mixed.py · JJ Distributed LLM Inference</sub>
</div>
