# RUN_STATIC — QuotaServe static 검증 실험

plan §8.4. victim은 Chat(sharegpt), antagonist는 Longctx(hotpotqa) 또는 Agent(traj).

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

- vLLM fork에 QuotaServe hook(PR 0) + config(PR 1)가 들어가 있어야 한다.
  - PR 1 단계에서는 `static`이 아직 서버에서 NotImplementedError를 낼 수 있다
    (전용 collector는 PR 3+). 그 전까지는 `off`로 parity만 확인한다.
- workload trace는 JJ repo에 있다. `JJ_ROOT`로 경로를 지정한다.
  - 기본값: `~/JJ-distributed-LLM-inference`

## 1. 경로 설정 (필요 시)

```bash
export VLLM_DIR=~/vllm
export JJ_ROOT=~/JJ-distributed-LLM-inference
# QUOTA_SERVE_CONFIG 기본 = $VLLM_DIR/vllm/quota_serve/quota_serve.yaml
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

> Agent는 `--max-model-len 12288`로 뜬다. KV-cache 압박이 크면 server_static.sh의
> `--max-num-seqs`를 16으로 낮춘다.

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
- `quota_protected_but_evicted` 비율 ≤ 임계(예 10%)

**Chat + Agent**
- `chat ← agent` useful eviction ≥20% 감소
- `chat.hit_rate_mean` 증가
- `agent.slo_attainment` ≤5%p 감소
- **agent self-reuse 보존**: `agent ← agent` useful eviction이 baseline 대비 크게
  악화되지 않음 (cap이 agent의 delayed self-reuse까지 깎으면 실패 신호)
- agent `cap_ratio`를 sweep하며 "Chat 보호 ↔ agent self-reuse 보존" 지점을 찾는다.
