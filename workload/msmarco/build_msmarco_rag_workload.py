"""역할: MS MARCO 데이터를 RAG JSONL trace로 변환한다.

상세 과정:
  1. Hugging Face datasets에서 MS MARCO split을 로드한다.
  2. query마다 이미 검색된 passages를 RAG retrieved chunks로 직렬화한다.
  3. RAG prompt와 후처리용 메타데이터를 JSONL로 저장한다.

설계 의도:
  이 trace는 QuotaServe 가설에서 RAG workload(저-reuse 대용량 antagonist) 역할을 한다.
  MS MARCO는 공유 corpus(web)에서 검색된 passage라 인기 문서는 반복돼 현실적 reuse가
  생기지만, query마다 대체로 다른 다수 passage를 만들어 RAG의 intra-reuse는 낮게 유지된다.
  기본값은 retrieval 순서를 보존해 reuse를 낮게 두고, prefix 공유를 의도적으로 높이고
  싶을 때만 --normalize-passage-order / --sort-by-context로 dial up 한다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 기본값
# ---------------------------------------------------------------------------


DEFAULT_DATASET_NAME = "microsoft/ms_marco"
DEFAULT_SUBSET = "v2.1"
DEFAULT_SPLIT = "validation"
# static runner의 DEFAULT_MODEL과 일치시켜 output_token_len이 실제 서빙 토큰 수와 맞도록 한다.
DEFAULT_TOKENIZER = "meta-llama/Llama-3.2-3B-Instruct"
# MS MARCO에서 답이 없는 row를 표시하는 문자열. 이런 row는 trace에서 제외한다.
NO_ANSWER_MARKER = "No Answer Present."

DEFAULT_INSTRUCTION = """You are a RAG question-answering assistant.
Use only the retrieved context to answer the question.
If the context does not contain enough evidence, say you do not know.
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
    """MS MARCO RAG workload 생성에 필요한 CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="MS MARCO를 RAG JSONL trace로 변환합니다.",
    )
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME)
    parser.add_argument("--subset", default=DEFAULT_SUBSET, help="config 이름입니다 (v2.1 또는 v1.1).")
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--num-requests",
        type=int,
        default=5_000,
        help="trace에 담을 요청 수입니다. 0 이하이면 사용 가능한 모든 row를 사용합니다.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=0,
        help=(
            "요청당 사용할 passage 수입니다. 0 이하이면 제공된 passage를 전부 사용합니다. "
            "양수면 상위 k개만 사용합니다 (RAG volume/pressure 노브)."
        ),
    )
    parser.add_argument("--instruction", default=DEFAULT_INSTRUCTION)
    parser.add_argument(
        "--streaming",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "streaming 모드입니다(기본 ON). ON이면 validation 파일만 lazy로 읽어 "
            "필요한 만큼만 받습니다. OFF이면 config 전체(train 포함)를 먼저 다운로드합니다."
        ),
    )
    parser.add_argument(
        "--normalize-passage-order",
        action="store_true",
        help=(
            "요청 내 passage를 본문 기준으로 정렬합니다. 같은 passage 집합이면 항상 같은 "
            "context 문자열이 되어, passage 순서가 달랐던 요청 사이에도 prefix cache hit가 늘어납니다."
        ),
    )
    parser.add_argument(
        "--sort-by-context",
        action="store_true",
        help=(
            "context 내용 기준으로 요청을 정렬합니다. 같은/유사 context가 연속 배치되어 "
            "RAG의 prefix cache reuse를 의도적으로 끌어올립니다(8절 reuse dial up)."
        ),
    )
    parser.add_argument(
        "--tokenizer",
        default=DEFAULT_TOKENIZER,
        help=(
            "output_token_len 계산용 tokenizer입니다. static runner의 모델과 일치시킵니다. "
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
    """Hugging Face datasets에서 MS MARCO row를 순회 가능한 형태로 로드한다.

    streaming=True이면 요청한 split 파일만 lazy로 읽으므로, split="validation"일 때
    train 샤드까지 전부 받는 일을 피한다(MS MARCO v2.1은 config 전체가 수 GB다).
    """
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "의존성 `datasets`가 없습니다. `pip install datasets`로 설치하세요."
        ) from exc

    # 비-streaming은 config 전체를 먼저 다운로드하므로 큰 데이터셋에선 기본 streaming을 권장한다.
    return load_dataset(dataset_name, subset, split=split, streaming=streaming)


# ---------------------------------------------------------------------------
# 프롬프트 생성
# ---------------------------------------------------------------------------


def select_passages(
    row: dict[str, Any],
    top_k: int,
    normalize_passage_order: bool,
) -> tuple[list[str], list[str], int]:
    """MS MARCO passages dict에서 사용할 passage 본문/URL과 정답 passage 위치를 고른다."""
    passages = row.get("passages") or {}
    texts = passages.get("passage_text") or []
    urls = passages.get("url") or []
    is_selected = passages.get("is_selected") or []

    # 세 리스트는 index 정렬돼 있으므로 함께 묶어 다룬다. URL/selected가 짧아도 안전하게 채운다.
    items: list[tuple[str, str, int]] = []
    for index, text in enumerate(texts):
        clean_text = str(text).strip()
        if not clean_text:
            continue
        url = str(urls[index]) if index < len(urls) else ""
        selected = int(is_selected[index]) if index < len(is_selected) else 0
        items.append((clean_text, url, selected))

    if top_k > 0:
        items = items[:top_k]

    # 같은 passage 집합이 항상 같은 prefix가 되도록 본문 기준으로 정렬해 reuse를 높인다.
    if normalize_passage_order:
        items.sort(key=lambda item: item[0])

    passage_texts = [item[0] for item in items]
    passage_urls = [item[1] for item in items]
    # 정답으로 표시된(is_selected==1) 첫 passage의 위치. 없으면 -1.
    selected_index = next(
        (index for index, item in enumerate(items) if item[2] == 1),
        -1,
    )
    return passage_texts, passage_urls, selected_index


def build_context_text(passage_texts: list[str]) -> str:
    """passage 본문들을 번호가 매겨진 retrieved chunks 문자열로 변환한다."""
    chunks = [f"[{index}] {text}" for index, text in enumerate(passage_texts, start=1)]
    return "\n\n".join(chunks)


def build_prompt(instruction: str, context_text: str, question: str) -> str:
    """instruction, retrieved context, question을 하나의 RAG 프롬프트로 합친다."""
    return (
        f"{instruction}\n\n"
        "[Retrieved context]\n"
        f"{context_text}\n\n"
        "[Question]\n"
        f"{question}\n\n"
        "[Answer]\n"
    )


def first_answer_text(row: dict[str, Any]) -> str:
    """MS MARCO answers에서 첫 정답을 고른다. 없거나 'No Answer Present.'면 빈 문자열을 반환한다."""
    for key in ("answers", "wellFormedAnswers"):
        answers = row.get(key) or []
        if isinstance(answers, list):
            for answer in answers:
                clean_answer = str(answer).strip()
                # 빈 답변과 No Answer 표식은 정답 없음으로 보고 건너뛴다.
                if clean_answer and clean_answer != NO_ANSWER_MARKER:
                    return clean_answer
    return ""


def load_tokenizer(tokenizer_name: str) -> Any:
    """output_token_len 계산용 tokenizer를 로드한다."""
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


def count_output_tokens(tokenizer: Any, text: str, max_output_tokens: int) -> int:
    """답변의 토큰 수를 센다. max_output_tokens가 양수면 그 값으로 clamp한다."""
    # add_special_tokens=False로 생성 budget에 해당하는 본문 토큰만 센다.
    token_count = len(tokenizer.encode(text, add_special_tokens=False))
    if max_output_tokens > 0:
        return min(token_count, max_output_tokens)
    return token_count


# ---------------------------------------------------------------------------
# JSONL row 생성
# ---------------------------------------------------------------------------


def build_output_item(
    prompt: str,
    emitted_index: int,
    row: dict[str, Any],
    context_text: str,
    passage_urls: list[str],
    top_k: int,
    selected_index: int,
    answer: str,
    output_token_len: int,
) -> dict[str, Any]:
    """MS MARCO RAG JSONL row와 분석용 메타데이터를 만든다."""
    return {
        # static runner가 식별할 요청 ID와 OpenAI-compatible 입력.
        "request_id": f"msmarco-rag-{emitted_index:06d}",
        "msmarco_query_id": row.get("query_id"),
        "messages": [{"role": "user", "content": prompt}],
        "prompt": prompt,
        "output_text": answer,
        # output_token_len은 static runner가 max_tokens로 소비해 RAG의 짧은 decode를 현실화한다.
        "output_token_len": output_token_len,
        # 분석과 sanity check를 위한 workload metadata.
        "source_dataset": "MS MARCO",
        "cache_pattern": "rag",
        "question": row.get("query"),
        "answer": answer,
        "query_type": row.get("query_type"),
        "passage_urls": passage_urls,
        "num_passages": len(passage_urls),
        "top_k": top_k,
        "selected_index": selected_index,
        "context_char_len": len(context_text),
        "prompt_char_len": len(prompt),
    }


def build_workload_rows(
    rows: Any,
    num_requests: int,
    instruction: str,
    top_k: int,
    normalize_passage_order: bool,
    sort_by_context: bool,
    tokenizer: Any,
    max_output_tokens: int,
) -> list[dict[str, Any]]:
    """MS MARCO row 순회 결과를 RAG JSONL trace row 목록으로 변환한다.

    streaming/비-streaming 모두 지원하기 위해 rows를 단순 iterator로 다루고,
    유효한 요청을 num_requests개 모으면 멈춰 불필요한 다운로드/순회를 피한다.
    """
    from tqdm import tqdm

    workload_rows: list[dict[str, Any]] = []
    # 유효 요청 기준으로 진행바를 갱신한다. num_requests를 알면 total로 남은 양을 보여준다.
    total = num_requests if num_requests > 0 else None
    progress = tqdm(total=total, desc="building", unit="req", dynamic_ncols=True)

    for row in rows:
        question = str(row.get("query", "")).strip()
        answer = first_answer_text(row)
        passage_texts, passage_urls, selected_index = select_passages(
            row=row,
            top_k=top_k,
            normalize_passage_order=normalize_passage_order,
        )
        # query·passage가 비거나 답이 없는(No Answer) row는 의미 없는 RAG 요청이므로 건너뛴다.
        # 답이 있는 row만 남겨 output_token_len이 항상 실제 답변 길이로 채워지게 한다.
        if not question or not passage_texts or not answer:
            continue

        context_text = build_context_text(passage_texts)
        prompt = build_prompt(instruction, context_text, question)
        workload_rows.append(
            build_output_item(
                prompt=prompt,
                emitted_index=len(workload_rows),
                row=row,
                context_text=context_text,
                passage_urls=passage_urls,
                top_k=top_k,
                selected_index=selected_index,
                answer=answer,
                output_token_len=count_output_tokens(tokenizer, answer, max_output_tokens),
            )
        )
        progress.update(1)

        # 필요한 만큼 모으면 멈춰 streaming 다운로드/순회를 더 진행하지 않는다.
        if num_requests > 0 and len(workload_rows) >= num_requests:
            break

    progress.close()

    # 기본은 정렬하지 않아 RAG의 intra-reuse를 낮게 유지한다(저-reuse 대용량 antagonist).
    # --sort-by-context를 주면 같은/유사 context가 연속 배치되어 prefix cache hit가 올라간다.
    if sort_by_context:
        workload_rows.sort(key=lambda item: item["prompt"])
        # 정렬 후 request_id를 재부여해 trace 순서와 ID 순서를 일치시킨다.
        for emitted_index, item in enumerate(workload_rows):
            item["request_id"] = f"msmarco-rag-{emitted_index:06d}"

    return workload_rows


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


def main() -> None:
    """MS MARCO 기반 RAG workload JSONL을 생성한다."""
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
        instruction=args.instruction,
        top_k=args.top_k,
        normalize_passage_order=args.normalize_passage_order,
        sort_by_context=args.sort_by_context,
        tokenizer=tokenizer,
        max_output_tokens=args.max_output_tokens,
    )
    write_jsonl(args.output, workload_rows)

    lengths = [row["context_char_len"] for row in workload_rows]
    out_lens = [row["output_token_len"] for row in workload_rows]
    print(f"requests: {len(workload_rows)}")
    print(f"top_k: {args.top_k if args.top_k > 0 else '전체'}")
    print(f"normalize_passage_order: {args.normalize_passage_order}")
    print(f"sort_by_context: {args.sort_by_context}")
    print(f"max_output_tokens 상한: {'제한 없음' if args.max_output_tokens <= 0 else args.max_output_tokens}")
    print(f"output_token_len min/avg/max: {min(out_lens) if out_lens else 0}/"
          f"{sum(out_lens) / len(out_lens) if out_lens else 0:.1f}/{max(out_lens) if out_lens else 0}")
    print(f"context char len min: {min(lengths) if lengths else 0}")
    print(f"context char len max: {max(lengths) if lengths else 0}")
    print(f"context char len avg: {sum(lengths) / len(lengths) if lengths else 0:.1f}")
    print(f"output: {args.output}")


if __name__ == "__main__":
    main()
