# RUN_STATIC — QuotaServe static 검증 실험

plan §8.4. victim은 Chat(sharegpt), antagonist는 Longctx(hotpotqa) 또는 Agent(traj).
static 정책은 고정 `quota_ratio → quota_w`와 strict `occupancy_w > quota_w`
victim selection을 사용한다.

```text
server: static/server_static.sh   <mix> <mode>   # vLLM 서버 (QUOTA_SERVE_MODE)
client: static/run_static.sh      <mix> <mode>   # 부하 클라이언트
  mix  = longctx | agent
  mode = off | static
```

QuotaServe static은 **서버측 env(`QUOTA_SERVE_MODE=static`)**로만 켜진다. 클라이언트
(`run_mixed_c2.py` / `run_mixed_agent_c2.py`)는 Case 1 runner를 복사·적응한 것으로,
동작은 동일하고 `--quota-mode`는 결과 파일명/메타데이터 라벨링에만 쓴다.

## 0. 전제

- vLLM fork에 QuotaServe lifecycle hook, request threading, block metadata,
  config schema가 들어가 있어야 한다.
  - request threading과 `KVCacheBlock` metadata 보완까지 들어간 상태를 기준으로 한다.
  - static collector/victim selection은 PR 3/4 소관이다. 그 전까지는 `off`로
    parity만 확인한다.
- workload trace는 JJ repo에 있다. `JJ_ROOT`로 경로를 지정한다.
  - 기본값: `~/JJ-distributed-LLM-inference`

## 1. 경로 설정 (필요 시)

```bash
export VLLM_DIR=~/vllm
export JJ_ROOT=~/JJ-distributed-LLM-inference
# QUOTA_SERVE_CONFIG 기본 = quotaserve/static/configs/quota_serve.yaml
```

고정 실행 기준:

```text
instance              = g5.xlarge
--max-model-len       = 8192
request max_tokens    = min(trace output_token_len, workload cap)
workload caps         = chat 691, rag 205, longctx 41, agent 1776
--agent-slo-ms        = 200
```

## 2. 실행 — 비교 3종 (각 mix마다)

각 실험은 **같은 mix를 server/client에 동일하게** 준다.

### 2.1 Chat + Longctx (§8.4.1)

```bash
# baseline (off)  — 터미널 A
./server_static.sh longctx off
# 터미널 B
./run_static.sh longctx off

# static          — 터미널 A (서버 재시작)
./server_static.sh longctx static
# 터미널 B
./run_static.sh longctx static
```

### 2.2 Chat + Agent (§8.4.2)

```bash
./server_static.sh agent off     # + ./run_static.sh agent off
./server_static.sh agent static  # + ./run_static.sh agent static
```

> Agent도 longctx와 동일하게 `--max-model-len 8192`로 뜬다(§2 고정값). KV-cache 압박이
> 크면 server_static.sh의 `--max-num-seqs`를 16으로 낮춘다.

## 3. 산출물

```text
static/
├── raw_results/
│   ├── c2_off_chat_longctx_apc_on.jsonl(+_summary.json)
│   ├── c2_static_chat_longctx_apc_on.jsonl
│   ├── c2_off_chat_agent_apc_on.jsonl
│   └── c2_static_chat_agent_apc_on.jsonl
└── eviction_logs/
    ├── eviction_<mode>_chat_<mix>.jsonl     # VLLM_EVICTION_LOG
    └── quota_<mode>_chat_<mix>.jsonl        # QUOTA_SERVE_LOG
```

`*_summary.json`에는 `quota_serve_mode` 필드가 들어가 어느 모드로 측정했는지 남는다.

## 4. 검증 기준 (plan §8.4)

**Chat + Longctx**
- `chat ← longctx` useful eviction ≥20% 감소
- `chat.hit_rate_mean` 증가
- `longctx.slo_attainment` ≤5%p 감소
- `over_quota_selected ratio`가 유의미하게 관측됨
- `fallback_no_over_quota ratio`가 과도하게 높지 않음
- counter drift / owner mismatch로 인한 RuntimeError 없음

**Chat + Agent**
- `chat ← agent` useful eviction ≥20% 감소
- `chat.hit_rate_mean` 증가
- `agent.slo_attainment` ≤5%p 감소
- **agent self-reuse 보존**: `agent ← agent` useful eviction이 baseline 대비 크게
  악화되지 않음
- agent/chat `quota_ratio`를 sweep하며 "Chat 보호 ↔ agent self-reuse 보존" 지점을 찾는다.
