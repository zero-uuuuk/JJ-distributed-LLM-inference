"""역할: SQuAD validation 데이터를 RAG JSONL trace로 변환한다.

상세 과정:
  1. Hugging Face datasets에서 rajpurkar/squad validation split을 로드한다.
  2. 같은 context를 공유하는 질문이 연속되도록 정렬해 prefix cache locality를 높인다.
  3. Chat Completions용 messages와 completions fallback용 prompt를 JSONL로 저장한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 기본 프롬프트
# ---------------------------------------------------------------------------


DEFAULT_SYSTEM_PROMPT = (
    "You are a question-answering assistant. "
    "Answer the question using only the provided context. "
    "If the context does not contain enough evidence, say you do not know. "
    "Keep the answer concise."
)


# ---------------------------------------------------------------------------
# JSON 입출력
# ---------------------------------------------------------------------------


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """dict 리스트를 JSONL 형식으로 저장한다."""
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    with resolved_path.open("w", encoding="utf-8") as output_file:
        for row in rows:
            output_file.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI 처리
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """SQuAD RAG workload 생성에 필요한 CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="SQuAD validation row를 RAG JSONL trace로 변환합니다.",
    )
    parser.add_argument("--dataset-name", default="rajpurkar/squad")
    parser.add_argument("--subset", default="plain_text")
    parser.add_argument("--split", default="validation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--num-requests", type=int, default=5000)
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 데이터 로드
# ---------------------------------------------------------------------------


def load_rows(
    dataset_name: str,
    subset: str,
    split: str,
) -> list[dict[str, Any]]:
    """Hugging Face datasets에서 SQuAD row를 로드한다."""
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "의존성 `datasets`가 없습니다. `pip install datasets`로 설치하세요."
        ) from exc

    # SQuAD는 plain_text config를 기본으로 쓰되, 빈 subset을 넘기면 기본 config 로드도 허용한다.
    if subset:
        dataset = load_dataset(dataset_name, subset, split=split)
    else:
        dataset = load_dataset(dataset_name, split=split)
    return [dict(row) for row in dataset]


# ---------------------------------------------------------------------------
# 텍스트 정규화
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """prefix 비교와 prompt 직렬화가 안정적이도록 내부 공백을 하나로 정규화한다."""
    return " ".join(text.strip().split())


def hash_context(context_text: str) -> str:
    """동일 context 그룹을 추적하기 위한 짧은 SHA-256 해시를 만든다."""
    return hashlib.sha256(context_text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 프롬프트 생성
# ---------------------------------------------------------------------------


def build_user_content(context_text: str, question: str) -> str:
    """SQuAD context와 question을 user message 본문으로 합친다."""
    return (
        "[Context]\n"
        f"{context_text}\n\n"
        "[Question]\n"
        f"{question}\n\n"
        "[Answer]\n"
    )


def build_messages(
    system_prompt: str,
    context_text: str,
    question: str,
) -> list[dict[str, str]]:
    """Chat Completions에서 실제 system role이 적용되도록 messages를 만든다."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_user_content(context_text, question)},
    ]


def build_prompt(system_prompt: str, context_text: str, question: str) -> str:
    """completions API fallback을 위한 단일 문자열 prompt를 만든다."""
    return (
        f"{system_prompt}\n\n"
        f"{build_user_content(context_text, question)}"
    )


# ---------------------------------------------------------------------------
# JSONL row 생성
# ---------------------------------------------------------------------------


def resolve_num_requests(num_requests: int, row_count: int) -> int:
    """CLI 요청 수를 실제 생성할 row 수로 변환한다."""
    if num_requests <= 0:
        return row_count
    if num_requests > row_count:
        raise SystemExit(
            f"요청 {num_requests}개를 만들 수 없습니다. 사용 가능한 row는 {row_count}개입니다."
        )
    return num_requests


def extract_answers(row: dict[str, Any]) -> tuple[list[str], list[int]]:
    """SQuAD answers dict에서 정답 문자열과 시작 offset을 분리한다."""
    answers = row.get("answers", {})
    answer_texts = list(answers.get("text", []))
    answer_starts = [int(offset) for offset in answers.get("answer_start", [])]
    return answer_texts, answer_starts


def build_output_item(
    emitted_index: int,
    row: dict[str, Any],
    system_prompt: str,
    split: str,
) -> dict[str, Any] | None:
    """SQuAD row 하나를 RAG JSONL row로 변환한다."""
    context_text = normalize_text(str(row["context"]))
    question = normalize_text(str(row["question"]))
    title = normalize_text(str(row["title"]))

    # 빈 context나 question은 유효한 RAG 요청을 만들 수 없으므로 trace에서 제외한다.
    if not context_text or not question:
        return None

    # Chat API는 messages를 직접 사용하고, completions API는 prompt fallback을 사용한다.
    messages = build_messages(system_prompt, context_text, question)
    prompt = build_prompt(system_prompt, context_text, question)
    answer_texts, answer_starts = extract_answers(row)

    # 후처리 분석에서 context 재사용 그룹과 정답 label을 함께 볼 수 있게 메타데이터를 보존한다.
    return {
        "messages": messages,
        "prompt": prompt,
        "request_id": f"squad-rag-{emitted_index:06d}",
        "squad_id": row["id"],
        "title": title,
        "question": question,
        "answer": answer_texts[0] if answer_texts else None,
        "answers": answer_texts,
        "answer_starts": answer_starts,
        "context_hash": hash_context(context_text),
        "context_char_len": len(context_text),
        "prompt_char_len": len(prompt),
        "source_dataset": "SQuAD",
        "split": split,
    }


def build_workload_rows(
    rows: list[dict[str, Any]],
    num_requests: int,
    system_prompt: str,
    split: str,
) -> list[dict[str, Any]]:
    """SQuAD row 목록을 cache locality가 높은 RAG JSONL trace row 목록으로 변환한다."""
    selected_rows = rows[:num_requests]

    # context 기준으로 정렬해 같은 문단을 공유하는 질문들이 연속 요청으로 배치되게 한다.
    selected_rows.sort(
        key=lambda row: (
            normalize_text(str(row["title"])),
            normalize_text(str(row["context"])),
            normalize_text(str(row["question"])),
            str(row["id"]),
        )
    )

    workload_rows: list[dict[str, Any]] = []
    for row in selected_rows:
        item = build_output_item(
            emitted_index=len(workload_rows),
            row=row,
            system_prompt=system_prompt,
            split=split,
        )
        if item is not None:
            workload_rows.append(item)

    return workload_rows


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


def main() -> None:
    """SQuAD 기반 RAG workload JSONL을 생성한다."""
    args = parse_args()

    rows = load_rows(args.dataset_name, args.subset, args.split)
    num_requests = resolve_num_requests(args.num_requests, len(rows))
    workload_rows = build_workload_rows(
        rows=rows,
        num_requests=num_requests,
        system_prompt=args.system_prompt,
        split=args.split,
    )
    write_jsonl(args.output, workload_rows)
    print(f"{args.output}에 요청 {len(workload_rows)}개를 저장했습니다.")


if __name__ == "__main__":
    main()
