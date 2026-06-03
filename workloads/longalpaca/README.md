<div align="center">

# LongAlpaca Long-Context Workload

**LongAlpaca-12k를 long-context 형태의 JSONL trace로 변환**

_Long-context · Drop-in · Length sweep_

</div>

---

## 개요

LongAlpaca-12k(`Yukang/LongAlpaca-12k`)를 long-context workload JSONL trace로 변환합니다. `hypothesis_validation`의 Chat + long-context mixed 실험에서 **저-reuse 대용량 antagonist**로 사용합니다.

> [!IMPORTANT]
> LongAlpaca-12k는 retriever나 공유 corpus 없이, 논문·책 본문 한 편을 읽고 답하는 **long-context QA**입니다.

기존 MS MARCO RAG는 passage가 짧아(평균 ~800 토큰) Chat과 체급이 비슷했고, 그래서 "대용량 workload가 신규 block을 쏟아내 Chat hot cache를 LRU에서 밀어낸다"는 가설 메커니즘을 약하게만 자극했습니다. LongAlpaca-12k는 요청 하나가 수천 토큰의 신규 block을 만들고, 행마다 본문이 달라 intra-reuse가 거의 0이라 이 메커니즘을 더 선명하게 자극합니다.

`instruction` 필드 하나가 이미 "긴 본문 + 질문"이 합쳐진 통짜 프롬프트라, **passage 이어붙이기 같은 가공 없이** 그대로 모델 입력으로 사용합니다.

```text
Hugging Face LongAlpaca-12k
        │
        ▼
build_longalpaca_workload.py
        │
        ├── instruction을 재가공 없이 messages / prompt로 사용
        ├── prompt 토큰 길이로 short Alpaca 제거 · length band 컷
        └── output 토큰 수 등 메타데이터 보존
        │
        ▼
workloads/longalpaca/*.jsonl
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
cd workloads/longalpaca
python build_longalpaca_workload.py \
  --num-requests 1000 \
  --min-prompt-tokens 3000 \
  --max-prompt-tokens 7000 \
  --output longalpaca_longctx.jsonl
```

### 주요 인자

| 인자 | 의미 |
|---|---|
| `--output` | 출력 JSONL 경로 (**필수**) |
| `--num-requests` | trace에 담을 요청 수. 필터를 통과한 원본 순서 기준으로 앞에서부터 이 수만큼 사용하고, 개수가 채워지면 로드를 멈춥니다. `0` 이하이면 통과한 전체를 사용합니다. |
| `--min-prompt-tokens` | prompt 토큰 하한. 기본은 `3000`입니다. |
| `--max-prompt-tokens` | prompt 토큰 상한. 기본은 `7000`입니다. |
| `--tokenizer` | `prompt_token_len` / `output_token_len` 계산용 tokenizer. 기본은 run_mixed의 모델과 동일합니다. |
| `--max-output-tokens` | `output_token_len` 상한. 양수면 그 값으로 clamp해 과도하게 긴 decode를 막습니다. |

> [!NOTE]
> 현재 LongAlpaca trace는 `3000~7000` prompt token band를 기본으로 사용합니다.

---

## 출력 스키마

생성되는 row는 `messages`를 주요 입력 필드로 사용하며(`prompt`는 동일 내용의 fallback), 나머지는 분석을 위한 메타데이터입니다. `쓰임` 열의 ✅는 파이프라인/분석에서 실제로 쓰는 필드입니다.

| 필드 | 쓰임 | 의미 |
|---|:---:|---|
| `messages` | ✅ | 모델 입력 (instruction 통짜 프롬프트). 서버로 전송 |
| `prompt` | ✅ | `messages`와 동일 내용의 평문. messages가 없는 러너를 위한 fallback |
| `request_id` | ✅ | trace 요청 ID. 결과 JSONL과 매칭하는 키 |
| `output_token_len` | ✅ | GT 답변 토큰 수. `run_mixed`가 max_tokens로 소비해 decode 길이를 현실화 |
| `output_text` | ✅ | 정답. 생성 결과 채점·참조용 |
| `prompt_token_len` | ✅ | prompt 토큰 수. length/pressure sweep과 band 필터 검증의 핵심 축 |
| `prompt_char_len` | ✅ | prompt 문자 길이. 보조 길이 지표 |
| `answer` | – | `output_text`와 동일 값 (빌더 관례상 중복 보존) |
| `source_dataset`, `cache_pattern` | – | workload 라벨 (`LongAlpaca-12k`, `longctx`) |

---

<div align="center">
<sub>LongAlpaca · Long-Context · Workloads · JJ Distributed LLM Inference</sub>
</div>
