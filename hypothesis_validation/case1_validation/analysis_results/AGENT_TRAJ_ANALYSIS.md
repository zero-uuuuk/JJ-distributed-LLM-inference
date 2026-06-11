# Agent Trajectory Analysis

이 문서는 Terminal-Bench trajectory 기반 Agent workload를 Case 1 분석에 넣을 때의 기준 문서다.
기존 `CASE1_ANALYSIS.md`는 Chat+RAG/Longctx 결과 해석이므로 보존하고, Chat+Agent 실험은 이 문서를 기준으로 따로 해석한다.

## 1. 데이터 sanity check

첨부 샘플 기준으로 trace row는 runner가 요구하는 필드를 갖고 있다.

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
tool_gap_seconds
source_dataset
cache_pattern
```

핵심 구조도 맞다.

```text
one Terminal-Bench trial = one agent session
one selected agent tool step = one request
session_step_count = 10
tool_gap_seconds = null in trace
actual tool gap = sampled by runner after previous step finishes
```

즉 `100 sessions x 10 steps`로 만들면:

```text
total agent requests = 1000
step1 requests = 100
follow-up requests = 900
```

tau2 `91 sessions / 123 requests`와 달리, follow-up step이 충분해서 delayed prefix reuse 실험에 맞다.

## 2. 샘플 데이터에서 주의할 점

Terminal-Bench trajectory에는 원래 agent scaffold의 system prompt, shell prompt, placeholder성 텍스트가 섞여 있다.
샘플에서도 `$31`, `$32`, `Retrieving content for: ...`, `Added workspace context` 같은 메시지가 보인다.

이것은 cache 실험을 깨는 문제는 아니다. 우리는 정답률을 평가하는 것이 아니라, 같은 session 안에서 prompt prefix가 단계적으로 길어지고 재사용되는지를 보는 것이 목적이다.

다만 해석할 때 다음처럼 표현한다.

```text
This is a replay-style cache workload, not a semantic Terminal-Bench evaluation.
```

즉 Agent가 실제로 task를 푸는지보다, multi-step tool-use transcript가 만드는 prefix reuse pressure가 중요하다.

## 3. 이번 분석 범위

Sweep은 하지 않는다.

필수 실험만 둔다.

```text
1. Agent-only, APC ON/OFF
2. Chat-only, APC ON/OFF
3. Chat+Agent mixed, APC ON/OFF
```

Gaussian tool gap은 본 분석의 기본 범위에서 제외한다. 필요하면 별도 robustness run으로만 기록한다.

## 4. 권장 raw result 파일명

`hypothesis_validation/agent_validation/raw_results/` 또는 기존 Case 1 폴더를 쓸 수 있지만, 분석 문서에서는 아래 이름을 기준으로 둔다.

```text
agent_only_fixed5_apc_on_len8192_summary.json
agent_only_fixed5_apc_off_len8192_summary.json

chat_only_apc_on_summary.json
chat_only_apc_off_summary.json

chat_traj_agent_fixed5_apc_on_len8192_summary.json
chat_traj_agent_fixed5_apc_off_len8192_summary.json
```

Eviction log가 있으면 아래 이름을 쓴다.

```text
eviction_logs/chat_traj_agent_fixed5_apc_on_len8192.jsonl
```

## 5. 봐야 하는 질문

### Q1. Agent-only에서 delayed prefix reuse가 보이는가?

Agent-only APC ON에서 step이 진행될수록 hit rate가 생겨야 한다.

확인할 것:

```text
agent hit_rate_mean
agent TTFT p50/p95
step_id별 hit rate 또는 TTFT
agent <- agent useful eviction
```

Agent-only에서 hit rate가 거의 없으면 trace가 prefix reuse를 충분히 만들지 못한 것이다.

### Q2. Chat+Agent mixed에서 Chat이 손해를 보는가?

Chat-only와 Chat+Agent mixed를 비교한다.

확인할 것:

```text
chat hit_rate_mean 감소
chat TTFT p50/p95 증가
chat SLO attainment 감소
APC_gain_mixed < APC_gain_single
```

여기서 APC gain은 기존 Case 1과 같은 방식으로 계산한다.

```text
APC_gain = TTFT(APC OFF) - TTFT(APC ON)
```

### Q3. Agent가 Chat hot cache를 직접 밀어내는가?

Eviction attribution에서 가장 중요한 방향은 아래다.

```text
chat <- agent
```

판정 기준:

```text
chat <- agent useful eviction count > 0
chat <- agent reused_later ratio가 높음
```

이 신호가 크면 Agent workload가 Chat hot cache를 실제로 밀어냈고, 그 block이 이후 Chat turn에서 다시 필요해졌다는 뜻이다.

### Q4. Agent self-reuse는 유지되는가?

Agent-heavy workload를 쓰는 이유는 Agent가 완전히 low-reuse garbage가 아니라, 자기 session 안에서는 재사용 가능한 prefix를 갖는다는 점이다.

확인할 방향:

```text
agent <- agent
```

해석:

```text
agent <- agent useful eviction이 존재하면 Agent 내부에도 delayed reusable prefix가 있다.
agent <- agent reused_later ratio가 높으면 Agent cache도 보호 대상이 될 수 있다.
```

## 6. 기존 RAG/Longctx 분석과 다른 점

기존 Case 1은 antagonist가 RAG 또는 Longctx였다.

```text
RAG: short request, low delayed reuse
Longctx: long prompt pressure, low self reuse
```

Trajectory Agent는 다르다.

```text
Agent: multi-step session, delayed self reuse 있음
```

따라서 단순히 "agent cap을 계속 낮춘다"로 해석하면 안 된다. Agent가 Chat을 밀어내는 위험 신호와 Agent 자기 prefix를 다시 쓰는 보호 신호를 같이 봐야 한다.

정책적으로 보고 싶은 방향은 아래다.

```text
chat <- agent useful eviction: Chat 보호를 위해 줄여야 하는 위험 신호
agent <- agent useful eviction: Agent도 무작정 버리면 안 된다는 보호 신호
```

## 7. 결과 표 템플릿

실험이 끝나면 아래 표를 채운다.

| Scope | Workload | APC | Hit rate mean | TTFT mean | TTFT p50 | TTFT p95 | SLO attainment |
|---|---|---|---:|---:|---:|---:|---:|
| single | chat | ON | TBD | TBD | TBD | TBD | TBD |
| single | chat | OFF | - | TBD | TBD | TBD | TBD |
| single | agent | ON | TBD | TBD | TBD | TBD | TBD |
| single | agent | OFF | - | TBD | TBD | TBD | TBD |
| mixed | chat | ON | TBD | TBD | TBD | TBD | TBD |
| mixed | chat | OFF | - | TBD | TBD | TBD | TBD |
| mixed | agent | ON | TBD | TBD | TBD | TBD | TBD |
| mixed | agent | OFF | - | TBD | TBD | TBD | TBD |

Eviction breakdown:

| Direction | Count | Reused later | Reused ratio | Interpretation |
|---|---:|---:|---:|---|
| `chat <- chat` | TBD | TBD | TBD | Chat self eviction |
| `chat <- agent` | TBD | TBD | TBD | Agent evicts reusable Chat cache |
| `agent <- chat` | TBD | TBD | TBD | Chat evicts reusable Agent cache |
| `agent <- agent` | TBD | TBD | TBD | Agent self churn or reusable Agent prefix loss |

## 8. Expected conclusion shape

결과가 기대대로 나오면 결론은 다음 형태가 된다.

```text
Terminal-Bench trajectory Agent workload creates a real delayed-reuse pressure:
Agent-only shows self prefix reuse, and Chat+Agent mixed reduces Chat cache hit
rate / TTFT / SLO compared with Chat-only. Eviction attribution confirms that
some Chat blocks are evicted by Agent and later reused.
```

만약 `chat <- agent` useful eviction은 큰데 `agent <- agent` reuse가 거의 없으면, Agent는 Longctx와 비슷한 pressure workload에 가깝다.

반대로 `agent <- agent` reuse가 크면, Agent는 단순히 버릴 workload가 아니라 Chat과 함께 보호 정책을 설계해야 하는 workload다.
