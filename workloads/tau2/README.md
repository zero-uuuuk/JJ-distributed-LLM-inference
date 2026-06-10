<div align="center">

# Tau2 Agent Workload

**tau2-bench를 agent multi-step JSONL trace로 변환하는 실험 계획**

_Agent session · Tool gap · Delayed prefix reuse_

</div>

---

## 개요

`sierra-research/tau2-bench` 데이터를 사용해 Agent workload를 만들기 위한 문서입니다. 이 README는 `hypothesis_validation`에서 Chat + Agent cache sanity check를 설계하기 위한 기준입니다.

tau2-bench는 고객 지원형 agent 평가 프레임워크입니다. 각 domain은 agent가 따라야 하는 policy, agent tools, tasks, 그리고 선택적으로 user tools를 포함합니다. 공식 repo의 사용 가능한 domain은 `mock`, `airline`, `retail`, `telecom`, `banking_knowledge`입니다.

Agent workload는 Chat workload와 비슷하게 multi-turn prefix reuse를 만들지만, turn gap의 의미가 다릅니다. Chat은 대화 turn 사이의 think-time을 흉내내기 위해 `turn-major` ordering을 사용합니다. Agent는 tool execution, environment wait, planning delay가 있으므로 같은 session의 다음 step은 단순 QPS producer가 아니라 이전 step 완료 후 tool gap을 기다린 뒤 들어와야 합니다.

```text
tau2-bench
        |
        v
build_tau2_agent_workload.py       (planned)
        |
        |- task / policy / tool schema 로드
        |- evaluation_criteria.actions를 reference step seed로 사용
        |- step별 prompt history 누적
        v
workloads/tau2/*.jsonl
        |
        v
run_agent_mixed.py                 (planned)
        |
        |- new session arrival: Poisson
        |- next step arrival: previous finish_time + tool_gap
        |- Chat workload와 동시 실행
```

---

## 데이터 사용 원칙

tau2-bench의 domain data는 일반적으로 아래 파일들을 가집니다.

| 파일 | 용도 |
|---|---|
| `tasks.json` | user scenario, initial state, evaluation criteria를 포함하는 task 목록 |
| `split_tasks.json` | `base` 등 task split 정의 |
| `policy.md` | agent가 따라야 하는 domain policy |
| `db.json` 또는 `db.toml` | agent environment database |
| `user_db.json` 또는 `user_db.toml` | user-side state가 필요한 domain의 user database |

중요한 점은 `evaluation_criteria.actions`를 해석하는 방식입니다. tau2-bench 문서 기준으로 이 field는 기본적으로 agent가 반드시 그대로 따라야 하는 유일한 path가 아니라, target DB end state를 만들기 위한 reference trajectory입니다. 이 workload에서는 correctness 평가가 아니라 cache sanity check가 목적이므로, `actions`를 현실적인 tool-call step seed로만 사용합니다.

즉, phase 1-3에서는 "agent가 정답을 맞히는가"가 아니라 다음 현상을 확인합니다.

```text
Agent -> Chat useful eviction: non-zero로 증가해야 함
Agent -> Agent useful eviction: Agent도 일부 reusable prefix를 가져야 함
정책 반응: chat floor는 보호되고, agent는 pressure와 reuse를 함께 가진 workload로 다뤄져야 함
```

---

## Workload 모델

### Agent session

하나의 tau2 task를 하나의 agent session으로 봅니다. 각 session은 최대 10개 step으로 잘라 사용합니다.

```text
agent_session_001:
  step 1: user task + policy + tool schema -> action
  [tool gap]
  step 2: user task + policy + tool schema + action1 + observation1 -> action
  [tool gap]
  step 3: user task + policy + tool schema + action1 + obs1 + action2 + obs2 -> action
  ...
```

Agent trace 기본 크기:

```text
100 sessions x 10 steps = 1000 requests
```

### Arrival 모델

목표 agent request rate가 5 rps이고 평균 10 steps/session이면 새 session start rate는 아래처럼 둡니다.

```text
new_agent_session_rps = target_agent_request_rps / avg_steps_per_session
                      = 5 / 10
                      = 0.5 sessions/s
```

`step1`은 session arrival process에서 들어옵니다.

```text
session inter-arrival ~ exponential(1 / 0.5)
```

`step2..10`은 단순 QPS producer가 아니라 이전 step의 실제 완료 시각에 종속됩니다.

```text
next_step_time = previous_step_finish_time + sampled_tool_gap
```

이 차이가 중요합니다. Agent trace를 미리 `step-major`로 정렬하고 기존 `qps=5` producer에 그대로 넣으면, 같은 session의 `step1 -> step2` gap은 대략 `100 / 5 = 20s`가 됩니다. 그러면 5초 tool gap 실험이 아니게 됩니다.

---

## Phase 계획

### Phase 1: Agent-only fixed 5s

목적은 Agent workload 자체가 원하는 delayed prefix reuse를 만드는지 확인하는 것입니다.

```text
workloads:
  agent only

agent:
  sessions = 100
  steps_per_session = 10
  target_request_rps = 5
  new_session_rps = 0.5
  tool_gap = fixed 5s
```

기대 결과:

```text
agent hit rate: step이 진행될수록 증가
agent TTFT: warm step 이후 안정화
mixed workload 관련 signal: 없음
```

### Mixed: Chat + Agent fixed 5s

목적은 Agent-heavy workload가 Chat의 reusable prefix를 밀어내는지 보는 것입니다.

```text
workloads:
  chat + agent

chat:
  conversations = 100
  turns_per_conversation = 10
  qps = 5
  source = workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl

agent:
  sessions = 100
  steps_per_session = 10
  target_request_rps = 5
  new_session_rps = 0.5
  tool_gap = fixed 5s
```

기대 결과:

```text
agent -> chat useful eviction: 증가
chat hit rate: Chat-only 대비 하락 가능
chat TTFT/SLO: Agent pressure가 강하면 악화 가능
agent hit rate: Agent session 내부에서 일부 reuse 유지
정책 반응: chat floor 보호, agent는 useful eviction 여부에 따라 cap/floor 조절
```

### Optional: Chat + Agent Gaussian tool gap

목적은 fixed 5s에서 본 신호가 tool gap variance에도 유지되는지 확인하는 단일 robustness check입니다. Sweep 실험은 아닙니다.

```text
workloads:
  chat + agent

agent:
  sessions = 100
  steps_per_session = 10
  target_request_rps = 5
  new_session_rps = 0.5
  tool_gap = truncated Gaussian

tool_gap:
  mean = 5s
  std = 2s
  clamp = 1s..15s
```

샘플링 식:

```python
tool_gap = max(1.0, min(random.gauss(5.0, 2.0), 15.0))
```

기대 결과:

```text
agent -> chat useful eviction: fixed gap과 같은 방향
agent -> agent useful eviction: Agent self-reuse 여부 확인
agent hit rate / TTFT: gap 분산 때문에 tail은 넓어질 수 있음
정책 반응: fixed gap과 같은 방향으로 유지
```

---

## 예정 Trace 생성

아직 구현 전 계획입니다. 실제 스크립트를 만들 때는 아래 형태를 기준으로 합니다.

```bash
cd workloads/tau2
python build_tau2_agent_workload.py \
  --repo-id sierra-research/tau2-bench \
  --domain telecom \
  --split base \
  --num-sessions 100 \
  --max-steps 10 \
  --output tau2_agent_100session_10step.jsonl
```

### 주요 인자

| 인자 | 의미 |
|---|---|
| `--repo-id` | tau2-bench source repo. 기본값은 `sierra-research/tau2-bench` |
| `--domain` | 사용할 tau2 domain. sanity check 기본 후보는 `telecom` |
| `--split` | task split. 기본값은 `base` |
| `--num-sessions` | 사용할 task/session 수 |
| `--max-steps` | session별 최대 step 수 |
| `--tokenizer` | `output_token_len` 계산용 tokenizer |
| `--max-output-tokens` | action decode length 상한 |
| `--output` | 출력 JSONL 경로 |

---

## 예정 Runner

Agent runner는 기존 단순 producer와 분리하는 편이 안전합니다.

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
  --output hypothesis_validation/case1_validation/raw_results/chat_agent_fixed5_apc_on_len8192.jsonl
```

Gaussian phase 예시:

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
  --output hypothesis_validation/case1_validation/raw_results/chat_agent_gaussian_gap.jsonl
```

---

## 출력 스키마

생성되는 row는 `messages`를 주요 입력으로 사용하고, 나머지는 scheduling 및 분석용 metadata로 둡니다.

| 필드 | 게임 | 의미 |
|---|:---:|---|
| `messages` | O | 모델 입력. policy + tool schema + user task + 누적 action/observation history |
| `output_text` | O | reference action JSON 또는 tool-call text. decode length 산정용 |
| `output_token_len` | O | `run_agent_mixed.py`가 `max_tokens`로 사용할 planned decode 길이 |
| `request_id` | O | `{session_id}_step_{n}` 형태의 request ID |
| `session_id` | O | agent session/task ID |
| `step_id` | O | session 내부 step 번호 |
| `domain` | O | tau2 domain (`telecom`, `airline`, `retail` 등) |
| `task_id` | O | tau2 task ID |
| `tool_name` | O | reference action의 tool name |
| `tool_arguments` | O | reference action arguments |
| `tool_gap_seconds` | O | runner가 sampling한 step 간 gap |
| `source_dataset` | O | `tau2-bench` |
| `cache_pattern` | O | `agent_multi_step` |

---

## Sanity Check 판정 기준

Phase 1은 Agent trace와 scheduler 자체의 검증입니다. Mixed는 Agent-heavy workload가 Chat prefix를 밀어내는지, 그리고 policy가 그 신호에 맞게 움직이는지 확인합니다.

| 신호 | 기대 |
|---|---|
| `agent -> chat useful eviction` | Mixed에서 non-zero로 증가 |
| `agent -> agent useful eviction` | Agent도 일부 reusable prefix를 가져야 함 |
| `chat hit_rate` | Chat-only 대비 하락 가능 |
| `chat TTFT p95/p99` | Agent 혼합 후 증가 가능 |
| `chat floor` | Chat 보호를 위해 상승하는 방향 |
| `agent cap/floor` | Agent self-reuse와 pressure 신호에 따라 조절 |

---

## 참고 자료

- tau2-bench repo: https://github.com/sierra-research/tau2-bench
- tau2 domain data structure: https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/domains/README.md
- tau2 task schema and evaluation: https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md

---

<div align="center">
<sub>tau2-bench · Agent · Workloads · JJ Distributed LLM Inference</sub>
</div>
