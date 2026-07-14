# PR3 실행 스크립트

PR3는 workload별 physical block owner와 `occupancy_w` counter를 연결하는
단계다. PR4 victim selection은 아직 사용하지 않으므로, 클라이언트 runner의
`--quota-mode`는 `off`로 둔다. 서버만 `QUOTA_SERVE_MODE=static`으로 실행해
PR3 collector를 활성화한다.

## 1. 공통 준비

<details>
<summary>실행 스크립트 보기</summary>

```bash
export VLLM_DIR=/home/ubuntu/vllm
export JJ_ROOT=/home/ubuntu/JJ-distributed-LLM-inference

cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate
```

</details>

PR3 결과 파일을 저장할 디렉터리를 만든다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
mkdir -p $JJ_ROOT/static/PR3
```

</details>

## 2. PR3 collector lifecycle smoke

실제 서버를 실행하기 전에 `allocated -> cached -> accessed -> freed ->
evicted` 상태 전이를 확인한다. 같은 hook을 반복 호출해도 occupancy가 중복
증가하지 않는지도 함께 확인한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR

python - <<'PY'
from types import SimpleNamespace

from vllm.quota_serve.collector import QuotaServeCollector


class FakeBlock:
    block_id = 7
    ref_cnt = 1
    block_hash = None
    workload_tag = ""
    is_counted_as_evictable_cached = False
    is_null = False


block = FakeBlock()
collector = QuotaServeCollector(sample_rate=1.0)
request = SimpleNamespace(request_id="chat_smoke_0")

collector.on_block_allocated(block, request)
assert block.workload_tag == "chat"

block.block_hash = object()
block.ref_cnt = 0
collector.on_block_cached(block, request)
collector.on_block_cached(block, request)
assert collector.occupancy_snapshot() == {"chat": 1}

block.ref_cnt = 1
collector.on_block_accessed(block, request)
assert collector.occupancy_snapshot() == {}

block.ref_cnt = 0
collector.on_block_freed(block, 1, 0)
assert collector.occupancy_snapshot() == {"chat": 1}

collector.on_block_evicted(block)
assert collector.occupancy_snapshot() == {}
# 실제 BlockPool에서는 on_block_evicted() 직후 reset_hash()가 호출된다.
block.block_hash = None
collector.verify_occupancy([block])

collector.reset()
assert collector.occupancy_snapshot() == {}
print("PR3 collector lifecycle: PASS")
PY
```

</details>

## 3. PR3 코드 문법 확인

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR

python -m py_compile \
  vllm/quota_serve/collector.py \
  vllm/v1/core/kv_cache_utils.py \
  vllm/v1/core/kv_cache_metrics.py \
  vllm/v1/core/block_pool.py \
  vllm/v1/core/sched/scheduler.py
```

</details>

## 4. Workload 생성

이미 생성한 workload가 있으면 생략한다.

Chat workload:

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT/workloads/sharegpt

python build_sharegpt_workload.py \
  --num-conversations 100 \
  --min-turns 10 \
  --max-turns 10 \
  --order turn-major \
  --output sharegpt_victim_100conv_10turn.jsonl \
  --tokenizer meta-llama/Llama-3.2-3B-Instruct \
  --max-output-tokens 691
```

</details>

Longctx workload:

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT/workloads/hotpotqa

python build_hotpotqa_workload.py \
  --output hotpotqa_longctx_2000_4000.jsonl \
  --num-requests 1000 \
  --min-prompt-tokens 2000 \
  --max-prompt-tokens 4000 \
  --tokenizer meta-llama/Llama-3.2-3B-Instruct \
  --max-output-tokens 41
```

</details>

Agent workload:

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT/workloads/traj

python build_traj_agent_workload.py \
  --dataset-id yoonholee/terminalbench-trajectories \
  --config default \
  --split train \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --max-prompt-chars 24000 \
  --output traj_agent_100session_10step.jsonl \
  --tokenizer meta-llama/Llama-3.2-3B-Instruct \
  --max-output-tokens 1776
```

</details>

생성된 파일 줄 수를 확인한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT

wc -l \
  workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl \
  workloads/traj/traj_agent_100session_10step.jsonl
```

</details>

## 5. vLLM 서버 실행: PR3 collector 활성화

이 터미널은 서버 실행 상태로 둔다. `QUOTA_SERVE_CONFIG`로 JJ repo의 config를
지정하고 `QUOTA_SERVE_MODE=static`으로 collector를 활성화한다. PR3에서는
victim selection을 바꾸지 않으므로 실제 eviction 순서는 global LRU와 같다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
QUOTA_SERVE_CONFIG=$JJ_ROOT/static/quota_serve.yaml \
QUOTA_SERVE_MODE=static \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR3/pr3_eviction_chat_longctx_apc_on_len8192.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --generation-config vllm \
  --port 8000
```

</details>

서버 시작 로그에서 다음과 같이 config가 active 상태로 로드되었는지 확인한다.

```text
QuotaServe: enabled=True mode=static active=True
```

## 6. 서버 확인

새 터미널에서 실행한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl http://127.0.0.1:8000/v1/models
```

</details>

## 7. Chat+Longctx 실행

새 클라이언트 터미널에서 실행한다. `--quota-mode off`는 의도한 설정이다.
PR3는 collector와 occupancy만 검증하고 victim selection은 변경하지 않기
때문이다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT
source $JJ_ROOT/.venv/bin/activate

python static/run_mixed_c2.py \
  --quota-mode off \
  --longctx-trace workloads/hotpotqa/hotpotqa_longctx_2000_4000.jsonl \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --chat-qps 5.0 \
  --longctx-qps 5.0 \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-longctx-prompts 1000 \
  --chat-slo-ms 400 \
  --longctx-slo-ms 7700 \
  --output static/raw_results/pr3_chat_longctx_apc_on_len8192.jsonl
```

</details>

## 8. Chat+Longctx eviction log flush

실험이 끝난 뒤 서버를 끄기 전에 실행한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```

</details>

## 9. Chat+Longctx workload attribution 확인

PR3에서는 eviction log의 workload attribution이 계속 채워지고, 서버 실행 중
occupancy collector가 hook을 처리하는지 확인한다. `unknown`이 나오면 PR2의
request ID 전달 또는 block owner 설정 경로를 먼저 확인한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT

python - <<'PY'
import json
from collections import Counter
from pathlib import Path

path = Path("static/PR3/pr3_eviction_chat_longctx_apc_on_len8192.jsonl")

if not path.exists() or path.stat().st_size == 0:
    print("eviction log is empty: run did not trigger eviction")
    raise SystemExit(0)

counts = Counter()
missing = Counter()

with path.open("r", encoding="utf-8") as f:
    for line in f:
        row = json.loads(line)
        evicted = row.get("evicted_workload")
        trigger = row.get("trigger_workload")
        counts[(evicted, trigger)] += 1
        if not evicted or evicted == "unknown":
            missing["evicted_workload"] += 1
        if not trigger or trigger == "unknown":
            missing["trigger_workload"] += 1

print("top pairs:", counts.most_common(10))
print("missing:", dict(missing))

assert missing["evicted_workload"] == 0
assert missing["trigger_workload"] == 0
print("PR3 workload attribution: PASS")
PY
```

</details>

## 10. Chat+Longctx 서버 종료

<details>
<summary>실행 스크립트 보기</summary>

```bash
# vLLM 서버 터미널에서 Ctrl+C
```

</details>

## 11. vLLM 서버 재실행: Chat+Agent

ON/OFF 또는 workload 조합을 바꿀 때는 서버를 재시작해 KV cache와 occupancy
state를 초기화한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
QUOTA_SERVE_CONFIG=$JJ_ROOT/static/quota_serve.yaml \
QUOTA_SERVE_MODE=static \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR3/pr3_eviction_chat_agent_apc_on_len8192.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --generation-config vllm \
  --port 8000
```

</details>

## 12. 서버 확인

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl http://127.0.0.1:8000/v1/models
```

</details>

## 13. Chat+Agent 실행

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT
source $JJ_ROOT/.venv/bin/activate

python static/run_mixed_agent_c2.py \
  --quota-mode off \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --phase chat_agent_exponential \
  --chat-qps 5.0 \
  --agent-target-rps 5.0 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode exponential \
  --agent-tool-gap-mean 2 \
  --agent-tool-gap-max 20 \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 200 \
  --output static/raw_results/pr3_chat_agent_apc_on_len8192.jsonl
```

</details>

## 14. Chat+Agent eviction log flush

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```

</details>

## 15. Chat+Agent workload attribution 확인

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT

python - <<'PY'
import json
from collections import Counter
from pathlib import Path

path = Path("static/PR3/pr3_eviction_chat_agent_apc_on_len8192.jsonl")

if not path.exists() or path.stat().st_size == 0:
    print("eviction log is empty: run did not trigger eviction")
    raise SystemExit(0)

counts = Counter()
missing = Counter()

with path.open("r", encoding="utf-8") as f:
    for line in f:
        row = json.loads(line)
        evicted = row.get("evicted_workload")
        trigger = row.get("trigger_workload")
        counts[(evicted, trigger)] += 1
        if not evicted or evicted == "unknown":
            missing["evicted_workload"] += 1
        if not trigger or trigger == "unknown":
            missing["trigger_workload"] += 1

print("top pairs:", counts.most_common(10))
print("missing:", dict(missing))

assert missing["evicted_workload"] == 0
assert missing["trigger_workload"] == 0
print("PR3 workload attribution: PASS")
PY
```

</details>

## 16. PR3 확인 범위

```text
확인하는 것:
- workload tag가 physical block owner로 설정되는가
- cached/accessed/freed/evicted 전이에서 occupancy counter가 중복 갱신되지 않는가
- prefix cache reset 후 occupancy state가 초기화되는가
- eviction log의 victim/trigger workload가 unknown이 아닌가

아직 확인하지 않는 것:
- quota_w 계산
- occupancy_w > quota_w 기반 victim selection
- global LRU와 다른 eviction 결과
```
