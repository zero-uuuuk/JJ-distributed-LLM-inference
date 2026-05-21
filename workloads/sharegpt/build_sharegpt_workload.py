"""역할: ShareGPT 원시 데이터를 multi-turn JSONL trace로 변환한다.

상세 과정:
  1. Hugging Face Hub에서 ShareGPT 원시 JSON 경로를 확보한다.
  2. human/gpt가 교대하는 clean 대화만 고른다.
  3. 각 대화를 turn 단위 요청으로 펼친 뒤 turn 번호 기준으로 JSONL trace를 저장한다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 기본값
# ---------------------------------------------------------------------------


DEFAULT_REPO_ID = "anon8231489123/ShareGPT_Vicuna_unfiltered"
DEFAULT_FILENAME = "ShareGPT_V3_unfiltered_cleaned_split.json"


# ---------------------------------------------------------------------------
# JSON 입출력
# ---------------------------------------------------------------------------


def load_json(path: Path) -> Any:
    """JSON 파일 전체를 로드한다."""
    resolved_path = path.expanduser().resolve()
    with resolved_path.open("r", encoding="utf-8") as input_file:
        return json.load(input_file)


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
    """ShareGPT multi-turn trace 생성에 필요한 CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="ShareGPT 원시 데이터를 multi-turn JSONL trace로 변환합니다.",
    )
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument("--filename", default=DEFAULT_FILENAME)
    parser.add_argument("--repo-type", default="dataset")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--num-conversations",
        type=int,
        default=5_000,
        help="trace에 사용할 clean 대화 수입니다. 0 이하이면 전체 clean 대화를 사용합니다.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 원시 데이터 경로 확보
# ---------------------------------------------------------------------------


def download_raw_path(repo_id: str, filename: str, repo_type: str) -> Path:
    """Hugging Face Hub cache에서 ShareGPT 원시 JSON 경로를 확보한다."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise SystemExit(
            "의존성 `huggingface_hub`가 없습니다. `pip install huggingface-hub`로 설치하세요."
        ) from exc

    # 원시 파일은 repo 내부 산출물로 복사하지 않고 Hugging Face cache 경로를 직접 사용한다.
    downloaded_path = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        repo_type=repo_type,
    )
    return Path(downloaded_path).resolve()


# ---------------------------------------------------------------------------
# 대화 필터링
# ---------------------------------------------------------------------------


def is_clean_conversation(messages: list[dict[str, Any]]) -> bool:
    """human/gpt 교대 구조가 완전하고 빈 발화가 없는 대화인지 검사한다."""
    if len(messages) < 2 or len(messages) % 2 != 0:
        return False

    for index, message in enumerate(messages):
        expected_role = "human" if index % 2 == 0 else "gpt"
        if message.get("from") != expected_role:
            return False
        if not str(message.get("value", "")).strip():
            return False

    return True


def select_clean_conversations(
    raw_rows: list[dict[str, Any]],
    num_conversations: int,
) -> list[dict[str, Any]]:
    """원시 row에서 clean 대화만 선택하고 최대 개수로 제한한다."""
    clean_rows = [
        row
        for row in raw_rows
        if is_clean_conversation(row.get("conversations", []))
    ]
    if num_conversations > 0:
        return clean_rows[:num_conversations]
    return clean_rows


# ---------------------------------------------------------------------------
# turn 단위 요청 생성
# ---------------------------------------------------------------------------


def build_requests(conversation_row: dict[str, Any]) -> list[dict[str, Any]]:
    """하나의 ShareGPT 대화를 turn별 요청 리스트로 변환한다."""
    conversation_id = conversation_row.get("id")
    messages = conversation_row["conversations"]
    history: list[dict[str, str]] = []
    requests: list[dict[str, Any]] = []

    for turn_index, message_index in enumerate(range(0, len(messages), 2), start=1):
        user_message = messages[message_index]
        assistant_message = messages[message_index + 1]

        # 현재 user 발화까지 포함한 prompt가 이번 요청의 입력이다.
        prompt_messages = history + [
            {"role": "user", "content": user_message["value"]},
        ]

        # 원본 assistant 응답은 후처리 분석에서 참조할 label로 보존한다.
        requests.append(
            {
                "request_id": f"{conversation_id}_turn_{turn_index}",
                "conversation_id": conversation_id,
                "turn_id": turn_index,
                "messages": prompt_messages,
                "output_text": assistant_message["value"],
                "source_dataset": "ShareGPT",
                "cache_pattern": "multi_turn",
            }
        )

        # 다음 turn의 prefix cache 재사용 구조를 보존하기 위해 전체 이력을 누적한다.
        history.append({"role": "user", "content": user_message["value"]})
        history.append({"role": "assistant", "content": assistant_message["value"]})

    return requests


def build_all_requests(clean_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """clean 대화 목록을 turn 번호 기준 요청 리스트로 펼친다."""
    conversation_requests = [build_requests(row) for row in clean_rows]
    requests: list[dict[str, Any]] = []
    max_turn_count = max((len(items) for items in conversation_requests), default=0)

    for turn_offset in range(max_turn_count):
        # 같은 turn 번호끼리 먼저 배치해 phase 실험의 warm/probe 간 prefix 재사용을 쉽게 만든다.
        for items in conversation_requests:
            if turn_offset < len(items):
                requests.append(items[turn_offset])
    return requests


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------


def main() -> None:
    """ShareGPT 원시 데이터를 로드하고 multi-turn trace를 저장한다."""
    args = parse_args()
    raw_path = download_raw_path(
        repo_id=args.repo_id,
        filename=args.filename,
        repo_type=args.repo_type,
    )

    raw_rows = load_json(raw_path)
    clean_rows = select_clean_conversations(raw_rows, args.num_conversations)
    requests = build_all_requests(clean_rows)

    # prefix cache locality 분석을 위해 같은 turn 번호끼리 묶은 trace를 저장한다.
    write_jsonl(args.output, requests)

    print(f"원시 파일: {raw_path}")
    print(f"원시 대화 수: {len(raw_rows)}")
    print(f"사용한 clean 대화 수: {len(clean_rows)}")
    print(f"요청 수: {len(requests)}")
    print(f"저장 완료: {args.output}")


if __name__ == "__main__":
    main()
