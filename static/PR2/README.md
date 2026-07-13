# PR2 실행 스크립트

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

## 2. workload inference unit smoke

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR

python - <<'PY'
from vllm.quota_serve.workload import infer_workload

cases = {
    "chat-abc": "chat",
    "chatcmpl-chat-abc": "chat",
    "cmpl-chat_abc": "chat",
    "longctx-abc": "longctx",
    "chatcmpl-longctx-abc": "longctx",
    "hotpotqa-longctx-abc": "longctx",
    "chatcmpl-hotpotqa-abc": "longctx",
    "agent-abc": "agent",
    "chatcmpl-agent-abc": "agent",
    "rag-abc": "rag",
    "msmarco-abc": "rag",
    "chatcmpl-msmarco-abc": "rag",
    "": "unknown",
    None: "unknown",
    "chatcmpl-other-abc": "unknown",
}

for request_id, expected in cases.items():
    actual = infer_workload(request_id)
    assert actual == expected, (request_id, actual, expected)

print("infer_workload: PASS")
PY
```

</details>

## 3. Workload 생성

Chat workload가 이미 있으면 생략한다.

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

Longctx workload가 이미 있으면 생략한다.

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

Agent workload가 이미 있으면 생략한다.

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

## 4. runner header 확인

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT

grep -n "X-Request-Id" \
  static/run_mixed_c2.py \
  static/run_mixed_agent_c2.py
```

</details>

## 5. vLLM 서버 실행

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR2/pr2_eviction_chat_longctx_apc_on_len8192.jsonl \
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

## 6. 서버 확인

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl http://127.0.0.1:8000/v1/models
```

</details>

## 7. Chat+Longctx 실행

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
  --output static/raw_results/pr2_chat_longctx_apc_on_len8192.jsonl
```

</details>

## 8. eviction log flush

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```

</details>

## 9. eviction log workload 확인

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT

python - <<'PY'
import json
from collections import Counter
from pathlib import Path

path = Path("static/PR2/pr2_eviction_chat_longctx_apc_on_len8192.jsonl")

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
print("eviction workload attribution: PASS")
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

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR2/pr2_eviction_chat_agent_apc_on_len8192.jsonl \
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
  --output static/raw_results/pr2_chat_agent_apc_on_len8192.jsonl
```

</details>

## 14. Chat+Agent eviction log flush

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
```

</details>

## 15. Chat+Agent eviction log workload 확인

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT

python - <<'PY'
import json
from collections import Counter
from pathlib import Path

path = Path("static/PR2/pr2_eviction_chat_agent_apc_on_len8192.jsonl")

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
print("eviction workload attribution: PASS")
PY
```

</details>
