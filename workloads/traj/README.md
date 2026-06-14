<div align="center">

# Terminal-Bench Trajectory Agent Workload

**Terminal-Bench trajectory를 multi-step Agent JSONL trace로 변환**

_Tool-use trajectory · Completion-based tool gap · Warm workload_

</div>

---

## 개요

Terminal-Bench trajectory 데이터(`yoonholee/terminalbench-trajectories`)를 multi-step Agent trace로 변환합니다. `hypothesis_validation`의 Chat + Agent mixed 실험에서 **Agent workload**로 사용합니다.

가설에서 Agent는 **warm·delayed-reuse workload** 역할입니다. 하나의 trajectory를 하나의 agent session으로 보고, tool call이 포함된 agent step을 요청 단위로 잘라 같은 session의 prefix가 이후 step에서 다시 쓰이도록 만듭니다. 이때 다음 step은 단순 QPS 순서가 아니라 이전 step 완료 뒤 tool gap을 거쳐 도착하므로, tool execution 또는 environment wait 때문에 생기는 delayed prefix reuse를 재현합니다.

```text
Hugging Face Terminal-Bench trajectories
        │
        ▼
build_traj_agent_workload.py
        │
        ├── trajectory row 로드 · session 후보 필터링
        ├── tool call이 있는 agent step 선택
        ├── step별 요청 생성(history 누적)
        └── GT agent step 토큰 수(output_token_len) 계산
        │
        ▼
workloads/traj/*.jsonl
```

---

## 환경 준비

레포지터리 루트에서 의존성을 설치합니다. (`output_token_len` 계산에는 기본적으로 serving 모델과 같은 `transformers` tokenizer를 사용합니다)

```bash
uv pip install -r requirements.txt
```

---

## Trace 생성

빌더는 아래 인자로 trace를 만듭니다. 실제 실행 명령은 [Agent Trace 생성](#agent-trace-생성)을 참고하세요.

### 주요 인자

| 인자 | 의미 |
|---|---|
| `--output` | 출력 JSONL 경로 (**필수**) |
| `--dataset-id` | Hugging Face dataset ID. 기본값은 `yoonholee/terminalbench-trajectories`입니다. |
| `--config` | dataset config. 기본값은 `default`입니다. |
| `--split` | 사용할 split. 기본값은 `train`입니다. |
| `--source-jsonl` | 로컬 JSONL export를 입력으로 사용합니다. 지정하면 Hugging Face 다운로드를 생략합니다. |
| `--num-sessions` | trace에 사용할 agent session 수. 기본 실험에서는 `100`을 사용합니다. |
| `--max-steps` | session별 최대 agent step 수. 기본 실험에서는 `10`으로 고정합니다. |
| `--min-steps` | session 후보가 가져야 하는 최소 step 수. 생략하면 `--max-steps`와 같게 맞춥니다. |
| `--order` | 요청 저장 순서. `step-major`(기본)는 같은 step 번호끼리 먼저 저장합니다. `session-major`는 session 단위로 이어 붙입니다. |
| `--allow-agent-steps-without-tools` | tool call이 없는 agent step도 요청으로 포함합니다. 기본값은 tool call이 있는 step만 선택합니다. |
| `--keep-warmup` | trajectory 앞쪽의 warmup/user-ready 메시지를 prompt에 유지합니다. 기본값은 leading warmup을 제거합니다. |
| `--reward` | Terminal-Bench reward 기준 필터입니다. `any`, `0`, `1` 중 하나를 사용합니다. |
| `--agent`, `--model`, `--task-name` | agent scaffold, source model, task name 기준으로 source row를 필터링합니다. |
| `--streaming` | Hugging Face streaming mode로 row를 읽습니다. |
| `--max-step-msg-chars` | 각 step message에서 유지할 최대 문자 수입니다. `0`이면 제한하지 않습니다. |
| `--max-observation-chars` | observation message에서 유지할 최대 문자 수입니다. `0`이면 제한하지 않습니다. |
| `--max-prompt-chars` | 선택된 요청 중 하나라도 prompt 총 문자 수가 이 값을 넘으면 session 전체를 버립니다. `0`이면 제한하지 않습니다. |
| `--tokenizer` | `output_token_len` 계산용 tokenizer. 기본값은 `meta-llama/Llama-3.2-3B-Instruct`입니다. |
| `--max-output-tokens` | `output_token_len` 상한. 양수면 그 값으로 clamp해 과도하게 긴 decode를 막습니다. |

> [!NOTE]
> **step-major는 저장 순서일 뿐입니다.**
> Agent runner는 JSONL row를 `session_id`로 다시 묶고, 각 session을 `step1 → tool gap → step2 → ... → step10` 형태로 스케줄링합니다. 따라서 trace 파일이 step-major로 저장되어도 실제 도착 과정은 session 내부 의존성을 유지합니다.

> [!TIP]
> **긴 terminal observation은 session 단위로 제한합니다.**
> 원본 trajectory에는 매우 긴 terminal observation과 system context가 포함될 수 있습니다. `--max-prompt-chars 24000`은 선택된 10개 요청 중 하나라도 prompt가 너무 길면 session 전체를 제외해, Agent workload가 Longctx workload처럼 변하는 것을 막습니다.

---

## 출력 스키마

생성되는 row는 `messages`를 주요 입력 필드로 사용합니다. `쓰임` 열의 ✅는 파이프라인/분석에서 실제로 쓰는 필드입니다.

| 필드 | 쓰임 | 의미 |
|---|:---:|---|
| `messages` | ✅ | 모델 입력. trajectory metadata, 이전 step history, 현재 next-action prompt를 포함해 서버로 전송 |
| `output_token_len` | ✅ | GT agent step 토큰 수. runner가 max_tokens로 소비해 decode 길이를 현실화 |
| `output_text` | ✅ | GT agent step. 생성 결과 채점·참조용 |
| `request_id` | ✅ | trace 요청 ID (`{session_id}_step_{n}`). 결과 매칭 키 |
| `session_id` | ✅ | agent session ID. 같은 trajectory의 step들을 묶어 delayed reuse 분석 |
| `task_id`, `trial_id`, `trial_name` | ✅ | Terminal-Bench 원본 task/trial 식별자. source row 추적용 |
| `step_id` | ✅ | session 내부 step 번호. warm/probe 구분 및 step별 hit rate 분석 |
| `session_step_count` | ✅ | 해당 session에서 선택된 총 step 수. 기본 trace에서는 `10` |
| `source_step_index` | – | 원본 trajectory 안에서 선택된 step의 index |
| `tool_name`, `tool_arguments` | ✅ | 해당 agent step의 첫 tool call 정보. tool-use step sanity check용 |
| `tool_gap_seconds` | – | trace 생성 시점에는 `null`. 실제 gap은 runner가 완료 시각 기준으로 샘플링 |
| `agent_scaffold`, `source_model`, `reward` | – | Terminal-Bench 원본 metadata. 필터링 및 사후 해석용 |
| `source_dataset`, `cache_pattern` | – | workload 라벨 (`yoonholee/terminalbench-trajectories`, `agent_multi_step`) |

---

## Agent Trace 생성

```bash
python workloads/traj/build_traj_agent_workload.py \
  --dataset-id yoonholee/terminalbench-trajectories \
  --config default \
  --split train \
  --num-sessions 100 \
  --max-steps 10 \
  --min-steps 10 \
  --max-prompt-chars 24000 \
  --output workloads/traj/traj_agent_100session_10step.jsonl
```

`100 sessions × 10 steps = 1,000 requests` 구조입니다. `--min-steps 10`과 `--max-steps 10`으로 session별 요청 수를 균일하게 맞추고, `--max-prompt-chars 24000`으로 지나치게 긴 terminal context를 가진 session을 제외합니다.

기본 선택 규칙은 `src == "agent"`이면서 tool call을 포함한 step만 요청으로 사용합니다. 따라서 생성된 trace는 단순 긴 prompt 모음이 아니라, 같은 session 안에서 prefix가 단계적으로 길어지고 tool gap 이후 다시 재사용되는 Agent cache sanity workload입니다.

빌드 로그에서는 아래 값들을 확인합니다.

```text
sessions: 100
requests: 1000
avg_steps_per_session: 10.00
overlong_session_rows: <count>
```

---

<div align="center">
<sub>Terminal-Bench · Agent · Workloads · JJ Distributed LLM Inference</sub>
</div>
