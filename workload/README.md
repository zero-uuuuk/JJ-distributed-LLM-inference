# workload — QuotaServe 실험용 trace 생성기

## 목적

Case 2 / QuotaServe 실험에 쓰는 workload **trace를 생성**하는 builder 모음이다.
각 builder는 외부 데이터셋(Hugging Face 등)을 받아 OpenAI Chat 형식의 JSONL
trace로 변환한다. `static/`의 runner(`run_mixed_c2.py` / `run_mixed_agent_c2.py`)가
이 trace를 읽어 부하를 생성한다.

> 이 builder들은 JJ repo(`JJ-distributed-LLM-inference/workloads/`)에서 복사해온
> 것이다. 원본과 동일하며, QuotaServe 워크로드(victim/antagonist) 역할 구분의
> 입력이 된다(plan §1, DESIGN §3).

## 파일 구조

```text
workload/
├── sharegpt/                       # chat (victim) — multi-turn, delayed reuse
│   ├── build_sharegpt_workload.py
│   └── README.md
├── msmarco/                        # rag (antagonist) — low intra-reuse, 대용량
│   ├── build_msmarco_rag_workload.py
│   └── README.md
├── traj/                           # agent (warm antagonist) — delayed self-reuse
│   ├── build_traj_agent_workload.py
│   └── README.md
└── hotpotqa/                       # longctx (cold antagonist) — prefill-heavy, low-reuse
    ├── build_hotpotqa_workload.py
    └── README.md
```

각 workload의 역할(victim / antagonist)과 신호(피해 / 낭비)는 `docs/QUOTASERVE_DESIGN.md`
§3·§4를 참고. static의 두 실험은 victim=chat, antagonist=longctx(§8.4.1) 또는
agent(§8.4.2)를 쓴다. rag(msmarco)는 config(§5.1)에 quota가 정의된 추가 antagonist다.

## 의존성

- 공통: Python 표준 라이브러리(argparse/json/re/pathlib)
- `sharegpt`: `huggingface_hub` (`pip install huggingface-hub`)
- `msmarco`, `traj`, `hotpotqa`: `datasets` (`pip install datasets`)
- gated 데이터셋이면 `huggingface-cli login` 필요

## 생성 명령 (plan §1 / RUN_MIXED 기준)

`--output`은 모든 builder에서 필수다. 아래는 Case 2에서 쓰는 표준 trace.

### chat (ShareGPT, victim)

```bash
python sharegpt/build_sharegpt_workload.py \
  --num-conversations 100 \
  --min-turns 10 --max-turns 10 \
  --order turn-major \
  --output sharegpt/sharegpt_victim_100conv_10turn.jsonl
```

### rag (MS MARCO, antagonist)

```bash
python msmarco/build_msmarco_rag_workload.py \
  --output msmarco/msmarco_v21_validation.jsonl
```

### agent (Terminal-Bench trajectories, warm antagonist)

```bash
python traj/build_traj_agent_workload.py \
  --dataset-id yoonholee/terminalbench-trajectories \
  --config default --split train \
  --num-sessions 100 --max-steps 10 --min-steps 10 \
  --tokenizer approx \
  --output traj/traj_agent_100session_10step.jsonl
```

### longctx (HotpotQA distractor, cold antagonist)

```bash
python hotpotqa/build_hotpotqa_workload.py \
  --output hotpotqa/hotpotqa_longctx_2000_4000.jsonl
```

각 trace는 1000 requests 규모다. 자세한 옵션은 각 폴더의 `README.md` 참고.

## runner와의 연결

runner는 기본적으로 `JJ_ROOT/workloads/...`에서 trace를 찾는다(`--workloads-root`).
이 폴더에서 생성한 trace를 쓰려면 runner에 `--workloads-root`를 이 경로로 주거나,
`--chat-trace` / `--agent-trace` / `--longctx-trace`로 직접 지정한다.
