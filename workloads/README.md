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
| SQuAD | [`squad/`](squad/) | Context 재사용성이 높은 RAG trace |
| ShareGPT | [`sharegpt/`](sharegpt/) | Multi-turn 대화 trace |

---

## 환경 준비

레포지터리 루트에서 의존성을 설치합니다.

```bash
uv pip install -r requirements.txt
```

---

## SQuAD

SQuAD `validation` split을 RAG 형태의 JSONL trace로 변환합니다.

동일한 `context`에 여러 `question`이 붙는 SQuAD 구조를 활용해, 같은 문단 prefix가 반복되는 RAG 요청을 생성합니다. RAG 요청의 prefix cache hit와 shared cache 점유가 scheduling 정책에 어떤 부하를 주는지 실험할 수 있습니다.

```text
Hugging Face SQuAD
        │
        ▼
build_rag_workload.py
        │
        ├── context · question 직렬화
        ├── system/user messages 생성
        └── 정답 · context hash 메타데이터 보존
        │
        ▼
workloads/squad/*.jsonl
```

### Trace 생성

```bash
cd workloads/squad
python build_rag_workload.py \
  --dataset-name rajpurkar/squad \
  --subset plain_text \
  --split validation \
  --num-requests 5000 \
  --system-prompt "You are a question-answering assistant. Answer the question using only the provided context. If the context does not contain enough evidence, say you do not know. Keep the answer concise." \
  --output squad_validation.jsonl
```

기본 dataset은 `rajpurkar/squad`, subset은 `plain_text`, split은 `validation`입니다.

생성되는 row는 Chat Completions용 `messages`를 주요 필드로 사용하고, completions fallback과 디버깅을 위해 `prompt`도 함께 저장합니다. 분석을 위해 `request_id`, `squad_id`, `title`, `question`, `answers`, `answer_starts`, `context_hash`, `context_char_len`, `prompt_char_len` 등을 함께 저장합니다.

> [!NOTE]
> **Prefix cache 최적화가 적용되어 있습니다.**
> 1. **Context 정규화** — context와 question 내부 공백을 정규화해 같은 문단이 항상 같은 prefix 문자열로 직렬화되게 합니다.
> 2. **요청 순서 정렬** — 생성된 trace 전체를 `(title, context)` 기준으로 정렬합니다. 같은 문단을 공유하는 요청이 연속으로 배치되어 vLLM 등의 KV cache 재사용 효과가 커집니다.

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

---

<div align="center">
<sub>SQuAD · ShareGPT · Workloads · JJ Distributed LLM Inference</sub>
</div>
