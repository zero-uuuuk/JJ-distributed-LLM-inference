# VLLM_EDITED_PR1

이 문서는 QuotaServe PR1을 위해 vLLM fork에 추가한 config 관련 수정사항을 정리한다.

PR1의 목적은 **QuotaServe 설정 진입점만 추가하고, `mode=off`에서 baseline LRU 동작을 그대로 유지하는 것**이다.

## 1. PR1 범위

PR1에서 하는 것:

```text
1. QuotaServe config schema 추가
2. YAML loader 추가
3. env override 추가
   - QUOTA_SERVE_CONFIG
   - QUOTA_SERVE_MODE
   - QUOTA_SERVE_LOG
4. quota_ratio 검증
5. unknown workload 기본 처리
```

PR1에서 하지 않는 것:

```text
1. victim selection 변경
2. occupancy counter 구현
3. block owner attribution 구현
4. static quota policy 적용
5. dynamic controller 구현
6. scheduler / collector wiring
```

즉 PR1은 순수 config 단계다. 이 PR만으로 eviction 순서가 바뀌면 안 된다.

## 2. 수정 파일

vLLM PR1 변경 파일:

```text
vllm/quota_serve/config.py
```

현재 `vllm/quota_serve/__init__.py`는 PR0 상태를 유지한다.

```python
from vllm.quota_serve.workload import infer_workload

__all__ = ["infer_workload"]
```

`config.py` re-export는 아직 하지 않았다. scheduler/collector wiring은 PR3 범위다.

## 3. `vllm/quota_serve/config.py`

추가한 symbol:

```text
QuotaServeMode
WorkloadQuota
QuotaServeConfig
load_quota_serve_config
```

### 3.1 mode

허용 mode:

```python
QuotaServeMode = Literal["off", "static", "dynamic"]
```

`dry_run`은 사용하지 않기로 했으므로 제거했다. `mode="dry_run"`은 validation에서 거부된다.

mode 의미:

```text
off
  QuotaServe 비활성. baseline LRU 유지.

static
  PR4에서 사용할 고정 quota mode.

dynamic
  PR7에서 사용할 feedback quota mode.
```

### 3.2 `WorkloadQuota`

workload 하나의 quota 설정이다.

```python
@dataclass(frozen=True)
class WorkloadQuota:
    quota_ratio: float
```

검증:

```text
quota_ratio는 숫자여야 한다.
0.0 <= quota_ratio <= 1.0 이어야 한다.
bool은 허용하지 않는다.
```

### 3.3 `QuotaServeConfig`

전체 QuotaServe 설정이다.

```python
enabled: bool = False
mode: QuotaServeMode = "off"
tick_sec: int = 30
shadow_ttl_sec: int = 120
workloads: Mapping[str, WorkloadQuota] = field(default_factory=dict)
log_path: str | None = None
```

기본값은 비활성 상태다.

```text
enabled=False
mode="off"
```

중요 property:

```python
is_active = enabled and mode != "off"
victim_selection_active = enabled and mode in ("static", "dynamic")
```

`victim_selection_active`는 PR4에서 quota-aware victim selection을 켤 때 쓰는 게이트다. PR1에서는 아직 소비하지 않는다.

### 3.4 workload 처리

YAML:

```yaml
workloads:
  chat:
    quota_ratio: 0.30
```

로드 후:

```python
{"chat": WorkloadQuota(quota_ratio=0.30)}
```

lookup:

```python
config.quota_for("chat")
```

unknown workload는 다음 기본값을 받는다.

```python
WorkloadQuota(quota_ratio=1.0)
```

이렇게 하면 config에 없는 workload는 PR4 over-quota victim 후보가 되기 어렵다.

### 3.5 loader

```python
load_quota_serve_config(path=None)
```

로딩 우선순위:

```text
1. path 인자
2. QUOTA_SERVE_CONFIG
3. disabled default config
```

그 다음 env override를 적용한다.

```text
QUOTA_SERVE_MODE
  mode override.
  off가 아니면 enabled=True로 자동 설정.

QUOTA_SERVE_LOG
  log_path override.
```

YAML은 두 형태를 허용한다.

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

기본 PR1 config는 `quota_serve:` top-level을 사용한다.

## 4. 검증 상태

확인한 것:

```text
python -m py_compile vllm/quota_serve/config.py 통과
dry_run 문자열 제거 확인
기본 config 객체 생성 확인
static mode config 객체 생성 확인
unknown workload quota_ratio=1.0 확인
```

로컬 Windows Python에는 `yaml` 모듈이 없어 YAML file load smoke test는 실행하지 못했다. vLLM `requirements/common.txt`에는 `pyyaml`이 포함되어 있으므로 EC2/vLLM venv에서는 동작해야 한다.

## 5. PR1 이후 남은 일

```text
1. quota_serve.yaml 최종 위치 결정
   - static/PR1/quota_serve.yaml: PR1 산출물
   - static/quota_serve.yaml: 실험용 기본 config 후보
   - vllm/quota_serve/quota_serve.yaml: vLLM fork 내 기본 config 후보

2. mode=off parity run

3. PR3에서 scheduler/collector wiring

4. PR4에서 victim_selection_active를 기준으로 quota-aware victim selection 적용
```

PR1의 핵심 원칙:

```text
config.py를 추가해도 mode=off에서는 eviction 순서가 baseline LRU와 같아야 한다.
```
