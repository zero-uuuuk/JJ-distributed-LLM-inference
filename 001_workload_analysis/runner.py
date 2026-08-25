"""워크로드를 준비하고 vLLM 측정을 실행하는 파이프라인 진입점."""

import argparse
from pathlib import Path
from typing import Any

import yaml

from scripts.dataset import load_dataset, normalize_dataset


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
    args = parser.parse_args()

    # 설정 로드
    print("[1/3] Loading config...")
    config_path = args.config.expanduser().resolve()
    config = load_config(config_path)

    # 원본 workload 로드
    print("[2/3] Loading datasets...")
    raw_records = load_dataset(config)
    print(f"       loaded raw records: {len(raw_records)}")

    # 공통 request schema 변환
    print("[3/3] Normalizing datasets...")
    records = normalize_dataset(raw_records, config)
    print(f"       normalized records: {len(records)}")


if __name__ == "__main__":
    main()
