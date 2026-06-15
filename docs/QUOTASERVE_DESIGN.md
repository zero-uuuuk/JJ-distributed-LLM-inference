# QuotaServe 제어 루프 — PFF식 원형

## 한 줄 요약

> 적정 cache량을 미리 계산하지 않는다.
> 대신 두 가지 증상을 본다. **쫓겨난 block이 나중에 다시 필요했는가**라는 피해 신호와, **쌓아 둔 block이 거의 다시 쓰이지 않는가**라는 낭비 신호다.
> 이 신호를 보고 workload별 cached-prefix quota를 천천히 조정하되, 두 기준선 사이에서는 아무것도 건드리지 않는다.

---

## 용어

본문에서 반복적으로 쓰는 용어를 먼저 정리한다. 이후 설명은 이 정의를 전제로 한다.

| 용어 | 뜻 |
|---|---|
| **cached prefix block** | running 요청이 끝나 `ref=0`이 된 prefix block. eviction 후보가 되는, QuotaServe가 다루는 대상이다. |
| **quota** | workload별로 cached prefix block을 얼마나 보호하거나 쌓을 수 있는지를 정하는 할당량. floor와 cap 두 형태를 가진다(§4). |
| **floor** | 보호 하한. "이 workload의 cached prefix block을 최소 이만큼은 보호한다." |
| **cap** | 누적 상한. "이 workload의 cached prefix block은 이 이상 쌓지 않는다." |
| **피해 신호** | useful-eviction-suffered rate. 쫓겨난 block이 나중에 다시 필요해진 비율(§3.1). |
| **낭비 신호** | low-reuse / self-churn rate. 재사용이 낮거나 자기 block끼리 밀어내는 비율(§3.2). |
| **shadow cache** | 쫓겨난 block의 hash를 기록해 두는 그림자 cache. 나중에 같은 block이 다시 필요해지는지를 추적한다. |
| **eviction attribution** | 누가 누구의 block을 쫓아냈는지 기록하는 장치. |
| **tick** | 제어 루프가 한 번 도는 주기. |
| **window** | 신호를 집계하는 관측 구간. |
| **upper / lower** | 신호를 판정하는 위쪽 / 아래쪽 기준선. 둘 사이는 유지 구간이다. |

---

## 1. 원형 — 운영체제의 PFF

QuotaServe의 골격은 운영체제의 PFF(Page-Fault Frequency)에서 가져왔다. 먼저 그 발상을 본다.

프로세스마다 memory frame을 얼마씩 줘야 할지 정확히 푸는 것은 어렵다. PFF는 그 계산을 직접 풀지 않고 **관측 가능한 증상**을 대신 본다.

- fault가 너무 잦다 → working set을 담기 부족하다 → frame을 더 준다.
- fault가 너무 드물다 → 할당량이 남는다 → frame을 회수한다.

핵심은 두 가지다.

1. 적정량을 사전에 계산하지 않고, 값싼 feedback signal로 대신한다.
2. 기준선을 위/아래 둘로 둬서, 그 사이에서는 아무것도 하지 않는다.

두 번째 원칙이 특히 중요하다. 단일 기준선만 두면 signal이 기준선 근처에서 흔들릴 때 quota도 따라 흔들린다. 반면 upper/lower 두 기준선을 두면 signal이 그 사이에 있는 동안 quota를 그대로 유지하므로, 작은 측정 흔들림에 반응하지 않는다.

---

## 2. QuotaServe로 옮기기

QuotaServe도 같은 골격을 따른다. 다만 page fault 대신, prefix cache에서 직접 관측되는 두 종류의 증상을 본다.

| 구분 | OS의 PFF | QuotaServe |
|---|---|---|
| 조절 대상 | 프로세스별 frame 수 | workload별 cached prefix block quota |
| 부족 신호 | page fault rate | 피해 신호 (useful-eviction-suffered rate) |
| 낭비 신호 | 매우 낮은 fault rate | 낭비 신호 (low-reuse / self-churn rate) |
| 신호 출처 | fault 카운터 | eviction attribution + shadow cache |
| 너무 부족할 때 | frame 증가 | floor 증가 |
| 너무 남을 때 | frame 회수 | cap 감소 또는 floor 감소 |
| 진동 방지 | upper/lower bound | 기준선 사이 유지 구간 + tick당 변화량 제한 |

> [!NOTE]
> 여기서 한 가지를 분명히 해 둔다. **QuotaServe의 quota는 전체 KV block pool에 대한 throttle이 아니다.** running 요청이 지금 쓰고 있는 KV block은 건드리지 않고, 요청이 끝나 `ref=0`이 되어 eviction 후보가 된 cached prefix block에만 적용한다.
>
> 즉 목적은 in-flight 요청을 막는 것이 아니다. 이미 evictable해진 cache block 중에서 **어떤 workload의 hot prefix를 더 오래 보호할지**를 정하는 것이다.

---

## 3. 신호 정의

QuotaServe는 PFF처럼 단순한 feedback loop를 지향하지만, 한 신호에 모든 의미를 억지로 욱여넣지는 않는다. 피해와 낭비, 최소한 이 둘은 구분한다.

> [!IMPORTANT]
> 두 신호는 일부 workload에만 붙는 것이 아니다. **모든 workload가 자신의 피해 신호와 낭비 신호를 동시에 가진다.** Chat / RAG(Longctx) 구분은 어느 신호를 측정하느냐가 아니라, 어느 신호가 주로 기준선을 넘느냐의 차이일 뿐이다.

### 3.1 피해 신호 — useful-eviction-suffered rate

피해 신호는 workload `w`가 겪은 eviction 중 **나중에 다시 필요해진 정도**이다. `w`를 **피해자(victim)** 로 보고, 다음 사건을 workload별로 집계한다.

1. workload `w`의 cached prefix block이 evict된다.
2. 해당 block의 hash를 shadow cache에 기록한다.
3. 이후 `w`의 요청이 같은 block을 다시 필요로 한다.
4. 이때 shadow cache hit가 발생하면, 그 eviction을 `w` 입장의 useful eviction suffered로 집계한다.

이 값이 높다는 것은, 그 block이 evict되지 않았다면 cache hit으로 이어졌을 가능성이 높았다는 뜻이다. 곧 LRU가 아직 쓸모 있는 block을 너무 일찍 내보내고 있다는 신호다. 따라서 **피해 신호가 높은 workload는 floor를 올려 더 오래 보호한다.** reuse 가치는 크지만 turn 사이 think-time gap 때문에 LRU에서 aging out되기 쉬운 Chat이 대표적인 예다.

### 3.2 낭비 신호 — low-reuse / self-churn rate

낭비 신호는 workload `w`가 만든 cached block이 공간을 차지하거나 eviction을 일으켰지만, 이후 거의 다시 쓰이지 않는 정도를 나타낸다. 대표적으로 다음을 관측한다.

- `w ← w` eviction 중 `reused_later` 비율이 매우 낮다.
- `w`가 cache에 올린 block 중 이후 다시 hit되는 비율이 낮다.
- `w`가 block을 많이 생산하지만, shadow cache에서 useful reuse로 돌아오는 경우가 거의 없다.

이 값이 높다는 것은, `w`가 cache에 남겨 둔 block이 hot cache 보호에는 기여하지 않고 eviction pressure만 만든다는 뜻이다. 따라서 **낭비 신호가 높은 workload는 cap을 내려 누적을 억제한다.** 대용량 one-shot prompt를 많이 만들어 self-churn이 큰 Longctx가 대표적인 예다.

---

## 4. 두 종류의 quota

피해 신호와 낭비 신호가 가리키는 대응이 다르듯, quota도 workload 성격에 따라 두 가지 형태를 가진다.

### 4.1 보호 floor

floor는 "이 workload의 cached prefix block을 최소 이만큼은 보호한다"는 뜻으로, **피해 신호에 반응하는 제어 변수**다. multi-turn reuse가 있어 LRU에 쉽게 밀려나는 Chat이 floor의 대표적인 수혜자다.

- 피해 신호가 upper를 넘으면 floor를 올린다.
- 피해 신호가 lower보다 충분히 낮고 실제 사용량도 낮으면 floor를 천천히 내린다.
- 한 대화의 reusable prefix조차 담지 못할 만큼 작아지지 않도록 floor에 하한을 둔다.

### 4.2 제한 cap

cap은 "이 workload의 cached prefix block은 이 이상 쌓지 않는다"는 뜻으로, **낭비 신호에 반응하는 제어 변수**다. self-churn이 높고 `reused_later` 비율이 낮은 RAG, Longctx가 cap의 대표적인 대상이다.

- 낭비 신호가 upper를 넘으면 cap을 내린다.
- 낭비 신호가 lower보다 낮고, 해당 workload에서도 유의미한 hit가 관측되면 cap을 천천히 올린다.
- cap은 running KV를 제한하지 않으므로, 실행 중인 요청의 prefill/decode를 직접 throttle하지 않는다.

---

## 5. 한 사이클

매 tick(예: 10-30s)마다 다음 순서로 제어 루프를 돈다.

```text
1. 집계
   지난 window(예: 2-5분) 동안 workload별 eviction attribution, shadow cache hit,
   hit rate, cached-block occupancy를 모은다.

2. 신호 계산
   피해 신호 = useful-eviction-suffered rate
   낭비 신호 = low-reuse / self-churn rate

3. 비교
   각 신호를 workload별 upper/lower 기준선과 비교한다.

4. 조정
   피해 신호 > upper   → floor 증가
   피해 신호 < lower   → floor 감소 후보
   낭비 신호 > upper   → cap 감소
   낭비 신호 < lower   → cap 증가 후보
   upper/lower 사이    → 변경 없음

5. 변화량 제한
   한 tick에서 floor/cap이 움직일 수 있는 최대 block 수(예: 15 blocks) 를 제한한다.

6. 정규화
   floor 합과 cap 제약이 전체 evictable cache pool 안에서
   동시에 만족되도록 맞춘다.

7. 운영
   다음 window 동안 새 quota로 eviction policy를 적용하고, 다시 반복한다.
```

---

## 6. 정규화와 양보 규칙

floor 합이 전체 evictable cache pool을 넘거나, 한 workload의 floor 증가가 다른 workload의 공간을 줄여야 하는 상황이 생길 수 있다. 이때 QuotaServe는 quota를 임의로 깎지 않고, 다음 우선순위로 양보 대상을 고른다.

1. 피해 신호가 lower보다 낮은 workload
2. 낭비 신호가 높은 workload
3. 현재 occupancy가 자기 floor보다 충분히 큰 workload
4. 최근 window에서 hit rate가 낮은 workload

요컨대 먼저 회수되는 쪽은 "쫓겨나도 다시 필요해진 증거가 약하고, 많이 쌓지만 재사용은 적은" workload다. 반대로 useful eviction 피해가 계속 관측되는 workload의 floor는 마지막까지 보호한다.

정규화도 급격하게 하지 않는다. 필요한 회수량이 크더라도 tick당 변화량 제한을 거치며, 여러 tick에 걸쳐 천천히 수렴한다.

---

## 7. 지연 관측과 안정성

shadow cache 기반 useful eviction은 eviction 순간에 곧장 확정되지 않는다. block이 evict된 뒤 **나중에** 같은 block이 다시 필요해져야 비로소 useful eviction으로 확인된다.

Chat의 경우 다음 turn까지의 think-time gap이 수 초에서 수십 초에 이를 수 있다. 따라서 tick이 너무 짧거나 window가 너무 작으면, 피해가 아직 관측되기도 전에 quota를 잘못 움직일 수 있다.

이 지연을 견디기 위해 다음 장치를 둔다.

- **upper/lower 두 기준선**: signal이 두 기준선 사이에 있으면 quota를 바꾸지 않는다.
- **window 또는 EWMA**: 순간값이 아니라 최근 구간의 완만한 signal을 쓴다.
- **reuse-delay 고려**: useful eviction이 늦게 확정된다는 점을 window 길이와 해석에 반영한다.
- **tick당 변화량 제한**: quota를 한 번에 크게 바꾸지 않는다.
- **floor 하한**: floor가 한 대화의 reusable prefix조차 담지 못할 만큼 작아지지 않게 한다.
- **cap 하한**: cap이 0으로 내려가 cache를 완전히 금지하는 일이 없게 한다. 최소 공간이 남아 있어야 그 workload의 hit를 다시 관측하고 cap을 회복할 수 있다.

---

## 8. 이 문서의 범위

이 문서는 QuotaServe 제어 루프의 **원형**을 정의한다. 다음 값들은 구현과 실험 과정에서 맞춰 갈 조정 파라미터로 남긴다.

- tick (제어 주기)
- window 길이 또는 EWMA 계수
- 피해 신호의 upper / lower
- 낭비 신호의 upper / lower
- floor/cap의 tick당 변화량 상한
- floor/cap의 절대 하한과 상한

> [!TIP]
> 핵심은 수치를 미리 맞히는 것이 아니다. **evictable cached prefix block에 한정된 feedback loop**로 hot cache 피해는 줄이고 low-reuse cache 낭비는 회수하는 것, 그것이 이 설계의 전부다.
