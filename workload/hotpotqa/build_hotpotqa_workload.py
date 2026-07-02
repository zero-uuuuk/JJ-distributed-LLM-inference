"""역할: HotpotQA distractor 데이터를 3k 내외 longctx JSONL trace로 변환한다.

상세 과정:
  1. Hugging Face datasets에서 HotpotQA distractor split을 로드한다.
  2. question과 10개 Wikipedia context paragraph를 원본 순서대로 프롬프트화한다.
  3. tokenizer 기준 prompt 길이가 지정 band에 들어오는 row만 선택한다.
  4. static runner가 소비할 수 있는 JSONL trace와 메타데이터를 저장한다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 기본값
# ---------------------------------------------------------------------------


DEFAULT_DATASET_NAME = "hotpotqa/hotpot_qa"
DEFAULT_SUBSET = "distractor"
DEFAULT_SPLIT = "train"
# static runner의 DEFAULT_MODEL과 일치시켜 prompt/output 토큰 수가 실제 서빙 토큰 수와 맞도록 한다.
DEFAULT_TOKENIZER = "meta-llama/Llama-3.2-3B-Instruct"
DEFAULT_NUM_REQUESTS = 1000
DEFAULT_MIN_PROMPT_TOKENS = 2000
DEFAULT_MAX_PROMPT_TOKENS = 4000

DEFAULT_INSTRUCTION = """You are a multi-hop question-answering assistant.
Use only the provided Wikipedia context to answer the question.
The answer may require combining evidence from multiple paragraphs.
Keep the answer concise."""


# ---------------------------------------------------------------------------
# JSON 입출력
# ---------------------------------------------------------------------------


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """dict 리스트를 static runner용 JSONL 파일로 저장한다."""
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    with resolved_path.open("w", encoding="utf-8") as output_file:
        for row in rows:
            output_file.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI 처리
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """HotpotQA longctx workload 생성에 필요한 CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="HotpotQA distractor를 longctx JSONL trace로 변환합니다.",
    )
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--subset", default=DEFAULT_SUBSET, help="config 이름입니다.")
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("hotpotqa_longctx_2000_4000.jsonl"),
        help="출력 JSONL 경로입니다.",
    )
    parser.add_argument(
        "--num-requests",
        type=int,
        default=DEFAULT_NUM_REQUESTS,
        help="trace에 담을 요청 수입니다. 0 이하이면 필터를 통과한 모든 row를 사용합니다.",
    )
    parser.add_argument(
        "--min-prompt-tokens",
        type=int,
        default=DEFAULT_MIN_PROMPT_TOKENS,
        help="prompt 토큰 하한입니다. 이 값 미만 요청은 제외합니다.",
    )
    parser.add_argument(
        "--max-prompt-tokens",
        type=int,
        default=DEFAULT_MAX_PROMPT_TOKENS,
        help="prompt 토큰 상한입니다. 0 이하이면 제한하지 않습니다.",
    )
    parser.add_argument(
        "--instruction",
        default=DEFAULT_INSTRUCTION,
        help="HotpotQA context 앞에 붙일 시스템성 instruction입니다.",
    )
    parser.add_argument(
        "--streaming",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="streaming 모드입니다(기본 ON). ON이면 필요한 row만 lazy로 읽습니다.",
    )
    parser.add_argument(
        "--tokenizer",
        default=DEFAULT_TOKENIZER,
        help=(
            "prompt/output 토큰 수 계산용 tokenizer입니다. static runner의 모델과 일치시킵니다. "
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


def iter_rows(dataset_name: str, subset: str, split: str, streaming: bool):
    """Hugging Face datasets에서 HotpotQA row를 순회 가능한 형태로 로드한다."""
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "의존성 `datasets`가 없습니다. `pip install datasets`로 설치하세요."
        ) from exc

    return load_dataset(dataset_name, subset, split=split, streaming=streaming)


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
    return len(tokenizer.encode(text, add_special_tokens=False))


def clamp_output_tokens(token_count: int, max_output_tokens: int) -> int:
    """답변 토큰 수를 max_output_tokens가 양수면 그 값으로 clamp한다."""
    if max_output_tokens > 0:
        return min(token_count, max_output_tokens)
    return token_count


# ---------------------------------------------------------------------------
# 프롬프트 생성
# ---------------------------------------------------------------------------


def extract_context_items(row: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """HotpotQA context dict에서 title과 sentence 목록을 원본 순서대로 추출한다."""
    context = row.get("context") or {}
    titles = context.get("title") or []
    sentences_by_title = context.get("sentences") or []

    items: list[tuple[str, list[str]]] = []
    for index, title in enumerate(titles):
        sentences = sentences_by_title[index] if index < len(sentences_by_title) else []
        clean_sentences = [str(sentence).strip() for sentence in sentences if str(sentence).strip()]
        if clean_sentences:
            items.append((str(title).strip(), clean_sentences))
    return items


def build_context_text(context_items: list[tuple[str, list[str]]]) -> str:
    """10개 Wikipedia paragraph를 번호가 붙은 context 문자열로 직렬화한다."""
    paragraphs: list[str] = []
    for index, (title, sentences) in enumerate(context_items, start=1):
        body = " ".join(sentences)
        paragraphs.append(f"[{index}] {title}\n{body}")
    return "\n\n".join(paragraphs)


def build_prompt(instruction: str, context_text: str, question: str) -> str:
    """instruction, HotpotQA context, question을 하나의 QA 프롬프트로 합친다."""
    return (
        f"{instruction}\n\n"
        "[Wikipedia context]\n"
        f"{context_text}\n\n"
        "[Question]\n"
        f"{question}\n\n"
        "[Answer]\n"
    )


def build_output_item(
    row: dict[str, Any],
    emitted_index: int,
    prompt: str,
    prompt_token_len: int,
    output_token_len: int,
    num_contexts: int,
) -> dict[str, Any]:
    """HotpotQA longctx JSONL row와 분석용 메타데이터를 만든다."""
    answer = str(row.get("answer", "")).strip()
    return {
        # static runner가 식별할 요청 ID와 OpenAI-compatible 입력.
        "request_id": f"hotpotqa-longctx-{emitted_index:06d}",
        "messages": [{"role": "user", "content": prompt}],
        "prompt": prompt,
        # output_token_len은 runner가 max_tokens로 소비해 decode 길이를 현실화한다.
        "output_token_len": output_token_len,
        "output_text": answer,
        # 분석과 sanity check를 위한 workload metadata.
        "source_dataset": "HotpotQA",
        "cache_pattern": "longctx",
        "answer": answer,
        "hotpotqa_id": row.get("id", row.get("_id")),
        "question_type": row.get("type"),
        "level": row.get("level"),
        "num_contexts": num_contexts,
        # prompt_token_len은 length/pressure sweep과 band 필터 검증의 핵심 축이다.
        "prompt_token_len": prompt_token_len,
        "prompt_char_len": len(prompt),
    }


def build_workload_rows(
    rows: Any,
    num_requests: int,
    min_prompt_tokens: int,
    max_prompt_tokens: int,
    instruction: str,
    tokenizer: Any,
    max_output_tokens: int,
) -> list[dict[str, Any]]:
    """HotpotQA row 순회 결과를 longctx JSONL trace row 목록으로 변환한다.

    원본 row의 paragraph를 자르거나 추가하지 않고, 완성된 prompt 길이만 기준으로
    필터링한다. 목표 개수를 채우지 못하면 split/band 조정을 안내하는 에러를 낸다.
    """
    from tqdm import tqdm

    workload_rows: list[dict[str, Any]] = []
    scanned_rows = 0
    skipped_short = 0
    skipped_long = 0
    skipped_invalid = 0
    target_label = str(num_requests) if num_requests > 0 else "all"
    progress = tqdm(desc="filtering", unit="row", dynamic_ncols=True)
    progress.set_postfix_str(f"accepted=0/{target_label}")

    try:
        for row in rows:
            scanned_rows += 1
            progress.update(1)
            question = str(row.get("question", "")).strip()
            answer = str(row.get("answer", "")).strip()
            context_items = extract_context_items(row)

            # question/answer/context가 비면 serving 요청으로 의미가 없으므로 제외한다.
            if not question or not answer or not context_items:
                skipped_invalid += 1
                continue

            context_text = build_context_text(context_items)
            prompt = build_prompt(instruction, context_text, question)
            prompt_token_len = count_tokens(tokenizer, prompt)

            # 원문은 그대로 두고 완성 prompt 길이만 기준으로 workload band를 만든다.
            if prompt_token_len < min_prompt_tokens:
                skipped_short += 1
                continue
            if max_prompt_tokens > 0 and prompt_token_len > max_prompt_tokens:
                skipped_long += 1
                continue

            emitted_index = len(workload_rows)
            output_token_len = clamp_output_tokens(
                count_tokens(tokenizer, answer), max_output_tokens
            )
            workload_rows.append(
                build_output_item(
                    row=row,
                    emitted_index=emitted_index,
                    prompt=prompt,
                    prompt_token_len=prompt_token_len,
                    output_token_len=output_token_len,
                    num_contexts=len(context_items),
                )
            )
            progress.set_postfix_str(
                f"accepted={len(workload_rows)}/{target_label}"
            )

            if num_requests > 0 and len(workload_rows) >= num_requests:
                break
    finally:
        progress.close()

    if num_requests > 0 and len(workload_rows) < num_requests:
        raise SystemExit(
            f"요청 수 {num_requests}개를 채우지 못했습니다. "
            f"accepted={len(workload_rows)}, scanned={scanned_rows}, "
            f"short={skipped_short}, long={skipped_long}, invalid={skipped_invalid}\n"
            "--num-requests를 줄이거나 --min-prompt-tokens/--max-prompt-tokens band를 조정하세요."
        )

    print(
        "filter_stats: "
        f"scanned={scanned_rows}, accepted={len(workload_rows)}, "
        f"short={skipped_short}, long={skipped_long}, invalid={skipped_invalid}"
    )
    return workload_rows


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


def main() -> None:
    """HotpotQA 기반 longctx workload JSONL을 생성한다."""
    args = parse_args()

    # 다운로드 전에 먼저 보이도록 flush한다(비-TTY 환경에서 stdout 버퍼링 방지).
    print(f"dataset: {args.dataset_name}", flush=True)
    print(f"subset: {args.subset}", flush=True)
    print(f"split: {args.split}", flush=True)
    print(f"streaming: {args.streaming}", flush=True)
    print(f"tokenizer: {args.tokenizer}", flush=True)

    tokenizer = load_tokenizer(args.tokenizer)
    rows = iter_rows(args.dataset_name, args.subset, args.split, args.streaming)
    workload_rows = build_workload_rows(
        rows=rows,
        num_requests=args.num_requests,
        min_prompt_tokens=args.min_prompt_tokens,
        max_prompt_tokens=args.max_prompt_tokens,
        instruction=args.instruction,
        tokenizer=tokenizer,
        max_output_tokens=args.max_output_tokens,
    )
    write_jsonl(args.output, workload_rows)

    # prompt 길이 분포는 antagonist가 실제로 3k 내외인지 확인하는 핵심 sanity check다.
    prompt_lens = [row["prompt_token_len"] for row in workload_rows]
    out_lens = [row["output_token_len"] for row in workload_rows]
    print(f"requests: {len(workload_rows)}")
    print(f"min_prompt_tokens: {args.min_prompt_tokens}")
    print(f"max_prompt_tokens 상한: {'제한 없음' if args.max_prompt_tokens <= 0 else args.max_prompt_tokens}")
    print("context_policy: keep original HotpotQA distractor paragraphs")
    print(f"max_output_tokens 상한: {'제한 없음' if args.max_output_tokens <= 0 else args.max_output_tokens}")
    print(f"prompt_token_len min/avg/max: {min(prompt_lens) if prompt_lens else 0}/"
          f"{sum(prompt_lens) / len(prompt_lens) if prompt_lens else 0:.1f}/{max(prompt_lens) if prompt_lens else 0}")
    print(f"output_token_len min/avg/max: {min(out_lens) if out_lens else 0}/"
          f"{sum(out_lens) / len(out_lens) if out_lens else 0:.1f}/{max(out_lens) if out_lens else 0}")
    print(f"output: {args.output}")


if __name__ == "__main__":
    main()
