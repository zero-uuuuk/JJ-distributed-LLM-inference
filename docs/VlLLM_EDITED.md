<div align="center">

# vLLM Fork — QuotaServe 수정 내역

**PR 0: hook map · hook 보완 · PR 1: config · PR 2/3: tag+owner/occupancy · PR 4: victim selection**

_`QUOTASERVE_STATIC_PLAN.md` §4·§5 기준_

</div>

---

## 0. 요약

vLLM fork에 들어간 QuotaServe 변경을 PR 단위로 정리한다.

- **PR 0** (§1~§4, 6): block lifecycle 전이마다 hook을 박는다. 실제 victim 선택(LRU)은 안 바꾼다. 이후 PR이 owner attribution·counter·정책을 이 hook 위에 얹는다.
- **Hook 보완**: `Request`를 `KVCacheManager → coordinator → single_type_manager → BlockPool`까지 전달하고, `KVCacheBlock(slots=True)`에 QuotaServe metadata 필드를 추가했다. 따라서 allocation/eviction/access hook이 실제 workload attribution에 쓸 request를 받을 수 있다.
- **PR 1** (§7): QuotaServe config schema + loader 패키지(`vllm/quota_serve/`).
- **PR 2/3** (§8): workload tag 추출(`X-Request-Id` 경유) + block owner attribution + workload별 occupancy counter. PR 3에서 collector를 scheduler에 wiring한다.
- **PR 4** (§9): `occupancy_w > quota_w` 2-tier victim selection. **이 단계부터 mode=static/dynamic에서 eviction 순서가 baseline과 달라진다**(off는 그대로).

| 파일 | PR | 역할 | 비고 |
|---|---|---|---|
| `vllm/v1/core/kv_cache_metrics.py` | 0 | hook **정의** 지점 (collector 인터페이스) | +119 (수정) |
| `vllm/v1/core/block_pool.py` | 0/4 | hook 배선 + quota-aware victim selection path | 수정 |
| `vllm/v1/core/kv_cache_utils.py` | 0/3 prep | `KVCacheBlock` QuotaServe metadata 필드 | `workload_tag`, counted flag |
| `vllm/v1/core/kv_cache_manager.py` | 0 | collector 전달 + request threading 시작점 | `Request`를 coordinator로 전달 |
| `vllm/v1/core/kv_cache_coordinator.py` | 0 | request threading 중간 경로 | manager로 `request` 전달 |
| `vllm/v1/core/single_type_kv_cache_manager.py` | 0 | request threading 중간 경로 | BlockPool hook에 `request` 전달 |
| `vllm/quota_serve/config.py` | 1 | config schema + loader | 신규 |
| `quotaserve/static/configs/quota_serve.yaml` | 1 | sweep용 기본 config | 신규 |
| `vllm/quota_serve/__init__.py` | 1 | 공개 심볼 re-export | 신규 |
| `vllm/quota_serve/workload.py` | 2 | workload tag 추출 (`infer_workload`) | 신규 |
| `vllm/quota_serve/collector.py` | 3/4 | owner attribution + occupancy counter + victim selection | 신규 |
| `vllm/v1/core/sched/scheduler.py` | 3 | `QuotaServeCollector` wiring | 수정 |

> [!IMPORTANT]
> **Parity 보장**: (PR 0) 추가한 모든 인자는 `Optional`(기본 `None`)이고, 신규 hook은 base collector에서 no-op이다. `KVCacheBlock`에 추가한 metadata 필드는 base vLLM path에서 읽히지 않는다. 따라서 `metrics_collector`가 없거나 base 구현이면 동작이 baseline LRU와 **완전히 동일**하다(plan §5.3 mode=off parity). 기존 테스트 `tests/v1/core/test_kv_cache_metrics.py`는 collector를 `block` 단일 인자로 호출하므로 하위 호환된다. (PR 1) config 패키지 자체는 순수 데이터다. (PR 2/3) collector는 QuotaServe active일 때만 생성된다. (PR 4) victim selection은 mode=static/dynamic에서만 켜지므로(`collector.victim_selection_active`), **mode=off이면 eviction 순서가 baseline LRU와 완전히 동일하다.**

---

## 1. Hook map (수정 hook 5개 + 관련 지점)

이 문서에서는 두 종류를 구분한다.

- **수정 hook**: QuotaServe를 위해 실제로 시그니처를 바꾸거나 새로 fire하도록 추가한 지점.
- **관련 기존 지점**: 코드는 원래 있던 vLLM 구조지만, owner/occupancy/victim selection의 의미를 해석할 때 반드시 같이 봐야 하는 지점.

### 1.1 실제 수정 hook

| # | Hook | 종류 | fire 위치 (`block_pool.py`) | 용도 (후속 PR) |
|---|---|---|---|---|
| 1 | `on_block_allocated(block, request=None)` | 시그니처 확장 | `get_new_blocks()` 할당 루프 (ref_cnt 0→1) | owner workload 부여 (PR 3) |
| 2 | `on_block_evicted(block, trigger_request=None, *, evicted_block_hash=None, victim_workload=None)` | 시그니처 확장 | `_maybe_evict_cached_block()` 실제 cache-map pop/reset 이후 | trigger/victim workload attribution, block hash logging |
| 3 | `on_block_cached(block, request=None)` | **신규** | `cache_full_blocks()` `insert` 직후 | is_cached F→T 전이 (counter, PR 3) |
| 4 | `on_block_accessed(block, request=None)` | 시그니처 확장 | `touch()` cache hit | hit-side workload (owner 불변) |
| 5 | `on_block_freed(block, prev_ref, new_ref)` | **신규** | `free_blocks()` `ref_cnt -= 1` 직후 | ref→0 transition (counter, PR 3) |

### 1.2 관련 기존 지점 (수정 hook은 아니지만 같이 추적)

| 지점 | 파일 | 왜 봐야 하는가 | 수정 여부 |
|---|---|---|---|
| `KVCacheBlock.block_hash` / `reset_hash()` | `kv_cache_utils.py` | `block_hash is not None`이 cached prefix 여부 판단 기준이다. eviction log용 hash는 reset 전에 캡처해야 한다. | 기존 구조 + metadata 필드 추가 |
| `KVCacheBlock` metadata | `kv_cache_utils.py` | `slots=True`라 동적 attribute를 못 붙인다. owner/count flag는 dataclass field로 있어야 한다. | 수정 |
| `FreeKVCacheBlockQueue.popleft_n()` | `kv_cache_utils.py` | baseline LRU가 여러 block을 한 번에 뽑는 경로다. static에서는 이 경로를 그대로 쓰지 않고 block별 선택으로 우회한다. | 기존 구조 |
| `FreeKVCacheBlockQueue.remove()` | `kv_cache_utils.py` | quota-aware selector가 고른 block을 free queue에서 제거할 때 필요하다. | 기존 구조 |
| `BlockHashToBlockMap.insert()` | `block_pool.py` | block이 prefix cache에 등록되는 순간이다. Hook #3은 이 직후에 fire한다. | 기존 구조 |
| `BlockHashToBlockMap.pop()` | `block_pool.py` | cached block eviction이 실제 확정되는 순간이다. Hook #2는 pop 성공 후 fire한다. | 기존 구조 |
| `get_cached_block()` | `block_pool.py` | prefix cache hit 후보를 찾는 경로다. 실제 ref_cnt 증가는 `touch()`에서 일어난다. | 기존 구조 |
| `evict_blocks()` | `block_pool.py` | 외부에서 block id로 prefix cache를 제거하는 경로다. 명시적 request가 없어 `trigger_request=None`으로 처리한다. | 기존 구조 |
| `reset_prefix_cache()` | `block_pool.py` | 모든 cache hash가 사라지는 경로다. owner/count flag와 collector occupancy도 같이 초기화해야 한다. | 일부 수정 |
| `get_new_blocks(num_blocks)` | `block_pool.py` | 새 block이 여러 개 필요할 수 있다. static path는 `select_victim → remove → evict → allocate`를 block마다 반복해야 한다. | 수정 |

> [!NOTE]
> victim 선택을 가로채는 정책 경로는 **PR 0에서 제외하고 PR 4로 옮겼다**. 이유: ① PR 0 시점엔 `block.workload_tag`(PR 2)·quota 정책(PR 4)이 없어 그 hook이 할 일이 없다. ② "실제로 무엇이 evict됐는지"는 Hook #2가 block 단위로 이미 기록한다. ③ victim 선택을 실제로 바꾸는 동작 변경은 PR 4에서 일어난다. (plan §4 NOTE 참조)

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
| `on_block_evicted` (L135) | `(self, block)` | `(self, block, trigger_request=None, *, evicted_block_hash=None, victim_workload=None)` |

base 구현은 추가 인자를 **무시**한다(샘플링 로직 그대로). 인자는 PR 3의 QuotaServe collector가 owner/trigger/victim attribution과 eviction hash logging에 사용한다.

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
- `popleft_n` 직전: victim 가로채기는 PR 4에서 추가한다는 NOTE만 둠 (hook 미설치, LRU 유지). PR 4에서는 `num_blocks > 1`일 때도 한 번에 `popleft_n`하지 않고 block을 하나씩 고르며 occupancy 변화를 반영해야 한다.
- 할당 루프에서:
  - `_maybe_evict_cached_block(block, trigger_request=request)` → **Hook #2** 경유
  - ref_cnt 0→1 후 `on_block_allocated(block, request)` → **Hook #1** (L386)

### 3.3 `_maybe_evict_cached_block()` (L399) — Hook #2

- 시그니처: `+ trigger_request: Request | None = None`
- 실제 cache-map pop이 성공한 뒤 `block_hash`와 `victim_workload`를 보존하고, `block.reset_hash()` 이후 `on_block_evicted(block, trigger_request, evicted_block_hash=block_hash, victim_workload=victim_workload)`를 호출한다.
- QuotaServe collector는 hook 시점의 `block.block_hash is None` 상태로 occupancy membership을 동기화하고, 로그에는 별도 인자로 받은 pre-reset hash/owner를 사용한다.
- 외부 evict 경로(`evict_blocks` → connector)는 trigger 없이 호출 → `None`

### 3.4 `cache_full_blocks()` (기존) — Hook #3

- `insert` 직후 `on_block_cached(blk, request)` (L288)
- 이 경로는 호출자가 `request`를 이미 가지므로 cache 등록 시점의 owner attribution에 바로 사용할 수 있다.

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

### 3.7 `reset_prefix_cache()` — metadata reset

전체 prefix cache reset 경로에서는 모든 block의 hash를 지우는 동시에 QuotaServe metadata도 초기화한다.

```python
for block in self.blocks:
    block.reset_hash()
    block.workload_tag = None
    block.is_counted_as_evictable_cached = False
```

collector table은 기존처럼 `metrics_collector.reset()`에서 비운다. 이렇게 해야 reset 이후 낡은 owner/count flag가 occupancy counter에 남지 않는다.

> [!NOTE]
> `get_new_blocks`/`touch`/`_maybe_evict_cached_block`에는 `KVCacheManager.allocate_slots()`에서 받은 `Request`가 coordinator와 single-type manager를 거쳐 전달된다. 외부 connector eviction처럼 명시적 request가 없는 경로만 `None`으로 남는다. `free_blocks()`의 request 인자는 optional로 유지하며, occupancy counter는 block owner 기준으로 처리한다.

---

## 4. `vllm/v1/core/kv_cache_manager.py` — collector 전달 경로

이 파일에서는 hook을 직접 fire하지 않는다. `metrics_collector`를 받아 coordinator → BlockPool로 넘기는 **순수 배선 경로**임을 주석으로 명시했다.

| 위치 | 내용 |
|---|---|
| import (L12~) | 배선 경로 역할 + 실제 fire는 `block_pool.py`임을 설명 |
| `__init__` `metrics_collector` param (L124) | hook 정의 지점, `None`이면 baseline 동일 |
| coordinator 생성부 | collector → BlockPool 전달 (이 한 줄이 hook map을 BlockPool에 연결) |
| `allocate_slots()` 할당부 (L417~) | `Request`를 `allocate_new_computed_blocks()` / `allocate_new_blocks()`로 전달해 Hook #1/#2/#4가 실제 workload attribution에 사용할 수 있게 함 |

### 4.1 request threading 경로

```text
KVCacheManager.allocate_slots(request)
  → KVCacheCoordinator.allocate_new_computed_blocks(..., request=request)
    → SingleTypeKVCacheManager.allocate_new_computed_blocks(..., request=request)
      → BlockPool.touch(..., request=request)

KVCacheManager.allocate_slots(request)
  → KVCacheCoordinator.allocate_new_blocks(..., request=request)
    → SingleTypeKVCacheManager.allocate_new_blocks(..., request=request)
      → BlockPool.get_new_blocks(..., request=request)
        → _maybe_evict_cached_block(..., trigger_request=request)
        → on_block_allocated(block, request)
```

외부 connector eviction처럼 명시적 request가 없는 경로는 `trigger_request=None`으로 남긴다.

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
| `allocate_new_blocks` | coordinator / single_type_manager | `+request` threading |
| `allocate_new_computed_blocks` | coordinator / single_type_manager | `+request` threading |
| `KVCacheBlock` | kv_cache_utils | `workload_tag`, `is_counted_as_evictable_cached` 필드 추가 |

> [!IMPORTANT]
> PR 0에서 새로 만든 no-op hook은 `on_block_cached`, `on_block_freed` 2개다. PR 3/4 현재 구현에서는 정책 로직이 `QuotaServeCollector._recount()`와 `QuotaServeCollector.select_victim()`에 들어가며, `KVCacheBlock`은 `slots=True`라 owner/counting metadata 필드를 dataclass field로 추가했다.

---

## 6. PR 0 검증 상태

- hook 관련 core 파일 모두 `python -m py_compile` 통과.
- 기존 `tests/v1/core/test_kv_cache_metrics.py`는 collector를 `block` 단일 인자로 호출 → optional 기본값으로 하위 호환.
- 정량 parity(hit rate / TTFT / SLO / eviction 총 건수)는 plan §5.3 `mode=off` parity test에서 확인한다(아래 §7.4 참고).

---

## 7. PR 1 — config schema 패키지 (`vllm/quota_serve/`)

plan §5. PR 1의 대상은 `vllm/quota_serve/config.py`의 **schema + loader**다. 이 파일은 **순수 데이터 + 로딩/검증**만 담당한다. 현재 branch의 `vllm/quota_serve/` 패키지에는 PR 2~4의 `workload.py`/`collector.py`도 함께 들어간 상태지만, PR 1의 parity gate는 `mode=off`/비활성일 때 QuotaServeCollector를 만들지 않는 분기로 유지된다.

### 7.1 `config.py` — schema + loader

| 심볼 | 내용 |
|---|---|
| `QuotaServeMode` | `Literal["off","static","dynamic"]` |
| `WorkloadQuota` | 단일 `quota_ratio` (frozen). `__post_init__`에서 `0 ≤ quota_ratio ≤ 1` 검증 |
| `QuotaServeConfig` | enabled / mode / tick_sec / shadow_ttl_sec / workloads / log_path. **기본값이 비활성(off)** |
| `load_quota_serve_config(path)` | YAML 로드 + env override + 검증 |

- `QuotaServeConfig.is_active` = `enabled and mode != "off"` → **PR 1 parity 게이트** (off면 caller가 baseline 경로 유지).
- `quota_for(w)` — 미설정 워크로드는 정책 제외 기본값(`quota_ratio 1.0` → 사실상 over-quota 안 됨).
- `validate()` — mode/tick_sec/shadow_ttl_sec 검사.

로딩 우선순위: `path` 인자 → env `QUOTA_SERVE_CONFIG` → (없으면) 비활성 기본값. 이후 env override: `QUOTA_SERVE_MODE`(off 아니면 enabled 자동 on), `QUOTA_SERVE_LOG`.

### 7.2 환경 변수 (§5.2)

| Env | 의미 |
|---|---|
| `QUOTA_SERVE_MODE` | config의 mode를 override |
| `QUOTA_SERVE_CONFIG` | yaml 경로 |
| `QUOTA_SERVE_LOG` | quota/eviction JSONL 로그 경로 |

### 7.3 `quotaserve/static/configs/quota_serve.yaml` — 기본 config

§5.1 값(chat/rag/longctx/agent의 단일 `quota_ratio` + tick_sec/shadow_ttl_sec). `quota_ratio`는 **임의값 금지 → Case 1 occupancy 측정값 참고**라는 NOTE 주석 포함. `mode: "off"`가 기본이라 파일이 있어도 baseline.

> [!NOTE]
> 이 config에는 dynamic 전용 파라미터(`ratio_low`/`ratio_high`, `step`, `window_size`, `tick`)가 **없다**. static은 quota 고정이라 이들이 불필요하다. 해당 파라미터는 PR 6(profile로 `ratio_low/high`·`floor/cap` 도출)/PR 7(dynamic controller)에서 추가한다.

### 7.4 PR 1 검증 상태

- `config.py` / `__init__.py` `py_compile` 통과.
- loader 스모크 테스트 통과: ① yaml/env 없음 → `off`/`is_active=False`(baseline) ② yaml 로드 → `agent.quota_ratio=0.25`, unknown 정책 제외(`quota_ratio 1.0`) ③ `QUOTA_SERVE_MODE=static` → `is_active=True`, log_path 반영 ④ `quota_ratio` 범위 밖 / 잘못된 mode 거부.
- 현재 branch에는 PR 3 wiring까지 들어간 상태다. PR 1 관점의 parity gate는 `mode=off`/비활성일 때 `QuotaServeCollector`를 만들지 않는 scheduler 분기로 유지된다.

> [!NOTE]
> PR 1에서 임시로 만들었던 `factory.py`(collector 결정 로직)는 **wiring 소관이라 제거**했다. wiring은 PR 3에서 collector와 함께 넣는다.

---

## 8. PR 2/3 — workload tag + owner attribution + occupancy counter

plan §6(PR 2)·§7(PR 3). PR 0의 hook 위에 실제 attribution/counter를 얹는다. **victim 선택은 바꾸지 않으므로**(PR 4 소관) 이 단계까지는 baseline LRU와 동일하게 동작한다.

### 8.1 `vllm/quota_serve/workload.py` (신규) — PR 2

`infer_workload(request_id) -> str`. 클라이언트가 workload 태그를 `X-Request-Id` 헤더로 보내면 vLLM OpenAI 레이어가 request_id로 반영한다(chat completions는 `chatcmpl-` 접두). engine 접두(`chatcmpl-`/`cmpl-`)를 벗기고 leading 토큰을 매핑한다.

- chat→chat, rag/msmarco→rag, longctx/hotpotqa→longctx, agent→agent, 그 외/빈값→unknown.
- config 미정의 workload는 quota 정책에서 제외된다(§3, `config.quota_for`의 무제약 기본값).

> [!NOTE]
> `user` 필드를 코어에 plumbing하는 대신 **request_id 경유**를 택했다. vLLM은 `X-Request-Id` 헤더를 이미 request_id로 반영하므로(`engine/serving.py`의 `_base_request_id`), 코어(msgspec `EngineCoreRequest` 등)를 건드리지 않고 태그를 전달할 수 있다. 클라이언트(실험 하니스)만 헤더를 추가하면 된다 → fork rebase 부담 최소.

### 8.2 `vllm/quota_serve/collector.py` (신규) — PR 3

`QuotaServeCollector(KVCacheMetricsCollector)`. PR 0의 base observation collector를 상속해 lifecycle hook을 override한다.

- **owner attribution**: `on_block_allocated`에서 `block.workload_tag = infer_workload(request.request_id)`(재사용 시 overwrite). request_id 단위 memoize로 allocation 루프 반복 파싱을 피한다.
- **occupancy counter**: `occupancy_w = count(ref_cnt==0 and cached)`를 flag(`is_counted_as_evictable_cached`) 기반 상태 전이로 유지. 갱신 경로: cache 등록(Hook #3)/hit(#4)/free(#5)/evict(#2). cached 판정은 `block.block_hash is not None`.
- `verify_occupancy(blocks)`: `sum(occupancy_w) == 실제 ref==0 cached block 수` 검증(§5.4/§7.5, 디버그용 O(N) 스캔).
- counter와 counted flag 갱신은 `QuotaServeCollector._quota_lock` 안에서 함께 처리한다. 현재 block pool 조작이 주로 단일 경로로 흘러도, 여러 hook에서 같은 block membership을 갱신하므로 lock 기준을 유지한다.

### 8.3 wiring — `vllm/v1/core/sched/scheduler.py`

collector 생성부에서 `load_quota_serve_config().is_active`면 `QuotaServeCollector`로, 아니면 기존 `KVCacheMetricsCollector`(observability) 또는 `None`. QuotaServeCollector는 base residency collector를 상속하므로 kv_cache_metrics 관측도 유지된다. mode=off/비활성이면 기존 경로 그대로 → parity.

### 8.4 클라이언트 (실험 하니스) — `static/run_mixed_c2.py` / `run_mixed_agent_c2.py`

요청마다 `X-Request-Id: {workload_tag}-{uuid4}` 헤더 추가(고유성 보장 + workload prefix). server env로 QuotaServe를 켜는 방식은 그대로. (이 두 파일은 vLLM fork가 아니라 quotaserve repo에 있다.)

### 8.5 검증 상태

- 전체 `py_compile` 통과 (vLLM 4파일 + 클라이언트 2파일).
- 격리 기능 테스트(실제 `collector.py`/`workload.py` 로드, heavy import chain은 stub): `infer_workload` 10케이스; collector lifecycle(alloc→cache(사용중 미집계)→free(집계)→hit(해제)→free→evict→realloc 새 owner); multi-block occupancy `{chat:2,rag:1,agent:2}` + drift 탐지.
- victim 선택 미변경 → mode=off 및 static(PR 4 이전) 모두 baseline과 동일 결과 기대.

---

## 9. PR 4 — static quota victim selection

plan §8. PR 3의 occupancy 위에서 victim 선택을 LRU → quota-aware로 바꾼다. **이 PR부터 mode=static/dynamic에서 eviction 동작이 baseline과 달라진다**(off는 그대로).

### 9.1 `collector.select_victim(free_queue, trigger_request)` — 2-tier (§8.2)

`occupancy_w > quota_w`인 workload를 우선 victim 후보로, 그 집합 안에서 LRU를 고른다.

- **0. uncached head**: LRU front가 uncached free block이면 그대로 사용(eviction 아님) → `uncached_head`.
- **1. fallback**: over-quota workload가 없으면 LRU head(=baseline). head는 cached라 evict됨 → `fallback_no_over_quota`.
- **2. over-quota**: free queue front→back 순회, 처음 만나는 over-quota workload의 evictable cached block → `over_quota_selected`.
- over-quota가 있는데 cached block을 못 찾으면 `RuntimeError`(occupancy counter 버그 신호).
- `quota_w = int(quota_ratio_w × quota_base_blocks)`. 미설정 workload는 quota_ratio 1.0이라 사실상 over-quota가 안 됨(§3 제외).

### 9.2 `collector.bind_pool(num_gpu_blocks)`

BlockPool 생성 시 호출. `quota_base_blocks`(=전체 KV block 수, §8.2 TIP)를 주입하고, mode∈{static,dynamic}이면 `victim_selection_active=True`로 켠다. off는 False(관측만).

### 9.3 `block_pool.get_new_blocks` — quota path

`victim_selection_active`(+caching)이면 block을 하나씩 `select_victim → remove → _maybe_evict_cached_block → allocate`한다. 매 선택 전에 occupancy가 갱신되도록 interleave(한 번에 `popleft_n`하지 않음). 비활성이면 기존 `popleft_n` LRU 경로 그대로 → parity.

### 9.4 로깅 (§8.3, §9.2)

`QUOTA_SERVE_LOG`에 JSONL. startup 1회 `quota_state_init`(quota_base_blocks + workload별 quota_w), eviction마다 `eviction`(evictor/victim/block_hash/selection_reason/is_cross_workload/victim_occupancy/victim_quota/occupancy_snapshot/scan_steps). `selection_counts`로 reason별 누적.

### 9.5 검증 상태

- `py_compile` 통과.
- 격리 통합 테스트(실제 `collector.py` 로드, heavy import는 stub): over-quota longctx가 LRU 순으로 evict되고 quota 안의 chat은 보호(occupancy 불변); `fallback_no_over_quota`→LRU head; `uncached_head`→free block 직접 사용; `selection_counts` 정확.
- baseline parity: mode=off는 `victim_selection_active=False`라 LRU 경로 그대로.

---

<div align="center">
<sub>QuotaServe · vLLM Fork Edits · PR 0 + PR 1 + PR 2/3 + PR 4</sub>
</div>
