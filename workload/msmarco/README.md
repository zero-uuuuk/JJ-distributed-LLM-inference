<div align="center">

# MS MARCO RAG Workload

**MS MARCO를 RAG 형태의 JSONL trace로 변환**

_Real RAG · Shared corpus · Reuse dial_

</div>

---

## 개요

MS MARCO(`microsoft/ms_marco`)를 RAG workload JSONL trace로 변환합니다. `hypothesis_validation`의 Chat + RAG mixed 실험에서 **RAG workload**로 사용합니다.

MS MARCO는 query마다 web에서 이미 검색된 passages(~10개)가 들어있는 공식 데이터셋이라, 별도 retriever 없이 진짜 RAG trace를 만들 수 있습니다. 공유 corpus(web)에서 온 passage이므로 인기 문서는 여러 query에 반복 등장해 **현실적이고 통제 가능한 prefix reuse**를 가지면서, query마다 대체로 다른 다수 passage를 생산해 가설의 **저-reuse 대용량(antagonist)** 역할을 합니다.

```text
Hugging Face MS MARCO
        │
        ▼
build_msmarco_rag_workload.py
        │
        ├── query별 passages를 retrieved chunks로 직렬화
        ├── messages (chat API) · prompt (fallback) 생성
        └── 정답 · query_type · passage URL 등 메타데이터 보존
        │
        ▼
workloads/msmarco/*.jsonl
```

---

## 환경 준비

레포지터리 루트에서 의존성을 설치합니다. (`output_token_len` 계산에 `transformers` tokenizer를 사용합니다)

```bash
uv pip install -r requirements.txt
```

---

## Trace 생성

```bash
cd workloads/msmarco
python build_msmarco_rag_workload.py \
  --num-requests 1000 \
  --output msmarco_v21_validation.jsonl
```

### 주요 인자

| 인자 | 의미 |
|---|---|
| `--output` | 출력 JSONL 경로 (**필수**) |
| `--num-requests` | trace에 담을 요청 수. 유효한 요청을 이 수만큼 모으면 멈춥니다. `0` 이하이면 split 전체를 사용합니다. |
| `--top-k` | 요청당 사용할 passage 수입니다. 양수면 상위 k개만 사용해 RAG의 context 길이·부하(volume)를 줄이거나 키울 수 있습니다. |
| `--normalize-passage-order` | 요청 내 passage를 본문 기준으로 정렬합니다. 같은 passage 집합이면 항상 같은 context가 되어, 순서만 달랐던 요청 사이에도 prefix cache hit가 늘어납니다. |
| `--sort-by-context` | trace 전체를 context 기준으로 정렬합니다. 같거나 유사한 context를 공유하는 요청이 연속 배치되어 prefix cache 재사용이 극대화됩니다. |
| `--tokenizer` | `output_token_len` 계산용 tokenizer. 기본은 run_mixed의 모델과 동일합니다. gated 모델 접근이 안 되면 접근 가능한 tokenizer로 바꿔 지정하세요. |
| `--max-output-tokens` | `output_token_len` 상한. 양수면 그 값으로 clamp해 과도하게 긴 decode를 막습니다. |

> [!TIP]
> **Reuse dial.**
> 기본값은 MS MARCO retrieval 순서를 보존해 RAG의 intra-reuse를 낮게 유지합니다(antagonist 역할). RAG의 prefix reuse를 의도적으로 높이는 실험에서는 `--normalize-passage-order`와 `--sort-by-context`를 함께 켜서 dial up 합니다.

> [!NOTE]
> **답변 필터링과 decode 현실화.**
> 답이 없는 row(빈 답변·`No Answer Present.`)는 trace에서 제외합니다. 따라서 모든 요청이 실제 GT 답변을 가지며, `output_token_len`이 그 답변 토큰 수로 채워집니다. RAG 답변은 보통 짧으므로, 이로써 RAG가 **prefill-heavy + 짧은 decode**로 동작해 Chat(긴 decode)과의 prefill/decode 대비가 살아납니다. (answerable query만 남는 약한 selection bias가 있으나, prefill/decode 부하 측정에는 무방합니다.)

---

## 출력 스키마

생성되는 row는 `messages`를 주요 입력 필드로 사용하며(`prompt`는 동일 내용의 fallback), 나머지는 분석을 위한 메타데이터입니다. `쓰임` 열의 ✅는 파이프라인/분석에서 실제로 쓰는 필드입니다.

| 필드 | 쓰임 | 의미 |
|---|:---:|---|
| `messages` | ✅ | 모델 입력 (instruction + retrieved context + question). 서버로 전송 |
| `prompt` | ✅ | `messages`와 동일 내용의 평문. messages가 없는 러너를 위한 fallback |
| `request_id` | ✅ | trace 요청 ID. 결과 JSONL과 매칭하는 키 |
| `output_token_len` | ✅ | GT 답변 토큰 수. `run_mixed`가 max_tokens로 소비해 RAG의 짧은 decode를 현실화 |
| `output_text` | ✅ | 정답. 생성 결과 채점·참조용 |
| `context_char_len`, `prompt_char_len` | ✅ | RAG context·prompt 길이. length/pressure sweep 분석용 |
| `answer` | – | `output_text`와 동일 값 (RAG 빌더 관례상 중복 보존) |
| `question` | – | 원본 질문 평문 (디버깅·확인용) |
| `msmarco_query_id` | – | MS MARCO 원본 query_id (원본 추적용) |
| `query_type` | – | 질문 유형 (DESCRIPTION / NUMERIC / ENTITY) |
| `passage_urls`, `num_passages` | – | passage 출처 URL과 개수 |
| `selected_index` | – | 정답 근거(`is_selected==1`) passage 위치 (없으면 -1) |
| `top_k` | – | 생성 시 사용한 top-k 값 기록 |
| `source_dataset`, `cache_pattern` | – | workload 라벨 (`MS MARCO`, `rag`) |

---

<div align="center">
<sub>MS MARCO · RAG · Workloads · JJ Distributed LLM Inference</sub>
</div>
