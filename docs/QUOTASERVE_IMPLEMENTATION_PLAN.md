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

| Tag | Trace | 역할 | 클라이언트가 보내는 `user` 필드 | Builder |
|---|---|---|---|---|
| `chat` | `workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl` | victim (multi-turn, delayed reuse) | `"chat"` | `workloads/sharegpt/build_sharegpt_workload.py` |
| `longctx` | `workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl` | cold antagonist (low-reuse, prefill-heavy) | `"longctx"` | `workloads/hotpotqa/build_hotpotqa_workload.py` |
| `agent` | `workloads/traj/traj_agent_100session_10step.jsonl` | warm antagonist (delayed self-reuse) | `"agent"` | `workloads/traj/build_traj_agent_workload.py` |

각 trace는 `1000 requests`이고, Case 1 클라이언트(`run_mixed.py`, `run_mixed_agent.py`)는 OpenAI Chat Completions payload의 `"user"` 필드에 위 workload tag를 명시적으로 넣어 보낸다. 따라서 **PR 2의 workload tag 추출은 이 `user` 필드를 1순위로 사용한다**(자세한 내용은 §6).

참고로 request_id도 workload별 prefix를 가지며, 이는 `user` 필드가 비어 있을 때의 **fallback**으로만 사용한다.

```text
chat_{conversation_id}_turn_{n}
hotpotqa-longctx-{n}        # longctx
agent_{session_id}_step_{n}
```

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
| **PR 0** | vLLM lifecycle hook map (5 hooks) | hook 위치 문서, 시그니처 확장 | 5개 hook이 fire되지만 victim은 LRU 그대로(parity 유지) |
| **PR 1** | config schema + `mode=off` parity | `quota_serve.yaml`, env loader | `mode=off`가 baseline LRU와 동일 결과 |
| **PR 2** | workload tag 전파 (`user` → `Request` → block) | entrypoint 파싱, `Request.workload_id`, fallback | eviction 로그의 `trigger_workload`가 클라이언트 `user` 필드와 일치, `unknown` 비율 0% |
| **PR 3** | block owner + evictable_cached counter | `block.workload_id`, `state[w].evictable_cached` + flag | invariant check pass |
| **PR 4** | victim 선택 hook 신설 + static quota victim selection | `select_victim_candidate` 진입점, `quota_aware_select_victim` (3-tier), (옵션) dry-run shadow 비교 | `chat ← longctx` useful eviction 감소 |
| PR 5 | shadow cache → online signal | runtime `damage_w`, `waste_w` 집계 | tick log가 신호 변화 추적 |
| PR 6 | `dynamic_floor` | floor feedback rule | Chat damage 신호에 따라 floor 자동 상승 |
| PR 7 | `dynamic_full` | floor + cap feedback rule | Case 2 main 실험에 사용 |

PR 0~PR 4까지가 본 문서의 1차 목표다.

---

## 4. PR 0 — vLLM lifecycle hook map (5 hooks)

> [!IMPORTANT]
> **이 PR이 가장 먼저 들어가야 한다.** 실제 victim 선택(LRU)은 전혀 바꾸지 않는다. PR 0의 목표는 이후 PR이 owner attribution과 counter를 붙일 수 있도록 block lifecycle 전이 지점마다 hook을 코드 베이스에 못 박는 것이다.
> 이 단계의 목표는 두 가지다.
> 1. vLLM block manager의 어느 함수 어느 줄에 hook을 걸어야 하는지 코드 베이스에 못 박는다.
> 2. hook이 fire되어도 baseline LRU와 결과가 동일함(parity)을 확인해, 이후 PR이 안전한 토대 위에서 시작하게 한다.

> [!NOTE]
> **victim 선택을 가로채는 정책 hook(`select_victim_candidate`)과 dry-run shadow policy(§4.2)는 PR 0에서 제외하고 PR 4로 미뤘다.** 이유:
> 1. PR 0 시점에는 그 hook이 할 일이 없다. "QuotaServe라면 어떤 victim을 골랐을지"를 계산하려면 `block.workload_id`(PR 2)와 floor/cap 정책(PR 4)이 필요한데 둘 다 아직 없다. 지금 박으면 hot path에 no-op 호출만 추가된다.
> 2. "실제로 무엇이 evict됐는지"는 이미 **Hook #2 `on_block_evicted`가 block 단위로** 기록한다(Case 1과 동일한 per-block 방식). 따라서 dry-run 비교의 "actual" 쪽은 별도 진입점 없이 커버된다.
> 3. victim 선택을 실제로 가로채는 동작 변경은 PR 4에서 일어나므로, 그 hook도 PR 4에서 `popleft_n` 경로에 추가하는 것이 자연스럽다.

### 4.1 Hook map 문서화

기존 `KVCacheMetricsCollector` hook들은 **observation 전용**이다. Case 1의 eviction 로깅에는 충분했지만, QuotaServe 정책 개입에는 두 가지 한계가 있다.

1. **인자 부족**: `on_block_allocated(block)`, `on_block_evicted(block)`은 trigger request/workload 정보를 받지 못한다. owner 부여와 trigger attribution이 불가능하다.
2. **개입 지점 없음**: `popleft_n`에 의한 victim 선택을 가로채는 hook이 없다.

PR 0의 hook map은 기존 3개 observation hook의 **시그니처를 확장**하고, 신규 hook 2개를 **새로 박는다**. 총 5개이며, 모두 block lifecycle 전이를 관찰/attribution하는 진입점이다. victim 선택을 가로채는 policy hook은 여기 포함하지 않는다(§4 NOTE 참조, PR 4로 이동). vLLM v1 코드 상의 정확한 위치는 다음과 같다.

| # | Hook | 위치 | 변경 종류 | 비고 |
|---|---|---|---|---|
| 1 | `on_block_allocated(block, request=None)` | `block_pool.py` `get_new_blocks()` 할당 루프 (ref_cnt 0→1) | **시그니처 확장** | request에서 owner workload 부여 (PR 3) |
| 2 | `on_block_evicted(block, trigger_request=None)` | `block_pool.py` `_maybe_evict_cached_block()` 진입부 | **시그니처 확장** | trigger workload 기록 (block 단위) |
| 3 | `on_block_cached(block, request=None)` (신설) | `block_pool.py` `cache_full_blocks()` `insert` 직후 | **신규 hook** | block이 cache에 등록되는 순간 (is_cached F→T) |
| 4 | `on_block_accessed(block, request=None)` | `block_pool.py` `touch()` cache hit 시 | **시그니처 확장** | hit-side workload 기록 (owner는 안 바꿈) |
| 5 | `on_block_freed(block, prev_ref, new_ref)` (신설) | `block_pool.py` `free_blocks()` `ref_cnt -= 1` 직후 | **신규 hook** | ref→0 transition (evictable_cached counter trigger) |

> [!IMPORTANT]
> 시그니처에 추가하는 `request`/`trigger_request`는 모두 **`Optional`(기본 `None`)**이다. 이유: `get_new_blocks`/`touch`/`free_blocks`의 호출자(`single_type_kv_cache_manager.py`)는 현재 `request_id`만 가지고 호출하므로, 전체 호출 사슬을 다시 쓰지 않으려면 새 인자를 optional로 둬야 한다. 실제 request threading은 PR 2/3에서 완성된다. PR 0에서는 인자가 `None`으로 들어와도 base collector가 무시하므로 동작이 baseline과 동일하다(parity). `cache_full_blocks`만은 예외적으로 호출자가 `request`를 이미 가지고 있어 Hook #3은 실제 request를 받는다.

> [!NOTE]
> `kv_cache_manager.py`는 `KVCacheMetricsCollector`를 받아 BlockPool까지 전달하는 순수 배선 경로다(import / `__init__` param / 보관 / coordinator 전달). 실제 hook fire는 모두 `block_pool.py`에서 일어난다. PR 0의 시그니처 확장은 collector 인터페이스 정의(`vllm/v1/core/kv_cache_metrics.py`)와 `BlockPool`의 호출 지점 양쪽을 함께 바꿔야 한다. 기존 테스트가 collector를 `block` 단일 인자로 호출하므로, optional 기본값 덕분에 하위 호환이 유지된다.

### 4.2 (이동됨 → PR 4) Dry-run shadow policy

> [!NOTE]
> **이 절의 dry-run shadow policy는 PR 0에서 구현하지 않고 PR 4로 옮겼다.** §4 NOTE의 이유 그대로다. PR 0에서는 victim 선택을 가로채는 `select_victim_candidate` hook 자체를 박지 않는다. 아래 설계는 PR 4에서 `quota_aware_select_victim`을 실제 적용하기 직전, 동일 hook을 dry-run으로 먼저 검증할 때 쓰기 위한 참조다.

PR 4에서 victim 선택 hook을 `get_new_blocks()`의 `popleft_n` 직전에 신설하면서, dry-run 변형을 다음과 같이 둔다.

```python
class QuotaServeShadowPolicy:
    """Logs what QuotaServe WOULD pick. Never changes actual victim. (PR 4)"""

    def select_victim_candidate(self, free_queue, trigger_request):
        # trigger_request.workload_id는 PR 2에서 user 필드로부터 채워진다.
        trigger_w = trigger_request.workload_id

        actual_victim = free_queue.head  # vLLM이 실제로 evict할 block (popleft 직전)
        shadow_victim = self._quota_aware_select(free_queue, trigger_request)

        log_jsonl({
            "ts": now(),
            "trigger_workload": trigger_w,
            "actual_victim_workload": actual_victim.workload_id,
            "shadow_victim_workload": shadow_victim.workload_id,
            "match": actual_victim.id == shadow_victim.id,
            "reason": shadow_victim.selection_reason,
        })

        # None을 반환하면 caller가 기존 LRU 경로로 fallback한다.
        # 실제 정책은 dry-run에서 절대 적용하지 않는다.
        return None
```

> [!TIP]
> `free_queue.head` 한 개만 비교하는 것은 단순화다. `popleft_n(N)`은 head부터 N개를 꺼내므로 victim이 여러 개일 수 있고, head가 uncached free block이면 eviction event가 없다(§8.2 step 0 `uncached_head`). Case 1처럼 **block 단위**로 비교하려면 꺼낸 block을 순회하며 per-block으로 shadow 판정을 기록하면, Hook #2(`on_block_evicted`)의 per-block eviction log와 1:1로 붙는다. PR 4에서 이 입도(head 1개 vs N개 전부)를 확정한다.

### 4.3 검증 (PR 0)

PR 0은 shadow 비교가 아니라 **hook 배선과 parity**를 검증한다.

- 5개 hook이 의도한 위치에서 fire되는지 확인 (간단한 로그/카운터 또는 단위 테스트)
- 기존 `tests/v1/core/test_kv_cache_metrics.py`가 그대로 통과하는지 확인 (optional 인자 하위 호환)
- 실제 결과(hit rate, TTFT, SLO)는 baseline LRU와 통계적으로 동일해야 한다 (victim 선택을 바꾸지 않았으므로). 이 parity는 PR 1 §5.3에서 정량 기준으로 다시 확인한다.

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

> [!NOTE]
> `mode=off`는 workload tag plumbing(PR 2) 없이도 baseline LRU와 동일해야 한다. mode=off에서는 `Request.workload_id`를 읽지 않으므로 PR 1 단독으로 parity test가 가능하다.

---

## 6. PR 2 — Workload tag 전파 (`user` → `Request.workload_id` → block)

> [!IMPORTANT]
> Case 1 클라이언트(`hypothesis_validation/case1_validation/run_mixed.py`, `hypothesis_validation/agent_validation/run_mixed_agent.py`)는 이미 OpenAI Chat Completions payload의 `"user"` 필드에 workload tag(`chat`, `rag`, `longctx`, `agent`)를 명시적으로 보내고 있다. 근거: `case1_validation/run_mixed.py` L271-289 `build_payload(...)`, L277 주석 — "vLLM은 이 값을 ChatCompletionRequest.user로 파싱하며, 계측 패치 적용 시 Request.workload_tag로 전파된다."
>
> PR 2의 본질은 새로운 추론 로직이 아니라, 이 tag를 vLLM 내부 `Request` 객체와 그 후의 block까지 안정적으로 전파하는 **배선 작업**이다. `request_id` prefix 추론은 어디까지나 fallback이다.

### 6.1 전파 사슬

```text
OpenAI request payload
  └─ "user": "chat"
        │ (vLLM API entrypoint, ChatCompletionRequest 파싱)
        ▼
  ChatCompletionRequest.user
        │ (Request 생성 시)
        ▼
  Request.workload_id        ← PR 2의 핵심 attribute
        │ (block allocation 시, PR 3에서)
        ▼
  KVCacheBlock.workload_id   ← 최초 owner. 이후 변경하지 않음
```

### 6.2 추출 규칙

```python
def resolve_workload_id(chat_request, request_id: str) -> str:
    # 1순위: OpenAI user 필드 (Case 1 클라이언트가 보내는 명시적 tag)
    user_field = getattr(chat_request, "user", None)
    if user_field:
        return user_field.lower()

    # 2순위 (fallback): request_id prefix
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

### 6.3 vLLM 내부 변경 지점

1. `vllm/entrypoints/openai/protocol.py` (또는 `ChatCompletionRequest`가 정의된 파일): `user` 필드는 OpenAI 호환 스펙에 이미 존재할 가능성이 높다. 없으면 추가.
2. `vllm/entrypoints/openai/serving_chat.py`: `Request` 객체 생성 시 `resolve_workload_id(...)` 호출하여 `request.workload_id`로 설정.
3. `vllm/v1/request.py`: `Request` 데이터클래스에 `workload_id: str = "unknown"` 필드 추가.

> [!NOTE]
> Case 1 코드 주석에 따르면 vLLM fork에 이미 `Request.workload_tag`가 추가되어 eviction log에 사용되고 있을 가능성이 높다. PR 0 코드 인스펙션에서 기존 attribute가 확인되면, PR 2는 새로 만들지 말고 이름을 `workload_id`로 통일하거나 alias만 추가한다.

### 6.4 검증

- Chat+Longctx mixed run을 실행 (shadow 로그는 PR 4 전까지 없으므로, PR 2 검증은 **eviction 로그**를 본다)
- Hook #2 `on_block_evicted`가 남기는 eviction 로그의 `trigger_workload`/`evicted_workload` 필드가 `{chat, longctx}`로만 채워지고 `unknown` 비율이 0%
- `unknown`이 발견되면 entrypoint 전파 경로 누락. fallback prefix 추론은 어디까지나 안전망이고, 실제 실험에서는 모든 요청이 `user` 필드를 갖는다.

---

## 7. PR 3 — Block owner + evictable_cached counter

### 7.1 Block metadata 및 owner 부여 (PR 0 시그니처 확장 결과 사용)

```python
class KVCacheBlock:
    workload_id: str | None = None               # PR 3에서 신설
    is_counted_as_evictable_cached: bool = False # PR 3에서 신설
```

owner 부여는 PR 0에서 `on_block_allocated(block, request)`로 시그니처가 확장된 hook을 사용한다. `request.workload_id`는 PR 2가 채워둔 값이다.

```python
# collector 내부, block_pool.py L342-343 / L348-349에서 호출됨
def on_block_allocated(self, block: KVCacheBlock, request: Request) -> None:
    # 이미 owner가 있으면 보존 (다른 workload의 hit이 owner를 바꾸지 못하게)
    if block.workload_id is None:
        block.workload_id = request.workload_id  # PR 2가 채워둔 값

# eviction 시 owner clear — block이 다음에 다른 workload에 재할당될 때
# 깨끗하게 새 owner를 받기 위해
def on_block_evicted(self, block: KVCacheBlock, trigger_request: Request) -> None:
    # ... eviction 로그 기록 ...
    block.workload_id = None  # 다음 owner를 받을 수 있도록 clear

# cache hit 시 owner는 절대 안 바꿈. hit-side workload만 별도로 기록.
def on_block_accessed(self, block: KVCacheBlock, request: Request) -> None:
    # owner(block.workload_id)는 그대로. 필요하면 hit-side workload만 metric에 기록.
    pass
```

**Owner 부여 시점의 의미**:
- `get_new_blocks()`에서 새로 popleft된 block은 `workload_id is None`. 여기서 owner가 부여된다.
- 이미 evict되어 재사용되는 block은 `_maybe_evict_cached_block`(혹은 `on_block_evicted`)에서 owner가 clear되어 있다.
- 다른 workload가 cache hit으로 `touch()`를 호출할 때는 owner를 절대 안 바꾼다. owner는 항상 최초 생성 workload.

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
- [ ] `_maybe_evict_cached_block()` (또는 동등한 hook)에서 `block.workload_id = None`으로 owner clear되는지 확인 — 재할당 시 새 owner를 받아야 함

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

PR 4에서 victim 선택 hook(`select_victim_candidate`)을 `block_pool.py` `get_new_blocks()`의 `popleft_n` 직전에 **신설**하고, 곧바로 정책에 사용한다. (PR 0~3에서는 이 진입점이 없고 victim은 LRU 그대로였다.)

```python
def select_victim(free_queue, trigger_request):
    mode = quota_config.mode
    if mode == "off":
        return free_queue.head
    if mode == "dry_run":
        return _dry_run_select(free_queue, trigger_request)  # §4.2 shadow (PR 4)
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

### 8.2.1 왜 eviction 순간에 3-tier로 처리하고 마지막엔 fallback하는가

#### (1) tick의 한계 — 왜 dynamic 루프가 이 결정을 못 하는가

QuotaServe의 본체는 `QUOTASERVE_DESIGN.md` §5의 **tick 기반 feedback 루프**다. tick(예: 10–30s)마다 신호를 집계하고 floor/cap을 천천히 조정한다(§5, §7). 그런데 **eviction 결정은 tick 주기로 미룰 수 없다**. 그 이유:

- `block_pool.py` `get_new_blocks()`는 **이미 free-block 충분 검사를 통과한 뒤** 호출된다. 즉 이 시점에는 victim을 **반드시 즉시 하나 반환**해야 하며, "이번 tick엔 양보 대상이 없으니 다음 tick까지 기다린다"가 불가능하다. 기다리면 in-flight 요청의 allocation이 막힌다.
- design §6의 정규화·양보(floor 합이 pool을 넘을 때 누가 공간을 내줄지)는 **여러 tick에 걸쳐 천천히 수렴**하는 절차다(§6 마지막 문단, §7 "tick당 변화량 제한"). 이 협상은 본질적으로 비동기·점진적이라 단일 eviction 순간에 실행할 수 없다.
- 따라서 tick 루프는 **"느린 평형"만 담당**하고, eviction 순간의 "지금 누구 block을 뺏을까"는 **동기적이고 결정론적인 별도 규칙**이 맡아야 한다. 그 규칙이 3-tier victim selection이다.

요컨대 tick의 한계는 **시간 입도(granularity) 불일치**다. floor/cap은 tick 단위로 움직이지만 eviction은 순간 단위로 일어나므로, eviction 시점엔 "현재 확정된 floor/cap 값"을 **그 자리에서 한 번에 집행**하는 동기 규칙이 필요하다.

#### (2) 3-tier란 무엇이고 근거는 무엇인가

3-tier는 design §4의 floor/cap을 eviction 순간에 집행하는 **우선순위 규칙**이다. "evict를 하냐 마냐"가 아니라(위 (1)에서 봤듯 반드시 한다) **"누구 block을 뺏는 게 가장 덜 나쁜가"**를 정한다. step 0(`uncached_head`)는 애초에 eviction이 아닌 경우라 곧장 통과시키고, 본 tier는 다음과 같다.

| tier | 대상 | design 근거 |
|---|---|---|
| **1. over_cap** | cap을 **초과한** workload의 cached block | §4.2 cap = 낭비 억제. cap 초과분은 self-churn으로 쌓인 low-reuse 누적이므로 **먼저 회수**한다(design §8 "낭비 회수"). 자기 몫 이상을 쓴 쪽이라 뺏어도 순손해가 없다. |
| **2. above_floor** | floor **위의 여유분**을 가진 workload의 block | §4.1 floor = 보호. floor는 hot prefix의 보장 하한이므로 **절대 건드리지 않고**, 그 위 slack만 evict한다(design §8 "hot cache 피해 줄이기"). |
| **3. fallback_lru** | (모두 floor 이하) head를 LRU로 | design §6/§7의 현실 — floor 합이 evictable pool을 넘을 수 있다. 순간엔 양보 협상을 못 하므로 LRU로 진행하되 `quota_protected_but_evicted`로 **계측**한다. |

**핵심: fallback_lru는 "포기"가 아니라 design으로 넘기는 신호다.**

- 이 카운트는 design §3.1 **피해 신호(useful-eviction-suffered)를 만들어내는 사건**이다. 보호받아야 할 block을 어쩔 수 없이 죽인 횟수이기 때문이다.
- dynamic 루프(design §5)가 이 신호를 받아 *"피해 신호 > upper → floor 증가"*(§4.1)로 **floor를 실제로 올린다**. 즉 static의 한계(순간엔 LRU밖에 없음)를 tick 루프가 사후에 교정한다.
- 그래서 §8.4 검증에서 `quota_protected_but_evicted` 비율이 임계(예 10%)를 넘으면 = "floor 과다설정 또는 총수요 > 용량" = PR 6 dynamic이 조정해야 할 상황을 static 단계에서 미리 드러내는 지표가 된다.

> [!NOTE]
> 3-tier는 design §6의 양보 우선순위(① 피해 신호 낮음 ② 낭비 신호 높음 ③ occupancy가 floor보다 큼 ④ hit rate 낮음)를 **런타임 신호 없이 구조적 임계(floor/cap)만으로 근사**한 static 버전이다. 진짜 피해/낭비 신호는 shadow cache 런타임 계측(PR 5)이 있어야 하므로, PR 4 static은 floor/cap 선만으로 격리하는 ablation(§10 C2-STAT-CL)이고, dynamic full(PR 7)에서 design §3 신호가 붙으면서 양보 규칙이 완성된다.

> [!IMPORTANT]
> `scan_lru(limit=scan_limit)`는 hot path를 O(N)이 아니라 O(scan_limit)으로 묶기 위한 **bounded scan**이다. tier 1/2에서 scan_limit(예 256)칸 안에 자격 block을 못 찾으면 다음 tier로 떨어진다. 따라서 fallback은 "정책상 후보 없음"뿐 아니라 **"제한된 스캔 안에서 못 찾음"**까지 받아내는 안전판이기도 하다. 이 trade-off(스캔 깊이 vs 정확도)는 `scan_steps_mean/p95`(§8.3)로 모니터링한다.

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

Static 검증은 **두 가지 mixed 워크로드**로 진행한다. 둘 다 victim은 Chat(sharegpt)이고, antagonist만 다르다(§1 표).

| 실험 | victim | antagonist | antagonist 성격 | Case 1 runner |
|---|---|---|---|---|
| §8.4.1 Chat + Longctx | chat | longctx (hotpotqa) | **cold** antagonist (low-reuse, prefill-heavy) | `case1_validation/run_mixed.py` |
| §8.4.2 Chat + Agent | chat | agent (traj) | **warm** antagonist (delayed self-reuse) | `agent_validation/run_mixed_agent.py` |

두 실험 모두 Case 1 runner를 그대로 쓰되, vLLM 서버를 `QUOTA_SERVE_MODE=static`으로 띄우고 QuotaServe env(§5.2)만 추가한다.

#### 8.4.1 Chat + Longctx

```bash
# server
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

검증 통과 기준:

- `chat ← longctx` useful eviction 건수 감소 (예: ≥20% 감소)
- `chat.hit_rate_mean` 증가
- `longctx.slo_attainment` 큰 폭 손상 없음 (예: ≤5%p 감소)
- `quota_protected_but_evicted` 비율이 전체 eviction의 일정 임계 이하 (예: 10%)

#### 8.4.2 Chat + Agent

Agent(traj)는 longctx와 달리 **자기 자신을 나중에 다시 쓰는(delayed self-reuse) warm antagonist**다. 즉 `agent ← agent` eviction 중 일부는 실제로 useful하다. 따라서 Chat을 floor로 보호하되 **agent의 self-reuse까지 과도하게 깎지 않는 것**이 검증 포인트다(§5.1에서 agent는 `floor_ratio 0.15`, `cap_ratio 0.80`으로 longctx보다 큰 몫을 받는 이유).

> [!NOTE]
> 서버 파라미터는 `agent_validation/RUN_MIXED.md`의 Chat+Agent 설정을 따른다. Agent trajectory는 context가 길어 `--max-model-len`을 **12288**로 키운다(§2 표의 8192은 Chat+Longctx 기준). KV-cache 압박이 크면 `--max-num-seqs`를 `16`으로 낮춘다.

```bash
# server (agent_validation/RUN_MIXED.md + QuotaServe env)
VLLM_SERVER_DEV_MODE=1 \
VLLM_EVICTION_LOG=/path/eviction_static_chat_agent.jsonl \
QUOTA_SERVE_MODE=static \
QUOTA_SERVE_CONFIG=/path/quota_serve.yaml \
QUOTA_SERVE_LOG=/path/quota_static_chat_agent.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 12288 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

```bash
# client — agent_validation/run_mixed_agent.py 그대로, output 경로만 case2로
python hypothesis_validation/agent_validation/run_mixed_agent.py \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --phase chat_agent_exponential \
  --chat-qps 5 \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode exponential \
  --agent-tool-gap-mean 2 \
  --agent-tool-gap-max 20 \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 10000 \
  --output hypothesis_validation/case2_validation/raw_results/c2_static_chat_agent_apc_on.jsonl
```

workload tag와 eviction 방향(runner가 OpenAI `user` 필드로 보냄: `chat`, `agent`):

```text
chat  ← agent     # 보호 대상: antagonist가 victim을 밀어냄 (줄여야 함)
agent ← agent     # agent self-churn: 일부는 useful(delayed self-reuse)
agent ← chat      # 역방향
chat  ← chat      # victim self
```

검증 통과 기준:

- `chat ← agent` useful eviction 건수 감소 (예: ≥20% 감소)
- `chat.hit_rate_mean` 증가
- `agent.slo_attainment` 큰 폭 손상 없음 (예: ≤5%p 감소)
- **agent self-reuse 보존**: `agent ← agent` useful eviction이 baseline 대비 크게 악화되지 않음 (cap이 agent의 delayed self-reuse까지 과하게 깎으면 실패 신호)
- `quota_protected_but_evicted` 비율이 전체 eviction의 일정 임계 이하 (예: 10%)

#### 8.4.3 공통 비교 대상

각 실험(§8.4.1, §8.4.2)마다 동일 trace로 세 mode를 비교한다.

| run | mode | 기대 |
|---|---|---|
| baseline | `off` | Case 1 결과 재현 |
| dry-run | `dry_run` | baseline과 동일 결과 + shadow 로그 채워짐 (shadow는 PR 4에서 활성, §4.2) |
| static | `static` | `chat ← {antagonist}` useful eviction 감소, Chat hit rate 회복 |

여기서 결과가 안 나오면 dynamic으로 가도 의미 없다. floor/cap 값을 먼저 sweep한다. 특히 Chat+Agent는 agent `cap_ratio`를 sweep하며 "Chat 보호 ↔ agent self-reuse 보존"의 trade-off 지점을 찾는다.

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
│   ├── c2_lru_chat_agent_apc_on.jsonl
│   ├── c2_static_chat_agent_apc_on.jsonl
│   ├── c2_dyn_chat_agent_apc_on.jsonl
│   └── ...
├── eviction_logs/
│   ├── eviction_static_chat_longctx.jsonl
│   ├── quota_tick_static_chat_longctx.jsonl
│   ├── eviction_static_chat_agent.jsonl
│   ├── quota_tick_static_chat_agent.jsonl
│   └── ...
├── analysis_results/
│   └── ...
├── run_mixed_c2.py         # Case 1 run_mixed.py에서 QuotaServe 모드 인자 추가 (Chat+Longctx)
├── run_mixed_agent_c2.py   # Case 1 run_mixed_agent.py에서 QuotaServe 모드 인자 추가 (Chat+Agent)
├── RUN_C2.md               # Case 2 실행 절차
└── README.md
```

---

## 12. 다음 작업 큐 (당장 할 것)

1. ~~PR 0~~ **(완료)**: vLLM block manager의 lifecycle 전이 5곳에 hook을 박았다 — `on_block_allocated`/`on_block_evicted`/`on_block_accessed` 시그니처 확장 + `on_block_cached`/`on_block_freed` 신설(`kv_cache_metrics.py` 정의, `block_pool.py` 배선, `kv_cache_manager.py` 배선 주석). victim 선택 hook과 dry-run shadow policy는 PR 4로 이동.
2. PR 1: `quota_serve.yaml` 스키마 확정 + `mode=off` parity test 통과.
3. Case 1 raw_results에서 Chat-only steady-state occupancy를 측정해 `chat.floor_ratio` 초기값 결정.
4. PR 2~3 동시 진행 가능 (workload tag → block owner → counter).
5. PR 4 static 실험 — Chat+Longctx(§8.4.1)와 Chat+Agent(§8.4.2) 둘 다. 결과를 보고 dynamic 단계 thresholds 확정. Chat+Agent는 agent `cap_ratio` sweep으로 "Chat 보호 ↔ agent self-reuse 보존" trade-off 지점을 찾는다.

---

<div align="center">
<sub>QuotaServe · Implementation Plan · JJ Distributed LLM Inference</sub>
</div>
