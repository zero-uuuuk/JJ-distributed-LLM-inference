# VLLM_EDITED_PR5

## PR5의 목적

PR5에서는 eviction된 prefix block이 나중에 다시 요청되었는지를 runtime에서
확인하고, workload별 `useful_eviction_ratio`를 계산한다.

PR5는 signal을 수집하고 로그로 남기는 단계다. `quota_w`를 바꾸거나 victim
selection 정책을 추가로 변경하지 않는다. victim selection은 PR4 구현을 그대로
사용한다.

## 1. `vllm/quota_serve/collector.py`

### PR5-1: useful eviction 판정

`is_useful_eviction()`을 추가했다.

다음 조건을 모두 만족할 때만 useful eviction으로 판정한다.

- 실제 cache miss 이후의 재계산 경로에서 호출됨
- eviction이 cross-workload임
- 요청한 workload가 evicted block의 victim workload와 같음
- eviction 후 `shadow_ttl_sec` 안에 재사용됨
- 같은 eviction event가 아직 useful로 집계되지 않음

### PR5-2: reuse hook 처리

`on_block_reused(event, request)`를 구현했다.

`BlockPool._complete_pending_reuse()`에서 전달받은 request의 workload를 확인하고,
해당 eviction event를 useful eviction으로 한 번만 집계한다.

### PR5-3: window와 TTL

workload별로 cross-workload eviction event를 bounded window에 저장한다.

- `window_size`: workload별 window 최대 event 수
- `shadow_ttl_sec`: shadow event의 유효 시간
- window를 초과하면 가장 오래된 event를 제거
- TTL이 지나면 event를 제거
- `useful_eviction_snapshot()`에서 workload별 ratio를 반환

snapshot에는 다음 값이 포함된다.

```text
window_size
sample_count
cross_workload_evictions
useful_evictions
useful_eviction_ratio
shadow_expired
shadow_dropped
```

### PR5-4: signal log

`tick_sec`에 따라 signal snapshot을 opportunistic하게
`QUOTA_SERVE_LOG`에 JSONL로 기록한다. 별도 background thread는 만들지 않았다.

기록 형식은 다음과 같다.

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

eviction/reuse hook과 allocation/free 경로에서 tick을 확인하고, reset 또는
pending eviction flush 시에는 마지막 snapshot을 강제로 기록한다.

## 2. `vllm/quota_serve/config.py`

PR5 runtime signal에 필요한 설정을 추가하고 YAML/env에서 읽도록 했다.

```text
tick_sec
shadow_ttl_sec
window_size
log_path
```

`QUOTA_SERVE_LOG` 환경 변수는 config의 `log_path`를 override한다.

## 3. `vllm/v1/core/kv_cache_metrics.py`

기본 collector에 PR5 hook과 signal log interface를 no-op으로 추가했다.

```python
on_eviction_recorded(event)
on_block_reused(event, request)
maybe_log_signal(now=None, force=False)
flush_signal_log(force=True)
```

따라서 일반 `KVCacheMetricsCollector`와 `mode=off`에서는 useful eviction counter나
signal log가 동작하지 않는다. QuotaServeCollector만 해당 기능을 override한다.

## 4. `vllm/v1/core/block_pool.py`

### reuse request 전달

`cache_full_blocks()`에서 pending eviction이 재사용되는 시점에 request를
`_complete_pending_reuse()`까지 전달한다.

`_complete_pending_reuse()`는 event와 request를 collector의
`on_block_reused(event, request)`로 전달한다.

### eviction event 전달

eviction event가 생성되면 `on_eviction_recorded(event)`를 호출해 PR5 window에
등록한다. 이후 같은 hash가 다시 요청되면 reuse hook에서 useful 여부를 판정한다.

### signal log tick과 flush

- 새 block allocation 후 signal tick 확인
- block free 후 signal tick 확인
- `flush_pending_evictions()`에서 signal snapshot 강제 flush
- prefix cache reset 시 collector reset 과정에서 signal snapshot flush

또한 일반 collector가 연결된 `mode=off`에서는 signal 수집 대상이 아니므로
불필요한 pending signal event를 보관하지 않도록 구분한다.

### PR5 runtime 흐름

PR3의 occupancy counter 흐름과 PR5의 useful eviction signal 흐름은 다음처럼
연결된다.

```text
새 block 필요
  -> selector가 free queue에서 block 선택
  -> free_block_queue.remove(block)
  -> _maybe_evict_cached_block(block)
  -> cached_block_hash_to_block.pop(block_hash)
  -> on_block_evicted(block)
       -> victim workload occupancy -= 1
  -> _remember_eviction_event(...)
       -> on_eviction_recorded(event)
            -> cross-workload eviction만 victim workload window에 등록
  -> block.reset_hash()
  -> block.ref_cnt += 1
  -> on_block_allocated(block, request)
       -> 새 owner workload 기록
```

나중에 같은 prefix가 다시 요청되면 cache miss 후 prefix를 다시 계산하고,
`cache_full_blocks()`에서 다음 경로가 실행된다.

```text
같은 prefix 재요청
  -> cache_full_blocks()
  -> _complete_pending_reuse(raw_hash, request)
       -> on_block_reused(event, request)
            -> 요청 workload 확인
            -> victim workload와 같은지 확인
            -> cross-workload인지 확인
            -> TTL 안인지 확인
            -> 아직 집계하지 않은 event인지 확인
            -> 조건을 만족하면 useful_evictions[victim] += 1
               (같은 event는 한 번만 집계)
```

여기서 `on_block_evicted()`의 occupancy `-1`과
`on_block_reused()`의 useful eviction `+1`은 서로 다른 값이다.

- occupancy: 현재 eviction 가능한 cached block 수
- useful eviction: 과거에 evict했지만 나중에 다시 필요했던 eviction 수

window 안의 event와 useful count는 `useful_eviction_ratio`로 계산된다.
기존 allocation/free/reuse 경로에서 `tick_sec`가 지났으면 이 snapshot을
`QUOTA_SERVE_LOG`에 기록하고, reset/flush 때는 강제로 기록한다.

## 5. `vllm/v1/core/sched/scheduler.py`

QuotaServeCollector를 생성할 때 PR5 설정을 전달한다.

```python
shadow_ttl_sec=config.shadow_ttl_sec
window_size=config.window_size
tick_sec=config.tick_sec
log_path=config.log_path
```

이 wiring을 통해 scheduler에서 읽은 config가 collector의 window, TTL, tick,
signal log 경로까지 전달된다.

## 6. 테스트

`tests/v1/core/test_quota_serve_pr5_contract.py`에 다음을 검증하는 테스트를
추가했다.

- useful eviction 판정 조건
- reuse hook에 request가 전달되는지
- useful event 중복 집계 방지
- TTL 만료 처리
- window size 초과 시 오래된 event 제거
- `tick_sec` 이전에는 중복 signal log를 기록하지 않는지
- 강제 flush 시 signal JSONL이 기록되는지

## PR5에서 변경하지 않은 것

- `quota_w` 계산 및 runtime quota 조정
- `occupancy_w > quota_w` 판단
- victim selection 알고리즘
- `ratio_low/high`, `floor/cap`, `step` 기반 dynamic controller

위 기능은 PR6/PR7 범위다.
