<div align="center">

# Prefix Caching Ablation Study

**Prefix cache reuse benefit vs. harmful eviction penalty**

_Mixed workload · Long-context RAG · Chat SLO_

</div>

---

## 1. 실험 목적

이 실험의 목적은 mixed workload에서 prefix caching을 켰을 때의 **순효과**를 확인하는 것이다.

핵심 질문은 다음과 같다.

> [!IMPORTANT]
> Prefix ON에서 생기는 eviction 손실보다,
> prefix reuse로 얻는 prefill/scheduling 부담 감소 이득이 더 큰가?

이를 위해 vLLM 서버의 prefix caching을 켠 조건과 끈 조건을 비교한다.

```text
Prefix caching OFF / Chat + RAG mixed = X
Prefix caching ON  / Chat + RAG mixed = X + E - B
```

여기서 `E`와 `B`는 다음을 의미한다.

```text
E = harmful eviction 손실
B = prefix reuse로 줄어든 prefill/scheduling 부담
```

해석 모델은 다음처럼 단순화한다.

```text
X = Prefix OFF mixed 결과

Prefix ON mixed 결과
  = X
  + E  # harmful eviction 손실
  - B  # prefix reuse로 줄어든 prefill/scheduling 부담
```

따라서 Prefix ON mixed 결과가 Prefix OFF mixed 결과보다 좋다면, 현재 workload에서는 `B > E`로 해석한다.

중요한 점은 Prefix OFF가 KV cache 자체를 없애는 실험은 아니라는 것이다. Decode를 위한 KV cache는 여전히 필요하다. 이 실험은 **이미 계산된 prefix KV block의 재사용 경로**를 끄고, Prefix ON의 순효과를 비교하는 ablation이다.

<details>
<summary>실험 설정 및 실행 스크립트</summary>

## 2. 실험 설정

최신 실험 기준은 다음과 같다.

| 항목 | 값 |
|---|---|
| Model | `meta-llama/Llama-3.2-3B-Instruct` |
| Chat trace | `workloads/sharegpt/sharegpt_turn_major_100conv_9turn.jsonl` |
| RAG trace | `workloads/squad/squad_validation_longctx_1500.jsonl` |
| Chat QPS | `5.0` |
| RAG QPS | `5.0` |
| Chat prompts | `900` |
| RAG prompts | `900` |
| Chat SLO | `TTFT <= 500ms` |
| RAG SLO | `TTFT <= 2000ms` |
| max concurrency | `64` |
| max model length | `8192` |
| GPU memory utilization | `0.6` |

Mixed run의 요청 수는 다음처럼 해석한다.

```text
Chat 900개 + RAG 900개 = 총 1800개
```

비교 대상은 전체 요청 수가 아니라 **Chat 요청 900개**이다.

RAG 900개는 Chat에 간섭을 만들기 위한 pressure workload다. `chat-qps=5`, `rag-qps=5`이므로 두 workload 모두 약 180초 동안 함께 도착한다.

## 3. 실험에 사용한 스크립트

### 3.1 Trace 생성

Chat victim trace를 생성한다.

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/workloads/sharegpt
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

python build_sharegpt_workload.py \
  --repo-id anon8231489123/ShareGPT_Vicuna_unfiltered \
  --filename ShareGPT_V3_unfiltered_cleaned_split.json \
  --repo-type dataset \
  --num-conversations 100 \
  --min-turns 9 \
  --max-turns 9 \
  --order turn-major \
  --output sharegpt_turn_major_100conv_9turn.jsonl
```

Long-context RAG trace를 생성한다.

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/workloads/squad
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

python build_squad_workload_long.py \
  --dataset-name squad \
  --split validation \
  --num-requests 5000 \
  --num-contexts 8 \
  --target-prompt-tokens 1500 \
  --output squad_validation_longctx_1500.jsonl
```

### 3.2 Prefix ON 서버

각 run 사이에는 서버를 재시작한다.

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/home/ubuntu/vllm/.venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib"

vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

### 3.3 Prefix OFF 서버

주의: `--enable-prefix-caching`을 생략하는 것만으로는 ablation 대조군이 성립하지 않을 수 있다. Prefix caching OFF run에서는 반드시 `--no-enable-prefix-caching`을 명시한다.

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/home/ubuntu/vllm/.venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib"

vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --no-enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

### 3.4 Prefix ON / Mixed

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate
mkdir -p results

python run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_turn_major_100conv_9turn.jsonl \
  --rag-trace ../workloads/squad/squad_validation_longctx_1500.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --rag-qps 5.0 \
  --max-concurrency 64 \
  --num-chat-prompts 900 \
  --num-rag-prompts 900 \
  --chat-slo-ms 500 \
  --rag-slo-ms 2000 \
  --output results/mixed_chat5_rag5_longctx1500_prefix_on_util06.jsonl
```

### 3.5 Prefix OFF / Mixed

Prefix OFF 서버를 재시작한 뒤 아래 명령을 수행한다.

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate
mkdir -p results

python run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_turn_major_100conv_9turn.jsonl \
  --rag-trace ../workloads/squad/squad_validation_longctx_1500.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --rag-qps 5.0 \
  --max-concurrency 64 \
  --num-chat-prompts 900 \
  --num-rag-prompts 900 \
  --chat-slo-ms 500 \
  --rag-slo-ms 2000 \
  --output results/mixed_chat5_rag5_longctx1500_prefix_off_util06.jsonl
```

</details>

<details>
<summary>현재 결과 상세 지표</summary>

## 4. 결과 정리

핵심 비교에 사용하는 JSONL은 다음 두 개다.

```text
mixed_chat5_rag5_longctx1500_prefix_on_util06.jsonl
mixed_chat5_rag5_longctx1500_prefix_off_util06.jsonl
```

각 파일의 요청 수는 실험 설계와 일치했다.

| 파일 | Chat 요청 | RAG 요청 | 실패 |
|---|---:|---:|---:|
| `mixed_chat5_rag5_longctx1500_prefix_on_util06.jsonl` | 900 | 900 | 0 |
| `mixed_chat5_rag5_longctx1500_prefix_off_util06.jsonl` | 900 | 900 | 0 |

### 4.1 Chat 전체 지표

| 조건 | Chat hit | TTFT P50 | TTFT P95 | TTFT P99 | SLO |
|---|---:|---:|---:|---:|---:|
| Prefix ON / mixed | 7.39% | 0.234s | 3.653s | 7.210s | 69.78% |
| Prefix OFF / mixed | N/A | 2.924s | 8.335s | 8.913s | 17.78% |

### 4.2 Chat turn-level SLO

| 조건 | Turn 6 | Turn 7 | Turn 8 | Turn 9 |
|---|---:|---:|---:|---:|
| Prefix ON / mixed | 79% | 51% | 0% | 0% |
| Prefix OFF / mixed | 0% | 0% | 0% | 0% |

### 4.3 RAG 참고 지표

| 조건 | RAG hit | TTFT P50 | TTFT P95 | SLO | 평균 prompt tokens |
|---|---:|---:|---:|---:|---:|
| Prefix ON / mixed | 90.88% | 0.190s | 2.942s | 84.67% | 2,095.7 |
| Prefix OFF / mixed | N/A | 3.065s | 8.172s | 37.33% | 2,095.7 |

</details>

## 5. 결과 해석

Prefix OFF mixed 결과를 `X`라고 두면, Prefix ON mixed 결과는 다음처럼 해석할 수 있다.

```text
Prefix OFF mixed = X

Prefix ON mixed
  = X
  + E  # harmful eviction 손실
  - B  # prefix reuse로 줄어든 prefill/scheduling 부담
```

이번 결과는 다음과 같다.

| 지표 | Prefix ON mixed | Prefix OFF mixed | ON - OFF |
|---|---:|---:|---:|
| Chat TTFT P95 | 3.653s | 8.335s | -4.682s |
| Chat SLO | 69.78% | 17.78% | +52.00%p |

TTFT는 작을수록 좋고, SLO는 클수록 좋다. 따라서 Prefix ON mixed는 Prefix OFF mixed보다 Chat TTFT P95를 `4.682s` 낮췄고, Chat SLO를 `52.00%p` 높였다.

이 비교를 위 모델에 대입하면 다음과 같다.

```text
Prefix ON mixed = X + E - B
Prefix OFF mixed = X

Prefix ON mixed가 더 좋음
=> X + E - B < X   # TTFT 기준
=> E - B < 0
=> B > E
```

SLO 기준으로도 같은 결론이다.

```text
Prefix ON mixed가 더 좋음
=> X + E - B > X   # SLO 기준, 높을수록 좋음
=> B > E
```

```text
B > E
```

따라서 이 ablation만 보면, harmful eviction 손실이 존재할 수는 있지만 prefix reuse가 줄여준 prefill/scheduling 부담 감소 이득이 더 크게 나타났다.
