# Terminal-Bench Trajectory Agent Workload

This directory builds an Agent workload from
`yoonholee/terminalbench-trajectories`.

The target trace shape is:

```text
100 sessions x 10 steps = 1000 requests
```

One Terminal-Bench trajectory row becomes one Agent session. Each selected
`src == "agent"` step with tool calls becomes one model request.

## Why We Clip

The raw trajectory trace can contain very long terminal observations and system
context. In the first build, prompt tail was too long:

```text
prompt_char_len max ~= 53083
approx prompt token max ~= 14288
```

That made the Agent workload behave more like a long-context workload and forced
vLLM to use a very large `--max-model-len`.

For the cache sanity experiment, the builder now drops a whole session if any of
its selected 10 request prompts exceed:

```text
--max-prompt-chars 24000
```

This keeps the trace Agent-like while preserving:

```text
100 sessions x 10 steps = 1000 requests
```

Set `--max-prompt-chars 0` only if you explicitly want to disable this filter.

## Build

From the repo root:

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
max_prompt_chars: 24000
```

The build log also reports how many candidate sessions were skipped:

```text
overlong_session_rows: <count>
```

## Selection Rules

Defaults:

```text
one dataset row = one session
select only src == "agent" steps
select only agent steps that contain tool calls
drop leading warmup / ready messages
require min_steps == max_steps unless --min-steps is set
drop sessions with any selected prompt over --max-prompt-chars
shuffle source rows with seed 42
output order = step-major
```

`step-major` is only the JSONL storage order. The runner groups rows by
`session_id` and schedules each session as:

```text
step1 -> tool gap -> step2 -> ... -> step10
```

## Useful Variants

Use streaming:

```bash
python workloads/traj/build_traj_agent_workload.py \
  --streaming \
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

Use only successful trials:

```bash
python workloads/traj/build_traj_agent_workload.py \
  --reward 1 \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --max-prompt-chars 24000 \
  --tokenizer approx \
  --output workloads/traj/traj_reward1_100session_10step.jsonl
```

## Runner Usage

QuotaServe Case 2 static의 Chat + Agent 실험은 `quotaserve/static` runner로 실행한다.
서버는 `--max-model-len 8192`, 클라이언트 요청은 `max_tokens =
min(output_token_len, 1776)`, agent SLO는 `200ms` 기준이다.

```bash
# terminal A
cd quotaserve/static
./server_static.sh agent off

# terminal B
cd quotaserve/static
./run_static.sh agent off
```

Static mode:

```bash
# terminal A
cd quotaserve/static
./server_static.sh agent static

# terminal B
cd quotaserve/static
./run_static.sh agent static
```

직접 trace 경로를 지정하려면 `run_mixed_agent_c2.py --agent-trace` 또는
`--workloads-root`를 사용한다.
