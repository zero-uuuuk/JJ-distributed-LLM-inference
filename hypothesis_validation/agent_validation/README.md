# Agent Validation

Terminal-Bench trajectory 기반 Agent workload 실험 폴더입니다.

이제 아래 runner 이름을 사용합니다.

```text
run_trace_agent.py  -> Agent-only
run_mixed_agent.py  -> Chat + Agent mixed
```

기본 Agent trace:

```text
workloads/traj/traj_agent_100session_10step.jsonl
```

기본 Chat trace:

```text
workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl
```

## 실험 구성

Sweep 없이 아래만 실행합니다.

```text
1. Agent-only APC ON/OFF
2. Chat + Agent APC ON/OFF
```

Agent scheduler는 session 단위입니다.

```text
step1: session arrival
step2..10: previous finish_time + tool_gap
```

기본 fixed gap은 2s입니다. Agent main sanity는 exponential gap을 권장합니다.

```python
tool_gap = min(random.expovariate(1 / 2.0), 20.0)
```

## 문서

```text
RUN_TRACE.md  -> Agent-only 실행 방법
RUN_MIXED.md  -> Chat + Agent mixed 실행 방법
```

## Token 분석 그림

Trajectory prompt token 그림은 아래 스크립트로 만듭니다.

```bash
python hypothesis_validation/agent_validation/plot_traj_prompt_tokens.py \
  --trace workloads/traj/traj_agent_100session_10step.jsonl \
  --output-dir hypothesis_validation/agent_validation/token_analysis \
  --tokenizer approx \
  --workload-label "Agent trajectory"
```
