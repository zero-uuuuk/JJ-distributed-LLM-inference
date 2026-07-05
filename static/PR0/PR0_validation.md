# PR0 검증 결과

PR0의 목표는 `mode=off`에서 QuotaServe 관련 변경이 기존 baseline LRU 동작을 깨지 않는지 확인하고, eviction attribution log가 정상적으로 기록되는지 검증하는 것이다.

## 결론

**PASS**

Chat+Longctx 기준으로 PR0 검증 항목을 모두 만족했다.

```text
mode=off mixed run 실행: PASS
baseline eviction log 생성: PASS
Case 1 baseline LRU와 결과 동일성: PASS
```

## 실행 조건

```text
model: meta-llama/Llama-3.2-3B-Instruct
max_model_len: 8192
prefix caching: enabled
quota_serve_mode: off
chat trace: workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl
longctx trace: workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl
chat qps: 5.0
longctx qps: 5.0
max concurrency: 32
chat requests: 1000
longctx requests: 1000
```

## PR0 실행 결과

```text
total requests: 2000
success: 2000
failed: 0
duration: 402.7s
throughput: 4.97 req/s
```

### Chat

```text
count: 1000
errors: 0
hit_rate mean: 0.082
hit_rate p50: 0.032
TTFT mean: 483.6 ms
TTFT p50: 415.8 ms
TTFT p95: 1125.0 ms
TTFT p99: 1684.3 ms
SLO attainment: 47.6%
```

### Longctx

```text
count: 1000
errors: 0
hit_rate mean: 0.029
hit_rate p50: 0.029
TTFT mean: 698.4 ms
TTFT p50: 654.5 ms
TTFT p95: 1227.9 ms
TTFT p99: 1662.0 ms
SLO attainment: 100.0%
```

## Case 1 baseline과 비교

Case 1 baseline LRU 결과:

```text
Chat hit_rate_mean: 0.08195
Chat TTFT mean: 486.4 ms
Chat TTFT p95: 1181.4 ms
Chat SLO attainment: 47.1%

Longctx hit_rate_mean: 0.02903
Longctx TTFT mean: 708.6 ms
Longctx TTFT p95: 1353.7 ms
Longctx SLO attainment: 100.0%
```

PR0 결과:

```text
Chat hit_rate_mean: 0.0824
Chat TTFT mean: 483.6 ms
Chat TTFT p95: 1125.0 ms
Chat SLO attainment: 47.6%

Longctx hit_rate_mean: 0.0290
Longctx TTFT mean: 698.4 ms
Longctx TTFT p95: 1227.9 ms
Longctx SLO attainment: 100.0%
```

차이:

```text
Chat hit_rate_mean: +0.00045
Chat TTFT mean: -2.8 ms
Chat TTFT p95: -56.4 ms
Chat SLO attainment: +0.5 percentage point

Longctx hit_rate_mean: 거의 동일
Longctx TTFT mean: -10.2 ms
Longctx SLO attainment: 0 percentage point
```

차이가 작고 GPU/runtime noise 범위로 볼 수 있다. 따라서 `mode=off`는 기존 baseline LRU 동작을 유지한다고 판단한다.

## Eviction log 검증

실험 종료 후 `/flush_eviction_log` 호출 결과:

```text
num_flushed: 148074
```

eviction log 파싱 결과:

```text
total eviction events: 182305
reused_later=true: 34231
reused_later=false: 148074
missing trigger_workload: 0
missing evicted_workload: 0
```

`total eviction events`와 `num_flushed`가 다른 것은 정상이다. 나중에 재사용된 eviction event는 재사용이 감지되는 순간 `reused_later=true`로 먼저 기록되고, 끝까지 재사용되지 않은 pending event만 flush 시점에 `reused_later=false`로 기록된다.

## Cross-workload useful eviction

```text
longctx -> chat:
  total eviction: 30872
  useful eviction: 24530
  useful ratio: 79.5%

chat -> longctx:
  total eviction: 35257
  useful eviction: 105
  useful ratio: 0.3%
```

baseline LRU에서는 Longctx가 Chat cached prefix block을 밀어냈을 때, 그 block이 나중에 다시 사용되는 경우가 많다. 즉 `chat <- longctx` useful eviction 문제가 실제로 관측된다.

이 결과는 Static Quota에서 Chat hot prefix를 보호해야 하는 근거가 된다.

## 산출물

```text
static/raw_results/pr0_chat_longctx_apc_on_len8192.jsonl
static/raw_results/pr0_chat_longctx_apc_on_len8192_summary.json
static/eviction_logs/pr0_eviction_chat_longctx_apc_on_len8192.jsonl
```
