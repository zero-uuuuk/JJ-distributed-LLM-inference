<div align="center">

# Hypothesis Validation

**Cache Pollution 가설 검증 실험 스크립트**

_Mixed workload · Prefix KV cache · SLO attainment_

</div>

---

## 개요

Mixed workload 환경에서 RAG(HotpotQA)의 prefix KV cache가 shared cache pool을 점유해 Chat(ShareGPT)의 reusable KV를 evict시키고, Chat의 TTFT · SLO attainment가 isolated 실행 대비 악화된다는 가설을 검증합니다.

가설 상세 및 실험 설계 → [`HYPOTHESIS.md`](HYPOTHESIS.md)

---

## 파일 구성

| 파일 | 설명 |
|---|---|
| `run_trace.py` | 단일 workload trace 전송 (isolated 실험용) |
| `run_mixed.py` | Chat + RAG 두 workload 동시 전송 (mixed 실험용) |
| `HYPOTHESIS.md` | 가설 정의 · 측정 지표 · 실험 케이스 설계 |

---

## 실험 케이스

| Case | 스크립트 | 설명 |
|---|---|---|
| Case 1: Chat-only | `run_trace.py --workload-tag chat` | baseline — Chat 단독 실행 |
| Case 2: RAG-only | `run_trace.py --workload-tag rag` | baseline — RAG 단독 실행 |
| Case 3: Mixed | `run_mixed.py` | Chat + RAG 동시 실행, pollution 측정 |

Case 1·2의 결과와 Case 3을 비교해 cache pollution을 정량화합니다.

---

## 사용법

vLLM이 `http://localhost:8000`에 서빙 중이라고 가정합니다.

### Case 1 — Chat isolated

```bash
python run_trace.py \
  --trace ../workloads/sharegpt/sharegpt_conversation.jsonl \
  --output results/chat_isolated.jsonl \
  --workload-tag chat \
  --qps 5.0 \
  --num-prompts 500
```

### Case 2 — RAG isolated

```bash
python run_trace.py \
  --trace ../workloads/hotpotqa/hotpotqa_distractor_validation.jsonl \
  --output results/rag_isolated.jsonl \
  --workload-tag rag \
  --qps 5.0 \
  --num-prompts 500
```

### Case 3 — Mixed (Chat + RAG)

```bash
python run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_conversation.jsonl \
  --rag-trace ../workloads/hotpotqa/hotpotqa_distractor_validation.jsonl \
  --chat-qps 5.0 \
  --rag-qps 5.0 \
  --output results/mixed.jsonl \
  --num-chat-prompts 500 \
  --num-rag-prompts 500
```

`--chat-qps` / `--rag-qps` 비율을 조절해 workload mix ratio sweep을 수행할 수 있습니다 (`hypothesis.md` §5.3 참고).

---

## 주요 출력 지표

결과 JSONL의 각 row에는 다음 필드가 포함됩니다.

| 필드 | 설명 |
|---|---|
| `workload` | `"chat"` 또는 `"rag"` |
| `ttft` | Time-to-First-Token (초) |
| `hit_rate` | 요청별 prefix cache hit rate |
| `h_r` / `u_r` | hit tokens / unhit tokens |
| `tpot` | Time-per-Output-Token (초) |
| `error` | 요청 실패 시 에러 메시지 |

콘솔 요약에는 workload별 **SLO attainment** (Chat ≤ 500ms, RAG ≤ 2000ms) 와 **cache hit rate** 가 출력됩니다.

---

<div align="center">
<sub>Cache Pollution · Hypothesis Validation · JJ Distributed LLM Inference</sub>
</div>
