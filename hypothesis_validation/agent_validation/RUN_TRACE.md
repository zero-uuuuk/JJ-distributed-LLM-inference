# RUN_TRACE

Agent-only 실험용 runner입니다.

```text
runner: hypothesis_validation/agent_validation/run_trace_agent.py
trace:  workloads/traj/traj_agent_100session_10step.jsonl
shape:  100 sessions x 10 steps = 1000 requests
gap:    previous finish_time + sampled tool gap
```

## 1. Agent trace 만들기

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
python workloads/traj/build_traj_agent_workload.py \
  --dataset-id yoonholee/terminalbench-trajectories \
  --config default \
  --split train \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --tokenizer approx \
  --output workloads/traj/traj_agent_100session_10step.jsonl
```

정상 생성 기준:

```text
sessions: 100
requests: 1000
avg_steps_per_session: 10.00
```

## 2. Main: exponential 2s tool gap

Agent main sanity는 exponential gap을 사용합니다.

```python
tool_gap = min(random.expovariate(1 / 2.0), 20.0)
```

의미:

```text
mean = 2s
cap  = 20s
```

APC ON:

```bash
python hypothesis_validation/agent_validation/run_trace_agent.py \
  --trace workloads/traj/traj_agent_100session_10step.jsonl \
  --phase agent_only_exponential \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode exponential \
  --agent-tool-gap-mean 2 \
  --agent-tool-gap-max 20 \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 10000 \
  --output hypothesis_validation/agent_validation/raw_results/agent_only_exp2_cap20_apc_on_len16384.jsonl
```

APC OFF:

```bash
python hypothesis_validation/agent_validation/run_trace_agent.py \
  --trace workloads/traj/traj_agent_100session_10step.jsonl \
  --phase agent_only_exponential \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode exponential \
  --agent-tool-gap-mean 2 \
  --agent-tool-gap-max 20 \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-concurrency 32 \
  --num-prompts 1000 \
  --slo-ms 10000 \
  --output hypothesis_validation/agent_validation/raw_results/agent_only_exp2_cap20_apc_off_len16384.jsonl
```

## 3. Fixed 2s baseline

고정 gap을 쓰는 baseline은 2초로 둡니다.

```bash
python hypothesis_validation/agent_validation/run_trace_agent.py \
  --trace workloads/traj/traj_agent_100session_10step.jsonl \
  --phase agent_only_fixed \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode fixed \
  --agent-tool-gap-seconds 2 \
  --num-prompts 1000 \
  --slo-ms 10000 \
  --output hypothesis_validation/agent_validation/raw_results/agent_only_fixed2_apc_on_len16384.jsonl
```

## 4. Scheduler 의미

```text
agent_target_rps = 전체 agent request 목표 rps
agent_steps_per_session = 평균 step/session
agent_session_start_rps = agent_target_rps / agent_steps_per_session
```

이번 trace는 `100 sessions x 10 steps`이므로:

```text
agent_session_start_rps = 5 / 10 = 0.5 sessions/s
```

`step1`은 session arrival로 들어오고, `step2..10`은 이전 step 완료 후 tool gap만큼 기다렸다가 들어옵니다.

```text
next_step_time = previous_finish_time + sampled_tool_gap
```
