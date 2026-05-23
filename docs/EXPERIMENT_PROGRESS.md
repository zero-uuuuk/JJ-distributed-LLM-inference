# Experiment Progress: ShareGPT Victim Workload

## 1. 작업 흐름

### 1.1 ShareGPT victim workload 변경

기존에는 `--num-prompts 920`처럼 앞부분만 자르면 turn 1 위주로 실행되는 문제가 있었다. 이 경우 같은 `conversation_id`의 later turn prefix reuse를 보기 어렵다.

이를 해결하기 위해 ShareGPT victim trace를 다음 구조로 바꿨다.

```text
100 conversations x 9 turns = 900 chat requests
```

ordering은 `turn-major`를 사용한다.

```text
conv1 turn1, conv2 turn1, ...
conv1 turn2, conv2 turn2, ...
...
conv1 turn9, conv2 turn9, ...
```

목적은 같은 conversation의 later turn에서 prefix reuse가 필요해지기 전까지 idle gap을 만들고, 그 사이에 eviction pressure가 발생할 수 있게 하는 것이다.

---

### 1.2 Chat baseline QPS 변경

처음에는 Chat QPS 10으로 isolated baseline을 측정했다. 그러나 QPS 10에서는 Chat-only 자체가 later turn에서 이미 크게 무너졌다.

따라서 mixed workload에서 추가적인 SLO degradation을 보기 어렵다.

이후 Chat QPS를 5로 낮췄고, QPS 5에서는 later turn이 일부 살아남아 mixed degradation을 관찰하기 좋은 baseline이 되었다.

---

### 1.3 Mixed pressure sweep 설계 수정

`chat5/rag10`에서 RAG prompt를 900개만 보내면 다음 문제가 생긴다.

```text
Chat: 900 / 5 QPS = 180s
RAG:  900 / 10 QPS = 90s
```

즉 RAG가 실험 중간에 먼저 끝나고, Chat 후반 turn은 RAG pressure 없이 실행될 수 있다.

따라서 mixed pressure sweep에서는 두 workload의 실행 시간을 맞춰야 한다.

```text
num_rag_prompts / rag_qps ~= num_chat_prompts / chat_qps
```

예시:

```text
chat5/rag10:
  --num-chat-prompts 900
  --num-rag-prompts 1800

chat5/rag15:
  --num-chat-prompts 900
  --num-rag-prompts 2700
```

---

## 2. 지금까지 결과

### 2.1 Chat-only QPS 10

QPS 10 isolated baseline은 두 번 반복했고 거의 동일하게 재현되었다.

| 항목 | qps10 prev | qps10 rep1 |
|---|---:|---:|
| token hit | 13.38% | 13.44% |
| TTFT P50 | 237 ms | 229 ms |
| TTFT P95 | 5.81 s | 5.74 s |
| SLO attainment | 57.78% | 57.56% |

해석:

```text
QPS 10에서는 Chat-only부터 turn 7~9가 이미 크게 무너진다.
따라서 mixed에서 추가적인 SLO degradation을 보기 어렵다.
```

---

### 2.2 Chat-only QPS 5

QPS 5는 더 적절한 Chat victim baseline이다.

| 항목 | Chat-only qps5 |
|---|---:|
| token hit | 12.80% |
| TTFT P50 | 128 ms |
| TTFT P95 | 2.16 s |
| SLO attainment | 81.33% |

turn별 SLO:

| Turn | SLO attainment |
|---:|---:|
| 5 | 100% |
| 6 | 99% |
| 7 | 83% |
| 8 | 41% |
| 9 | 9% |

해석:

```text
QPS 5에서는 later turn이 완전히 죽지 않는다.
특히 turn 7, 8이 mixed degradation 관찰에 좋다.
```

---

### 2.3 Mixed chat5/rag5

`chat5/rag5`에서는 유의미한 Chat degradation이 관찰되었고, 재실험에서도 거의 재현되었다.

| 항목 | Chat-only qps5 | Mixed chat5/rag5 rep1 |
|---|---:|---:|
| Chat token hit | 12.80% | 9.45% |
| Chat TTFT P50 | 128 ms | 150 ms |
| Chat TTFT P95 | 2.16 s | 2.91 s |
| Chat SLO attainment | 81.33% | 76.22% |

turn별 핵심 결과:

| Turn | Chat-only SLO | Mixed chat5/rag5 SLO | SLODrop |
|---:|---:|---:|---:|
| 7 | 83% | 79% | 4%p |
| 8 | 41% | 7% | 34%p |
| 9 | 9% | 1% | 8%p |

eviction attribution:

| 조합 | total | useful | useful rate |
|---|---:|---:|---:|
| chat-chat | 27,271 | 25,177 | 92.32% |
| chat-rag | 2,917 | 2,470 | 84.68% |
| rag-chat | 1,574 | 98 | 6.23% |
| rag-rag | 300 | 0 | 0% |

해석:

```text
RAG가 Chat useful block을 실제로 evict한다.
Chat SLO도 isolated baseline 대비 하락한다.
특히 turn 8에서 SLODrop이 크게 나타난다.
```

---

### 2.4 Mixed chat3/rag7

`chat3/rag7`에서는 Chat hit rate는 더 낮아졌지만, TTFT와 SLO는 오히려 좋아졌다.

| 항목 | Chat-only qps5 | Mixed chat3/rag7 |
|---|---:|---:|
| Chat token hit | 12.80% | 6.33% |
| Chat TTFT P95 | 2.16 s | 0.46 s |
| Chat SLO attainment | 81.33% | 96.11% |

해석:

```text
Hit rate degradation만으로 SLO degradation을 설명할 수 없다.
ratio sweep은 Chat QPS도 함께 변하므로 RAG pressure 인과 실험으로 부적절하다.
```

---

### 2.5 Mixed chat5/rag10, RAG 900 prompts

처음 수행한 `chat5/rag10`은 RAG prompt 수가 부족해 pressure sweep으로 해석하기 어렵다.

```text
Chat: 900 / 5 QPS = 180s
RAG:  900 / 10 QPS = 90s
```

| 항목 | Mixed chat5/rag5 rep1 | Mixed chat5/rag10, RAG 900 |
|---|---:|---:|
| Chat token hit | 9.45% | 7.15% |
| Chat TTFT P95 | 2.91 s | 2.30 s |
| Chat SLO attainment | 76.22% | 80.56% |
| RAG duration | 183 s | 89 s |

해석:

```text
RAG QPS는 올라갔지만 RAG가 실험 중간에 끝난다.
따라서 Chat 후반 turn은 RAG pressure 없이 실행됐을 가능성이 크다.
이 결과는 pressure sweep으로 해석하면 안 된다.
```

---

### 2.6 Mixed chat5/rag5, long-context RAG 1500

SQuAD RAG가 평균 250~280 tokens 정도로 짧아 RAG pressure가 약할 수 있다고 판단했다. 그래서 SQuAD context paragraph를 여러 개 붙여 long-context RAG workload를 만들었다.

long-context workload는 padding 문자를 반복하는 방식이 아니라, SQuAD의 다른 context paragraph들을 deterministic하게 이어 붙이는 방식이다.

```text
Passage 1: original SQuAD context
Passage 2: next SQuAD context
Passage 3: next SQuAD context
...
Question: original SQuAD question
Answer:
```

`longctx1500` 결과에서는 실제 RAG prompt가 평균 약 2K tokens까지 증가했다.

| 항목 | 기존 SQuAD chat5/rag5 | longctx1500 chat5/rag5 |
|---|---:|---:|
| RAG avg input tokens | 약 252 | 약 2,096 |
| RAG token hit | 75.7% | 90.8% |
| RAG SLO attainment | 약 95.7% | 85.6% |
| Chat token hit | 9.45% | 7.55% |
| Chat TTFT P95 | 2.91 s | 3.65 s |
| Chat SLO attainment | 76.22% | 69.44% |

eviction attribution:

| 조합 | 기존 SQuAD chat5/rag5 | longctx1500 chat5/rag5 |
|---|---:|---:|
| chat-rag total | 2,917 | 7,415 |
| chat-rag useful | 2,470 | 6,266 |
| chat-rag useful rate | 84.68% | 84.50% |
| cross-workload eviction ratio | 약 14.0% | 34.1% |

turn별 Chat SLO:

| Turn | Chat-only qps5 | longctx1500 chat5/rag5 | SLODrop |
|---:|---:|---:|---:|
| 6 | 99% | 76% | 23%p |
| 7 | 83% | 50% | 33%p |
| 8 | 41% | 1% | 40%p |
| 9 | 9% | 0% | 9%p |

해석:

```text
RAG prompt를 길게 만들면 RAG-triggered useful eviction of Chat blocks가 크게 증가한다.
그 결과 Chat later-turn TTFT tail과 SLO가 isolated baseline 대비 더 크게 악화된다.
현재까지 결과 중 QuataCache 문제 정의에 가장 가까운 신호다.
```

주의할 점:

```text
longctx1500에서는 RAG 자체도 무거워져 RAG SLO가 약 85.6%까지 하락한다.
따라서 이후 실험에서는 Chat degradation뿐 아니라 RAG SLO sacrifice도 함께 봐야 한다.
```

---

### 2.7 Capacity sweep

동일한 `chat5/rag5 longctx1500` 조건에서 KV capacity/utilization을 낮추며 pressure를 키웠다.

| util | Chat token hit | Chat TTFT P95 | Chat SLO | RAG token hit | RAG TTFT P95 | RAG SLO | chat-rag useful eviction |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.96 | 47.84% | 0.82 s | 85.00% | 90.88% | 0.68 s | 100.00% | 2,819 |
| 0.70 | 13.29% | 1.52 s | 78.56% | 90.88% | 0.94 s | 99.89% | 5,107 |
| 0.60 | 7.55% | 3.65 s | 69.44% | 90.82% | 2.95 s | 85.56% | 6,266 |
| 0.50 | 4.67% | 6.30 s | 63.00% | 90.63% | 5.70 s | 76.78% | 6,475 |

해석:

```text
KV capacity가 줄어들수록 Chat cache locality가 빠르게 붕괴한다.
RAG token hit은 약 90% 수준을 유지하지만, Chat SLO와 RAG SLO 모두 tail latency 악화로 하락한다.
특히 chat-rag useful eviction이 capacity pressure와 함께 증가해 cross-workload interference를 직접 보여준다.
```

---

### 2.8 RAG context length sweep

RAG context 길이를 `ctx500`, `ctx1500`, `ctx3000`으로 늘리며 shared KV cache interference가 어떻게 커지는지 확인했다.

| label | RAG avg prompt tokens | Chat token hit | Chat TTFT P95 | Chat SLO | RAG token hit | RAG TTFT P95 | RAG SLO | chat-rag useful eviction | RAG share of useful Chat evictions |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ctx500 | 1,259 | 7.69% | 3.26 s | 73.33% | 90.17% | 2.72 s | 91.22% | 4,520 | 15.90% |
| ctx1500 | 2,096 | 7.55% | 3.65 s | 69.44% | 90.82% | 2.95 s | 85.56% | 6,266 | 21.91% |
| ctx3000 | 4,088 | 5.72% | 6.06 s | 54.78% | 91.22% | 5.02 s | 77.67% | 8,724 | 30.84% |

해석:

```text
RAG context가 길어질수록 useful chat->RAG eviction이 거의 선형적으로 증가한다.
그 결과 Chat SLO는 73.33% -> 54.78%로 떨어지고, TTFT P95는 3.26s -> 6.06s로 악화된다.
RAG 자체 hit rate는 오히려 약간 높아지므로, raw hit rate만으로는 피해 workload의 SLO 악화를 설명하기 어렵다.
```

---

### 2.9 Turn-level SLO and TTFT tail

turn별로 보면 평균 지표보다 더 강한 신호가 나온다. 특히 later turn에서 prefix reuse가 필요해지는 순간, 이전에 밀려난 Chat KV block의 피해가 TTFT tail로 드러난다.

| label | Turn 6 SLO | Turn 7 SLO | Turn 8 SLO | Turn 9 SLO | Turn 9 TTFT P95 |
|---|---:|---:|---:|---:|---:|
| isolated | 99% | 83% | 41% | 9% | 3.86 s |
| ctx500 | 97% | 63% | 0% | 0% | 7.47 s |
| ctx1500 | 76% | 50% | 1% | 0% | 7.32 s |
| ctx3000 | 37% | 5% | 0% | 0% | 8.24 s |

해석:

```text
interference는 early turn보다 later turn에서 훨씬 뚜렷하다.
ctx3000은 turn 6부터 SLO가 37%까지 떨어지고, turn 7 이후에는 사실상 SLO를 만족하지 못한다.
이는 QuataCache가 보호해야 할 대상이 단순히 현재 hit rate가 낮은 block이 아니라 future-reusable Chat prefix block임을 보여준다.
```

---

## 3. 현재 결론

```text
1. QPS 10은 Chat baseline이 너무 빡세서 부적절하다.
2. QPS 5는 Chat victim baseline으로 적절하다.
3. chat5/rag5에서는 Chat SLO degradation과 chat-rag useful eviction이 함께 관찰된다.
4. ratio sweep은 Chat QPS가 같이 변해서 인과 해석이 어렵다.
5. pressure sweep은 Chat QPS 고정 + duration matching이 필요하다.
6. RAG prompt length를 늘리면 chat-rag useful eviction과 Chat later-turn SLO degradation이 강해진다.
7. capacity pressure가 커질수록 Chat cache locality와 Chat/RAG SLO가 함께 악화된다.
8. RAG context length가 길어질수록 useful Chat eviction 중 RAG가 차지하는 비중이 15.90% -> 30.84%로 증가한다.
9. 평균 지표보다 turn-level later-turn SLO/TTFT tail에서 QuataCache 문제 정의가 가장 선명하게 드러난다.
```

이 결과는 다음 주장을 뒷받침한다.

```text
Raw hit rate degradation만으로 serving 품질 저하를 설명할 수 없다.
중요한 것은 future-reusable Chat prefix block이 eviction되고,
그 결과 SLO-sensitive later turn에서 TTFT/SLO가 악화되는지이다.
```

현재까지의 판정:

```text
가설 방향성 검증은 성공했다.
RAG가 shared KV cache에서 Chat의 future-reusable prefix block을 밀어내고,
그 결과 multi-turn Chat의 later-turn TTFT tail과 SLO가 악화된다는 신호가 일관되게 관찰된다.

다만 논문급 causal proof로 만들려면 seed 반복, confidence interval,
no-RAG same-load baseline, cache policy 대조군을 추가하면 더 단단해진다.
```

---

## 4. 다음 실험

핵심 가설은 현재 결과로 충분히 지지된다. 다음 단계는 "가설 발견"보다 "causal proof 강화"와 "QuataCache 개선 효과 입증"에 가깝다.

### 4.1 반복 실험과 confidence interval

동일 조건을 seed만 바꿔 3회 이상 반복한다.

```text
권장 조건:
  isolated chat5
  chat5/rag5 ctx500
  chat5/rag5 ctx1500
  chat5/rag5 ctx3000

확인 지표:
  Chat SLO
  Chat TTFT P95
  turn 6/7/8/9 SLO
  chat-rag useful eviction
  RAG share of useful Chat evictions
```

### 4.2 no-RAG same-load baseline

RAG 대신 같은 token volume을 만드는 non-reusable synthetic workload를 넣어 compute pressure와 KV eviction pressure를 분리한다.

```text
목적:
  RAG context length 증가로 인한 단순 compute load 증가와
  shared KV cache eviction으로 인한 Chat prefix loss를 분리한다.
```

### 4.3 cache policy 대조군

baseline shared LRU와 QuataCache-style policy를 같은 workload에서 비교한다.

```text
비교군:
  shared LRU
  workload-aware partition
  Chat prefix pinning or priority
  QuataCache policy

성공 기준:
  Chat later-turn SLO 회복
  Chat TTFT P95 tail 감소
  RAG SLO sacrifice가 과도하지 않음
  useful cross-workload eviction 감소
```

### 4.4 correlation/regression summary

eviction attribution과 TTFT/SLO 사이의 관계를 표로 정리한다.

```text
분석:
  chat-rag useful eviction vs Chat TTFT P95
  RAG share of useful Chat evictions vs later-turn SLO
  RAG avg prompt tokens vs cross-workload eviction ratio

목적:
  그래프 해석을 정성 주장에 그치지 않고 정량 근거로 보강한다.
```
