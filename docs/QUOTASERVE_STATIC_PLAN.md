<div align="center">

# QuotaServe Implementation Plan (Static)

**Workload-aware Prefix Cache Quota — Case 2 Static Implementation Roadmap**

_PR0 Dry-run → Static (fixed quota) → (이후) Dynamic single-signal_

</div>

---

## 0. 이 문서의 위치

- **Static policy 상세**: `docs/QUOTASERVE_STATIC_QUOTA_IMPLEMENTATION.md` — 고정 `quota_ratio` + `occupancy_w > quota_w` victim selection. **본 로드맵의 static 정책 근거 문서.**
- **Dynamic design**: `docs/QUOTASERVE_DYNAMIC_QUOTA_DESIGN.md` — PFF식 feedback loop, **cross-workload useful eviction ratio 단일 신호**, profile로 얻은 `floor/cap` 범위 안에서 `quota_w` 조정.
- **Case 1**: `hypothesis_validation/case1_validation/` — eviction attribution + shadow cache offline 계측 완료
- **Case 2 (본 문서)**: QuotaServe 구현 단계화 및 실험 절차
- **vLLM fork**: 별도 repo (`~/vllm`, branch 추후 확정). 본 문서의 PR 0~PR 4는 vLLM fork에서 진행한다. JJ repo에는 `hypothesis_validation/case2_validation/`만 추가된다.

> [!NOTE]
> 본 문서는 **static까지의 우선 구현**을 자세히 다룬다. static은 workload별 **고정 단일 `quota_w`**만 쓰고 실행 중 자동 조정하지 않는다(`ratio_low/high`, `tick`, `window_size`, `step` 미사용). PR 5 이후(dynamic)는 큰 골격만 적고 전체 정의는 dynamic design 문서에 둔다.

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
| Instance | `g5.xlarge` (single GPU) |
| Model | `meta-llama/Llama-3.2-3B-Instruct` |
| `--max-model-len` | `8192` |
| max output tokens | `1776` (요청별 생성 토큰 상한. 기존 trace의 최대 `output_token_len` 기준) |
| `--max-num-seqs` | `32` |
| `--gpu-memory-utilization` | `0.6` |
| Per-workload QPS | `5.0` |
| `--chat-slo-ms` | `400` |
| `--longctx-slo-ms` | `7700` |
| `--agent-slo-ms` | `200` (APC ON, single 기준 1회 측정 후 p95로 설정) |
| Trace size | `1000 requests/workload` |

> [!NOTE]
> `max output tokens`는 server의 `--max-model-len`이 아니라 요청을 보낼 때 적용하는 생성 길이 상한이다. `--max-model-len=8192`로 낮춘 기준이므로, agent 요청의 `input_tokens + output_token_len`이 8192를 넘지 않는지는 실행 전 sanity check한다.

Case 1의 `RUN_MIXED.md`와 동일한 server 명령을 그대로 쓴다. Case 2에서는 `QUOTA_SERVE_MODE`, `QUOTA_SERVE_CONFIG` 등 env var만 추가한다.

---

## 3. PR 단계 — 전체 그림

| PR | 범위 | 왜 하는가 | 산출 | 검증 기준 |
|---|---|---|---|---|
| **PR 0** | vLLM hook map + dry-run shadow policy | 실제 정책을 바꾸기 전에 block lifecycle 관측 지점과 dry-run 비교 경로를 확보한다. | hook 위치 문서, `mode=dry_run` | shadow 로그가 채워지지만 실제 victim은 LRU와 동일 |
| **PR 1** | config schema + `mode=off` parity | QuotaServe를 켜고 끄는 설정 진입점을 만들고, 꺼진 상태에서는 baseline을 절대 깨지 않음을 보장한다. | `quota_serve.yaml`, env loader | `mode=off`가 baseline LRU와 동일 결과 |
| **PR 2** | request workload tag | eviction/caching 사건을 workload 단위로 귀속할 수 있게 request에 workload 정체성을 붙인다. | tag 추출/전파 경로 | eviction 로그의 `trigger_workload`가 trace prefix와 일치 |
| **PR 3** | block owner + occupancy counter | 각 cached block의 owner와 workload별 `occupancy_w`를 알아야 `occupancy_w > quota_w` 판단이 가능하다. | `block.workload_id`, `state[w].evictable_cached` + flag | invariant check pass |
| **PR 4** | static quota victim selection | 고정 단일 `quota_w`만으로 LRU보다 나은 victim 선택이 가능한지 먼저 검증한다. | `quota_aware_select_victim` (2-tier) | `chat ← longctx` useful eviction 감소 |
| PR 5 | shadow cache → online signal | eviction된 block이 나중에 victim workload에게 다시 필요했는지를 runtime에서 **cross-workload useful eviction ratio**로 관측한다. | runtime `useful_eviction_ratio_w` 집계 | tick log가 신호 변화 추적 |
| PR 6 | profile → `ratio_low/high` + `floor/cap` 도출 | static sweep 결과에서 기준선과 workload별 quota 범위를 뽑는다. | profile curve, `ratio_low/high`, `floor/cap` | quota↑에 따라 useful eviction ratio↓ curve 확인 |
| PR 7 | `dynamic` | 단일 `quota_w`를 useful eviction ratio에 따라 `±step`으로 움직이고 `[floor,cap]`에 clamp한다. | dynamic quota controller | Case 2 main 실험에 사용 |

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

static은 workload별 **단일 `quota_ratio`**만 쓴다. `floor/cap`은 dynamic에서 profile로 도출되는 범위이므로 static config에는 두지 않는다.

```yaml
quota_serve:
  enabled: true
  mode: "off"   # off | dry_run | static | dynamic
  tick_sec: 30          # dynamic 전용 (static 미사용)
  shadow_ttl_sec: 120   # PR 5 shadow cache 전용

  workloads:
    chat:
      quota_ratio: 0.30
    rag:
      quota_ratio: 0.08
    longctx:
      quota_ratio: 0.10
    agent:
      quota_ratio: 0.25
```

`quota_ratio`는 `quota_base_blocks` 대비 해당 workload가 보호받을 목표 cached-prefix quota 비율이다. 절대 block 수 변환은 `quota_w = quota_ratio_w × quota_base_blocks` (§8.2). 초기 구현에서 `quota_base_blocks`는 전체 KV cache block 수로 두고 **반드시 로그에 남긴다**.

> [!WARNING]
> `quota_ratio`의 초기값은 **Case 1의 Chat-only 정상 상태 cached occupancy 비율**을 참고해 잡는다. 순수 임의값 금지. 이후 §12의 static sweep으로 값을 바꿔가며 useful eviction ratio를 관찰한다.

### 5.2 환경 변수

| Env | 의미 |
|---|---|
| `QUOTA_SERVE_MODE` | config의 mode를 override |
| `QUOTA_SERVE_CONFIG` | yaml 경로 |
| `QUOTA_SERVE_LOG` | quota/eviction JSONL 로그 경로 |

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

## 7. PR 3 — Block owner + occupancy counter

### 7.1 Block metadata

```python
class KVBlock:
    workload_id: str | None              # PR 3에서 추가 (= owner_workload)
    is_counted_as_evictable_cached: bool # PR 3에서 추가, 초기 False
```

- `workload_id`는 **block을 (재)할당한 request의 workload**로 설정하고, block이 free→재할당될 때 새 owner로 overwrite한다.
- 다른 workload가 hit해도 owner 유지(hit-side는 owner를 바꾸지 않는다).

### 7.2 Counter 자료구조

static이 실제로 쓰는 counter는 **occupancy 하나**다.

```python
state: dict[str, WorkloadState]

class WorkloadState:
    evictable_cached: int = 0   # = occupancy_w : ref_cnt==0 and is_cached_prefix
```

`occupancy_w`는 workload `w`가 현재 들고 있는 evictable cached prefix block 수다(`ref_cnt>0` running block은 제외).

> [!NOTE]
> cross-workload useful eviction ratio용 window counter(`insertions_window`, `evictions_suffered_window`, `useful_evictions_suffered_window` 등)는 **dynamic(PR 5) 소관**이라 static에서는 두지 않는다.

### 7.3 Counter 업데이트 지점 (flag 기반 상태 전이)

이벤트가 아니라 **상태 전이**(`ref_cnt==0 and is_cached_prefix`)로 갱신한다. **한 함수에 wrapping**하여 ref_cnt/is_cached 변경을 모두 통과시킨다.

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

### 7.5 Invariant check (consistency check)

static에는 제어 tick 루프가 없다(§9 참조). 따라서 이 검사는 tick이 아니라 **구현 초기 디버그 단계에서 eviction 시점마다**(부담되면 매 K회 eviction마다, 또는 디버그 타이머로 주기적으로) 돌린다. `count_blocks(...)`는 전체 block을 훑는 O(N) 스캔이라 상시로 두지 말고 디버그 빌드/샘플링으로 제한한다.

```python
actual = count_blocks(lambda b: b.ref_cnt == 0 and b.is_cached())
expected = sum(s.evictable_cached for s in state.values())
assert actual == expected, f"counter drift: {actual} vs {expected}"
```

drift가 발견되면 owner 기록 / 상태 전이 처리 / eviction hook 중 하나가 잘못된 것이다. 즉시 fail 처리하고 PR 4 이전에 fix한다.

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
    if mode in ("static", "dynamic"):
        return quota_aware_select_victim(free_queue, trigger_request)
```

### 8.2 `quota_aware_select_victim` (2-tier)

`occupancy_w > quota_w`인 workload를 우선 victim 후보로 두고, 그 집합 안에서 LRU를 고른다. over-quota workload가 없으면 global LRU fallback이다.

```python
def quota_aware_select_victim(free_queue, trigger_request):
    if not quota_config.is_active:
        return free_queue.head, "baseline_lru_off_mode"

    head = free_queue.head

    # 0. head가 uncached free block이면 곧장 사용 → eviction 아님.
    #    cached block이 제거된 게 아니므로 eviction event log에 남기지 않는다.
    #    (집계가 필요하면 free_block_used 카운터로 별도로 센다. §8.3 참조)
    if not head.is_cached():
        return head, "uncached_head"   # non-eviction outcome

    # 1. over-quota workload 집합
    over_quota_workloads = {
        w for w in quota_config.workloads
        if state[w].evictable_cached > _abs_quota(w)
    }
    if not over_quota_workloads:
        return head, "fallback_no_over_quota"

    # 2. LRU head부터 순회하며 over-quota workload의 evictable cached block을 고른다.
    #    free queue front부터 보므로, 처음 만나는 block이 그 집합 안에서 가장 LRU다.
    #    occupancy_w > quota_w이면 그 block이 큐에 반드시 있으므로 unbounded 스캔이어도
    #    끝까지 가기 전에 찾는다(못 찾으면 버그 → 아래 raise).
    for block in scan_lru_head_until_found():
        if (
            block.ref_cnt == 0
            and block.is_cached()
            and block.workload_id in over_quota_workloads
        ):
            return block, "over_quota_selected"

    # over-quota workload가 있는데 evictable cached block을 못 찾으면 버그.
    raise RuntimeError(
        "over-quota workload exists but no evictable cached block was found"
    )


def _abs_quota(w):
    return int(quota_config.workloads[w].quota_ratio * quota_base_blocks)
```

> [!TIP]
> `quota_base_blocks`는 초기 구현에서 전체 KV cache block 수로 둔다. 실제 제어 대상은 `ref_cnt=0` evictable cached prefix block이라 같은 `quota_ratio`라도 환경/workload mix에 따라 의미가 달라지므로, **`quota_base_blocks`를 반드시 로그에 기록**해 절대 block 수 기준을 명확히 한다.

### 8.3 로그 스키마

**Startup log** (1회): static에서 `quota_w`/`quota_base_blocks`는 상수이므로 주기적 tick logger 없이 시작 시 한 번만 기록 — `quota_base_blocks`, workload별 `quota_w`. (occupancy는 아래 eviction 시점에만 기록)

**Eviction event log** entry:

```json
{
  "ts": 123.45,
  "evictor_workload": "longctx",
  "victim_workload": "chat",
  "block_hash": "...",
  "victim_occupancy": 1200,
  "victim_quota": 1000,
  "is_cross_workload": true,
  "selection_reason": "over_quota_selected | fallback_no_over_quota | baseline_lru_off_mode",
  "occupancy_snapshot": {"chat": 1200, "longctx": 300},
  "scan_steps": 37
}
```

`evictor_workload != victim_workload`이면 cross-workload eviction이다.

> [!IMPORTANT]
> **eviction event log는 실제로 cached block이 제거된 경우에만 기록한다.** `uncached_head`(eviction 없이 free block 사용)는 eviction 사건이 아니므로 이 로그에 넣지 않는다 — 넣으면 useful eviction ratio·cross-workload 집계 분모에 non-eviction 케이스가 섞인다. free block 사용 빈도를 보고 싶으면 아래 `free_block_used` 카운터로 따로 센다.

별도 메트릭(실험 종료 후 집계):

```text
over_quota_selected count / ratio
fallback_no_over_quota count / ratio
baseline_lru_off_mode count
free_block_used count            # uncached_head: eviction 아님, eviction 집계와 분리
workload별 eviction count
workload별 cross-workload eviction count
workload별 useful eviction count / ratio   # shadow cache 기반
scan_steps_mean / p95
```

### 8.4 Static 검증 실험


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
- `over_quota_selected ratio`가 유의미하게 관측되고, `fallback_no_over_quota ratio`가 과도하게 높지 않음

여기서 결과가 안 나오면 dynamic으로 가도 의미 없다. `quota_ratio` 값을 먼저 sweep한다(§12).

---

## 9. PR 5~7 — Dynamic (sketch)

> [!NOTE]
> 신호/제어 모델의 전체 정의는 `docs/QUOTASERVE_DYNAMIC_QUOTA_DESIGN.md`에 있다. 여기서는 PR 매핑만 적는다. static 결과를 본 뒤 파라미터를 확정한다.

### 9.1 PR 5: shadow cache runtime signal

- 기존 offline shadow cache 코드를 runtime path로 이식
- eviction 순간에 `shadow_cache[block_hash] = {victim_w, evictor_w, evict_ts, counted=False}`
- **cross-workload(evictor_w != victim_w) eviction만 신호 window에 집계** (self eviction은 보조 관측값)
- victim_w가 같은 block을 재요청 → real miss + shadow hit이면 `cross_workload_shadow_hits[victim_w] += 1` (한 block당 1회)
- workload별 최근 `window_size`개 cross-workload eviction으로 `useful_eviction_ratio_w = cross_workload_shadow_hits_w / window_size`
- TTL: `shadow_ttl_sec=120`

### 9.2 PR 6: profile → `ratio_low/high` + `floor/cap`

- static sweep(§12) 결과에서 `quota`↔`useful_eviction_ratio` profile curve 확보
- `ratio_high`에 대응되는 quota → `floor_w` (부족 판정선), `ratio_low`에 대응되는 quota → `cap_w` (여유 판정선)
- 정책을 바꾸지 않는 offline 분석 단계

### 9.3 PR 7: `dynamic`

```python
if useful_eviction_ratio_w > ratio_high:
    candidate = quota_w + step
elif useful_eviction_ratio_w < ratio_low:
    candidate = quota_w - step
else:
    candidate = quota_w

quota_w = clamp(candidate, floor_w, cap_w)
```

- eviction policy는 static과 동일: `occupancy_w > quota_w`인 workload를 우선 후보로, 그 안에서 LRU
- 전체 quota 합이 evictable pool을 넘으면 floor를 먼저 보장하고 남은 공간을 ratio가 큰 workload에 배분
- tick은 opportunistic. allocation/free path에서 `now - last_tick > tick_sec`이면 호출

---

## 10. Case 2 실험 매트릭스

| ID | Mixed | Cache policy | 목적 |
|---|---|---|---|
| C2-LRU-CL | Chat + Longctx | shared LRU (baseline) | Case 1 재현 |
| C2-REUSE-CL | Chat + Longctx | reuse-aware (LFU/GDSF) | quota 없이 똑똑한 eviction 분리 |
| C2-STAT-CL | Chat + Longctx | QuotaServe static | static 격리만의 효과 (ablation) |
| C2-DYN-CL | Chat + Longctx | QuotaServe dynamic | **메인 결과** |
| C2-LRU-CA | Chat + Agent | shared LRU | Case 1 재현 |
| C2-DYN-CA | Chat + Agent | QuotaServe dynamic | Agent에서도 같은 메커니즘 |
| (선택) C2-DYN-CLA | Chat + Longctx + Agent | dynamic | 3-workload 일반화 sanity |

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
│   ├── quota_static_chat_longctx.jsonl
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
2. PR 1: `quota_serve.yaml` 스키마 확정(단일 `quota_ratio`) + `mode=off` parity test 통과.
3. Case 1 raw_results에서 Chat-only steady-state occupancy를 측정해 `chat.quota_ratio` 초기값 결정.
4. PR 2~3 동시 진행 가능 (workload tag → block owner → occupancy counter).
5. PR 4 static + Chat+Longctx 실험. `quota_ratio` sweep(예: chat 0.10/0.20/0.30/0.50, longctx 0.05/0.10/0.20)으로 useful eviction ratio 변화를 관찰하고, 이 profile로 dynamic 단계의 `ratio_low/high` 후보를 잡는다.

---

<div align="center">
<sub>QuotaServe · Static Implementation Plan · JJ Distributed LLM Inference</sub>
</div>
