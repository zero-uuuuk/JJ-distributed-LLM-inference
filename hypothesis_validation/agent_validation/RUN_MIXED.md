<div align="center">

# Agent Validation

**run_mixed_tau.py 실행 방법**

_Chat + Agent · tau2-bench · Tool gap · Shared prefix cache_

</div>

---

## 실행 목적

`run_mixed_tau.py`는 ShareGPT Chat workload와 tau2 Agent workload를 동시에 실행하는 mixed runner입니다.

Chat은 기존 Case 1과 같은 방식으로 `turn-major` trace를 Poisson arrival로 보냅니다.

```text
chat inter-arrival ~ exponential(1 / chat_qps)
```

Agent는 session별 completion-based scheduler를 사용합니다.

```text
step1:
  new session arrival ~ exponential(1 / session_start_rps)

step2..N:
  previous_step_finish_time + sampled_tool_gap
```

이 실험의 주 목적은 Agent-heavy workload가 Chat hot cache를 밀어내는지 보는 것입니다.

```text
danger signal: evicted_workload="chat", trigger_workload="agent", reused_later=true
```

---

## 1. 공통 준비

### Windows PowerShell

```powershell
cd C:\JJ-distributed-LLM-inference

.\.venv\Scripts\python.exe -m pip install -r requirements.txt
New-Item -ItemType Directory -Force hypothesis_validation\case1_validation\raw_results
New-Item -ItemType Directory -Force hypothesis_validation\agent_validation\raw_results
```

### Linux / Ubuntu

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

pip install -r requirements.txt
mkdir -p hypothesis_validation/case1_validation/raw_results
mkdir -p hypothesis_validation/agent_validation/raw_results
```

---

## 2. Workload trace 생성

### Chat trace

```bash
python workloads/sharegpt/build_sharegpt_workload.py \
  --num-conversations 100 \
  --min-turns 10 \
  --max-turns 10 \
  --order turn-major \
  --output workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe workloads\sharegpt\build_sharegpt_workload.py `
  --num-conversations 100 `
  --min-turns 10 `
  --max-turns 10 `
  --order turn-major `
  --output workloads\sharegpt\sharegpt_victim_100conv_10turn.jsonl
```

### Agent trace

```bash
python workloads/tau2/build_tau2_agent_workload.py \
  --domain telecom \
  --split base \
  --num-sessions 100 \
  --max-steps 10 \
  --tokenizer approx \
  --output workloads/tau2/tau2_agent_100session_10step.jsonl
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe workloads\tau2\build_tau2_agent_workload.py `
  --domain telecom `
  --split base `
  --num-sessions 100 `
  --max-steps 10 `
  --tokenizer approx `
  --output workloads\tau2\tau2_agent_100session_10step.jsonl
```

> `--tokenizer approx`는 Agent trace sanity check용 빠른 토큰 길이 추정입니다. 실제 모델 tokenizer로 `output_token_len`을 맞추고 싶으면 Llama tokenizer를 지정하세요.

---

## 3. vLLM 서버 실행

Runner 자체는 CPU에서도 실행되는 HTTP client입니다. 실제 prefix-cache 실험은 vLLM GPU 서버에서 실행하는 것을 권장합니다.

<details>
<summary>APC ON + eviction log</summary>

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

mkdir -p /home/ubuntu/vllm/eviction_logs

VLLM_SERVER_DEV_MODE=1 \
VLLM_EVICTION_LOG=/home/ubuntu/vllm/eviction_logs/chat_agent_fixed5_apc_on_len8192.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

실험 종료 후 vLLM 서버가 살아있는 상태에서 eviction log를 flush합니다.

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log
grep '"reused_later": true' /home/ubuntu/vllm/eviction_logs/chat_agent_fixed5_apc_on_len8192.jsonl | head
```

</details>

<details>
<summary>APC OFF</summary>

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --no-enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

</details>

> APC ON/OFF arm 사이에는 vLLM 서버를 재시작해 cache/queue 상태를 초기화합니다.

---

## 4. Mixed: Chat + Agent fixed 5s

기본 sanity check는 fixed 5초 tool gap만 사용합니다.

### Windows PowerShell

```powershell
.\.venv\Scripts\python.exe hypothesis_validation\agent_validation\run_mixed_tau.py `
  --chat-trace workloads\sharegpt\sharegpt_victim_100conv_10turn.jsonl `
  --agent-trace workloads\tau2\tau2_agent_100session_10step.jsonl `
  --phase chat_agent_fixed `
  --chat-qps 5 `
  --agent-target-rps 5 `
  --agent-steps-per-session 10 `
  --agent-tool-gap-mode fixed `
  --agent-tool-gap-seconds 5 `
  --url http://127.0.0.1:8000/v1/chat/completions `
  --model meta-llama/Llama-3.2-3B-Instruct `
  --max-concurrency 32 `
  --num-chat-prompts 1000 `
  --num-agent-prompts 1000 `
  --chat-slo-ms 400 `
  --agent-slo-ms 400 `
  --output hypothesis_validation\case1_validation\raw_results\chat_agent_fixed5_apc_on_len8192.jsonl
```

### Linux / Ubuntu

```bash
python hypothesis_validation/agent_validation/run_mixed_tau.py \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --agent-trace workloads/tau2/tau2_agent_100session_10step.jsonl \
  --phase chat_agent_fixed \
  --chat-qps 5 \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode fixed \
  --agent-tool-gap-seconds 5 \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 400 \
  --output hypothesis_validation/case1_validation/raw_results/chat_agent_fixed5_apc_on_len8192.jsonl
```

APC OFF 서버로 재시작한 뒤에는 같은 명령을 사용하고 output만 바꿉니다.

```bash
python hypothesis_validation/agent_validation/run_mixed_tau.py \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --agent-trace workloads/tau2/tau2_agent_100session_10step.jsonl \
  --phase chat_agent_fixed \
  --chat-qps 5 \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode fixed \
  --agent-tool-gap-seconds 5 \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 400 \
  --output hypothesis_validation/case1_validation/raw_results/chat_agent_fixed5_apc_off_len8192.jsonl
```

---

## 5. Optional: Chat + Agent Gaussian tool gap

이 명령은 sweep이 아니라 fixed 5초 결과가 나온 뒤 한 번 더 확인하는 단일 robustness run입니다. 이번 실험 범위에서 fixed만 볼 거라면 실행하지 않아도 됩니다.

```bash
python hypothesis_validation/agent_validation/run_mixed_tau.py \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --agent-trace workloads/tau2/tau2_agent_100session_10step.jsonl \
  --phase chat_agent_gaussian \
  --chat-qps 5 \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode gaussian \
  --agent-tool-gap-mean 5 \
  --agent-tool-gap-std 2 \
  --agent-tool-gap-min 1 \
  --agent-tool-gap-max 15 \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 400 \
  --output hypothesis_validation/case1_validation/raw_results/chat_agent_gaussian_gap.jsonl
```

---

## 6. 측정값

Mixed 결과는 raw JSONL보다 `*_summary.json`을 먼저 보는 것을 권장합니다.

| 항목 | 의미 |
|---|---|
| `chat.ttft_ms_p50/p95/p99` | Chat 요청 TTFT 분포 |
| `agent.ttft_ms_p50/p95/p99` | Agent 요청 TTFT 분포 |
| `chat.slo_attainment` | `TTFT <= chat_slo_ms` 비율 |
| `agent.slo_attainment` | `TTFT <= agent_slo_ms` 비율 |
| `chat.hit_rate_mean` | Chat prefix cache hit rate 평균 |
| `agent.hit_rate_mean` | Agent prefix cache hit rate 평균 |
| `agent.queue_delay_ms_*` | Agent scheduled time 대비 dispatch 지연 |
| `scheduler.chat_qps` | Chat Poisson arrival QPS |
| `scheduler.agent_session_start_rps` | Agent session start rate |
| `scheduler.agent_tool_gap_*` | Agent tool gap 설정 |

Sanity check 기대 방향:

| 신호 | 기대 |
|---|---|
| `agent -> chat useful eviction` | Mixed APC ON에서 관측되어야 함 |
| `chat.hit_rate_mean` | Chat-only 대비 하락 가능 |
| `chat.ttft_ms_p95/p99` | Agent 혼합 후 증가 가능 |
| `chat.slo_attainment` | Agent pressure가 강하면 하락 가능 |
| `agent.hit_rate_mean` | Agent step이 진행되며 일부 reuse를 가져야 함 |

---

## 7. vLLM eviction 계측

`run_mixed_tau.py`는 OpenAI `user` field에 workload tag를 넣습니다.

```text
chat request  -> user="chat"
agent request -> user="agent"
```

eviction log에서는 아래 항목을 봅니다.

| 항목 | 의미 |
|---|---|
| `evicted_workload="chat"` and `trigger_workload="agent"` | Agent가 Chat cached prefix를 밀어낸 경우 |
| `evicted_workload="agent"` and `trigger_workload="agent"` | Agent가 Agent block을 밀어낸 경우 |
| `evicted_workload="agent"` and `trigger_workload="chat"` | Chat이 Agent cached prefix를 밀어낸 경우 |
| `reused_later=true` | evicted block이 나중에 다시 필요했던 경우 |
| `reused_later=false` | evicted block이 이후 재사용되지 않은 경우 |
| `time_until_next_reuse` | eviction 이후 다음 재사용까지 걸린 시간 |

핵심 sanity signal:

```text
danger signal: evicted_workload="chat", trigger_workload="agent", reused_later=true
self signal:   evicted_workload="agent", trigger_workload="agent", reused_later=true
```

---

<div align="center">
<sub>Agent Validation · run_mixed_tau.py · JJ Distributed LLM Inference</sub>
</div>
