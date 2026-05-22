<div align="center">

# Workloads

**LLM Serving 실험을 위한 JSONL trace 생성 도구 모음**

_JSONL trace · Prefix cache analysis · QuotaServe_

</div>

---

## 개요

이 디렉터리는 LLM serving 실험에 사용할 JSONL trace를 생성하는 워크로드 모음입니다.

| 워크로드 | 디렉터리 | 특징 |
|---|---|---|
| SQuAD | [`squad/`](squad/) | RAG 형태의 단일 요청 trace (mixed 실험용) |
| HotpotQA | [`hotpotqa/`](hotpotqa/) | RAG 형태의 단일 요청 trace |
| ShareGPT | [`sharegpt/`](sharegpt/) | Multi-turn 대화 trace |

---

## 환경 준비

레포지터리 루트에서 의존성을 설치합니다.

```bash
uv pip install -r requirements.txt
```

---

## SQuAD

SQuAD `validation` split을 RAG 형태의 JSONL trace로 변환합니다. `hypothesis_validation`의 Chat + RAG mixed 실험(5:5 등)에서 RAG workload로 사용합니다.

단일 passage `context`를 프롬프트에 넣고, 같은 context를 공유하는 질문이 연속되도록 정렬해 prefix cache locality를 높입니다.

```text
Hugging Face SQuAD
        │
        ▼
build_squad_workload.py
        │
        ├── context + question 직렬화
        ├── messages (chat API) · prompt (completions fallback) 생성
        └── 정답 · context_hash 메타데이터 보존
        │
        ▼
workloads/squad/*.jsonl
```

### Trace 생성

```bash
cd workloads/squad
python build_squad_workload.py \
  --dataset-name rajpurkar/squad \
  --subset plain_text \
  --split validation \
  --num-requests 5000 \
  --output squad_validation.jsonl
```

기본 dataset은 `rajpurkar/squad`, subset은 `plain_text`, split은 `validation`입니다.

생성되는 row는 `messages`를 주요 필드로 사용하고, 분석을 위해 `request_id`, `squad_id`, `question`, `answer`, `title`, `context_hash`, `context_char_len`, `prompt_char_len` 등을 함께 저장합니다.

> [!NOTE]
> **Prefix cache 최적화가 적용되어 있습니다.**
> trace 전체를 `title` → `context` → `question` 순으로 정렬해, 동일 passage를 공유하는 요청이 연속으로 배치됩니다.

---

## HotpotQA

HotpotQA `distractor` split을 RAG 형태의 JSONL trace로 변환합니다.

`context` 후보 문서를 이미 검색된 retrieved chunk처럼 프롬프트에 넣고, 질문과 정답 메타데이터를 함께 보존합니다. RAG 요청이 prefix cache와 scheduling 정책에 어떤 부하를 주는지 실험할 수 있습니다.

```text
Hugging Face HotpotQA
        │
        ▼
build_rag_workload.py
        │
        ├── context 후보 문서 직렬화
        ├── RAG prompt 생성
        └── 정답 · 난이도 · 문서 제목 메타데이터 보존
        │
        ▼
workloads/hotpotqa/*.jsonl
```

### Trace 생성

```bash
cd workloads/hotpotqa
python build_rag_workload.py \
  --dataset-name hotpotqa/hotpot_qa \
  --subset distractor \
  --split validation \
  --num-requests 5000 \
  --instruction "You are a RAG question-answering assistant. Use only the retrieved chunks to answer the question. If the chunks do not contain enough evidence, say you do not know. Keep the answer concise." \
  --output hotpotqa_distractor_validation.jsonl
```

기본 dataset은 `hotpotqa/hotpot_qa`, subset은 `distractor`, split은 `validation`입니다.

생성되는 row는 `prompt`를 주요 필드로 사용하고, 분석을 위해 `request_id`, `hotpot_id`, `question`, `answer`, `level`, `type`, `context_titles`, `prompt_char_len` 등을 함께 저장합니다.

> [!NOTE]
> **Prefix cache 최적화가 적용되어 있습니다.**
> 1. **청크 순서 정규화** — 각 요청의 retrieved chunk를 title 기준으로 정렬합니다. 동일한 문서 조합이면 항상 같은 context 문자열이 생성되어, 문서 순서가 달랐던 요청 사이에도 prefix cache hit가 발생합니다.
> 2. **요청 순서 정렬** — 생성된 trace 전체를 context 내용 기준으로 정렬합니다. 같거나 유사한 문서를 공유하는 요청이 연속으로 배치되어 vLLM 등의 KV cache 재사용 효과가 극대화됩니다.

---

## ShareGPT

ShareGPT 원시 데이터를 multi-turn JSONL trace로 변환합니다.

`human`과 `gpt`가 교대하는 clean 대화만 선택하고, 각 turn을 이전 대화 이력을 포함한 요청으로 펼칩니다. Multi-turn 요청이 prefix cache locality에 어떤 영향을 주는지 분석할 수 있습니다.

```text
Hugging Face ShareGPT
        │
        ▼
build_sharegpt_workload.py
        │
        ├── 원시 JSON 다운로드
        ├── clean 대화 필터링
        └── turn 단위 요청 생성
        │
        ▼
workloads/sharegpt/*.jsonl
```

### Trace 생성

```bash
cd workloads/sharegpt
python build_sharegpt_workload.py \
  --repo-id anon8231489123/ShareGPT_Vicuna_unfiltered \
  --filename ShareGPT_V3_unfiltered_cleaned_split.json \
  --repo-type dataset \
  --num-conversations 5000 \
  --output sharegpt_conversation.jsonl
```

기본 repo는 `anon8231489123/ShareGPT_Vicuna_unfiltered`, 파일명은 `ShareGPT_V3_unfiltered_cleaned_split.json`입니다.

생성되는 row는 `messages`를 주요 필드로 사용하고, 분석을 위해 `request_id`, `conversation_id`, `turn_id`, `output_text`, `source_dataset`, `cache_pattern` 등을 함께 저장합니다.

기본 저장 순서는 `turn-major`입니다. 즉 모든 conversation의 turn 1을 먼저 저장한 뒤, turn 2, turn 3 순서로 저장합니다.

```text
conv1_turn1
conv2_turn1
...
convN_turn1
conv1_turn2
conv2_turn2
...
convN_turn2
```

따라서 `run_mixed.py --num-chat-prompts 920`처럼 앞부분만 잘라 실행하면, `--num-conversations`가 920보다 큰 경우 turn 1만 전송될 수 있습니다.

### Victim Trace 생성

cache pollution 실험에서 Chat을 victim workload로 만들려면 같은 `conversation_id`가 여러 turn에 걸쳐 반복 등장해야 합니다. 이를 위해 turn 수가 충분한 conversation만 고르고, turn 9까지만 사용해 작은 victim trace를 만듭니다.

```bash
cd workloads/sharegpt
python build_sharegpt_workload.py \
  --repo-id anon8231489123/ShareGPT_Vicuna_unfiltered \
  --filename ShareGPT_V3_unfiltered_cleaned_split.json \
  --repo-type dataset \
  --num-conversations 100 \
  --min-turns 9 \
  --max-turns 9 \
  --order turn-major \
  --output sharegpt_turn_major_100conv_9turn.jsonl
```

이 trace는 최대 다음 구조를 가집니다.

```text
100 conversations × 9 turns = 900 requests
```

Mixed 실험에서는 다음처럼 앞 900개를 사용하면 turn 1부터 turn 9까지 같은 conversation들이 반복되어, delayed prefix reuse를 관찰할 수 있습니다.

```bash
python run_mixed.py \
  --chat-trace ../workloads/sharegpt/sharegpt_turn_major_100conv_9turn.jsonl \
  --rag-trace ../workloads/squad/squad_validation.jsonl \
  --chat-qps 10.0 \
  --rag-qps 10.0 \
  --max-concurrency 64 \
  --num-chat-prompts 900 \
  --num-rag-prompts 900 \
  --output results/mixed_5_5_victim_chat_squad.jsonl
```

이 구성의 목적은 production traffic을 그대로 모사하는 것이 아니라, `turn 1`에서 cache에 올라간 Chat prefix가 `turn 2`~`turn 9`에서 다시 필요해지는 victim 구조를 명확히 만드는 것입니다.

---

<div align="center">
<sub>SQuAD · HotpotQA · ShareGPT · Workloads · JJ Distributed LLM Inference</sub>
</div>
