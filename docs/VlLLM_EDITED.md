<div align="center">

# vLLM Fork — QuotaServe 수정 내역

**PR 0: lifecycle hook map (5 hooks) · PR 1: config schema 패키지**

_`QUOTASERVE_IMPLEMENTATION_PLAN.md` §4·§5 기준_

</div>

---

## 0. 요약

vLLM fork에 들어간 QuotaServe 변경을 PR 단위로 정리한다.

- **PR 0** (§1~§4, 6): block lifecycle 전이마다 hook을 박는다. 실제 victim 선택(LRU)은 안 바꾼다. 이후 PR이 owner attribution·counter·정책을 이 hook 위에 얹는다.
- **PR 1** (§7): QuotaServe config schema + loader 패키지(`vllm/quota_serve/`). 아직 엔진에 wiring하지 않아(scheduler 미수정) 런타임 동작에는 영향이 없다.

| 파일 | PR | 역할 | 비고 |
|---|---|---|---|
| `vllm/v1/core/kv_cache_metrics.py` | 0 | hook **정의** 지점 (collector 인터페이스) | +119 (수정) |
| `vllm/v1/core/block_pool.py` | 0 | hook **배선** 지점 (전이마다 fire) | +110 (수정) |
| `vllm/v1/core/kv_cache_manager.py` | 0 | collector **전달 경로** (배선 주석만) | +21 (수정) |
| `vllm/quota_serve/config.py` | 1 | config schema + loader | 신규 |
| `vllm/quota_serve/quota_serve.yaml` | 1 | 기본 config | 신규 |
| `vllm/quota_serve/__init__.py` | 1 | 공개 심볼 re-export | 신규 |

> [!IMPORTANT]
> **Parity 보장**: (PR 0) 추가한 모든 인자는 `Optional`(기본 `None`)이고, 신규 hook은 base collector에서 no-op이다. 따라서 `metrics_collector`가 없거나 base 구현이면 동작이 baseline LRU와 **완전히 동일**하다(plan §5.3 mode=off parity). 기존 테스트 `tests/v1/core/test_kv_cache_metrics.py`는 collector를 `block` 단일 인자로 호출하므로 하위 호환된다. (PR 1) config 패키지는 아직 import/wiring되지 않으므로 런타임에 영향이 없다.

---

## 1. Hook map (5 hooks)

| # | Hook | 종류 | fire 위치 (`block_pool.py`) | 용도 (후속 PR) |
|---|---|---|---|---|
| 1 | `on_block_allocated(block, request=None)` | 시그니처 확장 | `get_new_blocks()` 할당 루프 (ref_cnt 0→1) | owner workload 부여 (PR 3) |
| 2 | `on_block_evicted(block, trigger_request=None)` | 시그니처 확장 | `_maybe_evict_cached_block()` 진입부 | trigger workload attribution (block 단위) |
| 3 | `on_block_cached(block, request=None)` | **신규** | `cache_full_blocks()` `insert` 직후 | is_cached F→T 전이 (counter, PR 3) |
| 4 | `on_block_accessed(block, request=None)` | 시그니처 확장 | `touch()` cache hit | hit-side workload (owner 불변) |
| 5 | `on_block_freed(block, prev_ref, new_ref)` | **신규** | `free_blocks()` `ref_cnt -= 1` 직후 | ref→0 transition (counter, PR 3) |

> [!NOTE]
> victim 선택을 가로채는 정책 hook(`select_victim_candidate`)과 dry-run shadow policy는 **PR 0에서 제외하고 PR 4로 옮겼다**. 이유: ① PR 0 시점엔 `block.workload_id`(PR 2)·floor/cap(PR 4)이 없어 그 hook이 할 일이 없다. ② "실제로 무엇이 evict됐는지"는 Hook #2가 block 단위로 이미 기록한다. ③ victim 선택을 실제로 바꾸는 동작 변경은 PR 4에서 일어난다. (plan §4 NOTE 참조)

---

## 2. `vllm/v1/core/kv_cache_metrics.py` — hook 정의

collector(`KVCacheMetricsCollector`)에 hook을 정의했다. base 구현은 Case 1과 동일하게 **observation 전용**(샘플링 residency 계측)이며, 정책 개입은 이후 PR이 이 클래스를 상속한 collector로 담당한다.

### 2.1 import (TYPE_CHECKING)

hook 시그니처가 `Request`를 참조하므로 타입을 추가했다. hot-path 모듈이라 런타임 순환 import를 피하려 `TYPE_CHECKING` 블록 안에만 둔다.

```python
if TYPE_CHECKING:
    from vllm.v1.core.kv_cache_utils import KVCacheBlock
    from vllm.v1.request import Request   # ← 추가
```

### 2.2 시그니처 확장 (기존 hook 3개)

| 함수 | 변경 전 | 변경 후 |
|---|---|---|
| `on_block_allocated` (L103) | `(self, block)` | `(self, block, request=None)` |
| `on_block_accessed` (L119) | `(self, block)` | `(self, block, request=None)` |
| `on_block_evicted` (L135) | `(self, block)` | `(self, block, trigger_request=None)` |

base 구현은 추가 인자를 **무시**한다(샘플링 로직 그대로). 인자는 PR 3의 QuotaServe collector가 owner/trigger attribution에 사용한다.

### 2.3 신규 hook 2개 (base no-op)

```python
def on_block_cached(self, block, request=None) -> None:
    """Block이 prefix cache에 등록되는 순간(is_cached F→T). base는 no-op."""
    return None

def on_block_freed(self, block, prev_ref_cnt, new_ref_cnt) -> None:
    """ref_cnt 감소(특히 →0) 순간. base는 no-op."""
    return None
```

- `on_block_cached` (L168): PR 3 evictable_cached counter가 is_cached 전이에서 재평가되어야 해서 필요.
- `on_block_freed` (L183): ref_cnt→0 transition에서 block이 evictable 후보가 되므로 prev/new ref_cnt를 함께 받는다.

### 2.4 docstring

`KVCacheMetricsCollector` docstring에 PR 0 hook map(시그니처 확장 3 + 신규 2)과 victim hook을 PR 4로 미룬 이유를 정리했다.

---

## 3. `vllm/v1/core/block_pool.py` — hook 배선

`BlockPool`의 lifecycle 전이마다 위 5개 hook을 fire하도록 배선했다. 모든 호출은 `if self.metrics_collector:`로 가드된다.

### 3.1 `__init__` — hook map 요약 주석 (L181~)

collector 보관 지점에 5개 hook의 위치 요약을 주석으로 남겼다.

```text
Hook #1 on_block_allocated -> get_new_blocks()        (ref_cnt 0→1)
Hook #2 on_block_evicted   -> _maybe_evict_cached_block()
Hook #3 on_block_cached    -> cache_full_blocks()      (insert 직후)
Hook #4 on_block_accessed  -> touch()                  (cache hit)
Hook #5 on_block_freed     -> free_blocks()            (ref_cnt 감소)
```

### 3.2 `get_new_blocks()` (L343) — Hook #1, #2

- 시그니처: `+ request: Request | None = None`
- `popleft_n` 직전: victim 가로채기는 PR 4에서 추가한다는 NOTE만 둠 (hook 미설치, LRU 유지)
- 할당 루프에서:
  - `_maybe_evict_cached_block(block, trigger_request=request)` → **Hook #2** 경유
  - ref_cnt 0→1 후 `on_block_allocated(block, request)` → **Hook #1** (L386)

### 3.3 `_maybe_evict_cached_block()` (L399) — Hook #2

- 시그니처: `+ trigger_request: Request | None = None`
- 진입부에서 `on_block_evicted(block, trigger_request)` (L418)
- 외부 evict 경로(`evict_blocks` → connector)는 trigger 없이 호출 → `None`

### 3.4 `cache_full_blocks()` (기존) — Hook #3

- `insert` 직후 `on_block_cached(blk, request)` (L288)
- 이 경로는 호출자가 `request`를 이미 가지므로 **실제 request가 전달**되는 유일한 hook이다.

### 3.5 `touch()` (L450) — Hook #4

- 시그니처: `+ request: Request | None = None`
- ref_cnt += 1 후 `on_block_accessed(block, request)` (L478)
- owner는 절대 바꾸지 않는다(hit-side workload만 기록 예정).

### 3.6 `free_blocks()` (L482) — Hook #5

- 시그니처: `+ request: Request | None = None`
- ref_cnt 감소 전/후 값을 캡처해 `on_block_freed(block, prev_ref_cnt, block.ref_cnt)` (L501)

```python
for block in blocks_list:
    prev_ref_cnt = block.ref_cnt
    block.ref_cnt -= 1
    if self.metrics_collector:
        self.metrics_collector.on_block_freed(block, prev_ref_cnt, block.ref_cnt)
```

> [!NOTE]
> `get_new_blocks`/`touch`/`free_blocks`/`_maybe_evict_cached_block`의 호출자(`single_type_kv_cache_manager.py`)는 현재 `request_id`만 가지고 호출한다. 전체 호출 사슬을 다시 쓰지 않으려고 새 인자를 **`Optional=None`**으로 뒀다. 따라서 PR 0에서는 이 hook들에 `request=None`이 들어오고(=baseline 동작), 실제 request threading은 PR 2/3에서 완성한다. 예외는 `cache_full_blocks`(Hook #3)로, 호출자가 이미 `request`를 가진다.

---

## 4. `vllm/v1/core/kv_cache_manager.py` — collector 전달 경로

이 파일에서는 hook을 직접 fire하지 않는다. `metrics_collector`를 받아 coordinator → BlockPool로 넘기는 **순수 배선 경로**임을 주석으로 명시했다.

| 위치 | 내용 |
|---|---|
| import (L12~) | 배선 경로 역할 + 실제 fire는 `block_pool.py`임을 설명 |
| `__init__` `metrics_collector` param (L124) | hook 정의 지점, `None`이면 baseline 동일 |
| coordinator 생성부 | collector → BlockPool 전달 (이 한 줄이 hook map을 BlockPool에 연결) |
| `allocate_slots()` 할당부 (L417~) | **PR 2/3 request threading 지점** 마커 — 현재 `request_id`만 아래로 전달되어 Hook #1/#2의 request가 None으로 들어옴을 명시 |

---

## 5. 추가/변경한 함수 정리

| 함수 | 파일 | 변경 |
|---|---|---|
| `on_block_allocated` | metrics | 시그니처 확장 (`+request`) |
| `on_block_accessed` | metrics | 시그니처 확장 (`+request`) |
| `on_block_evicted` | metrics | 시그니처 확장 (`+trigger_request`) |
| `on_block_cached` | metrics | **신규** hook (no-op) |
| `on_block_freed` | metrics | **신규** hook (no-op) |
| `get_new_blocks` | block_pool | 시그니처 확장 (`+request`) + Hook #1/#2 배선 |
| `_maybe_evict_cached_block` | block_pool | 시그니처 확장 (`+trigger_request`) + Hook #2 |
| `touch` | block_pool | 시그니처 확장 (`+request`) + Hook #4 |
| `free_blocks` | block_pool | 시그니처 확장 (`+request`) + Hook #5 |
| `cache_full_blocks` | block_pool | Hook #3 배선 (시그니처 불변) |

> [!IMPORTANT]
> **새로 만든 `def`는 hook 2개(`on_block_cached`, `on_block_freed`)뿐**이다. plan §8.2의 `scan_lru`/`_over_cap`/`_above_floor`/`_abs_floor`/`_abs_cap`이나 §7.3의 `maybe_update_evictable_count` 같은 헬퍼는 **PR 3/4 소관이라 추가하지 않았다.** PR 0 범위(hook 배선)에 맞춰 최소한만 손댔다.

---

## 6. PR 0 검증 상태

- 3개 파일 모두 `python -m py_compile` 통과.
- 기존 `tests/v1/core/test_kv_cache_metrics.py`는 collector를 `block` 단일 인자로 호출 → optional 기본값으로 하위 호환.
- 정량 parity(hit rate / TTFT / SLO / eviction 총 건수)는 plan §5.3 `mode=off` parity test에서 확인한다(아래 §7.4 참고).

---

## 7. PR 1 — config schema 패키지 (`vllm/quota_serve/`)

plan §5. QuotaServe config의 **schema + loader**를 vLLM core와 분리된 패키지로 둔다. **순수 데이터 + 로딩/검증**만 담당하고, 정책/collector/hook 호출은 없다. core 코드(scheduler 등)는 아직 건드리지 않았다 — 엔진에 연결하는 wiring은 PR 3으로 미뤘다(주입할 collector가 PR 3에서 생기므로).

### 7.1 `config.py` — schema + loader

| 심볼 | 내용 |
|---|---|
| `QuotaServeMode` | `Literal["off","dry_run","static","dynamic_floor","dynamic_full"]` |
| `WorkloadQuota` | `floor_ratio`, `cap_ratio` (frozen). `__post_init__`에서 `0 ≤ floor ≤ cap ≤ 1` 검증 |
| `QuotaServeConfig` | enabled / mode / scan_limit / tick_sec / shadow_ttl_sec / workloads / log_path. **기본값이 비활성(off)** |
| `load_quota_serve_config(path)` | YAML 로드 + env override + 검증 |

- `QuotaServeConfig.is_active` = `enabled and mode != "off"` → **PR 1 parity 게이트** (off면 caller가 baseline 경로 유지).
- `quota_for(w)` — 미설정 워크로드는 무제약 기본값(`floor 0, cap 1.0`).
- `validate()` — mode/scan_limit/tick_sec/shadow_ttl_sec 검사.

로딩 우선순위: `path` 인자 → env `QUOTA_SERVE_CONFIG` → (없으면) 비활성 기본값. 이후 env override: `QUOTA_SERVE_MODE`(off 아니면 enabled 자동 on), `QUOTA_SERVE_LOG`.

### 7.2 환경 변수 (§5.2)

| Env | 의미 |
|---|---|
| `QUOTA_SERVE_MODE` | config의 mode를 override |
| `QUOTA_SERVE_CONFIG` | yaml 경로 |
| `QUOTA_SERVE_LOG` | eviction/tick JSONL 로그 경로 |

### 7.3 `quota_serve.yaml` — 기본 config

§5.1 값(chat/rag/longctx/agent의 floor_ratio/cap_ratio + scan_limit/tick_sec/shadow_ttl_sec). `floor_ratio`는 **임의값 금지 → Case 1 occupancy 측정값으로 교체**해야 한다는 WARNING 주석 포함. `mode: "off"`가 기본이라 파일이 있어도 baseline.

> [!NOTE]
> 이 config에는 dynamic 전용 파라미터(피해/낭비 신호의 upper/lower = `D_hi`/`D_lo`/`W_hi`/`W_lo`)가 **없다**. static은 floor/cap 고정 + tick·신호 off라 upper/lower가 불필요하다. 해당 파라미터는 PR 6/7에서 추가한다.

### 7.4 PR 1 검증 상태

- `config.py` / `__init__.py` `py_compile` 통과.
- loader 스모크 테스트 통과: ① yaml/env 없음 → `off`/`is_active=False`(baseline) ② yaml 로드 → `agent.cap=0.80`, unknown 무제약 ③ `QUOTA_SERVE_MODE=static` → `is_active=True`, log_path 반영 ④ `floor>cap` / 잘못된 mode 거부.
- **scheduler 미수정** — wiring(주입점 연결)과 §5.3 `mode=off` ↔ active parity test는 collector가 생기는 PR 3에서 수행한다.

> [!NOTE]
> PR 1에서 임시로 만들었던 `factory.py`(collector 결정 로직)는 **wiring 소관이라 제거**했다. wiring은 PR 3에서 collector와 함께 넣는다.

---

<div align="center">
<sub>QuotaServe · vLLM Fork Edits · PR 0 + PR 1</sub>
</div>
