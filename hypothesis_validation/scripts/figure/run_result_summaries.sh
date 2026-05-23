#!/usr/bin/env bash
set -euo pipefail

cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation/scripts/figure
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

python run_result_summaries.py \
  --results-dir /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation/results \
  --output-dir /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation/results/figures \
  --slo-s 0.5
