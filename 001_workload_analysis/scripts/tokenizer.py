"""normalized request의 prompt와 output token 수를 계산한다."""

import json
from pathlib import Path
from typing import Any

from transformers import AutoTokenizer
from tqdm import tqdm


def load_tokenized_workloads(path: str | Path) -> list[dict[str, Any]]:
    """저장된 tokenized workload JSONL을 읽는다."""
    records: list[dict[str, Any]] = []
    with Path(path).expanduser().resolve().open(encoding="utf-8") as input_file:
        for line in input_file:
            if line.strip():
                records.append(json.loads(line))
    return records


def tokenize_workloads(
    records: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """normalized workloads에 prompt_token_len과 output_token_len을 추가한다."""
    # 설정된 Hugging Face tokenizer 로드
    tokenizer = AutoTokenizer.from_pretrained(config["tokenizer"]["name"])

    # workload별 prompt·output token 길이 계산
    tokenized: list[dict[str, Any]] = []
    for record in tqdm(records, desc="tokenize"):
        prompt_tokens = tokenizer.apply_chat_template(
            record["messages"],
            tokenize=True,
            add_generation_prompt=True,
            return_dict=False,
        )
        output_tokens = tokenizer.encode(
            record.get("output_text", ""),
            add_special_tokens=False,
        )
        # token 길이 필드 병합
        item = record.copy()
        item["prompt_token_len"] = len(prompt_tokens)
        item["output_token_len"] = len(output_tokens)
        tokenized.append(item)
    return tokenized


def save_tokenized_workloads(records: list[dict[str, Any]], path: str | Path) -> Path:
    """tokenized workloads를 JSONL 파일로 저장한다."""
    # 출력 절대경로 변환 및 부모 디렉터리 생성
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    # record별 JSONL 직렬화 저장
    with output.open("w", encoding="utf-8") as output_file:
        for record in records:
            output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    return output
