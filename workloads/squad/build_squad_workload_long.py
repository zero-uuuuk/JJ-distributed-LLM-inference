"""역할: SQuAD validation 데이터를 RAG-like JSONL trace로 변환한다.

상세 과정:
  1. Hugging Face datasets에서 SQuAD 데이터를 로드한다.
  2. 원본 context에 추가 context들을 붙여 multi-passage RAG prompt를 만든다.
  3. target prompt 길이에 맞도록 context를 반복/확장한다.
  4. run_trace.py / run_mixed.py가 읽을 수 있는 JSONL trace로 저장한다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_DATASET_NAME = "squad"
DEFAULT_SPLIT = "validation"


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
    """Long-context SQuAD RAG workload 생성에 필요한 CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="SQuAD를 long RAG-like JSONL workload로 변환합니다.",
    )
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--num-requests", type=int, default=5_000)
    parser.add_argument(
        "--num-contexts",
        type=int,
        default=8,
        help="요청 하나에 붙일 passage/context 개수입니다.",
    )
    parser.add_argument(
        "--target-prompt-tokens",
        type=int,
        default=0,
        help=(
            "대략적인 목표 prompt token 수입니다. "
            "0 이하이면 num-contexts로 만든 context를 그대로 사용합니다. "
            "정확한 tokenizer가 아니라 whitespace token 기준입니다."
        ),
    )
    parser.add_argument(
        "--context-stride",
        type=int,
        default=1,
        help="추가 context를 고를 때 사용할 row index stride입니다.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 데이터 로드
# ---------------------------------------------------------------------------


def load_dataset_rows(dataset_name: str, split: str) -> list[dict[str, Any]]:
    """Hugging Face datasets에서 SQuAD row를 로드한다."""
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "의존성 `datasets`가 없습니다. `pip install datasets`로 설치하세요."
        ) from exc

    dataset = load_dataset(dataset_name, split=split)
    return [dict(row) for row in dataset]


# ---------------------------------------------------------------------------
# 길이 추정과 context 수집
# ---------------------------------------------------------------------------


def approximate_tokens(text: str) -> int:
    """빠른 길이 제어용 whitespace token count."""
    return len(text.split())


def collect_contexts(
    rows: list[dict[str, Any]],
    start_index: int,
    num_contexts: int,
    context_stride: int,
) -> list[str]:
    """start_index부터 순환하며 context를 num_contexts개 수집한다."""
    if num_contexts < 1:
        raise SystemExit("--num-contexts는 1 이상이어야 합니다.")
    if context_stride < 1:
        raise SystemExit("--context-stride는 1 이상이어야 합니다.")

    contexts: list[str] = []
    seen: set[str] = set()
    total_rows = len(rows)

    offset = 0
    while len(contexts) < num_contexts:
        row_index = (start_index + offset * context_stride) % total_rows
        context = str(rows[row_index].get("context", "")).strip()
        offset += 1

        if not context or context in seen:
            continue

        # 중복 passage를 피해서 같은 prompt 안에서 불필요한 반복을 줄인다.
        contexts.append(context)
        seen.add(context)

        # 데이터셋에 unique context가 너무 적은 경우 무한 루프 방지.
        if offset > total_rows * 2:
            break

    return contexts


# ---------------------------------------------------------------------------
# 프롬프트 구성
# ---------------------------------------------------------------------------


def format_passages(contexts: list[str]) -> str:
    """여러 SQuAD context를 Passage 1, Passage 2, ... 형태로 직렬화한다."""
    parts: list[str] = []
    for index, context in enumerate(contexts, start=1):
        parts.append(f"Passage {index}:\n{context}")
    return "\n\n".join(parts)


def expand_to_target_tokens(
    base_contexts: list[str],
    rows: list[dict[str, Any]],
    start_index: int,
    target_prompt_tokens: int,
    question: str,
) -> list[str]:
    """대략 target prompt token 수에 도달할 때까지 context를 추가한다."""
    if target_prompt_tokens <= 0:
        return base_contexts

    contexts = list(base_contexts)
    seen = set(contexts)
    total_rows = len(rows)
    offset = len(contexts)

    while True:
        # 실제 tokenizer가 아니라 빠른 whitespace count로 목표 길이에 도달했는지 확인한다.
        # 정확한 토큰 수는 실험 결과 JSONL의 prompt_tokens를 최종 기준으로 본다.
        prompt = build_prompt_from_contexts(contexts, question)
        if approximate_tokens(prompt) >= target_prompt_tokens:
            return contexts

        row_index = (start_index + offset) % total_rows
        context = str(rows[row_index].get("context", "")).strip()
        offset += 1

        if context and context not in seen:
            contexts.append(context)
            seen.add(context)

        # unique context를 더 못 찾으면 마지막 context를 반복해서라도 길이를 맞춘다.
        if offset > total_rows * 2:
            if contexts:
                contexts.append(contexts[-1])
            else:
                return contexts


def build_prompt_from_contexts(contexts: list[str], question: str) -> str:
    """multi-passage RAG prompt를 Chat Completions user message 본문으로 만든다."""
    passages = format_passages(contexts)
    return (
        "You are a question answering assistant. "
        "Answer the question using only the provided passages.\n\n"
        f"{passages}\n\n"
        f"Question:\n{question}\n\n"
        "Answer:"
    )


# ---------------------------------------------------------------------------
# JSONL row 생성
# ---------------------------------------------------------------------------


def first_answer_text(row: dict[str, Any]) -> str:
    """SQuAD answers dict에서 첫 번째 정답 문자열만 보조 metadata로 추출한다."""
    answers = row.get("answers") or {}
    texts = answers.get("text") or []
    if texts:
        return str(texts[0])
    return ""


def build_request(
    rows: list[dict[str, Any]],
    row: dict[str, Any],
    row_index: int,
    num_contexts: int,
    target_prompt_tokens: int,
    context_stride: int,
) -> dict[str, Any]:
    """SQuAD row 하나를 long-context RAG 요청 하나로 변환한다."""
    question = str(row.get("question", "")).strip()
    base_contexts = collect_contexts(
        rows=rows,
        start_index=row_index,
        num_contexts=num_contexts,
        context_stride=context_stride,
    )
    contexts = expand_to_target_tokens(
        base_contexts=base_contexts,
        rows=rows,
        start_index=row_index,
        target_prompt_tokens=target_prompt_tokens,
        question=question,
    )
    prompt = build_prompt_from_contexts(contexts, question)

    return {
        # run_trace.py / run_mixed.py가 식별할 요청 ID와 OpenAI-compatible 입력.
        "request_id": f"squad-rag-{row_index:06d}",
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "prompt": prompt,
        "output_text": first_answer_text(row),
        # 분석과 sanity check를 위한 workload metadata.
        "source_dataset": "SQuAD",
        "cache_pattern": "long_rag",
        "question": question,
        "title": row.get("title"),
        "num_contexts": len(contexts),
        "approx_prompt_tokens": approximate_tokens(prompt),
        "target_prompt_tokens": target_prompt_tokens,
    }


def build_requests(
    rows: list[dict[str, Any]],
    num_requests: int,
    num_contexts: int,
    target_prompt_tokens: int,
    context_stride: int,
) -> list[dict[str, Any]]:
    """선택된 SQuAD row들을 long-context RAG JSONL row 목록으로 변환한다."""
    if num_requests <= 0:
        selected_count = len(rows)
    else:
        selected_count = min(num_requests, len(rows))

    requests: list[dict[str, Any]] = []
    for row_index in range(selected_count):
        requests.append(
            build_request(
                rows=rows,
                row=rows[row_index],
                row_index=row_index,
                num_contexts=num_contexts,
                target_prompt_tokens=target_prompt_tokens,
                context_stride=context_stride,
            )
        )

    return requests


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


def main() -> None:
    """SQuAD 기반 long-context RAG workload JSONL을 생성한다."""
    args = parse_args()
    rows = load_dataset_rows(
        dataset_name=args.dataset_name,
        split=args.split,
    )

    requests = build_requests(
        rows=rows,
        num_requests=args.num_requests,
        num_contexts=args.num_contexts,
        target_prompt_tokens=args.target_prompt_tokens,
        context_stride=args.context_stride,
    )

    write_jsonl(args.output, requests)

    lengths = [row["approx_prompt_tokens"] for row in requests]
    print(f"dataset: {args.dataset_name}")
    print(f"split: {args.split}")
    print(f"requests: {len(requests)}")
    print(f"num_contexts default: {args.num_contexts}")
    print(f"target_prompt_tokens: {args.target_prompt_tokens}")
    print(f"approx prompt tokens min: {min(lengths) if lengths else 0}")
    print(f"approx prompt tokens max: {max(lengths) if lengths else 0}")
    print(f"approx prompt tokens avg: {sum(lengths) / len(lengths) if lengths else 0:.1f}")
    print(f"output: {args.output}")


if __name__ == "__main__":
    main()
