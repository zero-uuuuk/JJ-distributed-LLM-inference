# PR1 Config Schema / Loader Run

PR1에서는 vLLM의 eviction 정책을 바꾸지 않고, QuotaServe config schema와 loader만 확인한다.

목표:

```text
1. vllm/quota_serve/config.py가 import/compile 되는지 확인
2. static/quota_serve.yaml을 정상 로드하는지 확인
3. QUOTA_SERVE_MODE / QUOTA_SERVE_LOG override가 동작하는지 확인
4. 잘못된 mode와 quota_ratio를 거부하는지 확인
```

PR1에서는 workload 본 실행을 하지 않는다. 아직 scheduler/collector wiring이 없으므로 서버 실행 결과가 QuotaServe config의 영향을 받지 않아야 한다.

## 1. 공통 준비

<details>
<summary>실행 스크립트 보기</summary>

```bash
export VLLM_DIR=/home/ubuntu/vllm
export JJ_ROOT=/home/ubuntu/JJ-Distributed-LLM-Inference

cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate
```

</details>

`PyYAML`이 설치되어 있는지 확인한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
python -c "import yaml; print(yaml.__version__)"
```

</details>

`ModuleNotFoundError: No module named 'yaml'`가 나오면 vLLM venv가 아니거나 requirements 설치가 빠진 것이다.

## 2. Config 위치 확인

PR1 기본 config는 JJ repo의 `static/` 아래에 둔다.

```text
$JJ_ROOT/static/quota_serve.yaml
```

파일이 있는지 확인한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
ls -l $JJ_ROOT/static/quota_serve.yaml
```

</details>

vLLM fork 내부 기본 위치로도 쓰고 싶으면 복사한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cp $JJ_ROOT/static/quota_serve.yaml \
   $VLLM_DIR/vllm/quota_serve/quota_serve.yaml
```

</details>

단, 실험 스크립트에서는 `QUOTA_SERVE_CONFIG`로 경로를 명시하는 방식을 우선 사용한다.

## 3. py_compile

`config.py` 문법 오류를 먼저 확인한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR

python -m py_compile vllm/quota_serve/config.py
```

</details>

아무 출력 없이 종료되면 통과다.

## 4. 기본 config 객체 확인

config 파일 없이 loader를 호출하면 비활성 기본값이어야 한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR

python - <<'PY'
from vllm.quota_serve.config import load_quota_serve_config

cfg = load_quota_serve_config()

assert cfg.enabled is False
assert cfg.mode == "off"
assert cfg.is_active is False
assert cfg.victim_selection_active is False
assert cfg.quota_for("unknown").quota_ratio == 1.0

print("default config: PASS")
PY
```

</details>

## 5. YAML 로드 확인

`static/quota_serve.yaml`을 직접 로드한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR

python - <<'PY'
from pathlib import Path

from vllm.quota_serve.config import load_quota_serve_config

path = Path("/home/ubuntu/JJ-Distributed-LLM-Inference/static/quota_serve.yaml")
cfg = load_quota_serve_config(path)

assert cfg.enabled is True
assert cfg.mode == "off"
assert cfg.tick_sec == 30
assert cfg.shadow_ttl_sec == 120
assert cfg.is_active is False
assert cfg.victim_selection_active is False

assert cfg.quota_for("chat").quota_ratio == 0.30
assert cfg.quota_for("rag").quota_ratio == 0.08
assert cfg.quota_for("longctx").quota_ratio == 0.10
assert cfg.quota_for("agent").quota_ratio == 0.25
assert cfg.quota_for("unknown").quota_ratio == 1.0

print("yaml load: PASS")
PY
```

</details>

## 6. Env override 확인

`QUOTA_SERVE_CONFIG`, `QUOTA_SERVE_MODE`, `QUOTA_SERVE_LOG` override를 확인한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR

QUOTA_SERVE_CONFIG=$JJ_ROOT/static/quota_serve.yaml \
QUOTA_SERVE_MODE=static \
QUOTA_SERVE_LOG=$JJ_ROOT/static/eviction_logs/pr1_quota_smoke.jsonl \
python - <<'PY'
from vllm.quota_serve.config import load_quota_serve_config

cfg = load_quota_serve_config()

assert cfg.enabled is True
assert cfg.mode == "static"
assert cfg.is_active is True
assert cfg.victim_selection_active is True
assert cfg.log_path.endswith("pr1_quota_smoke.jsonl")

print("env override: PASS")
PY
```

</details>

## 7. 잘못된 mode 거부 확인

`dry_run`은 사용하지 않기로 했으므로 거부되어야 한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR

QUOTA_SERVE_MODE=dry_run \
python - <<'PY'
from vllm.quota_serve.config import load_quota_serve_config

try:
    load_quota_serve_config()
except ValueError as exc:
    assert "dry_run" in str(exc)
    print("invalid mode: PASS")
else:
    raise SystemExit("invalid mode was accepted")
PY
```

</details>

## 8. 잘못된 quota_ratio 거부 확인

`quota_ratio`는 `0.0 <= quota_ratio <= 1.0`이어야 한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR

python - <<'PY'
from vllm.quota_serve.config import QuotaServeConfig

try:
    QuotaServeConfig(
        enabled=True,
        mode="static",
        workloads={"chat": {"quota_ratio": 1.5}},
    )
except ValueError as exc:
    assert "quota_ratio" in str(exc)
    print("invalid quota_ratio: PASS")
else:
    raise SystemExit("invalid quota_ratio was accepted")
PY
```

</details>

## 9. PR1 통과 기준

아래 항목이 모두 통과하면 PR1 config 단계는 통과로 본다.

```text
1. py_compile 통과
2. config 없음 -> enabled=False, mode=off
3. static/quota_serve.yaml 로드 성공
4. workload별 quota_ratio 로드 성공
5. unknown workload -> quota_ratio=1.0
6. QUOTA_SERVE_MODE=static override 시 enabled=True / is_active=True
7. QUOTA_SERVE_LOG override 반영
8. mode=dry_run 거부
9. quota_ratio 범위 밖 값 거부
```

## 10. 주의 사항

```text
1. PR1은 config-only 단계다. victim selection은 바꾸지 않는다.
2. PR1에서 workload 본 실행을 돌려도 QuotaServe policy 효과는 없어야 한다.
3. mode=off parity 본 실행은 scheduler/collector wiring이 들어가는 PR3 이후 다시 확인한다.
4. static quota 효과 검증은 PR4에서 진행한다.
```
