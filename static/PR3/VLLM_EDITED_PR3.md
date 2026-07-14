# VLLM_EDITED_PR3

## 1. PR3 목표

PR3는 workload별 block owner와 `occupancy_w`를 관리하는 단계다.

`occupancy_w`는 workload `w`가 가진 physical block 중 다음 조건을 모두 만족하는
block 수다.

```text
block.ref_cnt == 0
and block.block_hash is not None
```

즉, cached prefix block이면서 현재 request가 사용하지 않아 eviction 가능한 block만
센다. PR3에서는 victim selection 순서를 바꾸지 않으며, 기존 global LRU를 그대로
사용한다.

## 2. 수정한 vLLM 파일

### 2.1 `vllm/v1/core/kv_cache_utils.py`

`KVCacheBlock`에 physical block 단위 membership flag를 추가했다.

```python
is_counted_as_evictable_cached: bool = False
```

이 flag는 해당 block이 이미 occupancy counter에 포함되어 있는지를 나타낸다.
동일한 lifecycle hook이 여러 번 호출되어도 중복 `+1` 또는 `-1`이 발생하지 않게
한다.

`reset_hash()`에서 다음 metadata를 함께 초기화한다.

```text
block_hash
workload_tag
is_counted_as_evictable_cached
cached_request_id
block_index
last_access_time
```

### 2.2 `vllm/quota_serve/collector.py` 신규 추가

#### `WorkloadState`

workload 하나의 occupancy counter를 저장한다.

```python
@dataclass(slots=True)
class WorkloadState:
    evictable_cached: int = 0
```

`state[w].evictable_cached`가 workload `w`의 `occupancy_w`다.

#### `QuotaServeCollector`

기존 `KVCacheMetricsCollector`를 상속하고, 기존 lifecycle hook 계약을 유지하면서
owner와 occupancy만 추가로 관리한다.

```text
on_block_allocated
    request_id에서 workload를 추출해 block.workload_tag 설정

on_block_cached
    cache 등록 직후 evictable cached 상태 재평가

on_block_accessed
    ref_cnt 증가 후 occupancy에서 제거

on_block_freed
    ref_cnt 감소 후 0이 되면 occupancy에 추가

on_block_evicted
    실제 eviction 확정 후 occupancy에서 제거

reset
    workload별 occupancy state 초기화
```

#### 상태 전이 helper

```python
_owner(block)
```

block의 `workload_tag`를 반환한다. owner가 비어 있는 예외 경로는 `unknown`으로
정규화한다.

```python
_is_evictable_cached(block)
```

`ref_cnt == 0`이고 `block_hash is not None`인지를 확인한다.

```python
_sync_membership(block)
```

현재 block 상태와 membership flag를 비교해 필요한 상태 전이만 수행한다.

```text
False → True
    state[owner].evictable_cached += 1
    block.is_counted_as_evictable_cached = True

True → False
    state[owner].evictable_cached -= 1
    block.is_counted_as_evictable_cached = False
```

flag와 counter를 변경하는 부분은 하나의 `Lock` 안에서 처리한다. counter underflow나
consistency drift가 발견되면 `RuntimeError`를 발생시킨다.

```python
occupancy_snapshot()
```

현재 workload별 occupancy를 복사해 반환한다.

```python
verify_occupancy(blocks)
```

physical block을 직접 스캔한 값과 collector counter를 비교한다. 두 값이 다르면
`RuntimeError`를 발생시킨다.

이 두 helper는 lifecycle hot path의 중복 로직이 아니라 각각 외부 occupancy 조회와
debug consistency check를 위한 별도 API다.

### 2.3 `vllm/v1/core/sched/scheduler.py`

Scheduler 시작 시 `load_quota_serve_config()`를 호출하고 mode에 따라 collector를
선택한다.

```python
if quota_serve_config.is_active:
    collector = QuotaServeCollector(...)
elif observability_config.kv_cache_metrics:
    collector = KVCacheMetricsCollector(...)
```

`static` 또는 `dynamic` mode에서는 QuotaServe collector가 사용된다. 일반
residency metrics는 `kv_cache_metrics`가 켜진 경우에만 함께 수집한다.

`mode=off`에서는 기존 collector 선택과 baseline LRU 동작을 유지한다.

### 2.4 `vllm/v1/core/block_pool.py`

기존 lifecycle hook에 PR3 collector가 연결되도록 동작 순서를 확정했다.

특히 eviction hook은 다음 순서로 실행된다.

```text
block_hash 확인
→ cache map에서 실제 제거
→ on_block_evicted()
→ eviction log 기록
→ reset_hash()
```

따라서 collector가 `reset_hash()`로 owner와 hash가 지워지기 전에 occupancy를
감소시킬 수 있다.

`reset_prefix_cache()`에서는 다음 순서를 사용한다.

```text
모든 block.reset_hash()
→ block별 occupancy flag 초기화
→ metrics_collector.reset()
→ workload별 occupancy counter 초기화
```

## 3. 수정하지 않은 파일

다음 파일은 request 전달 경로가 이미 충분하므로 PR3에서 추가 수정하지 않았다.

```text
vllm/v1/core/kv_cache_manager.py
vllm/v1/core/kv_cache_coordinator.py
vllm/v1/core/single_type_kv_cache_manager.py
```

현재 `get_new_blocks()`, `touch()`, `cache_full_blocks()`에는 request가 전달된다.
`free_blocks()`에는 request가 없어도 block 자체에 저장된 `workload_tag`를 사용하므로
occupancy 계산에 문제가 없다.

`vllm/v1/core/kv_cache_metrics.py`의 hook signature도 PR3에 필요한 형태가 이미
있다. PR3에서는 base collector를 변경하지 않고 `QuotaServeCollector`가 이를
override한다.

## 4. PR3에서 하지 않는 것

다음 기능은 PR4 범위다.

```text
occupancy_w > quota_w 기반 victim selection
quota-aware eviction 순서 변경
static quota 계산
global LRU 대체
```

따라서 PR3에서 `popleft_n(num_blocks)`는 그대로 유지된다.

## 5. 검증

- `KVCacheBlock`와 collector 파일 AST 문법 검사 통과
- cached/accessed/freed/evicted 상태 전이 smoke test 통과
- prefix cache reset 후 occupancy state 초기화 smoke test 통과
- counter underflow와 occupancy drift 시 `RuntimeError` 발생 확인
- manager/coordinator 계층에 불필요한 request 전달 변경이 없는지 확인
