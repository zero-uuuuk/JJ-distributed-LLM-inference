"""역할: LongAlpaca-12k 데이터를 long-context JSONL trace로 변환한다.

상세 과정:
  1. Hugging Face datasets에서 LongAlpaca-12k split을 로드한다.
  2. instruction(이미 "긴 본문 + 질문"이 합쳐진 통짜 프롬프트)을 재가공 없이 그대로 사용한다.
  3. prompt 토큰 길이로 요청을 필터링해 길이대를 통제하고, JSONL로 저장한다.

설계 의도:
  이 trace는 QuotaServe 가설에서 long-context workload(저-reuse 대용량 antagonist) 역할을
  한다. LongAlpaca-12k는 논문·책 본문 한 편을 읽고 답하는 long-context QA다. 
  다만 가설이 antagonist에게 요구하는 cache 거동(요청당 대량의 신규 block 생산 · 
  요청마다 unique · prefill-heavy)을 그대로 만족한다.

  기존 MS MARCO trace는 passage가 짧아(평균 ~800 토큰) Chat과 체급이 비슷해, "대용량
  workload가 신규 block을 쏟아내 Chat hot cache를 LRU에서 밀어낸다"는 메커니즘을 약하게만
  자극했다. LongAlpaca-12k는 요청 하나가 수천 토큰의 신규 block을 만들고, 행마다 본문이
  달라 intra-reuse가 거의 0이라, 이 메커니즘을 더 선명하게 자극한다.

  LongAlpaca-12k는 긴 QA 9k개에 짧은 Alpaca 3k개가 섞여 있다. --min-prompt-tokens로
  짧은 쪽을 걸러내 long-context만 남기고, --max-prompt-tokens로 max-model-len을 넘겨
  truncation될 요청을 배제하거나 길이 band를 잘라 length sweep을 구성한다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 기본값
# ---------------------------------------------------------------------------


DEFAULT_DATASET_NAME = "Yukang/LongAlpaca-12k"
DEFAULT_SPLIT = "train"
# run_mixed.py의 DEFAULT_MODEL과 일치시켜 prompt/output 토큰 수가 실제 서빙 토큰 수와 맞도록 한다.
DEFAULT_TOKENIZER = "meta-llama/Llama-3.2-3B-Instruct"
# 기본 long-context band. g5.xlarge + max-model-len 8192 실험에서 과도한 truncation을 피하면서
# 기존 RAG보다 강한 context pressure를 만들기 위한 범위다.
DEFAULT_MIN_PROMPT_TOKENS = 3000
DEFAULT_MAX_PROMPT_TOKENS = 7000


# ---------------------------------------------------------------------------
# JSON 입출력
# ---------------------------------------------------------------------------


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """dict 리스트를 run_trace.py / run_mixed.py용 JSONL 파일로 저장한다."""
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    with resolved_path.open("w", encoding="utf-8") as output_file:
        for row in rows:
            output_file.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI 처리
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """LongAlpaca long-context workload 생성에 필요한 CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="LongAlpaca-12k를 long-context JSONL trace로 변환합니다.",
    )
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--num-requests",
        type=int,
        default=1000,
        help="trace에 담을 요청 수입니다. 0 이하이면 필터를 통과한 모든 row를 사용합니다.",
    )
    parser.add_argument(
        "--min-prompt-tokens",
        type=int,
        default=DEFAULT_MIN_PROMPT_TOKENS,
        help=(
            "prompt 토큰 하한입니다. 이 값 미만 요청은 제외해 짧은 Alpaca 샘플을 걸러냅니다. "
            "length band의 아래쪽 경계로도 씁니다(기본 3000)."
        ),
    )
    parser.add_argument(
        "--max-prompt-tokens",
        type=int,
        default=DEFAULT_MAX_PROMPT_TOKENS,
        help=(
            "prompt 토큰 상한입니다. 0 이하이면 제한하지 않습니다. 양수면 그보다 긴 요청을 제외합니다. "
            "max-model-len truncation을 피하거나(기본 3000~7000 band) length band의 위쪽 "
            "경계를 정할 때 씁니다(length/pressure 노브)."
        ),
    )
    parser.add_argument(
        "--streaming",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "streaming 모드입니다(기본 OFF). LongAlpaca-12k는 작아 전체 로드가 가볍고, "
            "기본은 전체 로드 후 원본 순서를 유지합니다."
        ),
    )
    parser.add_argument(
        "--tokenizer",
        default=DEFAULT_TOKENIZER,
        help=(
            "prompt/output 토큰 수 계산용 tokenizer입니다. run_mixed의 모델과 일치시킵니다. "
            "gated 모델 접근이 안 되면 접근 가능한 tokenizer로 바꿔 지정하세요."
        ),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=0,
        help="output_token_len 상한입니다. 0 이하이면 제한하지 않습니다. 양수면 그 값으로 clamp합니다.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 데이터 로드
# ---------------------------------------------------------------------------


def iter_rows(dataset_name: str, split: str, streaming: bool):
    """Hugging Face datasets에서 LongAlpaca row를 순회 가능한 형태로 로드한다."""
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "의존성 `datasets`가 없습니다. `pip install datasets`로 설치하세요."
        ) from exc

    return load_dataset(dataset_name, split=split, streaming=streaming)


def load_tokenizer(tokenizer_name: str) -> Any:
    """prompt/output 토큰 수 계산용 tokenizer를 로드한다."""
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise SystemExit(
            "의존성 `transformers`가 없습니다. `pip install transformers`로 설치하세요."
        ) from exc

    # gated 모델(예: meta-llama)은 접근 권한이 없으면 여기서 실패하므로 친절히 안내한다.
    try:
        return AutoTokenizer.from_pretrained(tokenizer_name)
    except Exception as exc:
        raise SystemExit(
            f"tokenizer `{tokenizer_name}` 로드에 실패했습니다: {exc}\n"
            "gated 모델이면 `huggingface-cli login`으로 인증하거나, "
            "`--tokenizer`로 접근 가능한 tokenizer를 지정하세요."
        ) from exc


# ---------------------------------------------------------------------------
# 토큰 수 계산
# ---------------------------------------------------------------------------


def count_tokens(tokenizer: Any, text: str) -> int:
    """본문 토큰 수를 센다. chat template overhead(수 토큰)는 무시하는 근사값이다."""
    # add_special_tokens=False로 chat 래핑/BOS를 제외한 본문 토큰만 센다.
    return len(tokenizer.encode(text, add_special_tokens=False))


def clamp_output_tokens(token_count: int, max_output_tokens: int) -> int:
    """답변 토큰 수를 max_output_tokens가 양수면 그 값으로 clamp한다."""
    if max_output_tokens > 0:
        return min(token_count, max_output_tokens)
    return token_count


# ---------------------------------------------------------------------------
# JSONL row 생성
# ---------------------------------------------------------------------------


def build_output_item(
    prompt: str,
    emitted_index: int,
    answer: str,
    prompt_token_len: int,
    output_token_len: int,
) -> dict[str, Any]:
    """LongAlpaca long-context JSONL row와 분석용 메타데이터를 만든다."""
    return {
        # run_trace.py / run_mixed.py가 식별할 요청 ID와 OpenAI-compatible 입력.
        "request_id": f"longalpaca-longctx-{emitted_index:06d}",
        # instruction을 재가공 없이 그대로 보낸다(이미 본문 + 질문이 합쳐진 통짜 프롬프트).
        "messages": [{"role": "user", "content": prompt}],
        "prompt": prompt,
        "output_text": answer,
        # output_token_len은 run_mixed가 max_tokens로 소비해 decode 길이를 현실화한다.
        "output_token_len": output_token_len,
        # 분석과 sanity check를 위한 workload metadata.
        "source_dataset": "LongAlpaca-12k",
        "cache_pattern": "longctx",
        "answer": answer,
        # prompt_token_len은 length/pressure sweep과 band 필터 검증의 핵심 축이다.
        "prompt_token_len": prompt_token_len,
        "prompt_char_len": len(prompt),
    }


def build_workload_rows(
    rows: Any,
    num_requests: int,
    min_prompt_tokens: int,
    max_prompt_tokens: int,
    tokenizer: Any,
    max_output_tokens: int,
) -> list[dict[str, Any]]:
    """LongAlpaca row 순회 결과를 long-context JSONL trace row 목록으로 변환한다.

    길이 필터를 통과한 row를 원본 순서대로 사용한다. num_requests가 양수이면
    필요한 개수를 채우는 즉시 순회를 멈추고, request_id를 0부터 재부여한다.
    """
    from tqdm import tqdm

    workload_rows: list[dict[str, Any]] = []
    progress = tqdm(desc="filtering", unit="row", dynamic_ncols=True)
    target_label = str(num_requests) if num_requests > 0 else "all"
    progress.set_postfix_str(f"accepted=0/{target_label}")

    try:
        for row in rows:
            prompt = str(row.get("instruction", "")).strip()
            answer = str(row.get("output", "")).strip()
            progress.update(1)
            # instruction/output이 비면 의미 없는 요청이므로 건너뛴다.
            if not prompt or not answer:
                continue

            # prompt 토큰 길이로 short Alpaca를 걸러내고 max-model-len 초과를 배제한다.
            prompt_token_len = count_tokens(tokenizer, prompt)
            if prompt_token_len < min_prompt_tokens:
                continue
            if max_prompt_tokens > 0 and prompt_token_len > max_prompt_tokens:
                continue

            emitted_index = len(workload_rows)
            output_token_len = clamp_output_tokens(
                count_tokens(tokenizer, answer), max_output_tokens
            )
            workload_rows.append(
                build_output_item(
                    prompt=prompt,
                    emitted_index=emitted_index,
                    answer=answer,
                    prompt_token_len=prompt_token_len,
                    output_token_len=output_token_len,
                )
            )
            progress.set_postfix_str(
                f"accepted={len(workload_rows)}/{target_label}"
            )

            if num_requests > 0 and len(workload_rows) >= num_requests:
                break
    finally:
        progress.close()

    return workload_rows


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


def main() -> None:
    """LongAlpaca 기반 long-context workload JSONL을 생성한다."""
    args = parse_args()

    # 다운로드 전에 먼저 보이도록 flush한다(비-TTY 환경에서 stdout 버퍼링 방지).
    print(f"dataset: {args.dataset_name}", flush=True)
    print(f"split: {args.split}", flush=True)
    print(f"streaming: {args.streaming}", flush=True)
    print(f"tokenizer: {args.tokenizer}", flush=True)

    tokenizer = load_tokenizer(args.tokenizer)
    rows = iter_rows(args.dataset_name, args.split, args.streaming)
    workload_rows = build_workload_rows(
        rows=rows,
        num_requests=args.num_requests,
        min_prompt_tokens=args.min_prompt_tokens,
        max_prompt_tokens=args.max_prompt_tokens,
        tokenizer=tokenizer,
        max_output_tokens=args.max_output_tokens,
    )
    write_jsonl(args.output, workload_rows)

    # prompt 길이 분포는 antagonist가 실제로 long-context인지 확인하는 핵심 sanity check다.
    prompt_lens = [row["prompt_token_len"] for row in workload_rows]
    out_lens = [row["output_token_len"] for row in workload_rows]
    print(f"requests: {len(workload_rows)}")
    print(f"min_prompt_tokens: {args.min_prompt_tokens}")
    print(f"max_prompt_tokens 상한: {'제한 없음' if args.max_prompt_tokens <= 0 else args.max_prompt_tokens}")
    print("order: original dataset order")
    print(f"max_output_tokens 상한: {'제한 없음' if args.max_output_tokens <= 0 else args.max_output_tokens}")
    print(f"prompt_token_len min/avg/max: {min(prompt_lens) if prompt_lens else 0}/"
          f"{sum(prompt_lens) / len(prompt_lens) if prompt_lens else 0:.1f}/{max(prompt_lens) if prompt_lens else 0}")
    print(f"output_token_len min/avg/max: {min(out_lens) if out_lens else 0}/"
          f"{sum(out_lens) / len(out_lens) if out_lens else 0:.1f}/{max(out_lens) if out_lens else 0}")
    print(f"output: {args.output}")


if __name__ == "__main__":
    main()
