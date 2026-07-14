# QuotaServe PR3 Baseline Observation

이 문서는 QuotaServe PR3의 목표, 범위, 검증 기준을 기록한다.

실제 실행 명령은 [README.md](./README.md)에 둔다.

## 1. PR3 목표

PR3의 목표는 workload별 physical KV cache block owner와
`occupancy_w`를 관리하는 것이다.

`occupancy_w`는 workload `w`가 소유한 block 중 현재 eviction 가능한
cached prefix block의 개수다.

```text
occupancy_w = count(
    block.workload_tag == w
    and block.ref_cnt == 0
    and block.block_hash is not None
)
```

즉, request가 사용 중인 `ref_cnt > 0` block은 occupancy에 포함하지 않는다.
PR3는 running KV를 제한하는 단계가 아니라, eviction 가능한 cached block을
workload별로 세는 단계다.

## 2. 핵심 결정

새로운 workload id를 만들지 않고, PR2에서 사용한 workload tag를 physical
`KVCacheBlock`에 저장한다.

```text
request_id
  ↓
infer_workload(request_id)
  ↓
block.workload_tag
  ↓
state[workload].evictable_cached
```

다른 workload가 cached block을 hit하더라도 block owner는 바꾸지 않는다.
owner는 physical block이 새 request에 재할당될 때 새 workload로 덮어쓴다.

## 3. occupancy 상태 관리

각 physical block에는 membership flag를 둔다.

```python
block.is_counted_as_evictable_cached: bool = False
```

이 flag는 해당 block이 이미 occupancy counter에 포함되어 있는지를 나타낸다.
여러 lifecycle hook에서 같은 block을 처리하더라도 상태 전이가 실제로 일어난
경우에만 counter를 변경한다.

```text
False → True
  state[owner].evictable_cached += 1
  block.is_counted_as_evictable_cached = True

True → False
  state[owner].evictable_cached -= 1
  block.is_counted_as_evictable_cached = False
```

flag와 workload counter 변경은 하나의 `Lock` 안에서 처리한다. counter가
음수가 되거나 실제 physical block 상태와 counter가 달라지면
`RuntimeError`로 즉시 실패한다.

## 4. 기준 lifecycle 경로

```text
block 상태 변경
  ↓
BlockPool lifecycle hook 호출
  ↓
현재 ref_cnt / block_hash 상태 확인
  ↓
membership flag와 occupancy counter 상태 전이
  ↓
state[workload].evictable_cached 갱신
```

주요 hook의 역할은 다음과 같다.

```text
on_block_allocated
  새로 할당되거나 재사용되는 physical block의 owner 설정

on_block_cached
  block_hash가 등록된 직후 cached/evictable 상태 확인

on_block_accessed
  cache hit으로 ref_cnt가 증가한 뒤 occupancy에서 제거

on_block_freed
  ref_cnt가 0이 된 뒤 occupancy에 추가

on_block_evicted
  실제 cache map에서 제거된 뒤 reset_hash() 전에 occupancy에서 제거
```

eviction hook은 cache map에서 실제 제거가 확인된 뒤, `reset_hash()`로
`block_hash`와 owner metadata가 지워지기 전에 호출한다.

prefix cache 전체 reset에서는 모든 block의 hash와 membership flag를 먼저
초기화한 뒤 collector state를 비운다.

## 5. PR3 범위

PR3에 포함되는 것:

```text
KVCacheBlock membership flag 추가
QuotaServeCollector 추가
WorkloadState.evictable_cached counter 관리
workload별 physical block owner 관리
cached/accessed/freed/evicted hook 연결
occupancy counter 상태 전이의 Lock 보호
counter underflow / occupancy drift RuntimeError 처리
prefix cache reset 시 occupancy state 초기화
```

PR3에 포함되지 않는 것:

```text
quota_w 계산
occupancy_w > quota_w 판단에 따른 victim selection
static quota eviction 순서 변경
dynamic controller 구현
global LRU 대체
```

PR3에서는 victim selection을 바꾸지 않는다. 따라서 실제 eviction 순서는
기존 global LRU와 같고, `popleft_n(num_blocks)`도 변경하지 않는다.

## 6. 관련 파일

vLLM:

```text
vllm/v1/core/kv_cache_utils.py
vllm/quota_serve/collector.py
vllm/v1/core/sched/scheduler.py
vllm/v1/core/block_pool.py
```

`kv_cache_utils.py`에는 physical block membership flag와 reset 처리를 추가한다.

`collector.py`에는 다음 객체를 둔다.

```text
WorkloadState
  workload 하나의 evictable_cached counter

QuotaServeCollector
  lifecycle hook을 받아 owner와 occupancy를 관리
```

`scheduler.py`에서는 QuotaServe가 active일 때 `QuotaServeCollector`를 만들고,
`mode=off`에서는 기존 collector 선택과 baseline 경로를 유지한다.

`block_pool.py`는 lifecycle hook 호출 순서와 실제 eviction 확정 시점을
보장한다.

## 7. 실행 모드

PR3 collector를 활성화하려면 서버를 다음처럼 실행한다.

```text
QUOTA_SERVE_CONFIG=<JJ repo>/static/quota_serve.yaml
QUOTA_SERVE_MODE=static
```

여기서 `static`은 PR3에서 quota-aware victim selection을 실행한다는 뜻이
아니다. 현재 단계에서는 `is_active` 조건을 만족시켜 collector를 켜는 용도로
사용하며, victim selection 변경은 PR4에서 추가한다.

따라서 클라이언트 runner는 다음처럼 실행한다.

```text
--quota-mode off
```

이 설정은 PR3에서 실제 eviction 정책을 바꾸지 않고 occupancy 계측만 확인하기
위한 것이다.

## 8. PR3 통과 기준

아래 항목이 모두 만족되면 PR3 occupancy 단계는 통과로 본다.

```text
1. collector lifecycle smoke 통과
2. 같은 hook을 반복 호출해도 occupancy가 중복 증가하지 않음
3. cached → accessed 전이에서 occupancy가 감소함
4. freed로 ref_cnt가 0이 되면 occupancy가 증가함
5. 실제 eviction 시 occupancy가 감소함
6. prefix cache reset 후 occupancy state가 비어 있음
7. counter underflow / occupancy drift가 RuntimeError로 감지됨
8. Chat+Longctx와 Chat+Agent에서 workload attribution이 unknown이 아님
9. PR3 실행 결과의 victim 순서가 기존 global LRU와 동일함
```

## 9. 주의 사항

```text
1. occupancy는 모든 KV block이 아니라 ref_cnt=0인 cached prefix block만 센다.
2. owner는 hit-side workload가 아니라 block을 만든 workload다.
3. free_blocks()에 request가 없어도 block.workload_tag로 owner를 확인할 수 있다.
4. on_block_evicted는 reset_hash()보다 먼저 호출되어야 한다.
5. verify_occupancy(blocks)는 구현 초기 debug consistency check용이다.
6. quota-aware victim selection과 quota 효과 검증은 PR4에서 한다.
```
