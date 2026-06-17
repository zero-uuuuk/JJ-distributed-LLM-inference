<div align="center">

# QuotaServe Implementation Plan

**Workload-aware Prefix Cache Quota — Case 2 Implementation Roadmap**

_PR0 Dry-run → Static → Dynamic floor → Dynamic full_

</div>

---

## 0. 이 문서의 위치

- **Design**: `docs/QUOTASERVE_DESIGN.md` — 정책 원형(PFF식 feedback loop, floor/cap, 피해/낭비 신호)
- **Case 1**: `hypothesis_validation/case1_validation/` — eviction attribution + shadow cache offline 계측 완료
- **Case 2 (본 문서)**: QuotaServe 구현 단계화 및 실험 절차
- **vLLM fork**: 별도 repo (`~/vllm`, branch 추후 확정). 본 문서의 PR 0~PR 4는 vLLM fork에서 진행한다. JJ repo에는 `hypothesis_validation/case2_validation/`만 추가된다.

> [!NOTE]
> 본 문서는 **static까지의 우선 구현**을 자세히 다룬다. PR 5 이후(dynamic)는 의도와 큰 골격만 적어두고 static 결과를 본 뒤 확정한다.

---

## 1. 워크로드 구성 (3종)

Case 2는 Case 1과 동일한 워크로드 trace를 그대로 사용한다. 새로 빌드하지 않는다.

| Tag | Trace | 역할 | Builder |
|---|---|---|---|
| `chat` | `workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl` | victim (multi-turn, delayed reuse) | `workloads/sharegpt/build_sharegpt_workload.py` |
| `longctx` | `workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl` | cold antagonist (low-reuse, prefill-heavy) | `workloads/hotpotqa/build_hotpotqa_workload.py` |
| `agent` | `workloads/traj/traj_agent_100session_10step.jsonl` | warm antagonist (delayed self-reuse) | `workloads/traj/build_traj_agent_workload.py` |

각 trace는 `1000 requests`이고, request_id는 workload 별 prefix로 시작한다:

```text
chat_{conversation_id}_turn_{n}
hotpotqa-longctx-{n}        # longctx
agent_{session_id}_step_{n}
```

이 prefix가 **PR 2의 workload 태그 추출 단서**가 된다.

---

## 2. 실험 환경 (고정값)

| 항목 | 값 |
|---|---|
| Instance | g5 (single GPU) |
| Model | `meta-llama/Llama-3.2-3B-Instruct` |
| `--max-model-len` | `8192` |
| `--max-num-seqs` | `32` |
| `--gpu-memory-utilization` | `0.6` |
| Per-workload QPS | `5.0` |
| `--chat-slo-ms` | `400` |
| `--longctx-slo-ms` | `7700` |
| `--agent-slo-ms` | `10000` |
| Trace size | `1000 requests/workload` |

Case 1의 `RUN_MIXED.md`와 동일한 server 명령을 그대로 쓴다. Case 2에서는 `QUOTA_SERVE_MODE`, `QUOTA_SERVE_CONFIG` 등 env var만 추가한다.

---

## 3. PR 단계 — 전체 그림

| PR | 범위 | 산출 | 검증 기준 |
|---|---|---|---|
| **PR 0** | vLLM hook map + dry-run shadow policy | hook 위치 문서, `mode=dry_run` | shadow 로그가 채워지지만 실제 victim은 LRU와 동일 |
| **PR 1** | config schema + `mode=off` parity | `quota_serve.yaml`, env loader | `mode=off`가 baseline LRU와 동일 결과 |
| **PR 2** | request workload tag | tag 추출/전파 경로 | eviction 로그의 `trigger_workload`가 trace prefix와 일치 |
| **PR 3** | block owner + evictable_cached counter | `block.workload_id`, `state[w].evictable_cached` + flag | invariant check pass |
| **PR 4** | static quota victim selection | `quota_aware_select_victim` (3-tier) | `chat ← longctx` useful eviction 감소 |
| PR 5 | shadow cache → online signal | runtime `damage_w`, `waste_w` 집계 | tick log가 신호 변화 추적 |
| PR 6 | `dynamic_floor` | floor feedback rule | Chat damage 신호에 따라 floor 자동 상승 |
| PR 7 | `dynamic_full` | floor + cap feedback rule | Case 2 main 실험에 사용 |

PR 0~PR 4까지가 본 문서의 1차 목표다.

---

## 4. PR 0 — vLLM hook map + dry-run shadow policy

> [!IMPORTANT]
> **이 PR이 가장 먼저 들어가야 한다.** 실제 victim 선택은 바꾸지 않고, "QuotaServe가 적용되었다면 어떤 block을 victim으로 골랐을지"만 별도 로그로 남긴다.
> 이 단계의 목표는 두 가지다.
> 1. vLLM block manager의 어느 함수 어느 줄에 hook을 걸어야 하는지 코드 베이스에 못 박는다.
> 2. 이후 PR에서 실제 정책으로 전환할 때, dry-run 로그와 실제 동작의 일치 여부로 회귀를 즉시 잡는다.

### 4.1 Hook map 문서화

vLLM의 prefix cache eviction path에서 다음 위치를 식별하고 주석으로 표시한다.

| Hook | 호출 시점 | 들어가야 할 정보 |
|---|---|---|
| `on_block_allocate(block, request)` | 새 KV block이 할당될 때 | `request.workload_id`, `block.id` |
| `on_block_cached(block)` | block이 cached 상태가 될 때 (ref=0 + has hash) | owner workload, hash |
| `on_block_hit(block, request)` | cached block hit | hit-side workload, owner workload |
| `on_block_evict(block, trigger_request)` | block이 eviction되어 제거됨 | evicted owner workload, trigger workload, hash |
| `on_select_victim(free_queue, trigger_request)` | LRU victim을 고르기 직전 | hook return으로 victim 후보 override |

각 hook의 정확한 vLLM 함수명/줄번호는 PR 0에서 코드 인스펙션으로 확정해 본 문서에 채워 넣는다.

### 4.2 Dry-run shadow policy 구현

```python
class QuotaServeShadowPolicy:
    """Logs what QuotaServe WOULD pick. Never changes actual victim."""

    def on_select_victim(self, free_queue, trigger_request):
        actual_victim = free_queue.head  # what vLLM will actually evict
        shadow_victim = self._quota_aware_select(free_queue, trigger_request)

        log_jsonl({
            "ts": now(),
            "trigger_workload": trigger_request.workload_id,
            "actual_victim_workload": actual_victim.workload_id,
            "shadow_victim_workload": shadow_victim.workload_id,
            "match": actual_victim.id == shadow_victim.id,
            "reason": shadow_victim.selection_reason,
        })

        return actual_victim  # 절대 바꾸지 않는다
```

이렇게 두면 PR 4의 static victim selection 로직을 미리 dry-run 모드로 검증할 수 있다.

### 4.3 검증

- `mode=dry_run`으로 Chat+Longctx mixed run 1회 실행
- shadow 로그에서 `match=False`(QuotaServe라면 다른 victim을 골랐을) 비율이 0%가 아닌지 확인
- 실제 결과(hit rate, TTFT, SLO)는 baseline LRU와 통계적으로 동일해야 한다 (정책이 실제로는 안 바뀌었으므로)

---

## 5. PR 1 — Config schema + `mode=off` parity

### 5.1 Config 스키마 (`vllm/quota_serve/config.yaml` 또는 env)

```yaml
quota_serve:
  enabled: true
  mode: "off"   # off | dry_run | static | dynamic_floor | dynamic_full
  scan_limit: 256
  tick_sec: 30
  shadow_ttl_sec: 120

  workloads:
    chat:
      floor_ratio: 0.25
      cap_ratio: 1.00
    rag:
      floor_ratio: 0.02
      cap_ratio: 0.40
    longctx:
      floor_ratio: 0.02
      cap_ratio: 0.25
    agent:
      floor_ratio: 0.15
      cap_ratio: 0.80
```

> [!WARNING]
> `floor_ratio`의 초기값은 **Case 1의 Chat-only 정상 상태 cached occupancy 비율**을 측정해서 채울 것. 임의값 금지. 측정은 Case 1 `chat.hit_rate_mean`과 함께 raw_results에서 occupancy 시계열을 뽑아 사용한다.

### 5.2 환경 변수

| Env | 의미 |
|---|---|
| `QUOTA_SERVE_MODE` | config의 mode를 override |
| `QUOTA_SERVE_CONFIG` | yaml 경로 |
| `QUOTA_SERVE_LOG` | eviction/tick JSONL 로그 경로 |

### 5.3 검증 — `mode=off` parity test

> [!IMPORTANT]
> **이 검증을 통과하지 못하면 이후 모든 PR이 의미 없다.**

- Case 1 Chat+Longctx mixed APC ON과 **동일한** 명령을 `QUOTA_SERVE_MODE=off`로 실행
- 비교 metric:
  - `chat.hit_rate_mean` 차이 < 1%p
  - `chat.ttft_ms_p95` 차이 < 5%
  - `chat.slo_attainment` 차이 < 1%p
  - eviction 총 건수 동일
- 차이가 발생하면 metadata 추가나 로깅이 hot path를 건드린 것. PR 0 hook map을 재검토한다.

---

## 6. PR 2 — Request workload tag

### 6.1 추출 규칙 (MVP)

`request_id`의 prefix에서 추출한다.

```python
def infer_workload(request_id: str) -> str:
    if request_id.startswith("chat_"):
        return "chat"
    if request_id.startswith("hotpotqa-"):
        return "longctx"
    if request_id.startswith("agent_"):
        return "agent"
    if request_id.startswith("rag_") or request_id.startswith("msmarco-"):
        return "rag"
    return "unknown"
```

> [!NOTE]
> Case 1에서는 `user` 필드에 workload 태그를 박았다(`run_mixed.py`). PR 2에서는 두 경로를 모두 받아들이되, **`user` 필드를 우선**한다. 명시 태그가 없을 때 `request_id` prefix로 fallback.

### 6.2 전파 경로

- API request 진입점에서 `workload_id` 추출
- `Request` / `Sequence` 객체에 attribute로 추가
- block allocation 시 owner workload로 사용

### 6.3 검증

- Chat+Longctx mixed run
- 기존 `VLLM_EVICTION_LOG`의 `evicted_workload` / `trigger_workload` 값이 모두 `{chat, longctx, unknown}` 중 하나
- `unknown` 비율이 0%

---

## 7. PR 3 — Block owner + evictable_cached counter

### 7.1 Block metadata

```python
class KVBlock:
    workload_id: str | None             # PR 3에서 추가
    is_counted_as_evictable_cached: bool # PR 3에서 추가, 초기 False
```

- `workload_id`는 **block을 처음 생성한 request의 workload**로 한 번 설정하고 절대 바꾸지 않는다.
- 다른 workload가 hit해도 owner 유지.

### 7.2 Counter 자료구조

```python
state: dict[str, WorkloadState]

class WorkloadState:
    evictable_cached: int = 0   # ref=0 and is_cached
    insertions_window: int = 0
    evictions_suffered_window: int = 0
    useful_evictions_suffered_window: int = 0
```

### 7.3 Counter 업데이트 지점

**한 함수에 wrapping**하여 ref_cnt 변경을 모두 통과시킨다.

```python
def maybe_update_evictable_count(block, prev_ref, new_ref):
    is_cached = block.is_cached()
    should_be_counted = (new_ref == 0) and is_cached

    if should_be_counted and not block.is_counted_as_evictable_cached:
        state[block.workload_id].evictable_cached += 1
        block.is_counted_as_evictable_cached = True
    elif not should_be_counted and block.is_counted_as_evictable_cached:
        state[block.workload_id].evictable_cached -= 1
        block.is_counted_as_evictable_cached = False
```

### 7.4 적용 지점 grep checklist

PR 3 머지 전 반드시 다음을 확인한다.

- [ ] `ref_cnt += 1`이 일어나는 모든 줄에서 wrapper 호출
- [ ] `ref_cnt -= 1`이 일어나는 모든 줄에서 wrapper 호출
- [ ] block이 cache에 등록되는 시점 (`is_cached: False → True`)에서 wrapper 호출
- [ ] block이 eviction되어 cache에서 빠지는 시점에서 wrapper 호출
- [ ] block free path 전부

### 7.5 Invariant check

매 tick마다 다음을 assertion 또는 log로 찍는다.

```python
actual = count_blocks(lambda b: b.ref_cnt == 0 and b.is_cached())
expected = sum(s.evictable_cached for s in state.values())
assert actual == expected, f"counter drift: {actual} vs {expected}"
```

drift가 발견되면 즉시 fail. PR 4 이전에 fix.

---

## 8. PR 4 — Static quota victim selection

### 8.1 Hook 진입점

PR 0의 `on_select_victim`을 실제로 정책에 사용한다.

```python
def select_victim(free_queue, trigger_request):
    mode = quota_config.mode
    if mode == "off":
        return free_queue.head
    if mode == "dry_run":
        return _dry_run_select(free_queue, trigger_request)  # PR 0
    if mode in ("static", "dynamic_floor", "dynamic_full"):
        return quota_aware_select_victim(free_queue, trigger_request)
```

### 8.2 `quota_aware_select_victim`

```python
def quota_aware_select_victim(free_queue, trigger_request):
    head = free_queue.head

    # 0. head가 uncached free block이면 곧장 사용
    if not head.is_cached():
        head.selection_reason = "uncached_head"
        return head

    total_evictable = sum(s.evictable_cached for s in state.values())

    # 1. cap 초과 workload의 block 우선
    victim = scan_lru(
        limit=quota_config.scan_limit,
        predicate=lambda b: b.is_cached() and _over_cap(b),
    )
    if victim is not None:
        victim.selection_reason = "over_cap"
        return victim

    # 2. floor보다 여유 있는 workload의 block
    victim = scan_lru(
        limit=quota_config.scan_limit,
        predicate=lambda b: b.is_cached() and _above_floor(b),
    )
    if victim is not None:
        victim.selection_reason = "above_floor"
        return victim

    # 3. 모두 floor 이하 — 어쩔 수 없이 LRU
    head.selection_reason = "fallback_lru"
    metrics.quota_protected_but_evicted += 1
    return head


def _over_cap(block):
    w = block.workload_id
    return state[w].evictable_cached > _abs_cap(w)

def _above_floor(block):
    w = block.workload_id
    return state[w].evictable_cached > _abs_floor(w)

def _abs_floor(w):
    return int(quota_config.workloads[w].floor_ratio * total_block_pool)

def _abs_cap(w):
    return int(quota_config.workloads[w].cap_ratio * total_block_pool)
```

> [!TIP]
> `total_block_pool`은 전체 GPU KV block 수가 아니라 **evictable 후보가 될 수 있는 cached prefix block의 상한**으로 해석한다. 운영 중에는 `sum(state[w].evictable_cached)`로도 근사 가능. PR 4에서는 일단 전체 block 수의 일정 비율로 두고, dynamic PR에서 정밀화한다.

### 8.3 로그 스키마

eviction log entry에 최소 다음을 추가한다.

```json
{
  "ts": 123.45,
  "evicted_workload": "chat",
  "trigger_workload": "longctx",
  "block_hash": "...",
  "victim_occ": 1200,
  "victim_floor": 1000,
  "victim_cap": 5000,
  "selection_reason": "over_cap | above_floor | fallback_lru | uncached_head",
  "scan_steps": 37
}
```

별도 메트릭:

```text
quota_scan_success
quota_scan_fallback
quota_protected_but_evicted   # fallback_lru 횟수
quota_overcap_evicted          # over_cap 횟수
scan_steps_mean / p95
```

### 8.4 Static 검증 실험

기존 Case 1 `run_mixed.py`를 그대로 사용하되, vLLM 서버를 `QUOTA_SERVE_MODE=static`으로 띄운다.

```bash
# Chat + Longctx
VLLM_SERVER_DEV_MODE=1 \
VLLM_EVICTION_LOG=/path/eviction_static_chat_longctx.jsonl \
QUOTA_SERVE_MODE=static \
QUOTA_SERVE_CONFIG=/path/quota_serve.yaml \
QUOTA_SERVE_LOG=/path/quota_static_chat_longctx.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

비교 대상:

| run | mode | 기대 |
|---|---|---|
| baseline | `off` | Case 1 결과 재현 |
| dry-run | `dry_run` | baseline과 동일 결과 + shadow 로그 채워짐 |
| static | `static` | `chat ← longctx` useful eviction 감소, Chat hit rate 회복 |

검증 통과 기준:

- `chat ← longctx` useful eviction 건수 감소 (예: ≥20% 감소)
- `chat.hit_rate_mean` 증가
- `longctx.slo_attainment` 큰 폭 손상 없음 (예: ≤5%p 감소)
- `quota_protected_but_evicted` 비율이 전체 eviction의 일정 임계 이하 (예: 10%)

여기서 결과가 안 나오면 dynamic으로 가도 의미 없다. floor/cap 값을 먼저 sweep한다.

---

## 9. PR 5~7 — Dynamic (sketch)

### 9.1 PR 5: shadow cache runtime signal

- 기존 offline shadow cache 코드를 runtime path로 이식
- eviction 순간에 `shadow_cache[block_hash] = {victim_w, evictor_w, evict_ts, counted=False}`
- 이후 prefix lookup에서 real cache miss + shadow hit이면 `useful_evictions_suffered[victim_w] += 1`
- 한 block당 한 번만 카운트
- TTL: `shadow_ttl_sec=120`

### 9.2 PR 6: dynamic_floor

```python
damage_w = useful_evictions_suffered_w / max(evictions_suffered_w, min_samples)

if damage_w > D_hi:        # 0.5
    floor_w += floor_step
elif damage_w < D_lo and occ_w < floor_w:   # 0.1
    floor_w -= floor_step
```

cap은 고정. floor 효과만 분리하여 측정.

### 9.3 PR 7: dynamic_full

```python
reuse_w = useful_evictions_suffered_w / max(evictions_suffered_w, min_samples)
low_reuse_w = 1 - reuse_w
pressure_w = insertions_w / max(total_insertions, 1)
waste_w = low_reuse_w * pressure_w

if waste_w > W_hi and damage_w < D_hi:
    cap_w -= cap_step
elif waste_w < W_lo and cap_is_binding(w) and hit_rate_w > H_lo:
    cap_w += cap_step
```

tick은 opportunistic. allocation/free path에서 `now - last_tick > tick_sec`이면 호출.

---

## 10. Case 2 실험 매트릭스

| ID | Mixed | Cache policy | 목적 |
|---|---|---|---|
| C2-LRU-CL | Chat + Longctx | shared LRU (baseline) | Case 1 재현 |
| C2-REUSE-CL | Chat + Longctx | reuse-aware (LFU/GDSF) | quota 없이 똑똑한 eviction 분리 |
| C2-STAT-CL | Chat + Longctx | QuotaServe static | static 격리만의 효과 (ablation) |
| C2-DYN-CL | Chat + Longctx | QuotaServe dynamic_full | **메인 결과** |
| C2-LRU-CA | Chat + Agent | shared LRU | Case 1 재현 |
| C2-DYN-CA | Chat + Agent | QuotaServe dynamic_full | Agent에서도 같은 메커니즘 |
| (선택) C2-DYN-CLA | Chat + Longctx + Agent | dynamic_full | 3-workload 일반화 sanity |

각 row마다 동일 seed로 1회 + 가능하면 seed 3개로 반복.

---

## 11. 로그 산출물 위치

```text
hypothesis_validation/case2_validation/
├── raw_results/
│   ├── c2_lru_chat_longctx_apc_on.jsonl
│   ├── c2_static_chat_longctx_apc_on.jsonl
│   ├── c2_dyn_chat_longctx_apc_on.jsonl
│   └── ...
├── eviction_logs/
│   ├── eviction_static_chat_longctx.jsonl
│   ├── quota_tick_static_chat_longctx.jsonl
│   └── ...
├── analysis_results/
│   └── ...
├── run_mixed_c2.py         # Case 1 run_mixed.py에서 QuotaServe 모드 인자 추가
├── RUN_C2.md               # Case 2 실행 절차
└── README.md
```

---

## 12. 다음 작업 큐 (당장 할 것)

1. PR 0: vLLM block manager의 eviction path를 정독하고 `on_select_victim` 등 hook 후보 위치 5곳에 주석 마커를 박는다. dry-run shadow policy class 추가.
2. PR 1: `quota_serve.yaml` 스키마 확정 + `mode=off` parity test 통과.
3. Case 1 raw_results에서 Chat-only steady-state occupancy를 측정해 `chat.floor_ratio` 초기값 결정.
4. PR 2~3 동시 진행 가능 (workload tag → block owner → counter).
5. PR 4 static + Chat+Longctx 실험. 결과를 보고 dynamic 단계 thresholds 확정.

---

<div align="center">
<sub>QuotaServe · Implementation Plan · JJ Distributed LLM Inference</sub>
</div>
