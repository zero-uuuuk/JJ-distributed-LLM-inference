# RUN_TRACE

Agent-only runner.

```text
runner: hypothesis_validation/agent_validation/run_trace_agent.py
trace:  workloads/traj/traj_agent_100session_10step.jsonl
shape:  100 sessions x 10 steps = 1000 requests
gap:    previous finish_time + sampled tool gap
```

## Build Agent Trace

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
python workloads/traj/build_traj_agent_workload.py \
  --dataset-id yoonholee/terminalbench-trajectories \
  --config default \
  --split train \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --max-prompt-chars 24000 \
  --tokenizer approx \
  --output workloads/traj/traj_agent_100session_10step.jsonl
```

Expected shape:

```text
sessions: 100
requests: 1000
avg_steps_per_session: 10.00
```

## Start vLLM: APC ON

```bash
cd /home/ubuntu/vllm
source .venv/bin/activate
mkdir -p /home/ubuntu/vllm/eviction_logs

VLLM_SERVER_DEV_MODE=1 \
VLLM_EVICTION_LOG=/home/ubuntu/vllm/eviction_logs/agent_only_exp2_cap20_apc_on_len12288.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 12288 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

If KV-cache pressure appears, lower `--max-num-seqs` to `16`.

## Run Agent-only: APC ON

Main gap:

```python
tool_gap = min(random.expovariate(1 / 2.0), 20.0)
```

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
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
  --output hypothesis_validation/agent_validation/raw_results/agent_only_exp2_cap20_apc_on_len12288.jsonl
```

## Start vLLM: APC OFF

```bash
cd /home/ubuntu/vllm
source .venv/bin/activate

vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --disable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 12288 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

## Run Agent-only: APC OFF

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
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
  --output hypothesis_validation/agent_validation/raw_results/agent_only_exp2_cap20_apc_off_len12288.jsonl
```

## Fixed 2s Baseline

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
  --output hypothesis_validation/agent_validation/raw_results/agent_only_fixed2_apc_on_len12288.jsonl
```

## Scheduler

```text
agent_session_start_rps = agent_target_rps / agent_steps_per_session
                        = 5 / 10
                        = 0.5 sessions/s
```

```text
next_step_time = previous_finish_time + sampled_tool_gap
```
