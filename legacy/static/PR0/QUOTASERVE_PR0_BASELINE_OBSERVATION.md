# QuotaServe PR0 Baseline Observation

이 문서는 QuotaServe PR0 baseline observation 결과를 기록한다.

`QUOTASERVE_STATIC_PLAN.md`는 전체 static 구현 로드맵으로 유지하고, PR0의 실제 구현/검증 기록은 `static/PR0/` 산출물에 분리해 둔다.

## 1. PR0 목표

PR0의 목표는 실제 eviction 정책을 바꾸기 전에 vLLM prefix cache lifecycle을 관측할 수 있는 최소 hook/logging 경로를 확보하는 것이다.

핵심 질문:

```text
1. hook/logging을 추가해도 baseline LRU 동작이 깨지지 않는가?
2. baseline LRU eviction을 workload 단위로 분석할 수 있는 log가 남는가?
```

PR0에서는 victim selection을 바꾸지 않는다.

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

PR0에 포함되지 않는 것:

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

PR0 이후 PR1~PR4는 같은 branch에 순서대로 commit을 쌓는 방식으로 진행한다.

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
vllm/v1/core/kv_cache_manager.py
vllm/v1/core/kv_cache_coordinator.py
vllm/v1/core/single_type_kv_cache_manager.py
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
5. allocation path에서 Request를 BlockPool까지 전달해 eviction을 유발한 trigger_workload를 기록한다.
```

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
success: 2000
failed: 0
duration: 403.7s
```

Workload별 결과:

```text
Chat:
  total: 1000
  success: 1000
  failed: 0
  hit_rate_mean: 0.082
  hit_rate_p50: 0.032
  TTFT mean: 498.2 ms
  TTFT p50: 416.1 ms
  TTFT p95: 1197.6 ms
  TTFT p99: 2037.2 ms
  SLO attainment: 47.9%

Longctx:
  total: 1000
  success: 1000
  failed: 0
  hit_rate_mean: 0.029
  hit_rate_p50: 0.029
  TTFT mean: 725.9 ms
  TTFT p50: 662.1 ms
  TTFT p95: 1428.8 ms
  TTFT p99: 2030.6 ms
  SLO attainment: 100.0%
```

Eviction log 품질:

```text
parsed eviction events: 182328
bad json lines: 0
missing evicted_workload: 0
missing trigger_workload: 0
unknown evicted_workload: 0
unknown trigger_workload: 0
reused_later=true: 34233
reused_later=false: 148095
```

최종 결과에서는 `trigger_workload`가 정상적으로 기록되었다.

샘플:

```json
{
  "evicted_workload": "chat",
  "trigger_workload": "longctx",
  "evicted_request_id": "chatcmpl-chat-...",
  "trigger_request_id": "chatcmpl-longctx-...",
  "reused_later": true
}
```

Evicted workload별 집계:

```text
evicted_workload=chat:
  total eviction: 44192
  reused_later=true: 33705
  useful ratio: 76.3%

evicted_workload=longctx:
  total eviction: 138136
  reused_later=true: 528
  useful ratio: 0.38%
```

Eviction direction:

```text
longctx -> longctx: 103634
chat    -> longctx: 34502
longctx -> chat:    30118
chat    -> chat:    14074
```

Useful eviction direction:

```text
longctx -> chat:    24099
chat    -> chat:     9606
longctx -> longctx:   429
chat    -> longctx:    99
```

Cross-workload eviction:

```text
self eviction events: 117708
cross-workload eviction events: 64620
cross-workload eviction ratio: 35.4%
cross-workload useful eviction events: 24198
useful ratio among cross-workload evictions: 37.4%
```

### 6.2 Chat + Agent

요약:

```text
quota_serve_mode: off
phase: chat_agent_exponential
total requests: 2000
success: 2000
failed: 0
duration: 386.1s
```

Workload별 결과:

```text
Chat:
  total: 1000
  success: 1000
  failed: 0
  hit_rate_mean: 0.124
  hit_rate_p50: 0.033
  TTFT mean: 216.5 ms
  TTFT p50: 190.7 ms
  TTFT p95: 491.2 ms
  TTFT p99: 662.5 ms
  SLO attainment: 90.3%

Agent:
  total: 1000
  success: 1000
  failed: 0
  hit_rate_mean: 0.361
  hit_rate_p50: 0.161
  TTFT mean: 282.1 ms
  TTFT p50: 205.2 ms
  TTFT p95: 727.8 ms
  TTFT p99: 1209.7 ms
  SLO attainment: 48.6%
```

Eviction log 품질:

```text
parsed eviction events: 96838
bad json lines: 0
missing evicted_workload: 0
missing trigger_workload: 0
unknown evicted_workload: 0
unknown trigger_workload: 0
reused_later=true: 68061
reused_later=false: 28777
```

Evicted workload별 집계:

```text
evicted_workload=chat:
  total eviction: 47715
  reused_later=true: 33194
  useful ratio: 69.6%

evicted_workload=agent:
  total eviction: 49123
  reused_later=true: 34867
  useful ratio: 71.0%
```

Eviction direction:

```text
agent -> agent: 34189
chat  -> chat:  30637
agent -> chat:  17078
chat  -> agent: 14934
```

Useful eviction direction:

```text
agent -> chat:
  total eviction: 17078
  useful eviction: 10389
  useful ratio: 60.8%

chat -> agent:
  total eviction: 14934
  useful eviction: 10850
  useful ratio: 72.7%
```

Cross-workload eviction:

```text
self eviction events: 64826
cross-workload eviction events: 32012
cross-workload eviction ratio: 33.1%
cross-workload useful eviction events: 21239
useful ratio among cross-workload evictions: 66.3%
```

## 7. 해석

최종 PR0 결과에서 확인한 것:

```text
1. mode=off에서 request failure 없이 2000개 요청이 완료되었다.
2. VLLM_EVICTION_LOG 파일이 정상 생성되었다.
3. evicted_workload가 누락 없이 기록되었다.
4. trigger_workload가 누락 없이 기록되었다.
5. reused_later=true/false flush가 정상 동작했다.
6. cross-workload useful eviction direction을 계산할 수 있다.
```

중요한 관찰:

```text
longctx -> chat useful eviction: 24099
chat    -> longctx useful eviction: 99

agent -> chat useful eviction: 10389
chat  -> agent useful eviction: 10850
```

즉 baseline LRU에서는 Longctx 요청이 Chat의 나중에 다시 쓰일 prefix block을 많이 밀어내고 있다. Agent mix에서는 양방향 useful eviction이 모두 크게 관측된다. 이 값들이 static quota에서 줄어드는지가 PR4의 핵심 검증 대상이다.

## 8. PR0 통과 판정

Chat+Longctx 및 Chat+Agent 기준 PR0 판정:

```text
PASS
```

근거:

```text
mode=off mixed run 성공
request failure 없음
eviction log 생성
evicted_workload 누락 없음
trigger_workload 누락 없음
reused_later flush 정상
cross-workload useful eviction 계산 가능
```

이제 Chat+Longctx와 Chat+Agent 모두 PR0 baseline으로 사용할 수 있다.

## 9. 타임라인 메모

```text
1. 초기 PR0/기존 branch에서는 Chat+Agent attribution이 정상 계산된 적이 있다.
2. git history 확인 결과, PR4 commit(`2192c53a7`)에는 `request=request` 전달 경로가 구현되어 있었다.
3. 이후 stepwise PR0 commit(`6636e27c5`)으로 PR0 범위만 남기도록 정리하는 과정에서 이 경로가 빠졌다.
4. 그 상태로 다시 실행한 log에서는 trigger_workload가 null로 찍혔다.
5. request threading을 다시 보완한 뒤 Chat+Longctx와 Chat+Agent에서 trigger_workload 누락 0개를 확인했다.
```
다음 단계는 PR1 config/off-mode parity로 넘어가는 것이다.

PR1에서 확인할 것:

```text
1. PR0 logging을 유지한 상태에서 mode=off가 baseline과 동일하게 동작하는가?
2. QuotaServe config/env loader가 victim selection을 바꾸지 않는가?
3. 기존 PR0 지표가 유지되는가?
```
