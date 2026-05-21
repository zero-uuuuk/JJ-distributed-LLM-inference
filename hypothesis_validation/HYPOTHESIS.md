## 1. 문제 정의: Cache Pollution

> 
> 
> 
> Mixed Workload 환경에서 **workload A**의 prefix KV cache가 shared cache pool을 점유하여 **workload B**의 reusable prefix KV를 eviction시키고, 그 결과 B의 `cache hit rate`, `TTFT`, `SLO goodput`이 isolated 실행 대비 악화되는 현상.
> 
- Workload A: **RAG** (HotpotQA)
- Workload B: **Chat** (ShareGPT)

---

## 2. Workload 구체화

### 2.1 Chat workload (ShareGPT)

- **데이터셋**: ShareGPT
- **Prefix 구조**: system prompt(고정, 짧음) + conversation history(누적)
- **Reuse pattern**: multi-turn 대화 내에서 동일 prefix 반복 → 높은 reuse rate 기대
- **Prefix 길이**: 평균 ~500–2K tokens 예상
- **SLO 특성**: latency-sensitive, 짧은 TTFT 요구

### 2.2 RAG workload (HotpotQA)

- **데이터셋**: HotpotQA
- **Prefix 구조**: system prompt + retrieved top-k passages + query
- **Retrieval 설정**: `top-k = 10`
- **Reuse pattern**: 같은 document가 여러 query에 등장할 때만 hit
    - cache hit을 늘리는 방향으로 query 순서 미세 조정 예정
- **Prefix 길이**: 평균 ~4K tokens 예상
- **SLO 특성**: prefill-heavy, TTFT 허용치 상대적으로 큼

### 2.3 Arrival pattern

요청은 **Poisson process**를 따라 도착하도록 설정한다.

핵심은 요청량과 workload 비율을 조절해서, shared prefix cache에서 eviction 경쟁이 발생하는 조건을 만드는 것이다.

- 전체 QPS는 기본적으로 cache pressure가 발생하는 수준으로 설정한다.
    - 즉, Chat + RAG의 working set이 prefix cache size보다 커지도록 한다. ($5.2)
- 기본 workload mix는 Chat:RAG = 5:5로 설정한다.
    - 이후 QPS와 workload mix를 조절하며 pollution이 언제 심해지는지 관찰한다. ($5.3)
    - 예시:
        
        
        | Total QPS | Chat:RAG | Chat QPS | RAG QPS |
        | --- | --- | --- | --- |
        | 10 | 5:5 | 5 | 5 |
        | 10 | 7:3 | 7 | 3 |
        | 10 | 3:7 | 3 | 7 |

---

## 3. SLO 기준 정의

Cache pollution이 실제 serving 품질에 영향을 주는지 확인하기 위해 workload별 SLO를 정의한다. 

그 중 LLM serving에서 가장 흔히 쓰는 두 가지 metric:

- **TTFT (Time-to-First-Token)**: prefix cache가 직접 영향을 주는 지표 → **메인 SLO**
- **TPOT / ITL (Time-per-Output-Token)**: decode 단계 지표 → 보조 지표

### 3.1 SLO 기준값

초기 실험에서는 다음과 같은 가정 SLO를 사용한다. 이후 isolated 실행의 P95 TTFT 또는 서비스 목표에 맞춰 sensitivity analysis를 수행한다.

| Workload | TTFT SLO (P95) | TPOT SLO (P95, 보조) |
| --- | --- | --- |
| Chat | **500 ms** | 50 ms |
| RAG | **2 s** | 100 ms |

### 3.2 메인 metric: SLO attainment rate

SLO attainment rate는 각 workload에서 TTFT SLO를 만족한 요청의 비율로 정의하며, 이 값이 isolated 실행 대비 mixed 실행에서 감소하면, cache pollution이 실제 SLO 품질 저하로 이어졌다고 해석한다.

$$
\text{SLOAttainment}_w = \frac{\text{number of requests of } w \text{ where TTFT} \leq \text{SLO}_w}{\text{total requests of } w}
$$

---

## 4. 측정 지표

Cache pollution은 단순히 cache hit rate가 떨어지는 현상이 아니라, 한 workload가 다른 workload의 reusable prefix KV를 밀어내고, 그 결과 피해 workload의 latency / SLO가 악화되는 현상으로 본다.

따라서 측정 지표는 다음 세 단계로 구성한다.

> 
> 
> 1. *cache locality가 악화되었는가? (1 - Hit rate degradation)*
> 2. *실제로 cross-workload eviction이 발생했는가? (2 - Eviction attribution)*
> 3. *그 결과 SLO가 악화되었는가? (3 - SLO goodput degradation)*

### 4.1 Pollution metrics

### **(1) Hit rate degradation**

Chat workload가 단독 실행 대비 mixed 실행에서 얼마나 cache hit을 잃었는지 측정한다.

$$
\text{Pollution}{\text{HitDrop}}(\text{chat}) = \text{HitRate}{\text{chat}}^{\text{isolated}} - \text{HitRate}_{\text{chat}}^{\text{mixed}}
$$

해석:

- 값이 클수록 Chat의 prefix reuse가 mixed workload에서 깨졌다는 의미
- 다만 hit drop만으로는 RAG가 원인이라고 단정할 수 없으므로 eviction attribution과 함께 해석

### **(2) Eviction attribution**

Mixed 실행 중 RAG가 Chat의 cache를 밀어내는 현상을 두 가지 방식으로 측정한다.

**(2-a) 동적 증거: Cross-workload eviction events**

Mixed 실행 중 eviction event를 workload 단위로 기록한다. 핵심은 **RAG 요청으로 인해 Chat의 reusable prefix block이 evict되었는지** 확인하는 것이다.

각 eviction event에서 최소한 다음 정보를 기록한다.

```c
{
  "evicted_workload": "chat",              // evict된 block의 원래 workload
  "trigger_workload": "rag",              // eviction을 유발한 요청의 workload

  "evicted_request_id": "chat_1024",       // evict된 block을 생성/소유한 요청 ID
  "trigger_request_id": "rag_0831",        // cache 공간을 요구해 eviction을 발생시킨 요청 ID

  "evicted_prefix_hash": "abc123",         // evict된 prefix block을 식별하기 위한 prefix hash
  "evicted_block_index": 3,                // prefix 내 block 위치; 앞쪽 block일수록 재사용 가능성이 클 수 있음
  "evicted_block_size": 16,                // evict된 block 크기; 보통 token 수 또는 KV block 단위

  "eviction_time": 130.12,                 // eviction이 발생한 시각
  "last_access_time": 123.45,              // evict된 block이 마지막으로 hit/access된 시각

  "reused_later": true,                    // eviction 이후 동일 block이 다시 필요했는지 여부
  "time_until_next_reuse": 8.37            // eviction 이후 다음 재사용까지 걸린 시간; 짧을수록 pollution 심각
}
```

주요 metric:

```c
CrossEviction(chat ← RAG)
= # chat blocks evicted by RAG-triggered requests

UsefulCrossEviction(chat ← RAG)
= # chat blocks evicted by RAG-triggered requests and reused later
```

해석:

- `CrossEviction(chat ← RAG)`가 크면 RAG가 Chat cache를 밀어낸 증거
- `UsefulCrossEviction(chat ← RAG)`가 크면 RAG가 **나중에 다시 쓰일 Chat block**을 밀어낸 증거
- `time_until_next_reuse`가 짧을수록 pollution의 심각도가 큼

**(2-b) 정적 증거: Occupancy vs. utilization**

Cache 점유율과 hit rate를 workload별로 측정해, RAG가 cache를 차지하는 만큼 활용하고 있는지 본다.

- **Cache occupancy share (시간 가변)**: workload별 점유 block 수 / 전체 block 수
- **Per-workload hit rate vs. occupancy share**

해석:

- RAG occupancy share가 높은데 RAG hit rate가 낮으면 → "selfish" pollution (점유만 하고 reuse 안 함)
- 예: RAG occupancy 80%, RAG hit rate 15% → RAG가 cache의 정당한 수혜자가 아님에도 chat을 밀어냄

### **(3) SLO goodput degradation**

Cache pollution이 실제 serving 품질 저하로 이어졌는지 측정한다.

$$
\text{Pollution}{\text{SLODrop}}(\text{chat}) = \text{SLOAttainment} _{\text{chat}}^{\text{isolated}}- \text{SLOAttainment}_{\text{chat}}^{\text{mixed}}
$$

해석:

- 값이 클수록 mixed workload에서 Chat의 SLO 만족률이 악화됨
- 최종적으로 “cache pollution이 실제 문제인가?”를 판단하는 핵심 metric

### 4.2 기타 metric

- **TTFT tail latency**: P95, P99

---

## 5. 실험 케이스

### 5.1 기본 케이스

| Case | 설명 | 측정 |
| --- | --- | --- |
| Case 1 | Chat-only | HitRate, TTFT(P50/P95/P99), SLO attainment, Occupancy |
| Case 2 | RAG-only | 동일 |
| Case 3 | Chat + RAG mixed (shared LRU) | 동일 + Pollution metrics, Eviction attribution |

### 5.2 Cache size sweep

> Pollution은 cache가 부족할 때만 발생. cache size를 working set 대비 비율로 sweep.
> 

| Cache size (% of working set) | 예상 결과 |
| --- | --- |
| 200% | Pollution ≈ 0 (둘 다 충분) |
| 100% | Pollution 시작(default) |
| 50% | Pollution 심각 |
| 25% | Catastrophic |

→ x축: cache size, y축: chat SLO attainment / hit rate drop

### 5.3 Workload mix ratio sweep

| RAG : Chat 비율 | 의미 |
| --- | --- |
| 1 : 9 | RAG 적음, pollution 약함 예상 |
| 3 : 7 |  |
| 5 : 5 | balanced(default) |
| 7 : 3 | RAG-heavy |
| 9 : 1 | RAG가 cache 거의 점유 |

→ "어떤 mix에서 pollution이 심각해지는가" 곡선
