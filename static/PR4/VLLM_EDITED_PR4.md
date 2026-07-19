# VLLM_EDITED_PR4

## 1. PR4 목적

PR4에서는 PR3 collector가 계산한 `occupancy_w`와 static quota를 사용해
cached prefix block의 victim 선택 순서를 바꾼다.

기존 global LRU queue를 없애지 않는다. `QuotaAwareVictimSelector`가 선택할
block을 결정하고, `BlockPool`이 기존 free queue에서 실제로 제거한다.

## 2. 추가된 파일

### `vllm/quota_serve/victim_selector.py`

PR4 victim selection 정책을 구현한다.

```python
QuotaAwareVictimSelector(free_queue, trigger_request)
    → VictimSelection
```

selector가 확인하는 조건은 다음과 같다.

```python
not block.is_null
block.ref_cnt == 0
block.block_hash is not None
block.workload_tag in over_quota_workloads
```

free queue의 LRU head부터 순회하고, 조건을 만족하는 첫 block을 반환한다.
따라서 선택된 workload 내부에서도 LRU 순서가 유지된다.

selector는 queue를 직접 수정하지 않는다. 반환한 block의 queue 제거는
`BlockPool`이 담당한다.

`VictimSelection`에는 다음 정보가 포함된다.

```text
block
reason
scan_steps
victim_workload
victim_occupancy
victim_quota
occupancy_snapshot
```

over-quota workload가 있다고 판단했는데 끝까지 후보를 찾지 못하면 다음 오류를
발생시킨다.

```python
RuntimeError(
    "over-quota workload exists but no evictable cached block was found"
)
```

조용히 global LRU로 내려가지 않는 이유는 occupancy counter, owner metadata,
free queue 상태 중 하나가 잘못되었음을 즉시 확인하기 위해서다.

## 3. 수정된 파일

### 3.1 `vllm/v1/core/block_pool.py`

#### selector 진입점

`BlockPool`에 optional `victim_selector`를 전달한다.

```python
if self.enable_caching and self.victim_selector is not None:
    ret = self._select_blocks_with_policy(num_blocks, request)
else:
    ret = self.free_block_queue.popleft_n(num_blocks)
```

selector가 없으면 기존 global LRU 경로를 그대로 사용한다.

#### 여러 block 선택

`num_blocks > 1`일 때 `popleft_n()`으로 미리 뽑지 않고 block을 하나씩 선택한다.

```python
for _ in range(num_blocks):
    block = self.victim_selector(self.free_block_queue, request)
    self.free_block_queue.remove(block)
```

앞에서 제거한 block이 다음 선택에 다시 선택되지 않으며, 다음 selector 호출은
변경된 queue를 본다.

#### 선택 metadata 보관

한 allocation에서 여러 block을 선택할 수 있으므로 선택 결과를 physical
`block_id`별로 임시 보관한다.

```python
_pending_victim_selections[block.block_id] = selection
```

이후 `_maybe_evict_cached_block()`에서 같은 block의 metadata를 꺼내 eviction
log에 연결한다. 이 방식으로 여러 block의 `selection_reason`이 서로 섞이지 않는다.

#### 실제 eviction 연결

선택된 block은 다음 순서로 처리된다.

```text
free_block_queue에서 제거
→ cached_block_hash_to_block에서 제거 확정
→ on_block_evicted()
→ eviction event 기록
→ block.reset_hash()
```

cache map에서 제거가 확인되지 않은 block은 실제 cached eviction으로 기록하지
않는다. `reset_hash()` 전에 log와 collector 처리를 수행하는 이유는 hash와 owner
metadata가 초기화되기 전 정보를 보존하기 위해서다.

#### eviction log 필드

기존 workload attribution에 PR4 선택 정보를 추가한다.

```json
{
  "evicted_workload": "chat",
  "trigger_workload": "longctx",
  "is_cross_workload": true,
  "selection_reason": "over_quota_selected",
  "victim_occupancy": 180,
  "victim_quota": 100,
  "occupancy_snapshot": {
    "chat": 250,
    "longctx": 180
  },
  "scan_steps": 7
}
```

`is_cross_workload`는 두 workload가 모두 알려진 경우에만 계산한다. 정보가
없거나 `unknown`이면 `null`이다.

선택 reason은 다음과 같다.

```text
over_quota_selected   : over-quota workload 내부에서 선택
fallback_no_over_quota: over-quota workload가 없어 global LRU 사용
baseline_lru_off_mode : QuotaServe off 상태의 baseline LRU
external_eviction     : selector를 거치지 않은 외부 eviction
```

`uncached_head`는 eviction이 아니므로 eviction event log에 기록하지 않는다.

### 3.2 `vllm/v1/core/sched/scheduler.py`

static mode이고 prefix caching이 활성화된 경우에만
`QuotaAwareVictimSelector`를 생성한다.

```python
if (
    quota_serve_config.mode == "static"
    and cache_config.enable_prefix_caching
):
    victim_selector = QuotaAwareVictimSelector(
        quota_serve_config,
        collector.occupancy_snapshot,
        kv_cache_config.num_blocks,
    )
```

selector는 PR3 `QuotaServeCollector.occupancy_snapshot()`을 사용한다.

`mode=off`에서는 selector를 만들지 않으므로 기존 baseline LRU 경로를 사용한다.

### 3.3 `vllm/v1/core/kv_cache_manager.py`

`victim_selector` 인자를 받아 KV cache coordinator 생성 함수로 전달한다.

### 3.4 `vllm/v1/core/kv_cache_coordinator.py`

일반적인 단일 KV cache group을 사용하는 `UnitaryKVCacheCoordinator` 경로에서
selector를 `BlockPool`까지 전달한다.

```text
Scheduler
  → KVCacheManager
  → KVCacheCoordinator
  → UnitaryKVCacheCoordinator
  → BlockPool(victim_selector=...)
```

`HybridKVCacheCoordinator`와 `KVCacheCoordinatorNoPrefixCache`에는 PR4 selector를
전달하지 않는다.

```text
Hybrid       → 기존 경로
NoPrefix     → no-cache 경로
mode=off     → 기존 global LRU
```

## 4. PR4에서 수정하지 않는 기능

```text
dynamic quota controller
ratio_low / ratio_high
floor / cap
shadow cache 기반 useful eviction ratio controller
Hybrid KV cache의 victim policy
No-prefix-cache 경로
```

PR4는 static quota와 workload 내부 LRU victim selection만 검증한다.

## 5. 검증 포인트

```text
1. mode=off에서 기존 global LRU와 동일하게 동작
2. mode=static에서 selector가 실제 BlockPool까지 전달
3. over-quota workload block이 우선 선택
4. over-quota workload 내부에서는 LRU 순서 유지
5. num_blocks > 1에서 block별 metadata가 섞이지 않음
6. 실제 cache map 제거 후에만 eviction log 기록
7. is_cross_workload와 selection metadata가 log에 기록
8. strict 후보 누락 오류가 발생
```
