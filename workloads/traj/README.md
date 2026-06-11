# Terminal-Bench Trajectory Agent Workload

This directory builds an agent workload from
`yoonholee/terminalbench-trajectories`.

The goal is to replace the shallow tau2 `telecom/base` trace with a workload
that has enough repeated agent steps to stress delayed prefix reuse.

## Why This Dataset

The tau2 `telecom/base` trace produced only:

```text
91 sessions / 123 requests
avg_steps_per_session = 1.35
follow-up steps = 123 - 91 = 32
```

That is too shallow for an agent-heavy cache sanity check. Most sessions have
only one request, so the runner rarely observes:

```text
step 1 -> tool/env gap -> step 2 -> tool/env gap -> step 3
```

Terminal-Bench trajectory rows are closer to the target structure. One row is
one agent trial, and its `steps` field is a JSON-serialized list of user,
system, agent, tool-call, and observation records.

For the cache experiment, map the data like this:

```text
Terminal-Bench trial  -> agent session
agent step with tools -> one model request
previous steps        -> prompt history
tool/env wait         -> sampled by the runner, not stored in the dataset
```

## Output Shape

The builder emits JSONL rows compatible with
`hypothesis_validation/agent_validation/run_trace_tau.py` and
`hypothesis_validation/agent_validation/run_mixed_tau.py`.

Important fields:

```text
request_id
session_id
task_id
step_id
session_step_count
messages
output_text
output_token_len
tool_name
tool_arguments
tool_gap_seconds = null
source_dataset = yoonholee/terminalbench-trajectories
cache_pattern = agent_multi_step
```

The runner samples the actual gap:

```python
fixed:    tool_gap = 5.0
gaussian: tool_gap = max(1.0, min(random.gauss(5.0, 2.0), 15.0))
```

## Build

From the repo root:

```bash
cd workloads/traj
python build_traj_agent_workload.py \
  --dataset-id yoonholee/terminalbench-trajectories \
  --config default \
  --split train \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --tokenizer approx \
  --output traj_agent_100session_10step.jsonl
```

Expected high-level shape:

```text
sessions: 100
requests: 1000
avg_steps_per_session: 10.00
```

If the Hugging Face `datasets` package is missing:

```bash
pip install datasets pyarrow
```

Use streaming mode when you want to avoid downloading the full split:

```bash
python build_traj_agent_workload.py \
  --streaming \
  --dataset-id yoonholee/terminalbench-trajectories \
  --config default \
  --split train \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --tokenizer approx \
  --output traj_agent_100session_10step.jsonl
```

## Selection Rules

Defaults:

```text
one dataset row = one session
select only src == "agent" steps
select only agent steps that contain tool calls
drop leading warmup / ready messages
require min_steps == max_steps unless --min-steps is set
shuffle source rows with seed 42
output order = step-major
```

The default is intentionally strict. It makes the generated trace comparable to
the ShareGPT chat workload:

```text
Chat:  100 conversations x 10 turns = 1000 requests
Agent: 100 sessions      x 10 steps = 1000 requests
```

## Useful Variants

Filter to one scaffold:

```bash
python build_traj_agent_workload.py \
  --agent codex \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --tokenizer approx \
  --output traj_codex_100session_10step.jsonl
```

Use only successful trials:

```bash
python build_traj_agent_workload.py \
  --reward 1 \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --tokenizer approx \
  --output traj_reward1_100session_10step.jsonl
```

Include agent steps without tool calls:

```bash
python build_traj_agent_workload.py \
  --allow-agent-steps-without-tools \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --tokenizer approx \
  --output traj_agent_allsteps_100session_10step.jsonl
```

## Runner Usage

Agent-only:

```bash
python hypothesis_validation/agent_validation/run_trace_tau.py \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --phase agent_only_fixed \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode fixed \
  --agent-tool-gap-seconds 5 \
  --num-agent-prompts 1000 \
  --output hypothesis_validation/agent_validation/raw_results/traj_agent_only_fixed5_apc_on_len8192.jsonl
```

Chat + Agent:

```bash
python hypothesis_validation/agent_validation/run_mixed_tau.py \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --phase chat_agent_fixed \
  --chat-qps 5 \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode fixed \
  --agent-tool-gap-seconds 5 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --output hypothesis_validation/agent_validation/raw_results/chat_traj_agent_fixed5_apc_on_len8192.jsonl
```

Gaussian robustness run:

```bash
python hypothesis_validation/agent_validation/run_mixed_tau.py \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --phase chat_agent_gaussian \
  --chat-qps 5 \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode gaussian \
  --agent-tool-gap-mean 5 \
  --agent-tool-gap-std 2 \
  --agent-tool-gap-min 1 \
  --agent-tool-gap-max 15 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --output hypothesis_validation/agent_validation/raw_results/chat_traj_agent_gaussian_apc_on_len8192.jsonl
```

## Experiment Plan

No sweep is required.

Run only:

```text
1. Agent-only, APC on/off
2. Chat + Agent fixed 5s, APC on/off
3. Optional Chat + Agent Gaussian gap, APC on/off
```

Primary signal:

```text
agent -> chat useful eviction increases under mixed load
chat SLO/TTFT degrades under agent pressure if chat is not protected
agent self-reuse remains present inside each multi-step session
```
