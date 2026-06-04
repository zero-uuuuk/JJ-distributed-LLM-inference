<div align="center">

# HotpotQA Longctx Workload

**HotpotQA distractor를 3k 내외 longctx JSONL trace로 변환**

_Multi-hop QA · 10 Wikipedia paragraphs · Length-filtered longctx antagonist_

</div>

---

## 개요

HotpotQA(`hotpotqa/hotpot_qa`)의 distractor setting을 longctx workload JSONL trace로 변환합니다. `hypothesis_validation`의 Chat + Longctx mixed 실험에서 **RAG보다 긴 3k 내외 antagonist**로 사용합니다.

> [!IMPORTANT]
> 원본 데이터셋은 수정하지 않습니다. HotpotQA row의 10개 Wikipedia paragraph를 원본 순서대로 사용하고, 완성된 prompt의 tokenizer 기준 길이가 지정 band에 들어오는 row만 trace로 샘플링합니다.

HotpotQA distractor는 question마다 gold/supporting paragraph와 distractor paragraph가 함께 제공되는 multi-hop QA 데이터셋입니다. 따라서 임의로 문서를 합성하지 않고도 low-reuse multi-doc context를 만들 수 있습니다.

```text
Hugging Face HotpotQA
        │
        ▼
build_hotpotqa_workload.py
        │
        ├── question + 10개 Wikipedia paragraph를 원본 순서로 직렬화
        ├── prompt token 길이로 2000~4000 band 필터링
        └── answer/output 토큰 수와 분석 메타데이터 보존
        │
        ▼
workloads/hotpotqa/*.jsonl
```

---

## 환경 준비

레포지터리 루트에서 의존성을 설치합니다. (`prompt_token_len` / `output_token_len` 계산에 `transformers` tokenizer를 사용합니다)

```bash
uv pip install -r requirements.txt
```

---

## Trace 생성

```bash
cd workloads/hotpotqa
python build_hotpotqa_workload.py \
  --num-requests 1000 \
  --split train \
  --min-prompt-tokens 2000 \
  --max-prompt-tokens 4000 \
  --output hotpotqa_longctx_2000_4000.jsonl
```

### 주요 인자

| 인자 | 의미 |
|---|---|
| `--output` | 출력 JSONL 경로 |
| `--num-requests` | trace에 담을 요청 수. `0` 이하이면 필터를 통과한 전체 row를 사용합니다. |
| `--min-prompt-tokens` | prompt 토큰 하한. 기본은 `2000`입니다. |
| `--max-prompt-tokens` | prompt 토큰 상한. 기본은 `4000`입니다. |
| `--split` | 사용할 split. 기본은 `train`입니다. `validation`은 작은 sanity check용으로만 사용합니다. |
| `--tokenizer` | `prompt_token_len` / `output_token_len` 계산용 tokenizer. 기본은 run_mixed의 모델과 동일합니다. |
| `--max-output-tokens` | `output_token_len` 상한. 양수면 그 값으로 clamp해 과도하게 긴 decode를 막습니다. |

> [!NOTE]
> 기본 split/band에서 `--num-requests`를 채우지 못하면 빌더는 accepted/scanned/short/long/invalid 통계를 출력하고 종료합니다. 이 경우 원본을 바꾸지 말고 요청 수 또는 length band를 조정하세요.

---

## 출력 스키마

생성되는 row는 `messages`를 주요 입력 필드로 사용하며(`prompt`는 동일 내용의 fallback), 나머지는 분석을 위한 메타데이터입니다. `쓰임` 열의 ✅는 파이프라인/분석에서 실제로 쓰는 필드입니다.

| 필드 | 쓰임 | 의미 |
|---|:---:|---|
| `messages` | ✅ | 모델 입력 (instruction + Wikipedia context + question). 서버로 전송 |
| `prompt` | ✅ | `messages`와 동일 내용의 평문. messages가 없는 러너를 위한 fallback |
| `request_id` | ✅ | trace 요청 ID (`hotpotqa-longctx-{n}`). 결과 JSONL과 매칭하는 키 |
| `output_token_len` | ✅ | 정답 토큰 수. `run_mixed`가 max_tokens로 소비해 decode 길이를 현실화 |
| `output_text` | ✅ | HotpotQA 정답. 생성 결과 채점·참조용 |
| `prompt_token_len` | ✅ | prompt 토큰 수. length/pressure sweep과 band 필터 검증의 핵심 축 |
| `prompt_char_len` | ✅ | prompt 문자 길이. 보조 길이 지표 |
| `hotpotqa_id`, `question_type`, `level`, `num_contexts` | – | 원본 row 추적과 난이도/유형 분석용 메타데이터 |
| `source_dataset`, `cache_pattern` | – | workload 라벨 (`HotpotQA`, `longctx`) |

---

<div align="center">
<sub>HotpotQA · Longctx · Workloads · JJ Distributed LLM Inference</sub>
</div>
