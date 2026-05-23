"""Run the text summaries that are useful when inspecting reproduced results."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run(command: list[str]) -> None:
    print("\n$ " + " ".join(command))
    subprocess.run(command, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run turn-level SLO and useful eviction text summaries.")
    parser.add_argument("--results-dir", type=Path, default=Path("../../results"))
    parser.add_argument("--output-dir", type=Path, default=Path("../../results/figures"))
    parser.add_argument("--slo-s", type=float, default=0.5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    results_dir = args.results_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    run(
        [
            sys.executable,
            str(script_dir / "summarize_turn_level_slo.py"),
            "--results-dir",
            str(results_dir),
            "--slo-s",
            str(args.slo_s),
            "--output",
            str(output_dir / "turn_level_slo_observation.txt"),
        ]
    )
    run(
        [
            sys.executable,
            str(script_dir / "summarize_useful_evictions.py"),
            "--results-dir",
            str(results_dir),
            "--output",
            str(output_dir / "useful_eviction_observation.txt"),
        ]
    )


if __name__ == "__main__":
    main()
