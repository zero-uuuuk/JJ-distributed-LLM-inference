# RUN_MIXED

Chat + Agent mixed 실험용 runner입니다.

```text
runner: hypothesis_validation/agent_validation/run_mixed_agent.py
chat:   workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl
agent:  workloads/traj/traj_agent_100session_10step.jsonl
shape:  Chat 100 conv x 10 turns = 1000 requests
        Agent 100 sessions x 10 steps = 1000 requests
gap:    Agent previous finish_time + sampled tool gap
```

## 1. Trace 만들기

Chat trace:

```bash
python workloads/sharegpt/build_sharegpt_workload.py \
  --num-conversations 100 \
  --min-turns 10 \
  --max-turns 10 \
  --order turn-major \
  --output workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl
```

Agent trace:

```bash
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

## 2. Main: exponential 2s tool gap

Agent main sanity는 exponential gap을 사용합니다.

```python
tool_gap = min(random.expovariate(1 / 2.0), 20.0)
```

APC ON:

```bash
python hypothesis_validation/agent_validation/run_mixed_agent.py \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --phase chat_agent_exponential \
  --chat-qps 5 \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode exponential \
  --agent-tool-gap-mean 2 \
  --agent-tool-gap-max 20 \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 10000 \
  --output hypothesis_validation/agent_validation/raw_results/chat_traj_agent_exp2_cap20_apc_on_len16384.jsonl
```

APC OFF:

```bash
python hypothesis_validation/agent_validation/run_mixed_agent.py \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --phase chat_agent_exponential \
  --chat-qps 5 \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode exponential \
  --agent-tool-gap-mean 2 \
  --agent-tool-gap-max 20 \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 10000 \
  --output hypothesis_validation/agent_validation/raw_results/chat_traj_agent_exp2_cap20_apc_off_len16384.jsonl
```

## 3. Fixed 2s baseline

고정 gap을 쓰는 baseline은 2초로 둡니다.

```bash
python hypothesis_validation/agent_validation/run_mixed_agent.py \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --phase chat_agent_fixed \
  --chat-qps 5 \
  --agent-target-rps 5 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode fixed \
  --agent-tool-gap-seconds 2 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 10000 \
  --output hypothesis_validation/agent_validation/raw_results/chat_traj_agent_fixed2_apc_on_len16384.jsonl
```

## 4. Workload tags

Mixed runner는 OpenAI `user` field에 workload tag를 넣습니다.

```text
chat request  -> user="chat"
agent request -> user="agent"
```

eviction attribution이 켜져 있으면 이 tag를 기준으로 아래 방향을 볼 수 있습니다.

```text
chat <- agent
agent <- agent
agent <- chat
chat <- chat
```
