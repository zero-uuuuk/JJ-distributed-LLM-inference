# Cache Pollution 계측 가이드

> **목적**: vLLM KV cache에서 발생하는 cross-workload eviction(cache pollution)을 실험적으로
> 측정하기 위한 계측 아키텍처, 실험 시나리오, 결과 해석 방법을 설명한다.
>
> - **Workload A (가해자)**: RAG (SQuAD) — 반복 context prefix, 높은 reuse
> - **Workload B (피해자)**: Chat (ShareGPT) — 짧은 prefix, 높은 reuse
> - **핵심 질문**: RAG가 shared prefix cache를 점유해 Chat의 reusable block을 evict하고,
>   그 결과 Chat의 TTFT/SLO가 isolated 실행 대비 실제로 악화되는가?

---

## 1. 계측 아키텍처 전체 흐름

```
run_mixed.py
  payload["user"] = "chat" | "rag"          ← workload 태그를 OpenAI 'user' 필드로 전달
        │
        ▼ HTTP POST /v1/chat/completions
vllm/entrypoints/.../chat_completion/serving.py
  sampling_params.extra_args["workload_tag"] = request.user
        │
        ▼ SamplingParams → EngineCoreRequest (msgpack IPC 직렬화)
vllm/v1/request.py :: Request.__init__()
  self.workload_tag = sampling_params.extra_args["workload_tag"]
        │
        ├─── [캐시 기록 경로] ───────────────────────────────────────────
        │    allocate_slots(request)
        │      → coordinator.cache_blocks(request, ...)
        │          → block_pool.cache_full_blocks(request, ...)
        │              blk.workload_tag      = request.workload_tag   ← workload 刻印
        │              blk.cached_request_id = request.request_id     ← 소유 요청 ID
        │              blk.block_index       = prefix 내 위치 (0-indexed)
        │              blk.last_access_time  = time.time()
        │              # pending_evictions에 동일 hash가 있으면 → reused_later=True 로 완성 후 기록
        │
        └─── [할당/eviction 경로] ──────────────────────────────────────
             allocate_slots(request)
               → coordinator.allocate_new_blocks(
                     ...,
                     trigger_workload=request.workload_tag,
                     trigger_request_id=request.request_id,   ← 새로 추가
                   )
                   → manager.allocate_new_blocks(..., trigger_workload=..., trigger_request_id=...)
                       → block_pool.get_new_blocks(..., trigger_workload=..., trigger_request_id=...)
                           → _maybe_evict_cached_block(block, trigger_workload, trigger_request_id)
                               event = {
                                 "evicted_workload": block.workload_tag,
                                 "trigger_workload": trigger_workload,
                                 "evicted_request_id": block.cached_request_id,
                                 "trigger_request_id": trigger_request_id,
                                 "evicted_prefix_hash": raw_hash.hex(),
                                 "evicted_block_index": block.block_index,
                                 "evicted_block_size": hash_block_size,
                                 "eviction_time": time.time(),
                                 "last_access_time": block.last_access_time,
                                 "reused_later": False,        ← 초기값
                                 "time_until_next_reuse": None,
                               }
                               _pending_evictions[raw_hash] = event  ← 버퍼에 적재
                               # (cache_full_blocks에서 동일 hash 재캐시 시 완성 후 JSONL 기록)
```

**핵심**: block이 처음 캐시될 때 소유 workload·요청 ID·위치·접근 시각을 새긴다. eviction 시 모든 필드를 캡처해 `_pending_evictions` 버퍼에 보관한다. 동일 block이 나중에 재캐시(= reused)되면 `reused_later=True`, `time_until_next_reuse`를 채워 JSONL에 기록한다. 재사용 없이 버퍼가 가득 차거나 프로세스가 종료될 때는 `reused_later=False`로 기록한다.

---

## 2. 수정된 파일 목록

### 2.1 실험 클라이언트

| 파일 | 수정 내용 |
|---|---|
| `hypothesis_validation/run_mixed.py` | `build_payload()`에 `"user": workload_tag` 추가 |

### 2.2 vLLM 서버 계측 패치

| 파일 | 수정 내용 |
|---|---|
| `vllm/entrypoints/openai/chat_completion/serving.py` | `request.user` → `sampling_params.extra_args["workload_tag"]` 주입 |
| `vllm/v1/request.py` | `Request.workload_tag` 필드 추가 (extra_args에서 추출) |
| `vllm/v1/core/kv_cache_utils.py` | `KVCacheBlock`에 `workload_tag`, `cached_request_id`, `block_index`, `last_access_time` 필드 추가; `reset_hash()`에서 전체 초기화 |
| `vllm/v1/core/block_pool.py` | eviction log writer + `_pending_evictions` 버퍼 추가; `cache_full_blocks()`에서 block 메타데이터 기록 및 reuse 감지; `touch()`에서 `last_access_time` 갱신; `get_new_blocks()` / `_maybe_evict_cached_block()`에 `trigger_workload` + `trigger_request_id` 전파; `_flush_pending_evictions()` atexit 등록 |
| `vllm/v1/core/single_type_kv_cache_manager.py` | `allocate_new_blocks()`, `allocate_new_computed_blocks()` (Base + MambaManager + CrossAttentionManager)에 `trigger_workload` + `trigger_request_id` 파라미터 추가 |
| `vllm/v1/core/kv_cache_coordinator.py` | 동일 두 파라미터 전달 레이어 |
| `vllm/v1/core/kv_cache_manager.py` | `allocate_slots()`에서 `trigger_workload=request.workload_tag`, `trigger_request_id=request.request_id`를 coordinator로 전달 |

---

## 3. 결과 파일

### 3.1 요청 결과 JSONL (`--output` 경로)

`run_trace.py` 또는 `run_mixed.py`가 생성하는 요청 단위 결과 파일.

```jsonc
{
  "workload": "chat",            // 워크로드 태그 ("chat" | "rag")
  "request_id": "req_001",       // 클라이언트 측 요청 ID
  "conversation_id": "conv_42",  // ShareGPT의 대화 ID (chat 전용)
  "turn_id": 2,                  // 대화 턴 번호 (chat 전용)

  "ttft": 0.312,                 // Time-to-First-Token (초)
  "mean_itl": 0.041,             // 평균 Inter-Token Latency (초)
  "tpot": 0.041,                 // Time-per-Output-Token ≒ mean_itl (초)
  "e2e": 1.204,                  // 전체 요청 완료까지 걸린 시간 (초)

  "start_wall": 1710000000.123,  // 요청 시작 wall-clock 시각 (Unix timestamp)
  "end_wall":   1710000001.327,  // 요청 완료 wall-clock 시각

  "prompt_tokens": 1024,         // 입력 토큰 수
  "completion_tokens": 128,      // 출력 토큰 수
  "cached_tokens": 896,          // vLLM이 보고하는 prefix-cached 토큰 수

  "h_r": 896,                    // hit tokens (min(cached, prompt)로 클램프)
  "l_r": 1024,                   // = prompt_tokens
  "u_r": 128,                    // uncached tokens = prompt - hit
  "hit_rate": 0.875,             // h_r / l_r

  "error": null                  // 에러 메시지 (성공 시 null)
}
```

**저장 위치 예시**:
```
hypothesis_validation/results/
  chat_isolated_apc_on_len8192.jsonl   ← Case 1: run_trace.py --workload-tag chat
  rag_isolated_apc_on_len8192.jsonl    ← Case 2: run_trace.py --workload-tag rag
  mixed_5_5_apc_on_len8192.jsonl       ← Case 3: run_mixed.py (chat:rag = 5:5)
```

### 3.2 Eviction 로그 JSONL (`VLLM_EVICTION_LOG` 경로)

vLLM 서버 계측 패치가 block eviction 발생 시 실시간으로 기록하는 파일.

```jsonc
{
  // ── eviction 주체 ──────────────────────────────────────────────────────────
  "evicted_workload":    "chat",           // evict된 block을 처음 캐시한 workload
  "trigger_workload":    "rag",            // eviction을 유발한 요청의 workload

  // ── 요청 ID ────────────────────────────────────────────────────────────────
  "evicted_request_id":  "chat_1024",      // evict된 block을 소유한 요청 ID
  "trigger_request_id":  "rag_0831",       // 공간을 요구해 eviction을 유발한 요청 ID

  // ── block 식별 ─────────────────────────────────────────────────────────────
  "evicted_prefix_hash": "4a2f...",        // evict된 block의 prefix 콘텐츠 hash (hex)
  "evicted_block_index": 3,               // prefix 내 위치 (0-indexed; 작을수록 재사용 가능성 ↑)
  "evicted_block_size":  16,              // hash_block_size (토큰 수)

  // ── 시각 ───────────────────────────────────────────────────────────────────
  "eviction_time":       1710000045.712,   // eviction 발생 Unix timestamp
  "last_access_time":    1710000038.294,   // 마지막 prefix cache hit 시각

  // ── 재사용 여부 (reuse 감지 시 업데이트) ───────────────────────────────────
  "reused_later":        true,             // eviction 후 동일 block이 다시 필요했는지
  "time_until_next_reuse": 8.37           // 다음 재사용까지 걸린 시간 (초); 짧을수록 pollution 심각
}
```

**`reused_later` 기록 메커니즘**: eviction 시 이벤트를 `_pending_evictions` 메모리 버퍼에 임시 보관한다. 이후 `cache_full_blocks()`에서 동일 `evicted_prefix_hash`를 가진 block이 재캐시되면 `reused_later=True`, `time_until_next_reuse` 를 채워 JSONL에 기록한다. 프로세스 종료 시 `atexit` 핸들러가 남은 버퍼를 `reused_later=False`로 모두 플러시한다.

> **주의**: `VLLM_EVICTION_LOG`를 설정하지 않으면 eviction 이벤트가 기록되지 않는다.
> Case 3 (Mixed) 실험에서만 의미 있는 데이터가 생성된다.

**저장 위치 예시**:
```
hypothesis_validation/results/
  eviction_mixed_5_5_apc_on_len8192.jsonl   ← VLLM_EVICTION_LOG 절대 경로로 지정
```

---

## 4. 기록 시점

### 4.1 요청 결과 (`hit_rate`, `ttft` 등)

```
요청 수신 → prefill 시작 → [첫 토큰 생성] → TTFT 기록
                                ↓
                         스트리밍 응답 수신 중
                                ↓
                 마지막 청크(`usage` 포함) 도착 → cached_tokens 추출 → hit_rate 계산
                                ↓
                         JSONL 한 줄 append
```

hit_rate는 **요청이 완전히 끝난 시점**에 기록된다. vLLM이 스트림 마지막 청크에 `usage.prompt_tokens_details.cached_tokens`를 포함해 반환한다.

### 4.2 Eviction 이벤트

```
새 요청 A 도착 → allocate_slots(A) 호출
  → 빈 block 부족 → free_block_queue에서 LRU block B를 pop
      → B에 block_hash 있음 (= B는 캐시된 block)
          → _maybe_evict_cached_block(B,
                trigger_workload=A.workload_tag,
                trigger_request_id=A.request_id) 호출
              → cached_block_hash_to_block에서 B 제거
              → event 생성 (11개 필드 전부 포함)
              → _pending_evictions[raw_hash] = event   ← 버퍼에 적재
              → B.reset_hash() → 모든 attribution 필드 초기화

  ... (나중에, 다른 Chat 요청이 동일 prefix를 계산) ...

      → cache_full_blocks(request, ...)
          → 새 block에 block_hash 설정
          → raw_hash in _pending_evictions? → True
              → ev["reused_later"]          = True
              → ev["time_until_next_reuse"] = now - ev["eviction_time"]
              → _log_eviction_event(ev)     → JSONL 한 줄 append (line-buffered)
              → _pending_evictions에서 제거

  (재사용 없이 프로세스 종료 시)
      → atexit → _flush_pending_evictions()
              → 남은 이벤트 reused_later=False 로 JSONL 기록
              → 파일 flush
```

eviction은 **새 요청이 block을 요구하는 순간**, 즉 prefill 시작 직전에 발생한다. `eviction_time`은 이 순간의 Unix timestamp이다.

> **LRU 순서**: vLLM은 요청 완료 후 해당 요청의 block을 `free_block_queue` 끝에 역순 append한다 (tail block이 먼저 evict). 따라서 prefix 앞쪽 block일수록 더 오래 생존한다.

---

## 5. 실험 시나리오

아래 예시는 다음 경로를 가정한다.

| 항목 | 경로 |
|---|---|
| JJ repo | `/home/ubuntu/JJ-Distributed-LLM-Inference` |
| vLLM repo | `/home/ubuntu/vllm` |
| vLLM venv | `/home/ubuntu/vllm/.venv` |
| JJ runner venv | `/home/ubuntu/JJ-Distributed-LLM-Inference/.venv` |

실험은 두 터미널을 사용한다. 각 run 사이에 vLLM 서버를 재시작해 cache/queue 상태를 초기화한다.

---

### Case 1: Chat Isolated (기준선 A)

Chat workload만 단독 실행. RAG의 간섭이 없는 상태에서의 기준 성능을 측정한다.

**Terminal 1 — 서버:**

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

**Terminal 2 — 클라이언트:**

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
  --qps 10.0 \
  --max-concurrency 64 \
  --num-prompts 500 \
  --slo-ms 500 \
  --output results/chat_isolated_apc_on_len8192.jsonl
```

**측정 지표**: `hit_rate` (mean, p50), `ttft` (p50/p95/p99), `SLO attainment (TTFT ≤ 500ms)`.

---

### Case 2: RAG Isolated (기준선 B)

RAG workload만 단독 실행. Chat의 간섭이 없는 상태에서의 기준 성능을 측정한다.

Terminal 1에서 서버를 재시작한 뒤 Terminal 2에서 실행한다.

**Terminal 2 — 클라이언트:**

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
  --qps 10.0 \
  --max-concurrency 64 \
  --num-prompts 500 \
  --slo-ms 2000 \
  --output results/rag_isolated_apc_on_len8192.jsonl
```

**측정 지표**: `hit_rate` (mean, p50), `ttft` (p50/p95/p99), `SLO attainment (TTFT ≤ 2000ms)`.

---

### Case 3: Mixed (Cache Pollution 측정)

Chat + RAG를 동시에 전송. shared prefix cache에서 eviction 경쟁을 유발한다.

**Terminal 1 — 서버:**

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/home/ubuntu/vllm/.venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib"

# eviction log 절대 경로 지정 (서버 시작 전 설정 필수)
export VLLM_EVICTION_LOG=/home/ubuntu/JJ-Distributed-LLM-Inference/hypothesis_validation/results/eviction_mixed_5_5_apc_on_len8192.jsonl

vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

**Terminal 2 — 클라이언트:**

```bash
cd /home/ubuntu/JJ-Distributed-LLM-Inference/hypothesis_validation
source /home/ubuntu/JJ-Distributed-LLM-Inference/.venv/bin/activate
mkdir -p results

python run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_conversation.jsonl \
  --rag-trace  ../workloads/squad/squad_validation.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 10.0 \
  --rag-qps  10.0 \
  --max-concurrency 64 \
  --num-chat-prompts 500 \
  --num-rag-prompts  500 \
  --chat-slo-ms 500 \
  --rag-slo-ms  2000 \
  --output results/mixed_5_5_apc_on_len8192.jsonl
```

**측정 지표**: Case 1/2와 동일 + pollution metrics (§6).

---

### 5.1 Cache size sweep (§5.2 대응)

pollution은 cache 크기가 working set보다 작을 때만 발생한다. `--gpu-memory-utilization`을 조절해 cache pressure를 변화시킨다.

| GPU memory utilization | 예상 동작 |
|---|---|
| 높음 (≈ 0.96) | Cache 여유 → Pollution 약함 |
| 중간 (≈ 0.7) | Cache 압박 시작 → Pollution 발생 |
| 낮음 (≈ 0.5) | Cache 심각하게 부족 → Pollution 극심 |

---

### 5.2 Workload mix ratio sweep (§5.3 대응)

```bash
cd /home/ubuntu/JJ-Distributed-LLM-Inference/hypothesis_validation
source /home/ubuntu/JJ-Distributed-LLM-Inference/.venv/bin/activate

# Chat:RAG = 7:3
python run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_conversation.jsonl \
  --rag-trace  ../workloads/squad/squad_validation.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 7.0 --rag-qps 3.0 \
  --max-concurrency 32 \
  --num-chat-prompts 500 --num-rag-prompts 500 \
  --chat-slo-ms 500 --rag-slo-ms 2000 \
  --output results/mixed_7_3_apc_on_len8192.jsonl

# Chat:RAG = 3:7
python run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_conversation.jsonl \
  --rag-trace  ../workloads/squad/squad_validation.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 3.0 --rag-qps 7.0 \
  --max-concurrency 32 \
  --num-chat-prompts 500 --num-rag-prompts 500 \
  --chat-slo-ms 500 --rag-slo-ms 2000 \
  --output results/mixed_3_7_apc_on_len8192.jsonl
```

---

## 6. Pollution Metrics 계산 및 해석

아래 세 단계 지표를 순서대로 확인한다. 세 가지가 모두 유의미하게 나타날 때 "cache pollution이 실제 문제"라고 결론 내릴 수 있다.

### (1) Hit rate degradation

Chat의 cache locality가 mixed 실행에서 얼마나 깨졌는지 측정한다.

```
Pollution_HitDrop(chat) = HitRate_chat^isolated  −  HitRate_chat^mixed
```

**계산 방법**: `chat_isolated_apc_on_len8192.jsonl`과 `mixed_5_5_apc_on_len8192.jsonl`에서 `workload == "chat"` 행의 `hit_rate` 평균값을 각각 구한 뒤 차이를 계산한다.

**해석**:
- 양수 → Chat이 mixed 환경에서 cache hit을 잃음
- 0에 가까움 → cache 크기가 충분하거나 workload mix 비율에서 RAG의 영향이 작음
- 단독으로는 인과 관계 증명 불가 — (2) eviction attribution과 함께 해석

---

### (2-a) Eviction attribution — Cross-workload eviction events

`eviction_mixed_5_5_apc_on_len8192.jsonl`을 분석해 RAG가 Chat block을 밀어낸 횟수를 센다.

```
CrossEviction(chat ← RAG)    = evicted_workload == "chat" AND trigger_workload == "rag" 인 이벤트 수
SelfEviction(chat ← chat)    = evicted_workload == "chat" AND trigger_workload == "chat" 인 이벤트 수
SelfEviction(rag  ← rag)     = evicted_workload == "rag"  AND trigger_workload == "rag"  인 이벤트 수
CrossEviction(rag ← chat)    = evicted_workload == "rag"  AND trigger_workload == "chat" 인 이벤트 수
```

**해석**:
- `CrossEviction(chat ← RAG)`가 크면 **RAG가 Chat cache를 밀어낸 직접 증거**
- `CrossEviction`이 `SelfEviction`보다 현저히 크면 pollution의 주범이 cross-workload 경쟁임을 시사

추가로 `reused_later` 필드를 활용해 "유용한" cross-eviction을 분리할 수 있다:

```
UsefulCrossEviction(chat ← RAG)
  = evicted_workload=="chat" AND trigger_workload=="rag" AND reused_later==true 인 이벤트 수

AvgTimeToReuse = time_until_next_reuse 의 평균 (reused_later==true 행만)
```

- `UsefulCrossEviction`이 크면 RAG가 **나중에 다시 쓰일 Chat block**을 밀어낸 증거
- `AvgTimeToReuse`가 짧을수록 pollution의 심각도가 큼 (eviction 직후 재계산 낭비가 많음)

---

### (2-b) Eviction attribution — Occupancy vs. utilization

현재 구현에서 occupancy는 직접 로그에 기록되지 않는다. 대신 다음 proxy를 사용한다:

- **RAG hit rate** (`rag_isolated_apc_on_len8192.jsonl`에서 `hit_rate` 평균): RAG가 cache를 차지한 만큼 실제로 활용하는지 판단
- **CrossEviction 비율**: RAG trigger eviction / 전체 eviction 비율

해석 예시:

| RAG hit rate | CrossEviction(chat ← RAG) 비율 | 해석 |
|---|---|---|
| 낮음 (< 20%) | 높음 (> 50%) | "selfish" pollution — RAG가 cache 점유 후 reuse 안 하고 Chat을 밀어냄 |
| 높음 (> 60%) | 낮음 | RAG도 cache를 유용하게 활용 중 — pollution 아님 |
| 낮음 | 낮음 | Cache가 충분해 경쟁 없음 |

---

### (3) SLO goodput degradation

Chat의 SLO 만족률이 mixed 실행에서 실제로 떨어지는지 측정한다.

```
Pollution_SLODrop(chat) = SLOAttainment_chat^isolated  −  SLOAttainment_chat^mixed
```

**계산 방법**: 각 JSONL에서 `workload == "chat"` 행의 `ttft * 1000 ≤ 500` 비율을 계산한 뒤 차이를 구한다.

**해석**:
- 0.1 이상의 하락 → 실제 서비스 품질 저하 발생 (10%p 이상의 SLO 위반 증가)
- 세 지표 (HitDrop, CrossEviction, SLODrop) 모두 일관되게 나타나면 **cache pollution이 실제 서비스 문제임을 입증**

---

## 7. 결과 디렉토리 구조 (권장)

```
hypothesis_validation/results/
├── chat_isolated_apc_on_len8192.jsonl          # Case 1: Chat-only 요청 결과
├── rag_isolated_apc_on_len8192.jsonl           # Case 2: RAG-only 요청 결과
│
├── mixed_5_5_apc_on_len8192.jsonl              # Case 3 (5:5): 혼합 요청 결과
├── eviction_mixed_5_5_apc_on_len8192.jsonl     # Case 3 (5:5): vLLM eviction 로그
│
├── mixed_7_3_apc_on_len8192.jsonl              # mix sweep: Chat 7 : RAG 3
├── eviction_mixed_7_3_apc_on_len8192.jsonl
│
├── mixed_3_7_apc_on_len8192.jsonl              # mix sweep: Chat 3 : RAG 7
└── eviction_mixed_3_7_apc_on_len8192.jsonl
```

---

## 8. 빠른 체크리스트

실험 전 확인 사항:

- [ ] vLLM이 계측 패치가 적용된 소스로 빌드/설치되어 있는가 (`vllm/v1/core/block_pool.py` 수정 포함)
- [ ] `--enable-prefix-caching` 및 `--enable-prompt-tokens-details` 플래그가 vLLM 서버에 설정되어 있는가
- [ ] `--max-model-len 8192` 가 모든 run에서 동일하게 적용되었는가
- [ ] Case 3 실행 전 `VLLM_EVICTION_LOG` **절대 경로**로 설정되어 있는가 (상대 경로는 vLLM 실행 위치에 따라 달라짐)
- [ ] Case 1, 2의 기준선 결과가 먼저 수집되었는가 (비교 기준 없이 Case 3만 실행하면 pollution 정량화 불가)
- [ ] 각 run 사이에 vLLM 서버를 재시작했는가 (cache/queue 상태 초기화)
- [ ] 동일 `--gpu-memory-utilization 0.6`으로 Case 1/2/3를 실행했는가 (조건 통제)
- [ ] `--num-prompts` 가 충분히 큰가 (권장 ≥ 500 per workload)
