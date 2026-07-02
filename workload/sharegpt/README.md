<div align="center">

# ShareGPT Chat Workload

**ShareGPT 대화를 multi-turn JSONL trace로 변환**

_Multi-turn · Delayed prefix reuse · Victim workload_

</div>

---

## 개요

ShareGPT 원시 데이터를 multi-turn 대화 trace로 변환합니다. QuotaServe Case 2
static 실험에서 **Chat victim workload**로 사용합니다.

가설에서 Chat은 **high-reuse·long-gap victim** 역할입니다. multi-turn 대화는 이전 턴의 prefix를 다음 턴에서 다시 쓰지만, 그 사이 think-time gap이 길어 RAG가 만든 대량 block에 의해 hot cache가 밀려나기 쉽습니다.

```text
Hugging Face ShareGPT
        │
        ▼
build_sharegpt_workload.py
        │
        ├── 원시 JSON 다운로드 · clean 대화 필터링
        ├── turn 단위 요청 생성(history 누적)
        └── GT 응답 토큰 수(output_token_len) 계산
        │
        ▼
workloads/sharegpt/*.jsonl
```

---

## 환경 준비

레포지터리 루트에서 의존성을 설치합니다. (`output_token_len` 계산에 `transformers` tokenizer를 사용합니다)

```bash
uv pip install -r requirements.txt
```

---

## Trace 생성

빌더는 아래 인자로 trace를 만듭니다. 실제 실행 명령은 [Victim Trace 생성](#victim-trace-생성)을 참고하세요.

### 주요 인자

| 인자 | 의미 |
|---|---|
| `--output` | 출력 JSONL 경로 (**필수**) |
| `--num-conversations` | trace에 사용할 clean 대화 수. `0` 이하이면 전체 clean 대화를 사용합니다. |
| `--min-turns` | 최소 turn 수. victim trace에서는 `9`처럼 설정해 같은 `conversation_id`가 여러 turn에 걸쳐 반복되도록 보장합니다. |
| `--max-turns` | conversation별 최대 turn 수. `0` 이하이면 제한하지 않습니다. |
| `--order` | 요청 저장 순서. `turn-major`(기본)는 같은 turn 번호끼리 먼저 배치합니다. `conversation-major`는 대화 단위로 이어 붙입니다. |
| `--tokenizer` | `output_token_len` 계산용 tokenizer. 기본은 static runner의 모델과 동일합니다. gated 모델 접근이 안 되면 접근 가능한 tokenizer로 바꿔 지정하세요. |
| `--max-output-tokens` | `output_token_len` 상한. 양수면 그 값으로 clamp해 과도하게 긴 decode를 막습니다. |

> [!NOTE]
> **turn-major가 delayed reuse를 만듭니다.**
> 모든 대화의 turn 1을 먼저 보내고 그 다음 turn 2 … 순으로 배치합니다. 따라서 turn 1에서 cache에 올라간 Chat prefix가 turn 2에서 다시 필요해질 때까지 간격이 생겨, victim 구조(밀려나기 쉬운 hot cache)를 재현합니다.

> [!TIP]
> **ShareGPT 사용 방식과 한계.**
> 서빙 벤치마크는 ShareGPT를 (A) **single-turn**(첫 human+gpt 쌍만, GT 응답은 출력 길이용)으로 쓰거나 (B) **multi-turn**(GT 히스토리를 누적 prompt로 재사용)으로 씁니다. 본 빌더는 **B**입니다. 이전 assistant 턴들은 직전 prompt의 prefill로 캐시 재사용되고, **가장 최근 assistant 한 턴만 GT라 모델 생성물과 달라 재-prefill**됩니다(GT 히스토리를 쓰는 모든 벤치 공통). 한편 `output_token_len`은 A 방식처럼 GT 응답 토큰 수로 채워 decode 길이를 현실화합니다.

---

## 출력 스키마

생성되는 row는 `messages`를 주요 입력 필드로 사용합니다. `쓰임` 열의 ✅는 파이프라인/분석에서 실제로 쓰는 필드입니다.

| 필드 | 쓰임 | 의미 |
|---|:---:|---|
| `messages` | ✅ | 모델 입력. 누적된 대화 이력(history + 현재 user 발화). 서버로 전송 |
| `output_token_len` | ✅ | GT 응답 토큰 수. static runner가 `max_tokens = min(output_token_len, 1776)`으로 소비해 decode 길이를 현실화 |
| `output_text` | ✅ | GT assistant 응답. 생성 결과 채점·참조용 |
| `request_id` | ✅ | trace 요청 ID (`{conversation_id}_turn_{n}`). 결과 매칭 키 |
| `conversation_id` | ✅ | 대화 ID. 같은 대화의 turn들을 묶어 delayed reuse 분석 |
| `turn_id` | ✅ | turn 번호. warm/probe 구분 등 phase 분석 |
| `source_dataset`, `cache_pattern` | – | workload 라벨 (`ShareGPT`, `multi_turn`) |

---

## Victim Trace 생성

```bash
cd workloads/sharegpt
python build_sharegpt_workload.py '
  --num-conversations 100 '
  --min-turns 10 '
  --max-turns 10 '
  --order turn-major '
  --output sharegpt_victim_100conv_10turn.jsonl
```

`100 conversations × 10 turns = 1,000 requests` 구조입니다. `--min-turns 10`으로 10턴 이상 대화만 고르고 `--max-turns 10`으로 정확히 10턴으로 잘라 균일하게 맞춥니다.

turn-major라 `[모든 conv의 turn1] → [turn2] → … → [turn10]` 순으로 배치되어, 같은 대화의 재사용 간격(gap)이 약 100 요청이 됩니다. 이 gap이 think-time을 흉내내, RAG가 만든 대량 block에 의해 Chat hot cache가 밀려나는 victim 구조를 만듭니다(대화당 재사용 9회).

---

<div align="center">
<sub>ShareGPT · Chat · Workloads · JJ Distributed LLM Inference</sub>
</div>
