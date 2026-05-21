"""역할: HotpotQA validation 데이터를 RAG JSONL trace로 변환한다.

상세 과정:
  1. Hugging Face datasets에서 HotpotQA split을 로드한다.
  2. context 후보 문서를 RAG retrieved chunks 영역으로 직렬화한다.
  3. RAG prompt와 후처리용 메타데이터를 JSONL로 저장한다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 기본 프롬프트
# ---------------------------------------------------------------------------


DEFAULT_INSTRUCTION = """You are a RAG question-answering assistant.
Use only the retrieved chunks to answer the question.
If the chunks do not contain enough evidence, say you do not know.
Keep the answer concise."""


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
    """HotpotQA RAG workload 생성에 필요한 CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="HotpotQA distractor row를 RAG JSONL trace로 변환합니다.",
    )
    parser.add_argument("--dataset-name", default="hotpotqa/hotpot_qa")
    parser.add_argument("--subset", default="distractor")
    parser.add_argument("--split", default="validation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--num-requests", type=int, default=5000)
    parser.add_argument("--instruction", default=DEFAULT_INSTRUCTION)
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 데이터 로드
# ---------------------------------------------------------------------------


def load_rows(
    dataset_name: str,
    subset: str,
    split: str,
) -> list[dict[str, Any]]:
    """Hugging Face datasets에서 HotpotQA row를 로드한다."""
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "의존성 `datasets`가 없습니다. `pip install datasets`로 설치하세요."
        ) from exc

    # Dataset 객체를 일반 dict 리스트로 바꿔 이후 처리와 JSON 직렬화를 단순하게 유지한다.
    dataset = load_dataset(dataset_name, subset, split=split)
    return [dict(row) for row in dataset]


# ---------------------------------------------------------------------------
# 프롬프트 생성
# ---------------------------------------------------------------------------


def clean_sentence(sentence: str) -> str:
    """문장 내부 공백을 하나로 정규화한다."""
    return " ".join(sentence.strip().split())


def build_context_text(row: dict[str, Any]) -> tuple[str, list[str]]:
    """HotpotQA context 필드를 retrieved chunks 문자열로 변환한다."""
    context = row["context"]
    titles = context["title"]
    sentences_by_title = context["sentences"]
    chunks: list[str] = []
    used_titles: list[str] = []

    # title 기준으로 정렬해 동일 문서 조합이 항상 같은 순서로 직렬화되도록 한다.
    # 순서가 달랐던 요청들도 같은 prefix를 공유하게 되어 prefix cache hit가 높아진다.
    doc_pairs = sorted(zip(titles, sentences_by_title), key=lambda pair: pair[0])
    for index, (title, sentences) in enumerate(doc_pairs, start=1):
        # 빈 문서 후보는 prompt에서 제외해 의미 없는 chunk를 만들지 않는다.
        body = " ".join(clean_sentence(sentence) for sentence in sentences)
        if not body:
            continue

        # 각 후보 문서가 RAG retrieved chunk처럼 보이도록 제목과 본문을 함께 둔다.
        chunks.append(f"[{index}] Title: {title}\n{body}")
        used_titles.append(title)

    return "\n\n".join(chunks), used_titles


def build_prompt(instruction: str, context_text: str, question: str) -> str:
    """instruction, retrieved chunks, question을 하나의 RAG 프롬프트로 합친다."""
    return (
        f"{instruction}\n\n"
        "[Retrieved chunks]\n"
        f"{context_text}\n\n"
        "[Question]\n"
        f"{question}\n\n"
        "[Answer]\n"
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


def build_output_item(
    prompt: str,
    emitted_index: int,
    row: dict[str, Any],
    context_titles: list[str],
    context_text: str,
) -> dict[str, Any]:
    """RAG JSONL row와 분석용 메타데이터를 만든다."""
    item: dict[str, Any] = {
        "prompt": prompt,
        "request_id": f"hotpot-rag-{emitted_index:06d}",
        "hotpot_id": row["id"],
    }

    # 후처리 분석에서 정답과 prompt 길이를 함께 볼 수 있게 원본 label을 보존한다.
    item.update(
        {
            "question": row["question"],
            "answer": row["answer"],
            "level": row["level"],
            "type": row["type"],
            "context_titles": context_titles,
            "context_char_len": len(context_text),
            "prompt_char_len": len(prompt),
        }
    )
    return item


def build_workload_rows(
    rows: list[dict[str, Any]],
    num_requests: int,
    instruction: str,
) -> list[dict[str, Any]]:
    """HotpotQA row 목록을 RAG JSONL trace row 목록으로 변환한다."""
    workload_rows: list[dict[str, Any]] = []

    for row in rows[:num_requests]:
        context_text, context_titles = build_context_text(row)
        if not context_text:
            continue

        # HotpotQA 원본 context를 이미 검색된 RAG 문서처럼 prompt에 넣는다.
        prompt = build_prompt(instruction, context_text, row["question"])
        workload_rows.append(
            build_output_item(
                prompt=prompt,
                emitted_index=len(workload_rows),
                row=row,
                context_titles=context_titles,
                context_text=context_text,
            )
        )

    # context 텍스트 기준으로 정렬해 같은(또는 유사한) 문서를 공유하는 요청이 연속으로 오게 한다.
    # vLLM prefix cache는 position 0부터 exact match이므로, 앞쪽 청크가 같은 요청이 연속으로
    # 오면 공유 prefix 길이만큼 KV cache hit가 발생한다.
    workload_rows.sort(key=lambda item: item["context_titles"])
    return workload_rows


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


def main() -> None:
    """HotpotQA 기반 RAG workload JSONL을 생성한다."""
    args = parse_args()

    rows = load_rows(args.dataset_name, args.subset, args.split)
    num_requests = resolve_num_requests(args.num_requests, len(rows))
    workload_rows = build_workload_rows(
        rows=rows,
        num_requests=num_requests,
        instruction=args.instruction,
    )
    write_jsonl(args.output, workload_rows)
    print(f"{args.output}에 요청 {len(workload_rows)}개를 저장했습니다.")


if __name__ == "__main__":
    main()
