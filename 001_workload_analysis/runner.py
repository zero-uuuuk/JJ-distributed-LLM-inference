"""워크로드를 준비하고 vLLM 측정을 실행하는 파이프라인 진입점."""

import argparse
import asyncio
from pathlib import Path
from typing import Any

import yaml

from scripts.dataset import load_dataset, normalize_dataset
from scripts.vllm_benchmark import run_vllm, save_results
from scripts.tokenizer import (
    load_tokenized_workloads,
    save_tokenized_workloads,
    tokenize_workloads,
)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


def load_config(path: str | Path) -> dict[str, Any]:
    """YAML 설정을 읽고 상대 경로 기준 디렉터리를 추가한다."""
    # config 경로의 사용자 경로 확장 및 절대경로 변환
    config_path = Path(path).expanduser().resolve()

    # YAML 설정 파싱
    with config_path.open(encoding="utf-8") as input_file:
        config = yaml.safe_load(input_file) or {}
    return config


def main() -> None:

    # CLI 실행 인자 구성
    parser = argparse.ArgumentParser(description="Build and run one workload.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.yaml"),
    )
    parser.add_argument(
        "--workload",
        choices=("chat", "rag", "agent"),
        required=True,
    )
    args = parser.parse_args()

    # 설정 로드
    print("[1/4] Loading config...")
    config_path = args.config.expanduser().resolve()
    config = load_config(config_path)
    output_path = config_path.parent / config.get(
        "output", "workloads/tokenized_workloads.jsonl"
    )

    # tokenized workload 준비
    print("[2/4] Preparing tokenized workloads...")
    if output_path.exists():
        print(f"[SKIP] output already exists: {output_path}")
        records = load_tokenized_workloads(output_path)
    else:
        # 원본 workload 로드
        print("       Loading datasets...")
        raw_records = load_dataset(config)
        print(f"       loaded raw records: {len(raw_records)}")

        # 공통 request schema 변환
        print("       Normalizing datasets...")
        records = normalize_dataset(raw_records, config)
        print(f"       normalized records: {len(records)}")

        # prompt 및 output token 길이 계산
        print("       Tokenizing workloads...")
        records = tokenize_workloads(records, config)

        # tokenized workload 저장
        print("       Saving tokenized workloads...")
        saved_path = save_tokenized_workloads(records, output_path)
        print(f"       saved: {saved_path}")

    # 선택 workload의 vLLM 측정 실행 및 요청별 결과 저장
    print(f"[3/4] Running vLLM: {args.workload}...")
    results = asyncio.run(run_vllm(records, config, args.workload))
    print("[4/4] Saving results...")
    result_path = save_results(args.workload, results, config, config_path.parent)
    print(f"       saved: {result_path}")


if __name__ == "__main__":
    main()
