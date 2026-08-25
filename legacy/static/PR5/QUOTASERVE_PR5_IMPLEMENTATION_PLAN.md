# QuotaServe PR5 구현 계획

## 1. 목표

PR5의 목표는 victim selection이나 quota를 바꾸는 것이 아니다.
eviction 이후 victim workload가 같은 prefix block을 다시 필요로 했는지를
runtime에서 관측하고, Dynamic controller가 사용할
`useful_eviction_ratio_w`를 계산하는 것이다.

```text
실제 cached block eviction
→ eviction hash와 victim/evictor workload 기록
→ 나중에 같은 hash가 다시 계산되어 cache에 등록되는지 확인
→ cross-workload useful eviction 여부 확정
→ workload별 window ratio log 기록
```

PR5가 끝나도 eviction policy는 PR4와 같아야 한다.

## 2. 범위

### 포함

- 기존 pending eviction/reuse 흐름을 runtime shadow cache로 재사용
- cross-workload eviction만 signal window에 집계
- victim workload의 동일 block 재요청을 useful eviction으로 1회 집계
- shadow entry TTL 만료 처리
- workload별 event-count window 관리
- `useful_eviction_ratio_w` 계산 및 quota signal log 기록
- `mode=off` parity 유지

### 제외

- `quota_w` 증가/감소
- `ratio_low`, `ratio_high` 판단
- `floor`, `cap`, `step` 적용
- victim selector 변경
- occupancy counter 변경
- global LRU 순서 변경
- static quota sweep 결과에 따른 자동 설정

## 3. 현재 코드에서 재사용할 흐름

현재 PR4의 [block_pool.py](../../vllm/vllm/v1/core/block_pool.py)에 이미
다음 흐름이 있다.

```text
_maybe_evict_cached_block()
→ _remember_eviction_event()
→ _pending_evictions[block_hash]에 event 저장

cache_full_blocks()
→ _complete_pending_reuse(block_hash)
→ pending event의 reused_later 갱신
```

PR5에서는 같은 hash 매칭 경로를 새로 만들지 않는다. 기존 pending event를
shadow entry로 사용하고, runtime counter와 signal log만 추가한다.

## 4. 데이터 정의

### 4.1 Shadow entry

기존 eviction event에 다음 의미를 적용한다.

```text
block_hash       : evicted prefix block hash
victim_workload  : evicted_workload
evictor_workload : trigger_workload
eviction_time    : eviction 시각
counted          : useful eviction을 이미 집계했는지 여부
```

`reused_later`는 기존 eviction log와 호환성을 위해 유지한다.

### 4.2 집계 대상

```text
evictor_workload != victim_workload
→ cross-workload signal window에 추가

evictor_workload == victim_workload
→ self eviction. 보조 관측만 하고 useful ratio 분모에서 제외
```

이후 같은 hash가 다시 나타났을 때 다음 조건을 모두 만족해야 useful로
집계한다.

```text
재요청이 실제 cache miss/recompute 경로에서 발생
재요청 workload == victim_workload
shadow entry가 TTL 안에 있음
counted == False
```

조건을 만족하면 해당 window entry를 useful로 바꾸고 한 번만 카운트한다.

## 5. Window와 ratio

workload별로 최근 cross-workload eviction event를 보관한다.

```text
cross_workload_evictions_w
= victim workload가 w이고 evictor != w인 최근 event 수

cross_workload_useful_evictions_w
= 그중 같은 block 재요청이 확인된 event 수

useful_eviction_ratio_w
= cross_workload_useful_evictions_w
  / cross_workload_evictions_w
```

window가 아직 가득 차지 않은 시작 구간에서는 실제로 관측된 event 수를
분모로 기록하고, `sample_count`도 함께 log에 남긴다. Dynamic controller는
PR7에서 충분한 sample이 있는지 판단한다.

`window_size`는 event 개수 기준이다. 시간 기준 window를 새로 만들지 않는다.

## 6. TTL과 메모리 상한

- `shadow_ttl_sec`가 지난 pending entry는 signal에서 제거한다.
- 기존 `_MAX_PENDING_EVICTIONS` 상한을 유지한다.
- 상한 초과로 entry를 버릴 때는 useful로 집계하지 않고, 필요하면
  `shadow_expired` 또는 `shadow_dropped` 수만 signal log에 기록한다.
- 별도의 무제한 hash map을 만들지 않는다.

## 7. 수정 대상과 역할

### 7.1 `vllm/vllm/v1/core/block_pool.py`

- 실제 cache map 제거가 확인된 뒤 runtime signal에 eviction event 전달
- 기존 `_pending_evictions`와 `_complete_pending_reuse()`를 shadow 매칭에 사용
- reuse 시 request workload를 signal에 전달할 수 있는지 확인
- allocation/reuse/free 같은 기존 opportunistic 지점에서 signal tick 호출
- 기존 `VLLM_EVICTION_LOG` event 형식은 깨지지 않게 유지

여기서 occupancy를 직접 수정하지 않는다. occupancy는 PR3 collector hook이
계속 담당한다.

### 7.2 `vllm/vllm/v1/core/kv_cache_metrics.py`

필요한 경우에만 runtime signal 전달용 no-op hook을 추가한다.

```text
base collector: no-op
QuotaServeCollector: signal counter 갱신
```

기존 lifecycle hook의 의미를 바꾸지 않는다. 새 hook이 필요하지 않다면
`BlockPool`에서 현재 collector의 작은 method를 호출하는 방식으로 끝낸다.

### 7.3 `vllm/vllm/quota_serve/collector.py`

다음 상태를 occupancy 상태와 분리해 관리한다.

```text
workload별 cross-workload event window
workload별 useful event count
workload별 expired/dropped count
last_signal_log_time
```

occupancy counter와 useful eviction counter를 같은 값으로 취급하지 않는다.

### 7.4 `vllm/vllm/quota_serve/config.py`

기존 값을 재사용한다.

```text
tick_sec        : signal log 주기
shadow_ttl_sec  : shadow entry 보관 시간
log_path        : quota signal log 경로
```

`window_size`가 현재 schema에 없으면 PR5에서 추가한다. `ratio_low/high`,
`floor/cap`, `step`은 PR5 config에 추가하지 않는다.

### 7.5 `vllm/vllm/v1/core/sched/scheduler.py`

- runtime signal 객체가 별도로 필요할 때만 생성/주입
- signal log를 남기되 quota를 조정하지 않음
- `mode=off`에서는 signal을 비활성화

별도 background thread나 controller loop는 만들지 않는다. 기존 allocation,
free, reuse 경로에서 opportunistic tick을 호출한다.

## 8. Log schema

기존 eviction event log와 별도로 `QUOTA_SERVE_LOG`에 signal snapshot을
기록한다.

```json
{
  "type": "useful_eviction_signal",
  "ts": 123.45,
  "workload": "chat",
  "window_size": 1000,
  "sample_count": 1000,
  "cross_workload_evictions": 1000,
  "useful_evictions": 237,
  "useful_eviction_ratio": 0.237,
  "shadow_expired": 12,
  "shadow_dropped": 0
}
```

여러 workload의 snapshot은 한 tick에서 각각 한 event로 기록한다. 기존
`VLLM_EVICTION_LOG`의 `selection_reason`, `victim_quota` 등 PR4 필드는
그대로 유지한다.

## 9. 구현 순서

### PR5-1. 계약 고정

- useful eviction의 정확한 조건을 테스트 case로 먼저 정의
- self eviction 제외 확인
- victim workload와 재요청 workload가 같은지 확인
- 한 shadow entry당 최대 1회 집계 확인

### PR5-2. Runtime reuse 연결

- 기존 `_remember_eviction_event()`와 `_complete_pending_reuse()` 흐름을
  읽고 signal callback 위치 확정
- 필요하면 base collector에 no-op callback 추가
- request workload가 reuse 시점에 전달되는지 확인

### PR5-3. Window와 TTL 구현

- workload별 event window 추가
- `window_size` 상한 적용
- `shadow_ttl_sec` 만료 정리
- ratio와 sample count 계산

### PR5-4. Signal log 연결

- `tick_sec` 기준 opportunistic snapshot 기록
- reset/shutdown/flush 시 pending signal flush
- `mode=off`에서 log와 counter가 동작하지 않는지 확인

### PR5-5. 단위 검증

최소한 다음 case를 직접 검증한다.

```text
cross eviction 후 victim workload가 같은 hash 재요청
→ useful 1회

같은 hash를 두 번 재요청
→ useful 1회만

self eviction 후 재요청
→ cross useful 0회

다른 workload가 재요청
→ victim workload useful 0회

TTL 만료 후 재요청
→ useful 0회

window_size 초과
→ 가장 오래된 event부터 제거
```

### PR5-6. Integration 검증

- `mode=static`으로 Chat+Longctx smoke 실행
- PR4 victim selection 결과가 바뀌지 않는지 확인
- `useful_eviction_signal` log가 workload별로 생성되는지 확인
- `mode=off`에서 baseline LRU와 log 동작이 기존과 같은지 확인
- full workload 실행 후 signal ratio와 offline eviction log의 useful 결과를
  비교한다.

## 10. PR5 완료 기준

- selector가 PR4와 동일하게 동작함
- occupancy counter consistency가 유지됨
- cross-workload event만 ratio window에 포함됨
- useful event가 victim workload 기준으로 정확히 집계됨
- duplicate reuse가 중복 집계되지 않음
- TTL과 window 상한이 동작함
- `QUOTA_SERVE_LOG`에 workload별 ratio snapshot이 기록됨
- `mode=off` parity가 유지됨
- quota 값은 한 번도 자동으로 변경되지 않음

## 11. PR5 이후

PR5 signal이 검증되면 static quota sweep을 실행한다.

```text
PR6
→ quota와 useful_eviction_ratio profile curve 생성
→ ratio_low/high 결정
→ floor/cap 결정

PR7
→ ratio 기준으로 quota_w를 ±step 조정
→ floor/cap으로 clamp
→ dynamic victim selection에 연결
```

PR5 구현 중에는 PR6/PR7 코드를 미리 넣지 않는다.
