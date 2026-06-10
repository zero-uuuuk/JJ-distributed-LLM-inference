<div align="center">

# Agent Validation

**run_trace_tau.py 실행 방법**

_Agent-only · tau2-bench · Tool gap · Completion-based scheduler_

</div>

---

## 실행 목적

`run_trace_tau.py`는 tau2 Agent workload만 단독으로 실행하는 Phase 1 runner입니다.

기존 `run_trace.py`처럼 JSONL row를 단순 QPS로 밀어 넣지 않습니다. Agent workload에서는 같은 session의 다음 step이 이전 step 완료 후 tool gap만큼 기다렸다가 들어와야 하므로, runner가 session별로 step을 순차 실행합니다.

```text
step1 arrival:
  session_start_rps = agent_target_rps / agent_steps_per_session

step2..N arrival:
  previous_step_finish_time + sampled_tool_gap
```

---

## 1. 공통 준비

### Windows PowerShell

```powershell
cd C:\JJ-distributed-LLM-inference

.\.venv\Scripts\python.exe -m pip install -r requirements.txt
New-Item -ItemType Directory -Force hypothesis_validation\agent_validation\raw_results
```

### Linux / Ubuntu

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

pip install -r requirements.txt
mkdir -p hypothesis_validation/agent_validation/raw_results
```

---

## 2. Agent trace 생성

먼저 tau2 Agent trace가 필요합니다.

### Windows PowerShell

```powershell
.\.venv\Scripts\python.exe workloads\tau2\build_tau2_agent_workload.py `
  --domain telecom `
  --split base `
  --num-sessions 100 `
  --max-steps 10 `
  --tokenizer approx `
  --output workloads\tau2\tau2_agent_100session_10step.jsonl
```

### Linux / Ubuntu

```bash
python workloads/tau2/build_tau2_agent_workload.py \
  --domain telecom \
  --split base \
  --num-sessions 100 \
  --max-steps 10 \
  --tokenizer approx \
  --output workloads/tau2/tau2_agent_100session_10step.jsonl
```

> `--tokenizer approx`는 trace sanity check용 빠른 토큰 길이 추정입니다. 실제 모델 tokenizer로 `output_token_len`을 맞추고 싶으면 `--tokenizer meta-llama/Llama-3.2-3B-Instruct`를 사용하세요.

---

## 3. vLLM 서버 실행

Agent runner는 CPU에서도 실행되는 HTTP client입니다. 하지만 prefix-cache 실험 신호는 vLLM GPU 서버에서 보는 것을 권장합니다.

<details>
<summary>APC ON</summary>

```bash
cd /home/ubuntu/vllm
source /home/ubuntu/vllm/.venv/bin/activate

vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
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

## 4. Agent-only fixed 5s

### Windows PowerShell

```powershell
.\.venv\Scripts\python.exe hypothesis_validation\agent_validation\run_trace_tau.py `
  --trace workloads\tau2\tau2_agent_100session_10step.jsonl `
  --phase agent_only_fixed `
  --agent-target-rps 5 `
  --agent-steps-per-session 10 `
  --agent-tool-gap-mode fixed `
  --agent-tool-gap-seconds 5 `
  --url http://127.0.0.1:8000/v1/chat/completions `
  --model meta-llama/Llama-3.2-3B-Instruct `
  --max-concurrency 32 `
  --num-prompts 1000 `
  --slo-ms 400 `
  --output hypothesis_validation\agent_validation\raw_results\agent_only_fixed5_apc_on_len8192.jsonl
```

### Linux / Ubuntu

```bash
python hypothesis_validation/agent_validation/run_trace_tau.py \
  --trace workloads/tau2/tau2_agent_100session_10step.jsonl \
  --phase agent_only_fixed \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode fixed \
  --agent-tool-gap-seconds 5 \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 400 \
  --output hypothesis_validation/agent_validation/raw_results/agent_only_fixed5_apc_on_len8192.jsonl
```

APC OFF 서버로 재시작한 뒤에는 같은 명령을 사용하고 output만 바꿉니다.

```bash
python hypothesis_validation/agent_validation/run_trace_tau.py \
  --trace workloads/tau2/tau2_agent_100session_10step.jsonl \
  --phase agent_only_fixed \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode fixed \
  --agent-tool-gap-seconds 5 \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 400 \
  --output hypothesis_validation/agent_validation/raw_results/agent_only_fixed5_apc_off_len8192.jsonl
```

---

## 5. Optional: Agent-only Gaussian gap

고정 5초가 아니라 truncated Gaussian gap으로 Agent-only robustness를 보고 싶을 때 사용합니다.

```bash
python hypothesis_validation/agent_validation/run_trace_tau.py \
  --trace workloads/tau2/tau2_agent_100session_10step.jsonl \
  --phase agent_only_gaussian \
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
  --num-prompts 1000 \
  --slo-ms 400 \
  --output hypothesis_validation/agent_validation/raw_results/agent_only_gaussian_gap.jsonl
```

---

## 6. 측정값

결과 해석은 raw JSONL보다 `*_summary.json`을 먼저 보는 것을 권장합니다.

| 항목 | 의미 |
|---|---|
| `agent.n_total`, `agent.n_ok`, `agent.n_failed` | 전체/성공/실패 요청 수 |
| `agent.throughput_req_per_s` | 완료 기준 request throughput |
| `agent.slo_attainment` | `TTFT <= slo_ms` 비율 |
| `agent.hit_rate_mean`, `agent.hit_rate_p50` | Agent prefix cache hit rate |
| `agent.ttft_ms_*` | TTFT mean/p50/p95/p99 |
| `agent.tpot_ms_*` | TPOT mean/p50/p95/p99 |
| `agent.e2e_ms_*` | E2E mean/p50/p95/p99 |
| `agent.queue_delay_ms_*` | scheduled time 대비 실제 dispatch 지연 |
| `scheduler.agent_session_start_rps` | `agent_target_rps / steps_per_session` |
| `scheduler.agent_tool_gap_*` | tool gap 설정 |

---

<div align="center">
<sub>Agent Validation · run_trace_tau.py · JJ Distributed LLM Inference</sub>
</div>
