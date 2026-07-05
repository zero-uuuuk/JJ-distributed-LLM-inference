# static — QuotaServe static quota 검증 실험

## 목적

QuotaServe **static 모드**(고정 `quota_ratio → quota_w`, strict
`occupancy_w > quota_w` victim selection, plan §8)를 검증하는 실험 묶음이다.
victim은 Chat(sharegpt), antagonist는 Longctx(hotpotqa) 또는 Agent(traj)다.
같은 부하를 `off` / `static`으로 돌려, QuotaServe가 antagonist로부터 Chat의
hot prefix를 보호하는지(=`chat ← antagonist` useful eviction 감소, Chat hit
rate 회복)를 비교한다.

> QuotaServe static은 **서버측 env(`QUOTA_SERVE_MODE=static`)**로만 켜진다.
> 클라이언트 runner는 Case 1 runner를 복사·적응한 것이라 동작은 동일하고,
> `--quota-mode`는 결과 파일명/메타데이터 라벨링에만 쓴다. 즉 "정책"은 vLLM
> 서버 안에 있고, 이 폴더는 그 서버에 부하를 주고 결과를 모으는 **실험 하니스**다.

## 파일 구조

```text
static/
├── README.md                 # 이 문서
├── RUN_STATIC.md             # 실행 절차 + 검증 기준 (먼저 읽을 것)
├── server_static.sh          # vLLM 서버 런처 (QUOTA_SERVE_MODE / mix별 context len)
├── run_static.sh             # 클라이언트 런처 (mix/mode 인자로 아래 runner 호출)
├── run_mixed_c2.py           # Chat + Longctx 클라이언트 (§8.4.1, case1 복사·적응)
├── run_mixed_agent_c2.py     # Chat + Agent   클라이언트 (§8.4.2, agent 복사·적응)
├── raw_results/              # (생성됨) c2_<mode>_chat_<mix>_apc_on.jsonl + _summary.json
└── eviction_logs/            # (생성됨) eviction_/quota_<mode>_chat_<mix>.jsonl
```

| 파일 | 역할 |
|---|---|
| `RUN_STATIC.md` | 서버/클라이언트 실행 순서, 비교 2종, 검증 통과 기준 |
| `server_static.sh` | `./server_static.sh <mix> <mode>` — vLLM 서버 기동. max-model-len=8192 (§2 고정값) |
| `run_static.sh` | `./run_static.sh <mix> <mode>` — 부하 클라이언트 실행 |
| `run_mixed_c2.py` | Chat+Longctx(또는 RAG) mixed. `--longctx-trace`로 longctx 선택 |
| `run_mixed_agent_c2.py` | Chat+Agent mixed. 세션/tool-gap 스케줄링으로 delayed self-reuse 재현 |

`mix = longctx | agent`, `mode = off | static`. server와 client에 **같은 mix/mode**를 준다.

## 빠른 실행

```bash
# 터미널 A (서버)
./server_static.sh longctx static
# 터미널 B (클라이언트)
./run_static.sh   longctx static
```

자세한 절차·검증 기준은 [RUN_STATIC.md](./RUN_STATIC.md).

## 산출물

- `raw_results/c2_<mode>_chat_<mix>_apc_on.jsonl` — 요청별 raw 측정(TTFT/hit rate/…)
- `raw_results/..._summary.json` — 집계(+ 어느 모드로 측정했는지 `quota_serve_mode`)
- `eviction_logs/` — 서버측 eviction/quota 로그(`VLLM_EVICTION_LOG`, `QUOTA_SERVE_LOG`)

## 전제

- vLLM fork에 QuotaServe lifecycle hook, request threading, block metadata,
  config schema가 들어가 있어야 한다.
- static 정책 collector/victim selection은 PR 3/4 소관이다. 그 전엔 `off`로
  baseline parity만 확인한다.
- workload trace는 `../workloads/`에서 생성하거나 JJ repo에 있어야 한다
  (runner `--workloads-root`, 기본 `JJ_ROOT/workloads`). trace 생성은
  [../workloads/README.md](../workloads/README.md) 참고.

## 고정 실행 기준

- instance: `g5.xlarge`
- server `--max-model-len`: `8192`
- request `max_tokens`: `min(trace output_token_len, workload별 cap)`
  (`chat=691`, `rag=205`, `longctx=41`, `agent=1776`)
- `--agent-slo-ms`: `200`
