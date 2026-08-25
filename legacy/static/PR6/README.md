# PR6 실행 스크립트

PR6는 static quota sweep 결과로 `quota_ratio`와 useful eviction ratio의 profile을
만들고, `ratio_low/high`, `floor/cap` 후보를 해석하는 단계다. vLLM 코드는 바꾸지
않고, `static/quota_serve.yaml`의 workload별 `quota_ratio`만 바꿔가며 같은
Chat+Agent 부하를 반복 실행한다.

## 1. 공통 준비

<details>
<summary>실행 스크립트 보기</summary>

```bash
export VLLM_DIR=/home/ubuntu/vllm
export JJ_ROOT=/home/ubuntu/JJ-distributed-LLM-inference

cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

mkdir -p $JJ_ROOT/static/PR6/raw_result
```

</details>

## 2. Sweep 기준값 확인

`static/quota_serve.yaml`에서 다음 값은 고정하고, `workloads.chat.quota_ratio`와
`workloads.agent.quota_ratio`만 합이 1이 되도록 바꿔가며 실행한다.

고정값:

```text
mode: "off"                  # 서버 실행 env QUOTA_SERVE_MODE=static으로 override
tick_sec: 30
shadow_ttl_sec: 120
window_size: 10000
quota_base_blocks: 1600
```

추천 sweep:

```text
chat0.1_agent0.9
chat0.2_agent0.8
chat0.3_agent0.7
chat0.4_agent0.6
chat0.5_agent0.5
chat0.6_agent0.4
chat0.7_agent0.3
chat0.8_agent0.2
chat0.9_agent0.1
```

## 3. Config 수정

각 run 전에 `static/quota_serve.yaml`에서 아래 두 값만 수정한다.

<details>
<summary>예시 config 보기</summary>

```yaml
workloads:
  chat:
    quota_ratio: 0.10 # 수정 부분 1
  rag:
    quota_ratio: 0.08
  longctx:
    quota_ratio: 0.10
  agent:
    quota_ratio: 0.90 # 수정 부분 2
```

</details>

서버 및 스크립트 실행 시 파일명을 변경한다.

```text
chat0.1_agent0.9
```

## 4. Static 서버 실행

이전 서버를 종료한 뒤 실행한다. 아래 예시는 `chat0.1_agent0.9` run이다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $VLLM_DIR
source $VLLM_DIR/.venv/bin/activate

VLLM_SERVER_DEV_MODE=1 \
QUOTA_SERVE_CONFIG=$JJ_ROOT/static/quota_serve.yaml \
QUOTA_SERVE_MODE=static \
VLLM_EVICTION_LOG=$JJ_ROOT/static/PR6/raw_result/pr6_eviction_chat0.1_agent0.9.jsonl \
QUOTA_SERVE_LOG=$JJ_ROOT/static/PR6/raw_result/pr6_signal_chat0.1_agent0.9.jsonl \
vllm serve meta-llama/Llama-3.2-3B-Instruct \
  --enable-prefix-caching \
  --enable-prompt-tokens-details \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.6 \
  --generation-config vllm \
  --port 8000
```

</details>

서버 로그에서 다음을 확인한다.

```text
QuotaServe config ... mode=static ... quota_base_blocks=1600
```

## 5. Static Chat+Agent 실행

<details>
<summary>실행 스크립트 보기</summary>

```bash
cd $JJ_ROOT
source $JJ_ROOT/.venv/bin/activate

python static/run_mixed_agent_c2.py \
  --quota-mode static_pr6_chat0.1_agent0.9 \
  --agent-trace workloads/traj/traj_agent_100session_10step.jsonl \
  --chat-trace workloads/sharegpt/sharegpt_victim_100conv_10turn.jsonl \
  --url http://127.0.0.1:8000/v1/chat/completions \
  --model meta-llama/Llama-3.2-3B-Instruct \
  --phase chat_agent_exponential \
  --chat-qps 5.0 \
  --agent-target-rps 5.0 \
  --agent-steps-per-session 10 \
  --agent-tool-gap-mode exponential \
  --agent-tool-gap-mean 2 \
  --agent-tool-gap-max 20 \
  --max-concurrency 32 \
  --num-chat-prompts 1000 \
  --num-agent-prompts 1000 \
  --chat-slo-ms 400 \
  --agent-slo-ms 200 \
  --output static/PR6/raw_result/pr6_chat0.1_agent0.9.jsonl
```

</details>

## 6. Static log flush와 signal 확인

실험이 끝난 뒤 서버가 살아 있는 상태에서 실행한다.

<details>
<summary>실행 스크립트 보기</summary>

```bash
curl -X POST http://127.0.0.1:8000/flush_eviction_log

grep '"type":"useful_eviction_signal"' \
  $JJ_ROOT/static/PR6/raw_result/pr6_signal_chat0.1_agent0.9.jsonl | tail
```

</details>

서버 터미널에서 `Ctrl+C`로 종료한다.

## 7. 다음 ratio 실행

`static/quota_serve.yaml`에서 ratio를 바꾸고, 4~6번을 반복한다.

예시:

```text
chat0.1_agent0.9
chat0.2_agent0.8
chat0.3_agent0.7
chat0.4_agent0.6
chat0.5_agent0.5
chat0.6_agent0.4
chat0.7_agent0.3
chat0.8_agent0.2
chat0.9_agent0.1
```

## 8. 기록할 값

각 run마다 다음 값을 남긴다.

```text
chat quota_ratio
agent quota_ratio
chat useful_eviction_ratio
agent useful_eviction_ratio
chat hit_rate_mean
agent hit_rate_mean
over_quota_selected count
fallback_no_over_quota count
chat <- agent useful eviction
agent <- chat useful eviction
```

PR6에서는 이 값으로 profile curve를 만들고 `ratio_low/high`, `floor/cap` 후보를
해석한다. config에 넣거나 runtime quota를 조정하는 일은 PR7에서 한다.
