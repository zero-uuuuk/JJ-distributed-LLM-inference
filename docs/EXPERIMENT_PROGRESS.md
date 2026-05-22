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

## 3. 현재 결론

```text
1. QPS 10은 Chat baseline이 너무 빡세서 부적절하다.
2. QPS 5는 Chat victim baseline으로 적절하다.
3. chat5/rag5에서는 Chat SLO degradation과 chat-rag useful eviction이 함께 관찰된다.
4. ratio sweep은 Chat QPS가 같이 변해서 인과 해석이 어렵다.
5. pressure sweep은 Chat QPS 고정 + duration matching이 필요하다.
```

이 결과는 다음 주장을 뒷받침한다.

```text
Raw hit rate degradation만으로 serving 품질 저하를 설명할 수 없다.
중요한 것은 future-reusable Chat prefix block이 eviction되고,
그 결과 SLO-sensitive later turn에서 TTFT/SLO가 악화되는지이다.
```

---

## 4. 다음 실험

이제 duration matching을 적용한 pressure sweep을 수행한다.

```text
chat5/rag10:
  --num-chat-prompts 900
  --num-rag-prompts 1800

chat5/rag15:
  --num-chat-prompts 900
  --num-rag-prompts 2700
```

확인할 흐름:

```text
RAG QPS 증가
-> chat-rag useful eviction 증가
-> Chat turn 7/8/9 SLODrop 증가
```

이 흐름이 관찰되면 QuataCache의 문제 정의가 훨씬 강해진다.
