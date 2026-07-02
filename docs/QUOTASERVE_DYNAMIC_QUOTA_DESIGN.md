# QuotaServe 제어 루프 — Cross-workload Useful Eviction Ratio with PFF

## 한 줄 요약

> QuotaServe는 workload별 cached-prefix quota를 런타임에 조정한다.
중심 신호는 **다른 workload에게 쫓겨난 block이 나중에 다시 필요했는가**이다.
사전 profile은 `ratio_low/high` 기준선과 그에 대응되는 `floor/cap`을 정하고, 런타임 quota는 그 사이에서 천천히 움직인다.

> 💡 핵심은 단순하다. **cross-workload useful eviction ratio가 높으면 quota를 늘리고, 낮으면 줄이며, 그 움직임을 profile curve에서 얻은 `floor`와 `cap` 사이에 둔다.**

---

## 용어

| 용어 | 뜻 |
| --- | --- |
| **cached prefix block** | running 요청이 끝나 `ref=0`이 된 prefix block. QuotaServe가 보호하거나 회수할 수 있는 대상이다. |
| **quota** | workload `w`가 보호받는 cached prefix block 목표량. 런타임에서 조정되는 값이다. (단위: block 수) |
| **ratio_high** | quota 부족을 판단하는 useful eviction ratio 상한. 이 값보다 높으면 quota를 늘린다. (단위: 0~1 비율) |
| **ratio_low** | quota 여유를 판단하는 useful eviction ratio 하한. 이 값보다 낮으면 quota를 줄인다. (단위: 0~1 비율) |
| **floor** | profile curve에서 `ratio_high`에 대응되는 workload별 quota 하한. (단위: block 수) |
| **cap** | profile curve에서 `ratio_low`에 대응되는 workload별 quota 상한. (단위: block 수) |
| **eviction attribution** | evictor workload와 victim workload를 함께 기록하는 장치. |
| **shadow cache** | evict된 block의 hash를 보관해, 이후 같은 block이 다시 요청되는지 추적하는 그림자 cache. |
| **cross-workload eviction** | `evictor != victim`인 eviction 사건. |
| **cross-workload useful eviction** | cross-workload eviction 이후 victim workload가 같은 block을 다시 요청한 사건. |
| **useful eviction ratio** | workload `w`가 겪은 cross-workload eviction 중 useful eviction으로 확인된 비율. |
| **self eviction** | `evictor == victim`인 eviction 사건. workload 내부 pressure를 보여 주는 보조 관측값이다. |
| **tick** | 제어 루프가 한 번 도는 주기. |
| **window** | workload별 신호를 집계하는 최근 `window_size`개의 cross-workload eviction 사건 묶음. |
| **window_size** | `useful_eviction_ratio_w`를 계산할 때 분모가 되는 cross-workload eviction 사건 수. |

---

## 1. PFF식 발상

운영체제의 PFF(Page-Fault Frequency)는 프로세스의 적정 frame 수를 직접 계산하지 않고, 관측 가능한 fault rate로 working set 부족을 추정한다.

QuotaServe도 같은 방식으로 접근한다. 다만 page fault 대신 prefix cache에서 관측되는 **cross-workload useful eviction ratio**를 본다.

| 구분 | OS의 PFF | QuotaServe |
| --- | --- | --- |
| 조절 대상 | 프로세스별 frame 수 | workload별 cached-prefix quota |
| 관측 신호 | page fault rate | cross-workload useful eviction ratio |
| 신호가 높을 때 | frame 부족 | 다른 workload에게 hot prefix가 밀림 |
| 런타임 조정값 | frame 할당량 | `quota_w` |
| 사전 산정값 | fault-rate 기준선과 frame 범위 | `ratio_low/high`, `floor_w`, `cap_w` |

![figure](image.png)

*그림 1. PFF는 page-fault rate가 상한보다 높으면 frame을 늘리고, 하한보다 낮으면 frame을 줄인다.*

![figure](image.png)

*그림 2. QuotaServe는 useful eviction ratio가 `ratio_high`보다 높으면 quota를 늘리고, `ratio_low`보다 낮으면 quota를 줄인다.*

여기서 `ratio_low/high`는 useful eviction ratio의 기준선이고, `floor/cap`은 그 기준선에 대응되는 workload별 quota 범위다.

$$
floor_w \leq quota_w \leq cap_w
$$

---

## 2. 대상 범위

QuotaServe는 전체 KV block pool을 직접 throttle하는 정책이 아니다. 대상은 요청이 끝나 `ref=0`이 된 cached prefix block이다.

즉 running 요청의 prefill/decode KV block은 실행 경로 그대로 두고, evictable cache block 중 어떤 workload의 prefix를 더 오래 보호할지 결정한다.

---

## 3. 신호 정의

### 3.1 Cross-workload useful eviction ratio

workload `w`의 중심 신호는 `w`가 다른 workload에게 eviction 당한 뒤, 같은 block을 나중에 다시 필요로 한 비율이다.

집계 사건은 다음과 같다.

1. workload `x`가 workload `w`의 cached prefix block을 evict한다.
2. 이때 `x != w`이다.
3. evict된 block의 hash를 shadow cache에 기록한다.
4. 이후 workload `w`가 같은 block을 다시 요청한다.
5. shadow cache hit가 발생하면 cross-workload useful eviction으로 집계한다.

수식은 다음과 같다.

```
cross_workload_evictions_w
= window_size개의 최근 eviction where victim = w and evictor != w

cross_workload_shadow_hits_w
= count(shadow hit within the window)

useful_eviction_ratio_w
= cross_workload_shadow_hits_w / window_size
```

![figure](image.png)

*그림 3. self eviction은 workload 내부 pressure를 설명하는 보조 관측값이고, cross-workload case의 useful eviction ratio가 런타임 quota 조정 기준이 된다.*

![figure](image.png)

해석은 단순하다.

```
useful_eviction_ratio_w 높음
→ w의 hot prefix가 다른 workload에게 밀리고 있음
→ quota_w를 cap_w 방향으로 올림

useful_eviction_ratio_w 낮음
→ 추가 보호의 근거가 약함
→ quota_w를 floor_w 방향으로 내림
```

비율 기반 신호는 작은 표본에서 쉽게 튄다. 따라서 workload별로 최근 `window_size`개의 cross-workload eviction 사건을 하나의 window로 삼고, 그 안에서 useful eviction으로 확인된 사건 비율을 계산한다.

### 3.2 Self eviction

`evictor == victim`인 self eviction은 workload 내부 cache pressure를 보여 준다.

예를 들어 `Chat ← Chat`은 Chat이 자기 block끼리 경쟁한 사건이고, `Longctx ← Longctx`는 Longctx 내부에서 대량 prefix가 서로 밀어낸 사건이다. 이 값은 profiling과 분석에서 workload의 working-set 크기, 내부 churn, cache 수요를 이해하는 데 사용한다.

> 💡 self eviction은 workload 내부 pressure를 설명하는 보조 관측값이며, quota 조정의 직접 입력으로 사용하지 않는다. 런타임 quota 조정은 `evictor != victim`인 cross-workload useful eviction ratio를 기준으로 한다.

---

## 4. Profile curve와 ratio 기준선

profile 단계에서는 먼저 workload별 quota와 useful eviction ratio의 관계를 얻는다. quota 후보를 sweep하면, quota가 늘어날수록 useful eviction ratio가 낮아지는 profile curve를 관측할 수 있다. (그림 2)

```
quota 후보: 0%, 5%, 10%, 15%, ...
관측값: cross-workload useful eviction ratio, shadow hit, cache hit, miss
```

그 다음 여러 `ratio_low/high` 후보를 평가한다.

```
ratio 후보: (low=0.05, high=0.20), (low=0.10, high=0.30), ...
평가값: cache hit, recomputation 감소, cross-workload useful eviction 감소, 처리량
선택값: profile 결과가 가장 좋은 ratio_low/high
```

### 4.1 ratio_high와 floor

`ratio_high`는 quota 부족을 판단하는 useful eviction ratio 상한이다. profile curve에서 `ratio_high`에 대응되는 quota를 workload `w`의 `floor_w`로 둔다.

직관적으로는 `useful_eviction_ratio_w`가 이 값보다 높을 때 workload `w`의 cached prefix가 다른 workload에게 자주 밀린다는 뜻이다.

### 4.2 ratio_low와 cap

`ratio_low`는 quota 여유를 판단하는 useful eviction ratio 하한이다. profile curve에서 `ratio_low`에 대응되는 quota를 workload `w`의 `cap_w`로 둔다.

직관적으로는 `useful_eviction_ratio_w`가 이 값보다 낮을 때 workload `w`의 quota를 줄여도 cross-workload useful eviction 증가가 작다는 뜻이다.

### 4.3 profile 결과

profile 단계의 결과는 ratio 기준선과 workload별 quota 범위다.

```
ratio_low <= useful_eviction_ratio_w <= ratio_high

Chat:    floor_chat    <= quota_chat    <= cap_chat
RAG:     floor_rag     <= quota_rag     <= cap_rag
Longctx: floor_longctx <= quota_longctx <= cap_longctx
Agent:   floor_agent   <= quota_agent   <= cap_agent
```

이 범위는 실험 설정, cache 크기, workload mix가 바뀌면 다시 profile한다.

---

## 5. 런타임 quota 조정

매 tick마다 최근 window의 cross-workload useful eviction ratio를 계산하고, `ratio_low/high`와 비교한다.

```
useful_eviction_ratio_w > ratio_high
→ candidate_quota_w = quota_w(now) + step

useful_eviction_ratio_w < ratio_low
→ candidate_quota_w = quota_w(now) - step

ratio_low <= useful_eviction_ratio_w <= ratio_high
→ candidate_quota_w = quota_w(now)
```

그 다음 quota 범위를 적용한다.

```
quota_w(next)
= clamp(candidate_quota_w, floor_w, cap_w)
```

예를 들어 `step=30`이고 현재 `quota_w=250`일 때:

```
ratio > ratio_high → quota_w(next) = 280
ratio < ratio_low  → quota_w(next) = 220
ratio가 기준선 안 → quota_w(next) = 250
```

마지막 clamp는 quota가 profile curve에서 얻은 범위 밖으로 나가지 않게 한다.

$$
floor_w \leq quota_w \leq cap_w
$$

> 💡 전체 quota 합이 evictable cache pool을 넘는 경우에는 `floor`를 먼저 보장하고, 남은 공간을 ratio가 큰 workload에 우선 배분한다.

---

## 6. 한 사이클

```
1. 집계
   지난 window 동안 workload별 eviction attribution과 shadow cache hit를 모은다.

2. cross-workload 사건 선택
   victim = w, evictor != w인 eviction만 useful eviction ratio 계산에 사용한다.

3. window 구성
   workload별 최근 window_size개의 cross-workload eviction 사건을
   useful_eviction_ratio_w 계산 window로 사용한다.

4. 신호 계산
   useful_eviction_ratio_w =
   cross_workload_shadow_hits_w / window_size

5. 기준선 비교
   useful_eviction_ratio_w > ratio_high 이면 quota_w를 증가시킨다.
   useful_eviction_ratio_w < ratio_low 이면 quota_w를 감소시킨다.
   ratio_low <= useful_eviction_ratio_w <= ratio_high 이면 quota_w를 유지한다.

6. 변화량 제한
   tick당 최대 이동량 step 안에서 quota_w를 이동시킨다.

7. 범위 적용
   floor_w <= quota_w <= cap_w를 만족시킨다.

8. eviction policy 적용
   다음 window 동안 quota를 기준으로 cached prefix block을 보호한다.
```

---

## 7. Eviction policy 적용

evictable cached prefix block이 부족해지면, 각 workload의 현재 occupancy와 quota를 비교한다.

```
occupancy_w > quota_w
→ w는 자기 quota보다 많이 쌓은 상태
→ w의 evictable block을 우선 후보로 둠

occupancy_w <= quota_w
→ w는 quota 안에 있음
→ 다른 초과 workload보다 늦게 후보가 됨
```

![figure](image.png)

예시는 다음과 같다.

```
Chat quota = 300, Chat occupancy = 250
→ Chat은 quota 안에 있으므로 보호 우선순위가 높다.

Longctx quota = 100, Longctx occupancy = 180
→ Longctx는 quota를 초과했으므로 eviction 우선 후보가 된다.
```

> 💡 QuotaServe는 `occupancy_w > quota_w`인 workload를 우선하고, 선택된 workload 안에서는 기존 LRU로 cached prefix block을 evict한다.

---

## 8. 지연 관측과 안정성

shadow cache 기반 useful eviction은 eviction 이후의 재요청으로 확정된다. block이 evict된 뒤 나중에 같은 block이 다시 필요해지는 순간 useful eviction으로 확인된다.

이 지연을 견디기 위해 다음 장치를 둔다.

- **event-count window**: workload별 최근 `window_size`개의 cross-workload eviction 사건으로 `useful_eviction_ratio_w`를 계산한다.
- **tick당 변화량 제한**: quota가 한 번에 크게 움직이는 것을 막는다.
- **ratio_low/high 기준선**: useful eviction ratio가 기준선 밖으로 벗어날 때 quota를 움직인다.
- **profile 기반 floor/cap**: quota가 profile curve에서 얻은 범위 안에서 움직인다.
- **reuse-delay 고려**: Chat처럼 turn 사이 think-time gap이 있는 workload는 window를 충분히 길게 둔다.

---

## 9. 이 문서의 범위

이 문서는 QuotaServe 제어 루프의 원형을 정의한다. 다음 값들은 구현과 실험 과정에서 맞춰 갈 파라미터다.

- tick
- `window_size`
- `ratio_low`, `ratio_high`
- tick당 quota 변화량 `step`
- 전체 evictable cache pool 안에서 quota를 정규화하는 방식
