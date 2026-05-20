<div align="center">

# Hypothesis Validation

**Cache Pollution 가설 검증 실험 스크립트**

_Mixed workload · Prefix KV cache · SLO attainment_

</div>

---

## 개요

Mixed workload 환경에서 RAG(SQuAD)의 prefix KV cache가 shared cache pool을 점유해 Chat(ShareGPT)의 reusable KV를 evict시키고, Chat의 TTFT · SLO attainment가 isolated 실행 대비 악화된다는 가설을 검증합니다.

가설 상세 및 실험 설계 → [`HYPOTHESIS.md`](HYPOTHESIS.md)

---

## 파일 구성

| 파일 | 설명 |
|---|---|
| `run_trace.py` | 단일 workload trace 전송 (isolated 실험용) |
| `run_mixed.py` | Chat + RAG 두 workload 동시 전송 (mixed 실험용) |
| `HYPOTHESIS.md` | 가설 정의 · 측정 지표 · 실험 케이스 설계 |

---

## 실험 케이스

| Case | 스크립트 | 설명 |
|---|---|---|
| Case 1: Chat-only | `run_trace.py --workload-tag chat` | baseline — Chat 단독 실행 |
| Case 2: RAG-only | `run_trace.py --workload-tag rag` | baseline — RAG 단독 실행 |
| Case 3: Mixed | `run_mixed.py` | Chat + RAG 동시 실행, pollution 측정 |

Case 1·2의 결과와 Case 3을 비교해 cache pollution을 정량화합니다.

---

## 사용법

실험은 보통 두 개 터미널을 사용합니다.

| 터미널 | 역할 |
|---|---|
| Terminal 1 | vLLM 서버 실행 |
| Terminal 2 | workload 전송 (`run_trace.py` / `run_mixed.py`) |

아래 예시는 다음 경로를 가정합니다.

| 항목 | 경로 |
|---|---|
| JJ repo | `/home/ubuntu/JJ-Distributed-LLM-Inference` |
| vLLM repo | `/home/ubuntu/vllm` |
| vLLM venv | `/home/ubuntu/vllm/.venv` |
| JJ runner venv | `/home/ubuntu/JJ-Distributed-LLM-Inference/.venv` |

서버는 모든 run에서 `--max-model-len 8192`로 고정합니다.

### 0. 공통 준비

Terminal 2에서 최초 1회 실행합니다.

```bash
cd /home/ubuntu/JJ-Distributed-LLM-Inference

export PATH="$HOME/.local/bin:$PATH"

uv venv --python 3.12
source /home/ubuntu/JJ-Distributed-LLM-Inference/.venv/bin/activate

uv pip install -r requirements.txt

python -c "import aiohttp, numpy, tqdm; print('runner deps ok')"
```

워크로드가 아직 없으면 생성합니다.

```bash
cd /home/ubuntu/JJ-Distributed-LLM-Inference/workloads/squad
source /home/ubuntu/JJ-Distributed-LLM-Inference/.venv/bin/activate

python build_rag_workload.py \
  --dataset-name rajpurkar/squad \
  --subset plain_text \
  --split validation \
  --num-requests 5000 \
  --output squad_validation.jsonl
```

```bash
cd /home/ubuntu/JJ-Distributed-LLM-Inference/workloads/sharegpt
source /home/ubuntu/JJ-Distributed-LLM-Inference/.venv/bin/activate

python build_sharegpt_workload.py \
  --repo-id anon8231489123/ShareGPT_Vicuna_unfiltered \
  --filename ShareGPT_V3_unfiltered_cleaned_split.json \
  --repo-type dataset \
  --num-conversations 5000 \
  --output sharegpt_conversation.jsonl
```

생성 확인:

```bash
ls -lh /home/ubuntu/JJ-Distributed-LLM-Inference/workloads/squad/*.jsonl
ls -lh /home/ubuntu/JJ-Distributed-LLM-Inference/workloads/sharegpt/*.jsonl
```

### 1. Chat Isolated

Terminal 1 — 서버:

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/home/ubuntu/vllm/.venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib"

vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.96 \
  --port 8000
```

Terminal 2 — 클라이언트:

```bash
cd /home/ubuntu/JJ-Distributed-LLM-Inference/hypothesis_validation
source /home/ubuntu/JJ-Distributed-LLM-Inference/.venv/bin/activate
mkdir -p results

python run_trace.py \
  --trace ../workloads/sharegpt/sharegpt_conversation.jsonl \
  --api chat \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --workload-tag chat \
  --qps 5.0 \
  --max-concurrency 16 \
  --num-prompts 500 \
  --slo-ms 500 \
  --output results/chat_isolated_apc_on_len8192.jsonl
```

### 2. RAG Isolated

Terminal 1에서 서버를 재시작합니다. cache/queue 상태 초기화를 위해 각 run 사이 서버 재시작을 권장합니다.

Terminal 2:

```bash
cd /home/ubuntu/JJ-Distributed-LLM-Inference/hypothesis_validation
source /home/ubuntu/JJ-Distributed-LLM-Inference/.venv/bin/activate
mkdir -p results

python run_trace.py \
  --trace ../workloads/squad/squad_validation.jsonl \
  --api chat \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --workload-tag rag \
  --qps 5.0 \
  --max-concurrency 16 \
  --num-prompts 500 \
  --slo-ms 2000 \
  --output results/rag_isolated_apc_on_len8192.jsonl
```

### 3. Mixed 5:5

Terminal 1 — 서버:

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/home/ubuntu/vllm/.venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib"

vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.96 \
  --port 8000
```

Terminal 2:

```bash
cd /home/ubuntu/JJ-Distributed-LLM-Inference/hypothesis_validation
source /home/ubuntu/JJ-Distributed-LLM-Inference/.venv/bin/activate
mkdir -p results

python run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_conversation.jsonl \
  --rag-trace ../workloads/squad/squad_validation.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --rag-qps 5.0 \
  --max-concurrency 32 \
  --num-chat-prompts 500 \
  --num-rag-prompts 500 \
  --chat-slo-ms 500 \
  --rag-slo-ms 2000 \
  --output results/mixed_5_5_apc_on_len8192.jsonl
```

`--chat-qps` / `--rag-qps` 비율을 조절해 workload mix ratio sweep을 수행할 수 있습니다 (`HYPOTHESIS.md` §5.3 참고).

### 실행 원칙

- 각 run 사이에 vLLM 서버를 재시작해 cache/queue 상태를 초기화합니다.
- 비교군 사이에서 `num-prompts`, `qps`, `max-concurrency`, `--max-model-len 8192`를 고정합니다.
- isolated와 mixed 모두 chat completions endpoint를 사용합니다.
- vLLM 서버는 `/home/ubuntu/vllm/.venv`, JJ runner는 `/home/ubuntu/JJ-Distributed-LLM-Inference/.venv`를 사용합니다.

---

## 주요 출력 지표

결과 JSONL의 각 row에는 다음 필드가 포함됩니다.

| 필드 | 설명 |
|---|---|
| `workload` | `"chat"` 또는 `"rag"` |
| `ttft` | Time-to-First-Token (초) |
| `hit_rate` | 요청별 prefix cache hit rate |
| `h_r` / `u_r` | hit tokens / unhit tokens |
| `tpot` | Time-per-Output-Token (초) |
| `error` | 요청 실패 시 에러 메시지 |

콘솔 요약에는 workload별 **SLO attainment** (Chat ≤ 500ms, RAG ≤ 2000ms) 와 **cache hit rate** 가 출력됩니다.

---

<div align="center">
<sub>Cache Pollution · Hypothesis Validation · JJ Distributed LLM Inference</sub>
</div>
