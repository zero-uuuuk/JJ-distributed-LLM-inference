# VLLM_EDITED_PR1

이 문서는 QuotaServe PR1에서 vLLM fork에 추가한 config 관련 변경 사항을 정리한다.

PR1의 목표는 QuotaServe 설정 진입점을 추가하는 것이다. 이 단계에서는 KV cache allocation, free queue, eviction order, victim selection을 바꾸지 않는다. 따라서 `mode=off`에서는 baseline LRU와 동일하게 동작해야 한다.

## 1. 수정 파일

PR1에서 vLLM에 추가/수정한 파일은 다음이다.

```text
vllm/quota_serve/config.py
```

현재 `vllm/quota_serve/__init__.py`는 PR0 상태를 유지한다.

```python
from vllm.quota_serve.workload import infer_workload

__all__ = ["infer_workload"]
```

즉 PR1에서는 config module을 vLLM hot path에 연결하지 않는다. scheduler, collector, block pool wiring은 후속 PR 범위다.

## 2. PR1에서 한 것

```text
1. QuotaServe config schema 추가
2. YAML loader 추가
3. env override 추가
   - QUOTA_SERVE_CONFIG
   - QUOTA_SERVE_MODE
   - QUOTA_SERVE_LOG
4. workload별 quota_ratio 검증
5. unknown workload 기본 quota 처리
6. type check helper를 단일 함수로 정리
```

## 3. PR1에서 하지 않은 것

```text
1. victim selection 변경
2. occupancy counter 구현
3. block owner metadata 구현
4. request workload tag 전파
5. static quota policy 적용
6. dynamic controller 구현
7. scheduler / collector / block_pool 연결
```

PR1은 설정 객체를 만들 수 있게 하는 단계다. config가 존재한다고 해서 eviction 정책이 활성화되는 것은 아니다.

## 4. Config schema

### 4.1 Mode

현재 허용하는 mode는 다음 3개다.

```python
QuotaServeMode = Literal["off", "static", "dynamic"]
```

의미는 다음과 같다.

```text
off:
  QuotaServe 비활성화. baseline LRU 유지.

static:
  PR4에서 사용할 fixed quota mode.

dynamic:
  PR7 이후 사용할 feedback quota mode.
```

`dry_run`은 현재 PR1 schema에 넣지 않는다.

### 4.2 WorkloadQuota

workload 하나의 quota 설정이다.

```python
@dataclass(frozen=True)
class WorkloadQuota:
    quota_ratio: float
```

검증 규칙은 다음과 같다.

```text
quota_ratio는 int 또는 float이어야 한다.
bool은 숫자로 취급하지 않고 reject한다.
0.0 <= quota_ratio <= 1.0 범위여야 한다.
```

### 4.3 QuotaServeConfig

전체 QuotaServe 설정 객체다.

```python
enabled: bool = False
mode: QuotaServeMode = "off"
tick_sec: int = 30
shadow_ttl_sec: int = 120
workloads: Mapping[str, WorkloadQuota] = field(default_factory=dict)
log_path: str | None = None
```

기본값은 disabled 상태다.

```text
enabled=False
mode="off"
```

중요 property는 다음과 같다.

```python
is_active = enabled and mode != "off"
victim_selection_active = enabled and mode in ("static", "dynamic")
```

`is_active`는 QuotaServe 설정이 켜졌는지 보는 일반 gate다.

`victim_selection_active`는 후속 PR에서 victim selection을 바꿔도 되는지 판단하는 더 좁은 gate다. PR1에서는 아직 사용하지 않는다.

## 5. Workload 처리

YAML 예시는 다음과 같다.

```yaml
quota_serve:
  enabled: true
  mode: "off"
  workloads:
    chat:
      quota_ratio: 0.30
    longctx:
      quota_ratio: 0.10
```

로드 후 내부에서는 다음 형태가 된다.

```python
{
    "chat": WorkloadQuota(quota_ratio=0.30),
    "longctx": WorkloadQuota(quota_ratio=0.10),
}
```

lookup은 다음 API를 쓴다.

```python
config.quota_for("chat")
```

config에 없는 workload는 다음 기본값을 받는다.

```python
WorkloadQuota(quota_ratio=1.0)
```

이 값은 후속 PR4에서 unknown workload가 over-quota victim 후보로 쉽게 들어가지 않도록 하기 위한 기본값이다.

## 6. Loader 동작

config loader는 다음 함수다.

```python
load_quota_serve_config(path=None)
```

로드 우선순위는 다음과 같다.

```text
1. 함수 인자로 받은 path
2. QUOTA_SERVE_CONFIG env
3. disabled default config
```

그 다음 env override를 적용한다.

```text
QUOTA_SERVE_MODE:
  mode를 override한다.
  mode가 "off"가 아니면 enabled=True로 자동 설정한다.

QUOTA_SERVE_LOG:
  log_path를 override한다.
```

YAML은 두 형태를 모두 허용한다.

```yaml
quota_serve:
  enabled: true
  mode: "off"
```

또는:

```yaml
enabled: true
mode: "off"
```

실험용 기본 config는 JJ repo 쪽에 둔다.

```text
quotaserve/static/quota_serve.yaml
```

vLLM 내부의 `config.py`는 이 YAML을 읽어서 `QuotaServeConfig` 객체로 바꾸는 역할만 한다.

## 7. Type check helper 정리

피드백 반영 사항:

```text
config.py에서 type check helper는 하나로만 유지한다.
```

현재 타입 확인은 `_expect_type()` 하나로 통일했다.

```python
def _expect_type(
    name: str,
    value: Any,
    expected_type: type[Any] | tuple[type[Any], ...],
    *,
    optional: bool = False,
    reject_bool: bool = False,
) -> Any:
    ...
```

제거한 중복 helper:

```text
_coerce_bool
_coerce_positive_int
_coerce_float
_coerce_optional_str
```

또한 `validate()`는 실제 검증을 새로 하지 않고 `self`만 반환하던 함수라 제거했다. 검증은 dataclass `__post_init__()`에서 수행한다.

남겨둔 helper:

```text
_coerce_mode
_coerce_workloads
```

이 둘은 단순 type check helper가 아니다.

`_coerce_mode`는 mode 값이 허용된 enum 값인지 확인한다.

`_coerce_workloads`는 nested YAML mapping을 `WorkloadQuota` 객체로 변환한다.

따라서 type check helper를 여러 개 둔 것이 아니라, 공통 타입 확인은 `_expect_type()` 하나로 모으고 mode/workload의 의미 검증만 별도 함수로 남긴 상태다.

## 8. 검증 상태

로컬에서 확인한 내용:

```text
AST parse 통과
config import smoke test 통과
static mode config 생성 확인
unknown workload quota_ratio=1.0 확인
bool quota_ratio reject 확인
tick_sec=0 reject 확인
```

확인한 동작:

```text
QuotaServeConfig(enabled=True, mode="static", workloads={"chat": {"quota_ratio": 0.3}})

is_active == True
victim_selection_active == True
quota_for("chat").quota_ratio == 0.3
quota_for("unknown").quota_ratio == 1.0
```

## 9. PR1의 핵심 원칙

PR1은 config-only 변경이다.

```text
config.py를 추가해도 mode=off에서는 eviction 순서가 baseline LRU와 같아야 한다.
```

실제 정책 연결은 후속 PR에서 한다.

```text
PR2: request workload tag
PR3: block owner + occupancy counter
PR4: static quota victim selection
```
