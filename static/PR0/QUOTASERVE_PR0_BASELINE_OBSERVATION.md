# QuotaServe PR0 Baseline Observation

이 문서는 QuotaServe PR0 baseline observation 결과를 기록하기 위한 템플릿이다.

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

PR0-only 브랜치:

```text
JJ repo:
implementation/QuotaServe-pr0

vLLM repo:
implementation/QuotaServe-pr0
```

기존 `implementation/QuotaServe` 브랜치는 PR4까지 진행된 작업을 보존한다. PR0-only 브랜치는 PR0 검증 이력을 위해 별도로 분리한 브랜치다.

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

## 5. JJ repo 산출물

PR0 실행 스크립트와 검증 결과는 아래에 둔다.

```text
static/PR0/README.md
static/PR0/PR0_validation.md
static/PR0/VLLM_EDITED_PR0.md
static/PR0/QUOTASERVE_PR0_BASELINE_OBSERVATION.md
```

`README.md`는 실험 실행 절차를 담고, `PR0_validation.md`는 재실험 후 최종 검증 결과를 담는다.

## 6. PR0 실행 결과

재실험 후 아래 항목을 채운다.

### 6.1 Chat + Longctx

요약:

```text
quota_serve_mode:
total requests:
success:
failed:
duration:
```

주요 metric:

```text
Chat hit_rate_mean:
Chat TTFT mean:
Chat TTFT p95:
Chat SLO attainment:

Longctx hit_rate_mean:
Longctx TTFT mean:
Longctx TTFT p95:
Longctx SLO attainment:
```

Eviction log:

```text
total eviction events:
reused_later=true:
reused_later=false:
missing trigger_workload:
missing evicted_workload:
```

Cross-workload useful eviction:

```text
longctx -> chat:
  total eviction:
  useful eviction:
  useful ratio:

chat -> longctx:
  total eviction:
  useful eviction:
  useful ratio:
```

### 6.2 Chat + Agent

요약:

```text
quota_serve_mode:
phase:
total requests:
success:
failed:
duration:
```

주요 metric:

```text
Chat hit_rate_mean:
Chat TTFT p95:
Chat SLO attainment:

Agent hit_rate_mean:
Agent TTFT p95:
Agent SLO attainment:
```

Cross-workload useful eviction:

```text
agent -> chat:
  total eviction:
  useful eviction:
  useful ratio:

chat -> agent:
  total eviction:
  useful eviction:
  useful ratio:
```

## 7. PR0 통과 기준과 판정

통과 기준:

```text
mode=off mixed run 성공
request failure 없음
eviction log 생성
trigger_workload / evicted_workload 누락 없음
Case 1 baseline과 hit rate, TTFT, SLO가 runtime noise 범위에서 유사
```

판정:

```text
PR0 PASS / FAIL:
근거:
```
