# VLLM_EDITED_PR0

이 문서는 QuotaServe PR0를 위해 vLLM fork에서 수정한 내용을 정리한다.

PR0의 목적은 **victim selection을 바꾸지 않은 상태**에서 baseline LRU eviction을 workload 단위로 관측하는 것이다. 따라서 PR0는 quota 계산, occupancy counter, static/dynamic policy를 구현하지 않는다.

## 1. PR0 범위

PR0에서 하는 일:

```text
1. cached prefix block eviction을 JSONL로 기록
2. eviction된 prefix가 나중에 다시 필요해졌는지 reused_later로 기록
3. pending eviction event를 실험 종료 시 flush하는 endpoint 추가
4. request_id prefix에서 workload tag 추출
5. block에 eviction attribution용 metadata 추가
```

PR0에서 하지 않는 일:

```text
1. victim selection 변경
2. quota 계산
3. occupancy counter
4. static quota policy
5. dynamic controller
6. quota config schema / yaml loader
```

즉 PR0 상태에서 victim은 여전히 vLLM 기본 LRU가 고른다.

## 2. 수정 파일 요약

vLLM PR0 변경 파일:

```text
vllm/v1/core/kv_cache_metrics.py
vllm/v1/core/block_pool.py
vllm/v1/core/kv_cache_utils.py
vllm/v1/core/kv_cache_manager.py
vllm/v1/core/kv_cache_coordinator.py
vllm/v1/core/single_type_kv_cache_manager.py
vllm/quota_serve/__init__.py
vllm/quota_serve/workload.py
vllm/v1/core/sched/scheduler.py
vllm/v1/engine/core.py
vllm/v1/engine/core_client.py
vllm/v1/engine/async_llm.py
vllm/engine/protocol.py
vllm/entrypoints/serve/cache/api_router.py
```

핵심은 `block_pool.py`이고, `kv_cache_manager.py` → `kv_cache_coordinator.py` → `single_type_kv_cache_manager.py` 수정은 실제 `Request`를 `BlockPool`까지 전달하기 위한 경로다. engine/protocol/router 수정은 `/flush_eviction_log` 요청을 `BlockPool`까지 전달하기 위한 경로다.

## 3. `vllm/v1/core/kv_cache_utils.py`

`KVCacheBlock`에 PR0 attribution용 metadata를 추가했다.

```python
workload_tag: str = ""
cached_request_id: str = ""
block_index: int = -1
last_access_time: float = 0.0
```

각 필드 의미:

```text
workload_tag
  이 block을 cached prefix로 만든 request의 workload.
  eviction log의 evicted_workload로 사용한다.

cached_request_id
  이 block을 cache에 등록한 request id.
  eviction log의 evicted_request_id로 사용한다.

block_index
  request prefix 안에서 이 block이 몇 번째 block인지 나타낸다.
  eviction log의 evicted_block_index로 사용한다.

last_access_time
  block이 마지막으로 cache 등록 또는 hit/touch된 시간.
  eviction 후 재사용까지 걸린 시간 분석에 사용한다.
```

`KVCacheBlock`은 `slots=True` dataclass라서 나중에 `block.workload_tag = ...`처럼 동적 attribute를 붙일 수 없다. 그래서 PR0에서 필요한 metadata를 class field로 추가했다.

`research/QuotaServe`와 맞춰서 `reset_hash()`가 hash뿐 아니라 PR0 metadata도 같이 초기화한다.

```python
def reset_hash(self):
    self._block_hash = None
    self.workload_tag = ""
    self.cached_request_id = ""
    self.block_index = -1
    self.last_access_time = 0.0
```

PR3 occupancy counter용 `is_counted_as_evictable_cached`는 PR0 범위가 아니므로 넣지 않는다.

## 4. `vllm/v1/core/kv_cache_metrics.py`

PR0 hook interface를 정의한다. base collector는 기존 residency sampling만 유지하고, 새 인자는 모두 optional이라 baseline LRU 동작을 바꾸지 않는다.

시그니처를 확장한 hook:

```python
def on_block_allocated(self, block, request=None): ...
def on_block_accessed(self, block, request=None): ...
def on_block_evicted(self, block, trigger_request=None): ...
```

PR0에서 새로 둔 no-op hook:

```python
def on_block_cached(self, block, request=None): ...
def on_block_freed(self, block, prev_ref_cnt, new_ref_cnt): ...
```

이 hook들은 PR0에서는 관측 지점만 제공한다. 실제 owner/occupancy policy collector는 PR1 이후 범위다.

## 5. `vllm/quota_serve/workload.py`

request id에서 workload tag를 추출하는 helper를 추가했다.

```python
def infer_workload(request_id: str | None) -> str:
    ...
```

처리 규칙:

```text
chatcmpl-chat-...      -> chat
chatcmpl-longctx-...   -> longctx
chatcmpl-agent-...     -> agent
chatcmpl-msmarco-...   -> rag
hotpotqa-...           -> longctx
unknown / None         -> unknown
```

PR0는 OpenAI `user` 필드를 workload 기준으로 쓰지 않는다. 실험 runner가 `X-Request-Id`에 workload prefix를 넣고, vLLM 내부에서는 `request.request_id`에서 workload를 복원한다.

## 6. `vllm/quota_serve/__init__.py`

PR0에서는 config schema와 yaml loader를 제공하지 않는다. 패키지 export는 workload 추출 helper만 둔다.

```python
from vllm.quota_serve.workload import infer_workload

__all__ = ["infer_workload"]
```

`config.py`, `quota_serve.yaml`, `QUOTA_SERVE_CONFIG`, `QUOTA_SERVE_MODE`는 PR1 이후 범위다.

## 7. `vllm/v1/core/block_pool.py`

PR0의 핵심 수정 파일이다.

### 7.1 `VLLM_EVICTION_LOG` 파일 열기

환경 변수로 eviction log path를 받는다.

```python
_eviction_log_path = os.environ.get("VLLM_EVICTION_LOG")
```

`VLLM_EVICTION_LOG`가 없으면 eviction file logging은 비활성화된다.

```text
VLLM_EVICTION_LOG=/path/to/eviction.jsonl
```

### 7.2 Pending eviction buffer

eviction 직후에는 그 block이 useful eviction인지 아직 알 수 없다. 그래서 먼저 pending buffer에 저장한다.

```python
self._pending_evictions: dict[bytes, list[dict[str, Any]]] = {}
self._pending_evictions_count = 0
atexit.register(self._flush_pending_evictions)
```

동작:

```text
cached block eviction 발생
  -> pending buffer에 저장

같은 prefix hash가 나중에 다시 cache됨
  -> reused_later=true로 파일에 기록

실험 종료까지 재사용되지 않음
  -> flush 시 reused_later=false로 파일에 기록
```

pending buffer는 최대 `200_000`개까지 유지한다.

```python
_MAX_PENDING_EVICTIONS = 200_000
```

초과하면 가장 오래된 pending event를 `reused_later=false` 상태로 먼저 파일에 기록한다.

### 7.3 Cached prefix 등록 시 metadata 기록

`cache_full_blocks()`에서 block hash를 붙이고 cache map에 넣는 시점에 metadata를 저장한다.

```python
blk.block_hash = block_hash_with_group_id
blk.workload_tag = _request_workload(request)
blk.cached_request_id = request.request_id
blk.block_index = num_cached_blocks + i
blk.last_access_time = time.time()
self.cached_block_hash_to_block.insert(block_hash_with_group_id, blk)
self._complete_pending_reuse(bytes(block_hash))
```

여기서 `block_hash is not None`이면 prefix cache에 등록된 cached block으로 본다.

`_complete_pending_reuse()`는 같은 prefix hash가 pending eviction buffer에 있는지 확인한다. 있으면 그 eviction event를 useful eviction으로 판단해 `reused_later=true`로 기록한다.

### 7.4 Cached block eviction 기록

`_maybe_evict_cached_block()`에서 실제 cached block이 cache map에서 제거된 뒤 pending event를 만든다.

```python
self._remember_eviction_event(block, block_hash, trigger_request)
block.reset_hash()
```

중요한 순서:

```text
1. block_hash 저장
2. cache map에서 pop 성공
3. eviction event를 pending buffer에 저장
4. block.reset_hash()로 hash와 PR0 metadata 정리
```

`block.reset_hash()` 전에 기록해야 `evicted_prefix_hash`, `evicted_workload`, `evicted_request_id`를 잃지 않는다.

PR0는 victim selection을 바꾸지 않는다. 기존 LRU가 고른 block을 evict하고, 그 사건을 기록만 한다.

### 7.5 Eviction event schema

`_remember_eviction_event()`가 만드는 event:

```json
{
  "evicted_workload": "chat",
  "trigger_workload": "longctx",
  "evicted_request_id": "...",
  "trigger_request_id": "...",
  "evicted_prefix_hash": "...",
  "evicted_block_index": 1,
  "evicted_block_size": 16,
  "eviction_time": 123.45,
  "last_access_time": 120.00,
  "reused_later": false,
  "time_until_next_reuse": null
}
```

필드 의미:

```text
evicted_workload
  eviction 당한 cached block의 owner workload.

trigger_workload
  새 block 필요로 eviction을 유발한 request의 workload.

evicted_request_id
  evicted block을 cache에 등록했던 request id.

trigger_request_id
  eviction을 유발한 request id.

evicted_prefix_hash
  evicted block의 prefix hash.

evicted_block_index
  원래 request prefix 안에서 block 위치.

evicted_block_size
  KV block size.

eviction_time
  eviction 발생 시간.

last_access_time
  eviction 전 마지막 touch/cache hit 시간.

reused_later
  나중에 같은 prefix hash가 다시 필요해졌는지 여부.

time_until_next_reuse
  eviction 이후 같은 prefix가 다시 cache될 때까지 걸린 시간.
```

### 7.6 Reuse 감지

`_complete_pending_reuse(raw_hash_bytes)`는 새 cached block이 등록될 때 호출된다.

동작:

```text
1. 새로 cache된 prefix hash 확인
2. pending eviction buffer에 같은 hash가 있는지 확인
3. 있으면 가장 오래된 pending event 하나를 꺼냄
4. reused_later=true로 변경
5. time_until_next_reuse 기록
6. JSONL 파일에 즉시 기록
```

이 방식으로 "evict하지 않았다면 나중에 cache hit이 되었을 block"을 useful eviction으로 본다.

### 7.7 Flush 처리

`flush_pending_evictions()`는 아직 재사용되지 않은 pending event를 모두 파일에 기록한다.

```python
event["reused_later"] = False
event["time_until_next_reuse"] = None
```

실험 종료 후 서버를 끄기 전에 반드시 `/flush_eviction_log`를 호출해야 한다. 그래야 `reused_later=false` event까지 파일에 남는다.

### 7.8 Touch 시간 갱신

`touch()`에서 cached block이 hit되어 ref count가 올라갈 때 `last_access_time`을 갱신한다.

```python
block.last_access_time = time.time()
```

이 값은 eviction event의 `last_access_time`으로 기록된다.

### 7.9 Prefix cache reset

`reset_prefix_cache()`는 모든 block에 대해 `block.reset_hash()`만 호출한다.

```python
for block in self.blocks:
    block.reset_hash()
```

PR0 metadata cleanup은 `KVCacheBlock.reset_hash()` 안에 모았다. 이 부분은 `research/QuotaServe`와 맞춘 결정이다.

`reset_prefix_cache()`는 collector reset 후 pending eviction도 flush한다. prefix cache를 비울 때 아직 `reused_later=false`로 확정되지 않은 event가 파일에 남도록 하기 위해서다.

```python
if self.metrics_collector:
    self.metrics_collector.reset()
self.flush_pending_evictions()
```

## 8. Request threading 경로

PR0 eviction attribution에서 `trigger_workload`를 기록하려면 eviction을 유발한 실제 `Request`가 `BlockPool`까지 내려와야 한다.

수정한 경로:

```text
KVCacheManager.allocate_slots(request)
-> KVCacheCoordinator.allocate_new_computed_blocks(..., request=request)
-> SingleTypeKVCacheManager.allocate_new_computed_blocks(..., request=request)
-> BlockPool.touch(..., request=request)

KVCacheManager.allocate_slots(request)
-> KVCacheCoordinator.allocate_new_blocks(..., request=request)
-> SingleTypeKVCacheManager.allocate_new_blocks(..., request=request)
-> BlockPool.get_new_blocks(..., request=request)
-> _maybe_evict_cached_block(..., trigger_request=request)
```

포함된 파일:

```text
vllm/v1/core/kv_cache_manager.py
vllm/v1/core/kv_cache_coordinator.py
vllm/v1/core/single_type_kv_cache_manager.py
```

이 변경은 logging attribution만 보강한다. victim selection은 여전히 기존 LRU다.

## 9. `/flush_eviction_log` endpoint plumbing

pending eviction buffer를 실험 종료 시점에 파일로 확정 기록하기 위해 HTTP endpoint를 추가했다.

### 9.1 `vllm/entrypoints/serve/cache/api_router.py`

dev mode cache router에 endpoint를 추가했다.

```python
@router.post("/flush_eviction_log")
async def flush_eviction_log(raw_request: Request):
    num_flushed = await engine_client(raw_request).flush_eviction_log()
    return {"num_flushed": num_flushed}
```

이 endpoint는 `VLLM_SERVER_DEV_MODE=1`일 때 attach된다.

실행:

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```

응답 예:

```json
{"num_flushed":148074}
```

### 9.2 Engine protocol/client 경로

아래 파일들은 `/flush_eviction_log` 요청을 core scheduler까지 넘기기 위한 plumbing이다.

```text
vllm/engine/protocol.py
vllm/v1/engine/async_llm.py
vllm/v1/engine/core_client.py
vllm/v1/engine/core.py
vllm/v1/core/sched/scheduler.py
```

흐름:

```text
HTTP /flush_eviction_log
-> AsyncLLM.flush_eviction_log()
-> CoreClient.flush_eviction_log()
-> EngineCore.flush_eviction_log()
-> Scheduler.flush_eviction_log()
-> BlockPool.flush_pending_evictions()
```

## 10. `research/QuotaServe`와의 관계

PR0에서 `research/QuotaServe`와 맞춘 부분:

```text
1. eviction log의 pending buffer 방식
2. /flush_eviction_log endpoint 방식
3. KVCacheBlock.reset_hash()에서 attribution metadata까지 같이 초기화
```

PR0에서 의도적으로 다르게 둔 부분:

```text
1. workload tag 전달 방식
   research/QuotaServe는 OpenAI user 필드를 core Request까지 plumbing한다.
   PR0는 X-Request-Id prefix를 사용해 request_id에서 workload를 추출한다.

2. trigger 전달 방식
   research/QuotaServe는 trigger_workload / trigger_request_id 문자열을 넘긴다.
   PR0는 Request 객체를 block_pool까지 넘기고 내부에서 workload를 추출한다.

3. config
   research/QuotaServe와 달리 PR0에는 quota config schema/yaml을 두지 않는다.
   config는 PR1부터 다룬다.
```

## 11. PR0 통과 기준

PR0가 통과하려면 다음이 성립해야 한다.

```text
1. mode/off/static 같은 quota 정책 없이 baseline LRU와 동일하게 victim이 선택된다.
2. VLLM_EVICTION_LOG 파일이 생성된다.
3. /flush_eviction_log 호출 후 reused_later=false event가 기록된다.
4. evicted_workload / trigger_workload가 unknown 없이 기록된다.
5. Chat+Longctx, Chat+Agent baseline 결과가 Case 1 baseline과 통계적으로 같은 범위에 있다.
```
