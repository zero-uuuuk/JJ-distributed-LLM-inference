# RUN_MIXED

Chat + Agent mixed runner.

```text
runner: hypothesis_validation/agent_validation/run_mixed_agent.py
chat:   workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl
agent:  workloads/traj/traj_agent_100session_10step.jsonl
shape:  Chat 100 conv x 10 turns = 1000 requests
        Agent 100 sessions x 10 steps = 1000 requests
gap:    Agent previous finish_time + sampled tool gap
```

## Build Traces

Chat trace:

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
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
  --max-prompt-chars 24000 \
  --tokenizer approx \
  --output workloads/traj/traj_agent_100session_10step.jsonl
```

## Start vLLM: APC ON

```bash
cd /home/ubuntu/vllm
source .venv/bin/activate
mkdir -p /home/ubuntu/vllm/eviction_logs

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

If KV-cache pressure appears, lower `--max-num-seqs` to `16`.

## Run Mixed: APC ON

Main gap:

```python
tool_gap = min(random.expovariate(1 / 2.0), 20.0)
```

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
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
  --output hypothesis_validation/agent_validation/raw_results/chat_traj_agent_exp2_cap20_apc_on_len12288.jsonl
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

## Run Mixed: APC OFF

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference
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
  --output hypothesis_validation/agent_validation/raw_results/chat_traj_agent_exp2_cap20_apc_off_len12288.jsonl
```

## Fixed 2s Baseline

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
  --output hypothesis_validation/agent_validation/raw_results/chat_traj_agent_fixed2_apc_on_len12288.jsonl
```

## Workload Tags

The mixed runner sets the OpenAI `user` field:

```text
chat request  -> user="chat"
agent request -> user="agent"
```

Eviction attribution can then use these directions:

```text
chat <- agent
agent <- agent
agent <- chat
chat <- chat
```
