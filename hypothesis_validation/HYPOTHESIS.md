# QuotaServe 가설 초안

## 1. 문제 배경

단일 모델은 이제 하나의 목적에만 쓰이지 않는다.

하나의 LLM serving system이 Chat, RAG, Agent 등 여러 workload를 동시에 처리하는 경우가 많다.

이때 mixed workload는 단일 workload에 비해 어쩔 수 없이 손해가 있다.

- 서로 다른 길이의 요청이 같은 scheduler queue에 들어온다.
- Prefill-heavy 요청과 decode-heavy 요청이 같은 batch에 섞인다.
- 그 결과 단일 workload에서보다 TTFT, TPOT, SLO가 나빠질 수 있다.

즉 mixed workload에서는 scheduling, batching, prefill/decode interference 때문에 단일 workload보다 성능 손해가 발생할 수 있고, 이는 기존 LLM serving 연구에서 많이 다뤄진 문제다.

---

## 2. 하지만 cache 문제도 있다 — LRU의 구조적 결함

Mixed workload에서는 scheduling, batching 문제뿐 아니라 prefix cache의 이점도 잘 못 살리는 문제가 생긴다.

흔히 이를 "다른 workload가 cache를 많이 차지해서(occupancy) Chat이 밀려난다"고 설명하지만, 이 프레이밍은 정확하지 않다. vLLM의 APC(automatic prefix caching)는 점유율 비례로 block을 쫓아내지 않는다. **참조가 끝난(ref=0) free block에 대한 LRU eviction**이다. 그리고 LRU는 원래 자주 재사용되는 hot block을 보호하도록 설계되어 있다.

그렇다면 왜 hot cache가 밀려나는가? 진짜 원인은 점유율이 아니라 **reuse 시간 척도의 불일치(temporal mismatch)에서 오는 LRU의 구조적 결함**이다.

- Chat의 재사용은 **multi-turn 사이 think-time gap**이 크다 (수 초~수십 초). 같은 prefix를 다시 쓰지만, 다시 쓰기까지의 간격이 길다.
- 그 gap 동안 RAG/Longctx 같은 다른 workload는 신규 block을 계속 생산한다. Chat block은 touch되지 않은 채 LRU tail로 밀려난다.
- 특히 Longctx처럼 low-reuse 대용량 prompt를 가진 workload는 수천 token 규모의 신규 block을 만들고, 갓 끝난 block은 "방금 쓰인" 것이라 LRU상 MRU 쪽에 위치한다. **LRU는 "최근 생산된 일회용 대용량 block"과 "최근 가치 있는(곧 재사용될) block"을 구분하지 못한다.**
- 결국 Chat이 다음 turn에 돌아왔을 때, 재사용 가능했던 prefix block은 이미 evict되어 있다.

즉 문제는 *"특정 workload가 공간을 많이 먹어서"*가 아니라, **reuse 간격이 긴 workload(Chat)가 volume이 큰 workload에게 LRU 상에서 aging out 당하기 때문**이다. recency(마지막 접근 시각)만 보는 LRU는 reuse 가치를 알지 못한다.

이 경우 Chat은 원래 cache hit으로 이득을 볼 수 있었지만, eviction 때문에 다시 prefill을 수행해야 한다. 그 결과 mixed workload에서 다음 문제가 나타난다.

- Cache hit rate 감소
- TTFT 증가
- SLO attainment 감소
- Prefix cache ON의 이득(APC gain) 감소

이 문제를 여기서는 **hot cache eviction**으로 본다. 그리고 이것은 LRU가 reuse 가치가 아니라 recency만 보기 때문에 발생하는, **policy 차원의 결함**이라는 점이 핵심이다.

---

## 3. 가설

Mixed workload에서 prefix cache의 이점을 잘 살리지 못하는 주요 원인 중 하나는 **reuse 간격이 긴 workload의 hot block이 volume이 큰 workload에 의해 LRU 상에서 밀려나는 cross-workload eviction**이다.

따라서 다음을 확인한다.

1. mixed workload에서 prefix cache ON의 이득(APC gain)이 단일 workload만큼 유지되지 않는가?
2. 그 이득 감소가 scheduling/batching 손해와 분리해서, **cache eviction 자체**로 설명되는가?
3. 특히 reusable prefix를 가진 workload(Chat)의 hot cache가, recency만 보는 LRU 때문에 다른 workload(RAG/Longctx)에 의해 밀려나는가?
4. QuotaServe가 이 cross-workload eviction을 줄여 prefix cache의 이점을 회복시키는가?

---

## 4. QuotaServe (설계 공간 — 아직 확정 전)

QuotaServe는 workload별 cache quota를 동적으로 조절해, LRU가 보호하지 못하는 hot cache를 보호하려는 방향이다.

목표는 cache를 고정 분할하는 것이 아니다. Mixed workload 상황에 따라 어떤 workload가 cache를 얼마나 써야 하는지 계속 조정하는 것이다.

직관은 단순하다.

- hot cache를 자주(그리고 손해보며) 잃는 workload는 더 보호한다.
- cache를 많이 차지하지만 hit을 만들지 못하는 workload는 quota를 제한한다.
- cache 여유가 있으면 불필요하게 강하게 나누지 않는다.

조절을 위해 다음 신호를 본다.

- workload별 cache occupancy
- workload별 cache hit rate
- workload 간 eviction rate (누가 누구를 evict하는지)
- evict된 block이 이후 다시 쓰였는지 여부 (useful eviction)
- workload별 TTFT와 SLO attainment

### 4.1 아직 확정하지 못한 설계 결정 (열린 질문)

QuotaServe의 구체 메커니즘은 아직 정하지 않았다. 다만 다음 두 가지는 결과 해석을 좌우하므로 실험을 본격화하기 전에 반드시 결정해야 한다.

1. **Quota의 대상이 무엇인가?**
   - (a) *evictable한 cached prefix block만* 대상으로 quota를 거는가, 아니면
   - (b) *전체 KV block pool(running 요청의 KV 포함)* 에 quota를 거는가?
   - vLLM은 동일한 block pool이 running 요청의 KV와 cached prefix를 공유한다. (b)로 RAG/Longctx 총 할당을 제한하면 in-flight 요청을 throttle하여 해당 workload의 TTFT/throughput을 크게 해칠 수 있다. 반면 (a)는 running KV는 건드리지 않고 "이미 쓸모를 다한 cached block"만 제한하므로, reuse가 낮은 workload라면 손해가 거의 없는 **Pareto win**이 될 수 있다. 본 연구는 (a) 방향을 우선 검토한다.

2. **목적함수가 무엇인가?**
   - aggregate goodput / per-workload SLO attainment / Pareto improvement / fairness 중 무엇을 최적화하는가? "개선"의 정의가 없으면 Case 2 결과를 해석할 수 없다. 적어도 "per-workload SLO를 깨지 않으면서 aggregate를 높인다" 수준의 기준은 먼저 고정한다.

3. **제어 루프**: quota 업데이트 주기, 임계값, 안정성(quota 진동 방지)은 메커니즘 확정 시 함께 정한다.

### 4.2 Workload 식별

본 연구는 요청에 **명시적 workload 태그**가 붙어 있다고 가정한다(예: API 호출 시 Chat/RAG/Agent 라벨). 즉 attribution과 quota는 이 태그를 신뢰한다. 태그 없이 추론하는 문제는 본 연구 범위 밖으로 둔다.

---

## 5. 비교군

### Case 1. 단일 workload vs. mixed workload (+ APC OFF arm으로 cache 손해 분리)

먼저 mixed workload에서 문제가 실제로 생기는지, 그리고 그 손해 중 **cache-specific 부분**을 분리해서 확인한다.

여기서 핵심은 "prefix cache의 이득(APC gain)"을 직접 측정하는 것이다. 이득은 정의상 `(APC OFF) − (APC ON)` 이므로, **각 조건마다 APC OFF arm을 함께 돌린다.**

| Case | APC | 목적 |
|---|---|---|
| Chat-only | ON / OFF | Chat baseline 및 single APC gain |
| RAG-only | ON / OFF | RAG baseline 및 single APC gain |
| Longctx-only | ON / OFF | Longctx baseline 및 single APC gain |
| Chat + RAG mixed | ON / OFF | 현실적 RAG mixed에서의 APC gain |
| Chat + Longctx mixed | ON / OFF | 강한 대용량 antagonist mixed에서의 APC gain |

분리 측정:

- `APC_gain_single = single(OFF) − single(ON)`
- `APC_gain_mixed  = mixed(OFF)  − mixed(ON)`
- 같은 workload 조건에서 APC ON/OFF만 바꿔 비교하면 scheduling/batching 효과는 대부분 공통으로 반영되므로, `APC OFF - APC ON`은 해당 조건에서 prefix cache가 제공한 이득을 근사한다.
- **`APC_gain_mixed < APC_gain_single` 이면**, mixed workload에서 prefix cache의 이득이 single workload만큼 유지되지 않는다는 뜻이다.

이 비교에서 함께 보는 것:

- mixed에서 Chat hit rate가 떨어지는가?
- mixed에서 Chat TTFT가 증가하는가?
- mixed에서 Chat SLO attainment가 감소하는가?
- mixed에서 antagonist(RAG/Longctx)의 TTFT와 SLO attainment는 어떻게 변하는가?
- mixed에서 antagonist pressure(비율·context length·prompt length)가 커질수록 eviction이 발생해 Chat hit rate가 감소하는가?

### Case 2. Cache policy 비교

그 다음 같은 mixed workload 조건에서 cache 정책만 바꿔 비교한다. APC는 ON으로 고정하고 정책만 바꾼다.

여기서 reuse-aware eviction을 베이스라인에 반드시 포함한다. 그래야 기여가 **"workload 격리(quota)"** 때문인지 **"그냥 더 나은 eviction policy"** 때문인지 분리된다.

| Case | 설명 | 목적 |
|---|---|---|
| Pure shared cache (LRU) + APC ON | vLLM 기본 shared prefix cache | 현재 cache 정책 baseline |
| Reuse-aware eviction + APC ON | quota 없이 reuse 가치를 보는 eviction (예: LFU/GDSF, hit-count 기반 hot prefix pinning) | "quota 없이 똑똑한 eviction"만으로 되는지 분리 |
| QuotaServe + APC ON | workload별 quota 동적 조절 | workload 격리의 추가 효과 측정 |

이 비교에서 보는 것:

- QuotaServe가 LRU 대비 cache hit rate를 회복시키는가?
- **reuse-aware eviction만으로 이미 회복되는가?** (그렇다면 quota의 추가 기여가 작다는 신호)
- QuotaServe가 TTFT와 SLO attainment를 개선하는가?
- QuotaServe가 Chat을 보호하는 대신 antagonist workload의 TTFT/SLO를 얼마나 희생시키는가? (4.1의 quota 대상 결정에 따라 달라짐)

---

## 6. 공통 측정 지표

아래 지표는 Case 1과 Case 2에 공통으로 사용한다.

| Case | 비교 기준 |
|---|---|
| Case 1 | `Chat-only`, `RAG-only`, `Longctx-only` 대비 `Chat + RAG mixed`, `Chat + Longctx mixed` (각각 APC ON/OFF) |
| Case 2 | 같은 mixed 조건에서 `LRU` vs `reuse-aware eviction` vs `QuotaServe` (APC ON) |

핵심 지표는 세 묶음이다.

| 분류 | 지표 | 의미 |
|---|---|---|
| Cache | hit rate, hit drop, **APC gain** | cache 이점을 얼마나 살렸는지 |
| Eviction attribution | cross-workload eviction, useful eviction | 어떤 workload가 누구의 hot cache를 밀어냈는지 |
| Serving | TTFT, TPOT, SLO attainment | 실제 serving 품질이 좋아졌는지 |

### 6.1 Eviction attribution 계측 (구현됨)

vLLM 내부에 eviction attribution 계측을 추가했다. 어떤 workload가 어떤 workload의 cache block을 evict했는지 직접 기록한다. 따라서 RAG/Longctx 비율이나 prompt length를 높였을 때 Chat hit rate가 감소하는지만 보는 것이 아니라, **실제로 antagonist 요청이 Chat cache block을 evict했는지**(상관이 아닌 인과)도 함께 측정한다.

`useful eviction` 측정은 **shadow cache** 방식으로 구현했다. evict된 block의 hash를 따로 보관해 두고, 이후 요청이 그 block을 다시 요구했는지(즉 evict하지 않았다면 hit이었을지)를 매칭한다. 이로써 "쫓아내도 됐던 block"과 "쫓아내서 손해 본 hot block"을 구분한다.

### 6.2 TPOT — eviction이 decode에 미치는 간접 경로

prefix cache는 1차적으로 prefill/TTFT에 영향을 준다. 다만 TPOT도 함께 보는 이유가 있다. hot cache가 evict되면 Chat이 prefill을 다시 수행해야 하고, 이 **재-prefill이 batch에 끼어들면 동시에 진행 중인 decode step을 지연시켜 TPOT를 끌어올린다.** 즉 eviction은 prefill 재계산 → prefill/decode interference → TPOT 증가라는 간접 경로를 가진다. 따라서 TPOT를 보면 eviction이 decode 품질까지 번지는지 확인할 수 있다.

---

## 7. 분산 설정에서의 위치

본 연구의 cache 스토리는 단일 인스턴스 block pool을 기준으로 기술했지만, 분산 serving에서도 동일한 결함이 인스턴스 단위로 재현된다.

- **Multi-instance**: 요청이 여러 인스턴스로 라우팅되면 prefix cache locality가 인스턴스별로 갈린다. cross-workload eviction은 각 인스턴스 내부에서 발생하므로, QuotaServe의 quota도 **인스턴스 단위로 enforce**하는 것을 기본으로 둔다. 어떤 workload를 어느 인스턴스로 보낼지(cache-aware routing)는 직교하는, 보완적인 축으로 본다.
- **PD disaggregation**: prefill과 decode가 분리되면 prefix cache는 prefill 노드에 집중되고, eviction 압력도 그쪽에 몰린다. 이 경우 QuotaServe는 prefill 노드의 cache pool을 대상으로 동작한다.

분산 설정은 본 연구의 1차 검증 범위(단일 인스턴스에서 메커니즘 확인) 이후의 확장 축으로 다룬다.

---

## 8. 워크로드 현실성

결과가 합성 설정의 artifact가 되지 않도록, reuse 구조가 현실적인 trace를 사용한다.

- **Chat**: multi-turn conversation으로, turn 사이 think-time gap을 포함한다(이 gap이 2절의 LRU aging out을 일으키는 핵심 변수다). ShareGPT류의 실제 대화 trace를 기반으로 한다.
- **RAG**: 공유 corpus에서 문서를 끌어오는 현실적 RAG baseline이다. MS MARCO trace의 token 분석상 Chat보다 약간 긴 수준이므로, prefill-heavy workload로 유지하되 long-context 압력을 대표한다고 보지는 않는다.
- **Longctx**: HotpotQA distractor 기반 multi-hop QA로, 요청마다 3k 내외의 low-reuse multi-doc prompt를 넣는다. RAG보다 강한 antagonist로 사용해 Chat hot cache eviction 메커니즘을 자극한다.

---

## 9. Related work (정리 필요 — 차별점 명시)

이 영역은 이미 붐비므로, 아래와의 차별점을 명시하지 않으면 novelty가 부정당한다. 본 절은 채워 넣을 자리표시이며, 최소한 다음을 다뤄야 한다.

- **vLLM APC / SGLang RadixAttention**: prefix cache 자체의 기본 메커니즘. 본 연구는 이들이 쓰는 LRU의 recency 결함을 문제 삼는다.
- **Preble 등 prefix-aware / cache-aware scheduling·routing**: 어디로 보낼지를 다룬다. 본 연구의 quota(인스턴스 내부 보호)와 직교·보완 관계임을 주장.
- **Multi-tenant KV cache fairness / FairServe류**: fairness·격리를 다룬다. 본 연구가 fairness가 아니라 **reuse 가치 기반 hot cache 보호**라는 점, 그리고 workload-aware cache quota가 기존에 동일하게 존재하는지 여부를 명시해야 한다.
- **Reuse-aware eviction(LFU/GDSF 등)**: Case 2의 베이스라인. quota가 이들 대비 추가로 기여하는 지점을 명확히 한다.

---

## 10. 기대 결론

이 실험이 보이고 싶은 이야기는 다음과 같다.

1. Mixed workload는 단일 workload보다 scheduling, batching 측면에서 기본 손해가 있다.
2. 여기에 더해 shared prefix cache에서도 손해가 발생하며, 이는 APC gain 감소로 분리 측정된다.
3. 이 손해의 원인은 단순 점유율이 아니라, recency만 보는 LRU가 reuse 간격이 긴 workload의 hot cache를 volume이 큰 workload에게 aging out시키는 구조적 결함이다.
4. QuotaServe는 workload별 quota를 동적으로 조절해 이 cross-workload eviction을 줄인다. (reuse-aware eviction 베이스라인 대비 추가 기여를 보인다.)
5. 그 결과 mixed workload에서도 cache hit rate, TTFT, SLO attainment를 개선할 수 있다.
