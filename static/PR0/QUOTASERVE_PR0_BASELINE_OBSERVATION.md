# QuotaServe PR0 Baseline Observation

이 문서는 QuotaServe PR0 baseline observation 결과를 기록한다.

`QUOTASERVE_STATIC_PLAN.md`는 전체 static 구현 로드맵으로 유지하고, PR0의 실제 구현/검증 기록은 `static/PR0/` 산출물에 분리해서 둔다.

## 1. PR0 목표

PR0의 목표는 실제 eviction 정책을 바꾸기 전에 vLLM prefix cache lifecycle을 관측할 수 있는 최소 hook/logging 경로를 확보하는 것이다.

핵심 질문:

```text
1. hook/logging을 추가해도 baseline LRU 동작이 깨지지 않는가?
2. baseline LRU eviction을 workload 단위로 분석할 수 있는 log가 남는가?
```

PR0에서 victim selection은 바꾸지 않는다.

```text
victim selection: 기존 LRU 그대로
quota 계산: 없음
occupancy counter: 없음
static quota policy: 없음
dynamic controller: 없음
```

## 2. PR0 범위

PR0에 포함되는 것:

```text
vLLM block lifecycle hook 위치 주석
VLLM_EVICTION_LOG 기반 eviction attribution log
pending eviction buffer
reused_later=true/false 기록
/flush_eviction_log endpoint
request_id prefix 기반 workload 추출
mode=off baseline parity 검증
```

PR0에 포함하지 않는 것:

```text
dry_run shadow policy
quota_ratio 계산
quota_base_blocks 계산
block occupancy counter
occupancy_w > quota_w victim selection
static quota 실험
dynamic quota controller
```

## 3. 구현 브랜치

현재는 PR0 이후 PR1~PR4도 같은 branch에 순서대로 commit을 쌓는 방식으로 진행한다.

```text
JJ repo:
implementation/QuotaServe-stepwise

vLLM repo:
implementation/QuotaServe-stepwise
```

## 4. vLLM 구현 요약

vLLM PR0에서 수정한 핵심 파일:

```text
vllm/v1/core/block_pool.py
vllm/v1/core/kv_cache_utils.py
vllm/v1/core/sched/scheduler.py
vllm/v1/engine/core.py
vllm/v1/engine/core_client.py
vllm/v1/engine/async_llm.py
vllm/engine/protocol.py
vllm/entrypoints/serve/cache/api_router.py
vllm/quota_serve/workload.py
```

핵심 동작:

```text
1. block이 cached prefix가 될 때 owner workload와 prefix hash를 기록한다.
2. cached block이 eviction되면 pending eviction event로 저장한다.
3. 같은 prefix hash가 나중에 다시 cache되면 reused_later=true로 기록한다.
4. 실험 종료 후 /flush_eviction_log를 호출하면 남은 pending event를 reused_later=false로 기록한다.
```

`VLLM_EVICTION_LOG`를 지정하지 않으면 eviction attribution log는 파일로 남지 않는다.

## 5. 분석 대상 파일

이번 분석에 사용한 파일:

```text
pr0_chat_longctx_apc_on_len8192.jsonl
pr0_chat_longctx_apc_on_len8192_summary.json
pr0_eviction_chat_longctx_apc_on_len8192.jsonl

pr0_chat_agent_apc_on_len8192.jsonl
pr0_chat_agent_apc_on_len8192_summary.json
pr0_eviction_chat_agent_apc_on_len8192.jsonl
```

## 6. PR0 실행 결과

### 6.1 Chat + Longctx

요약:

```text
quota_serve_mode: off
total requests: 2000
success: 1003
failed: 997
duration: 339.4s
```

이 run은 정상 baseline으로 보기 어렵다. 실패 예시는 모두 서버 연결 실패다.

```text
ClientConnectorError:
Cannot connect to host 127.0.0.1:8000
```

Workload별 결과:

```text
Chat:
  total: 1000
  success: 498
  failed: 502
  hit_rate_mean: 0.026
  hit_rate_p50: 0.018
  TTFT mean: 672.1 ms
  TTFT p50: 507.9 ms
  TTFT p95: 2231.9 ms
  TTFT p99: 3742.7 ms
  SLO attainment: 17.6%

Longctx:
  total: 1000
  success: 505
  failed: 495
  hit_rate_mean: 0.029
  hit_rate_p50: 0.029
  TTFT mean: 893.1 ms
  TTFT p50: 705.3 ms
  TTFT p95: 2916.1 ms
  TTFT p99: 4262.5 ms
  SLO attainment: 50.5%
```

Eviction log:

```text
total eviction events: 101830
reused_later=true: 24076
reused_later=false: 77754
missing evicted_workload: 0
missing trigger_workload: 101830
```

Evicted workload별 useful 재사용:

```text
evicted_workload=chat:
  total eviction: 31928
  reused_later=true: 23908
  useful ratio: 74.9%

evicted_workload=longctx:
  total eviction: 69902
  reused_later=true: 168
  useful ratio: 0.2%
```

Cross-workload useful eviction:

```text
계산 불가
```

이유:

```text
trigger_workload가 모든 eviction event에서 null이다.
따라서 longctx -> chat, chat -> longctx 방향성을 계산할 수 없다.
```

### 6.2 Chat + Agent

요약:

```text
quota_serve_mode: off
phase: chat_agent_exponential
total requests: 2000
success: 2000
failed: 0
duration: 382.7s
```

Chat 결과:

```text
total: 1000
success: 1000
failed: 0
hit_rate_mean: 0.123
hit_rate_p50: 0.033
TTFT mean: 222.0 ms
TTFT p50: 199.5 ms
TTFT p95: 495.0 ms
TTFT p99: 694.8 ms
SLO attainment: 89.0%
```

Agent 결과:

```text
total: 1000
success: 1000
failed: 0
hit_rate_mean: 0.364
hit_rate_p50: 0.169
TTFT mean: 267.6 ms
TTFT p50: 208.8 ms
TTFT p95: 703.7 ms
TTFT p99: 986.7 ms
SLO attainment: 48.1%
```

Eviction log:

```text
total eviction events: 96142
reused_later=true: 67381
reused_later=false: 28761
missing evicted_workload: 0
missing trigger_workload: 96142
```

Evicted workload별 useful 재사용:

```text
evicted_workload=chat:
  total eviction: 47743
  reused_later=true: 33208
  useful ratio: 69.6%

evicted_workload=agent:
  total eviction: 48399
  reused_later=true: 34173
  useful ratio: 70.6%
```

Cross-workload useful eviction:

```text
계산 불가
```

이유:

```text
trigger_workload가 모든 eviction event에서 null이다.
따라서 agent -> chat, chat -> agent 방향성을 계산할 수 없다.
```

## 7. 해석

이번 결과에서 확인된 것:

```text
1. VLLM_EVICTION_LOG 파일은 생성된다.
2. evicted_workload는 누락 없이 기록된다.
3. reused_later=true/false flush도 동작한다.
4. evicted_workload 기준으로 useful 재사용 여부는 계산할 수 있다.
```

하지만 PR0 통과 조건으로는 부족하다.

문제 1: Chat + Longctx run이 유효하지 않다.

```text
2000개 중 997개가 실패했다.
실패 원인은 ClientConnectorError로, 실험 중 서버 연결이 끊긴 것으로 보인다.
따라서 hit rate, TTFT, SLO를 baseline parity 판단에 사용할 수 없다.
```

문제 2: trigger_workload가 전부 null이다.

```text
Chat+Longctx eviction log:
  missing trigger_workload = 101830 / 101830

Chat+Agent eviction log:
  missing trigger_workload = 96142 / 96142
```

이는 현재 PR0 코드에서 allocation/eviction trigger request가 `BlockPool.get_new_blocks()`까지 실제로 내려오지 않았기 때문이다. 결과적으로 evicted workload는 알 수 있지만, 어떤 workload가 eviction을 유발했는지는 알 수 없다.

## 8. PR0 통과 기준과 판정

통과 기준:

```text
mode=off mixed run 성공
request failure 없음
eviction log 생성
trigger_workload / evicted_workload 누락 없음
Case 1 baseline과 hit rate, TTFT, SLO가 runtime noise 범위에서 유사
```

현재 판정:

```text
PR0 FAIL / 보완 필요
```

근거:

```text
1. Chat+Longctx run에서 request failure가 997개 발생했다.
2. 두 eviction log 모두 evicted_workload는 기록되지만 trigger_workload가 전부 null이다.
3. cross-workload useful eviction 방향성을 계산할 수 없다.
```

## 9. 다음 조치

우선순위:

```text
1. vLLM 서버가 실험 중 죽거나 재시작되지 않았는지 확인하고 Chat+Longctx를 재실행한다.
2. trigger_request 또는 trigger workload tag가 BlockPool.get_new_blocks()까지 내려오도록 보완한다.
3. 보완 후 eviction log에서 trigger_workload null 비율이 0%인지 확인한다.
4. 그 다음 Chat+Longctx / Chat+Agent를 다시 실행해 PR0 baseline observation을 재판정한다.
```

## 10. 코드 보완 기록

이번 실패의 핵심 원인은 `Request` 객체가 `BlockPool.get_new_blocks()`까지 내려오지 않았다는 점이다.

기존 흐름:

```text
KVCacheManager.allocate_slots(request)
-> coordinator.allocate_new_blocks(request_id, ...)
-> single_type_manager.allocate_new_blocks(request_id, ...)
-> block_pool.get_new_blocks(num_blocks, request=None)
```

즉 위쪽에는 실제 `request`가 있었지만, 중간 경로에서 `request_id`만 전달되면서 eviction trigger 정보가 사라졌다. 그래서 eviction log에서 `evicted_workload`는 기록되지만 `trigger_workload`는 전부 `null`이 되었다.

보완한 흐름:

```text
KVCacheManager.allocate_slots(request)
-> coordinator.allocate_new_blocks(request_id, ..., request=request)
-> single_type_manager.allocate_new_blocks(request_id, ..., request=request)
-> block_pool.get_new_blocks(num_blocks, request=request)
-> _maybe_evict_cached_block(block, trigger_request=request)
-> trigger_workload = infer_workload(trigger_request.request_id)
```

같이 보완한 경로:

```text
allocate_new_computed_blocks(..., request=request)
touch(new_computed_blocks, request=request)
external computed token allocation의 get_new_blocks(..., request=request)
Mamba align path의 get_new_blocks(..., request=request)
```

이 보완은 victim selection을 바꾸지 않는다. 기존 LRU가 고른 block을 그대로 evict하되, 그 eviction을 유발한 request/workload를 log에 남기기 위한 PR0 attribution 보완이다.

재실험에서 확인할 것:

```text
trigger_workload null 비율: 0%
trigger_request_id null 비율: 0%
evicted_workload null 비율: 0%
Chat+Longctx request failure: 0
```
