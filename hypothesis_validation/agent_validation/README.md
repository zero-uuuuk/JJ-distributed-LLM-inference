# Agent Validation

Terminal-Bench trajectory based Agent workload experiments.

Runners:

```text
run_trace_agent.py  -> Agent-only
run_mixed_agent.py  -> Chat + Agent mixed
```

Default traces:

```text
Agent: workloads/traj/traj_agent_100session_10step.jsonl
Chat:  workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl
```

Experiment set:

```text
1. Agent-only APC ON/OFF
2. Chat + Agent APC ON/OFF
```

Agent scheduling:

```text
step1: session arrival
step2..10: previous finish_time + tool_gap
```

Main Agent tool gap:

```python
tool_gap = min(random.expovariate(1 / 2.0), 20.0)
```

Fixed baseline:

```text
tool_gap = 2s
```

## Start vLLM

Run from the vLLM checkout:

```bash
cd /home/ubuntu/vllm
source .venv/bin/activate
mkdir -p /home/ubuntu/vllm/eviction_logs
```

Agent-only APC ON:

```bash
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

Chat + Agent APC ON:

```bash
VLLM_SERVER_DEV_MODE=1 \
VLLM_EVICTION_LOG=/home/ubuntu/vllm/eviction_logs/chat_traj_agent_exp2_cap20_apc_on_len12288.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 12288 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

APC OFF:

```bash
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --disable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 12288 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --port 8000
```

The clipped trajectory workload is intended to run with `--max-model-len 12288`
at `--gpu-memory-utilization 0.6`. If KV-cache pressure still appears, lower
`--max-num-seqs` to `16`.

## Docs

```text
RUN_TRACE.md  -> Agent-only commands
RUN_MIXED.md  -> Chat + Agent mixed commands
```

## Token Plots

```bash
python hypothesis_validation/agent_validation/plot_traj_prompt_tokens.py \
  --trace workloads/traj/traj_agent_100session_10step.jsonl \
  --output-dir hypothesis_validation/agent_validation/token_analysis \
  --tokenizer approx \
  --workload-label "Agent trajectory"
```
