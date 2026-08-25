# QuotaServe Static Quota 구현 계획

## 1. 목표

Static Quota version의 목표는 workload별 고정 quota만으로 기존 LRU보다 나은 TTFT와 SLO를 만족할 수 있는지 확인하는 것이다.

이 버전에서는 workload별 `quota_w`를 YAML config에 고정값으로 넣고, 실행 중에는 이 값을 자동으로 조정하지 않는다. 즉 `ratio_low`, `ratio_high`, `tick`, `window_size`, `step`은 아직 사용하지 않는다.

핵심 정책은 다음과 같다.

```text
occupancy_w > quota_w
→ workload w가 자기 quota보다 많은 evictable cached prefix block을 들고 있음
→ eviction이 필요할 때 workload w를 우선 victim workload 후보로 선택
→ 선택된 workload 내부에서는 기존 LRU로 block evict
```

따라서 Static Quota는 기존 LRU를 완전히 대체하는 정책이 아니라, **LRU victim selection 전에 workload-level quota filter를 하나 추가하는 정책**이다.

---

## 2. Config 설계

Static mode를 위해 `mode=static`을 추가한다.

예시 YAML:

```yaml
quota_serve:
  enabled: true
  mode: static

  log_path: "./quotaserve_static_log.jsonl"

  workloads:
    chat:
      quota_ratio: 0.30
    rag:
      quota_ratio: 0.08
    longctx:
      quota_ratio: 0.10
    agent:
      quota_ratio: 0.25
```

`quota_ratio`는 기준 block 수 대비 해당 workload가 보호받을 목표 cached-prefix quota 비율이다.

예를 들어 기준 block 수가 10,000이고:

```yaml
chat:
  quota_ratio: 0.30
```

이면:

```text
quota_chat = 10,000 × 0.30 = 3,000 blocks
```

Static mode에서는 이 값이 실험 중 변하지 않는다.

기존 config loader의 원칙은 유지한다.

```text
환경변수 > YAML config
```

또한 `enabled=False` 또는 `mode=off`일 때는 QuotaServe 로직이 완전히 비활성화되어 baseline LRU와 동일하게 동작해야 한다.

---

## 3. Workload tag 추출

각 request에서 workload tag를 추출한다.

예상 workload tag는 다음과 같다.

```text
chat
rag
longctx
agent
unknown
```

```text
request.workload_tag = extract_workload_tag(request)
```

tag 추출 실패 시 `unknown`으로 설정하며, config에 정의되지 않은 workload는 quota 정책에서 제외하고 baseline LRU와 동일하게 취급한다.

이 tag는 이후 block owner 기록, occupancy counter, eviction attribution에 사용된다.

---

## 4. Block owner 기록

Block owner는 block이 특정 request에 할당되는 시점에 설정한다. 즉, KV block이 새 request에 allocation될 때 해당 request의 workload tag를 owner로 기록한다.

```text
block.owner_workload = request.workload_tag
```

중요한 점은 block이 eviction되거나 free 상태로 돌아간 뒤, 다른 request에 재사용될 때는 새로운 owner로 overwrite되어야 한다는 것이다.

```text
block이 free 상태 → 새로운 request에 allocation
→ block.owner_workload = new_request.workload_tag (overwrite)
```

따라서 owner는 block의 lifetime 동안 고정된 값이 아니라, allocation 단위로 갱신되는 속성이다.

필요한 block metadata는 다음과 같다.

```text
block_hash
owner_workload
ref_cnt
is_cached_prefix
last_access_time
```

Eviction 시점에는 해당 block의 `owner_workload`를 사용하여 victim workload를 판단한다.

---

## 5. Occupancy counter 구현

Static Quota에서 `occupancy_w`는 workload `w`가 현재 들고 있는 evictable cached prefix block 수이다.

정의는 다음과 같다.

```text
occupancy_w =
count(block where
    block.owner_workload == w
    and block.ref_cnt == 0
    and block.is_cached_prefix == True
)
```

즉 running request가 현재 사용 중인 `ref_cnt > 0` block은 occupancy에 포함하지 않는다. QuotaServe는 running KV를 제한하는 정책이 아니라, eviction 가능한 cached prefix block만 대상으로 한다.

### 5.1 안전한 counter 관리 (flag 기반)

단순히 이벤트마다 `+=1 / -=1`로 counter를 갱신하면, cache 등록, hit/touch, ref_cnt 감소, eviction 등 여러 경로가 섞일 때 double decrement 또는 누락이 발생하기 쉽다.

이를 방지하기 위해 block이 현재 occupancy에 포함되어 있는지를 나타내는 flag를 둔다.

```text
block.is_counted_as_evictable_cached
```

counter 갱신은 lock 안에서 flag 확인과 함께 수행한다.

### 5.2 상태 전이 기반 업데이트

counter는 이벤트 자체가 아니라 상태 전이를 기준으로 갱신한다.

```text
조건:
evictable_cached 상태 =
    (block.ref_cnt == 0 and block.is_cached_prefix == True)
```

전이 규칙:

```text
False → True 전이
→ block.is_counted_as_evictable_cached == False
→ occupancy[owner_workload] += 1
→ block.is_counted_as_evictable_cached = True

True → False 전이
→ block.is_counted_as_evictable_cached == True
→ occupancy[owner_workload] -= 1
→ block.is_counted_as_evictable_cached = False
```

### 5.3 이벤트별 처리 예시

```text
1. block이 ref_cnt=0 cached prefix 상태가 됨
→ False → True 전이이면 +1

2. cache hit으로 ref_cnt>0이 됨
→ True → False 전이이면 -1

3. eviction 발생
→ True 상태였다면 -1 후 제거

4. 단순 touch (LRU 갱신)
→ 상태 변화 없음 → counter 변화 없음
```

이 방식으로 구현하면 동일 이벤트가 여러 경로에서 호출되더라도 **중복 감소/증가를 방지**할 수 있다.

### 5.4 consistency check

구현 초기에는 counter consistency check를 반드시 넣는다.

```text
sum(occupancy_w)
==
실제 ref_cnt=0 cached prefix block 수
```

이 값이 맞지 않으면 owner 기록, 상태 전이 처리, eviction hook 중 하나가 잘못된 것이다.

---

## 6. Static quota 계산

실행 시작 시 workload별 `quota_ratio`를 실제 block 수로 변환한다.

```text
quota_w = quota_ratio_w × quota_base_blocks
```

초기 구현에서는 `quota_base_blocks`를 전체 KV cache block 수로 둔다. 이는 MVP 단계에서는 충분히 합리적인 선택이지만, 실제 제어 대상은 `ref_cnt=0`인 evictable cached prefix block이므로, 동일한 `quota_ratio`라도 실험 환경이나 workload mix에 따라 의미가 달라질 수 있다.

따라서 다음 값은 반드시 로그에 기록해야 한다.

```text
quota_base_blocks
```

이를 통해 실험 결과 해석 시 quota_ratio가 어떤 절대 block 수 기준에서 적용되었는지 명확히 알 수 있어야 한다.

예시:

```text
quota_base_blocks = 10,000

chat.quota_ratio = 0.30
longctx.quota_ratio = 0.10

quota_chat = 3,000
quota_longctx = 1,000
```

Static mode에서는 `quota_w`가 runtime feedback에 의해 변하지 않는다.

---

## 7. Victim selection 구현

`occupancy_w > quota_w` 기반 2-tier victim selection을 구현한다.

기본 흐름은 다음과 같다.

```text
새 KV block 필요
↓
free block 있음?
  → 있으면 eviction 없이 free block 사용
↓
free block 없음
  → cached prefix block 중 victim 선택 필요
↓
QuotaServe active?
  → 아니면 global LRU
↓
occupancy_w > quota_w 인 workload가 있음?
  → 있으면 해당 workload의 cached prefix block 중 LRU를 evict
  → 없으면 global LRU fallback
```

pseudocode:

```python
def select_victim(current_request):
    if not quota_serve_config.is_active:
        return global_lru_victim(), "baseline_lru_off_mode"

    over_quota_workloads = {
        w for w in workloads
        if occupancy[w] > quota[w]
    }

    if not over_quota_workloads:
        return global_lru_victim(), "fallback_no_over_quota"

    for block in scan_lru_head_until_found():
        if (
            block.ref_cnt == 0
            and block.is_cached_prefix
            and block.owner_workload in over_quota_workloads
        ):
            return block, "over_quota_selected"

    raise RuntimeError(
        "over-quota workload exists but no evictable cached block was found"
    )
```

`scan_lru_head_until_found`는 vLLM free queue의 front/head부터 순회한다. 즉 LRU victim은 `popleft()`로 선택되는 가장 오래된 block이므로, 이 순회에서 처음 만나는 over-quota workload의 cached prefix block이 곧 over-quota workload 집합 안에서 가장 LRU인 block이다.

이 방식은 over-quota workload의 evictable cached block이 존재하면 반드시 그 집합 안에서 LRU 순으로 victim을 고른다.

---

## 8. Eviction attribution과 shadow cache

Eviction이 발생할 때 다음 정보를 기록한다.

```text
evictor_workload = current_request.workload_tag
victim_workload = evicted_block.owner_workload
block_hash = evicted_block.hash
selection_reason = over_quota_selected / fallback_no_over_quota / baseline_lru_off_mode
```

`evictor_workload != victim_workload`이면 cross-workload eviction이다.

Static mode에서는 shadow cache가 victim selection에 직접 사용되지는 않지만, 실험 분석을 위해 유지한다.

Shadow cache는 다음을 확인하기 위해 필요하다.

```text
evict된 block이 나중에 다시 요청되었는가?
즉 evict하지 않았다면 cache hit이 되었을 useful eviction인가?
```

이 정보는 static quota sweep 결과를 분석하고, 이후 dynamic version의 `ratio_low/high`를 정하는 근거로 사용한다.

---

## 9. Logging

Static Quota 실험에서는 다음 로그를 남긴다.

### 9.1 Quota state log

Static mode에서는 `quota_w`와 `quota_base_blocks`가 상수이고, 정책이 판단하는 유일한 순간이 eviction이다. 따라서 **주기적 tick logger를 두지 않는다.** 대신:

- **시작 시 1회**: 상수인 `quota_base_blocks`와 workload별 `quota_w`를 기록한다.

```text
(startup, 1회)
quota_base_blocks
workload별 quota_w
```

- **occupancy는 eviction 시점에만** 기록한다. 이때 전체 workload occupancy 스냅샷을 함께 남겨(§9.2의 `occupancy_snapshot`), 그 eviction 순간 어느 workload가 over/under-quota였는지 재구성할 수 있게 한다.

> tick마다 `quota_w`가 변하는 dynamic version에서는 주기적 quota state log가 다시 필요하다.

### 9.2 Eviction event log

```text
time
evictor_workload
victim_workload
selection_reason
block_hash
is_cross_workload
victim_occupancy
victim_quota
occupancy_snapshot   # {workload: occupancy_w} — eviction 순간 전체 workload 스냅샷
scan_steps           # LRU head부터 selector가 확인한 block 수
```

일반적인 allocation eviction에서는 `selection_reason`이
`over_quota_selected`, `fallback_no_over_quota`, 또는
`baseline_lru_off_mode` 중 하나가 된다. Selector를 거치지 않는 외부
eviction 경로는 `external_eviction`으로 기록한다.

### 9.3 Summary metric

실험이 끝난 뒤 계산한다.

```text
over_quota_selected count
fallback_no_over_quota count
baseline_lru_off_mode count
workload별 eviction count
workload별 cross-workload eviction count
workload별 useful eviction count
workload별 useful eviction ratio
```

특히 다음 두 값은 반드시 확인한다.

```text
over_quota_selected ratio
fallback_no_over_quota ratio
```

`over_quota_selected ratio`는 quota policy가 실제 victim selection에 얼마나 개입했는지를 보여 준다. `fallback_no_over_quota ratio`가 높으면 대부분의 eviction 시점에 quota를 초과한 workload가 없었다는 뜻이다.

---

## 10. Sanity check

구현 후 먼저 다음을 확인한다.

1. `enabled=False` 또는 `mode=off`일 때 baseline LRU와 동일하게 동작하는가?
2. block owner가 allocation 시점에 올바르게 설정되고, 재사용 시 overwrite되는가?
3. `occupancy_w`가 실제 `ref_cnt=0 cached prefix block` 수와 일치하는가?
4. `quota_w`가 config의 `quota_ratio`대로 계산되는가?
5. `quota_base_blocks`가 로그에 정확히 기록되는가?
6. `occupancy_w > quota_w`인 workload 집합 안에서 LRU 순으로 victim이 선택되는가?
7. over-quota workload가 없으면 global LRU fallback이 동작하는가?
8. over-quota workload가 있는데 해당 workload의 evictable cached block을 찾지 못하면 exception으로 처리되는가?
9. shadow cache 기반 useful eviction logging이 정상적으로 기록되는가?

---

## 11. 1차 실험 목표

Static Quota mode의 목적은 dynamic controller 없이도 workload-level victim selection만으로 LRU보다 나은 cache behavior가 나오는지 확인하는 것이다.

핵심 질문은 다음과 같다.

```text
고정 quota만으로 Longctx/RAG가 Chat hot prefix를 밀어내는 useful eviction을 줄일 수 있는가?
```

주요 지표는 다음과 같다.

```text
chat <- longctx useful eviction count
chat <- rag useful eviction count
Chat hit rate
Chat APC gain
Chat TTFT
over-quota selected ratio
fallback_no_over_quota ratio
workload별 occupancy/quota 변화
```

SLO와 throughput은 이번 단계에서 controller나 quota 설정의 직접 기준으로 사용하지 않는다. 문제가 관측되면 이후 guardrail로 추가하는 방향을 검토한다.

---

## 12. Static quota sweep 계획

Static Quota는 이후 dynamic version의 `ratio_low/high`를 정하기 위한 profile 실험으로도 사용한다.

먼저 workload별 `quota_ratio`를 바꿔가며 useful eviction ratio가 어떻게 변하는지 관찰한다.

예시 sweep:

```text
Chat quota_ratio:
0.10, 0.20, 0.30, 0.50

Longctx quota_ratio:
0.05, 0.10, 0.20
```

분석 목표:

```text
quota_ratio 증가에 따라
- chat useful eviction ratio가 낮아지는가?
- chat useful eviction count가 줄어드는가?
- Chat hit rate가 증가하는가?
- over_quota_selected ratio가 어떻게 변하는가?
- fallback_no_over_quota ratio가 과도하게 높지는 않은가?
```

이 결과를 바탕으로 dynamic version에서 사용할 `ratio_low/high` 후보를 잡는다.

```text
ratio_high:
이 값보다 높으면 under-protected 상태라고 볼 수 있는 useful eviction ratio

ratio_low:
이 값보다 낮으면 추가 보호 이득이 작아지는 useful eviction ratio
```

---

## 13. 구현 순서

1. `mode=static`의 의미를 fixed quota policy로 정의
2. config schema에 `quota_ratio` 추가 (`quota_serve` top key 기준)
3. workload tag 추출 구현
4. block owner metadata 추가 (allocation 시 설정, 재사용 시 overwrite)
5. flag 기반(`is_counted_as_evictable_cached`) occupancy counter 구현
6. occupancy consistency check 실패 시 exception 처리
7. `quota_ratio`를 `quota_w` block 수로 변환
8. `occupancy_w > quota_w` workload 집합 안에서 LRU victim을 고르는 strict victim selection 구현
9. selection_reason logging 추가
10. eviction attribution log 연결
11. shadow cache 기반 useful eviction logging 확인
12. off mode baseline 동일성 확인
13. static sanity test
14. Chat+Longctx / Chat+RAG static sweep 실행
