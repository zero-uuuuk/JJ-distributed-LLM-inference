# Figure Scripts

`hypothesis_validation/results/`에 있는 JSONL 결과를 읽어서 논문용 확인 지표를 CSV와 PNG로 저장한다.

기본 출력 위치는 다음과 같다.

```text
hypothesis_validation/results/figures/
```

## 준비

`matplotlib`가 필요하다.

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation
source /home/ubuntu/JJ-distributed-LLM-inference/.venv/bin/activate

uv pip install matplotlib
```

Windows 로컬에서도 workspace root가 같으면 동일하게 실행할 수 있다.

## 1. Context Length Sweep

RAG long context 길이에 따라 Chat victim이 얼마나 무너지는지 본다.

비교 대상:

```text
ctx500
ctx1500
ctx3000
```

필요 파일:

```text
mixed_chat5_rag5_longctx500_util06.jsonl
eviction_mixed_chat5_rag5_longctx500_util06.jsonl
mixed_chat5_rag5_longctx1500_util06.jsonl
eviction_mixed_chat5_rag5_longctx1500_util06.jsonl
mixed_chat5_rag5_longctx3000_util06.jsonl
eviction_mixed_chat5_rag5_longctx3000_util06.jsonl
```

실행:

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation/scripts/figure

python plot_context_length_sweep.py
```

출력:

```text
hypothesis_validation/results/figures/context_length_sweep.png
hypothesis_validation/results/figures/context_length_sweep_summary.csv
```

주요 지표:

- Chat SLO attainment
- RAG SLO attainment
- UsefulCrossEviction(chat <- RAG)
- RAGShareOfUsefulChatEviction

## 2. Capacity Sweep

`--gpu-memory-utilization` 값을 바꾸면서 KV cache capacity가 줄어들 때 피해가 커지는지 본다.

비교 대상:

```text
util 0.96
util 0.7
util 0.6
util 0.5
```

필요 파일:

```text
mixed_chat5_rag5_longctx1500_util096.jsonl
eviction_mixed_chat5_rag5_longctx1500_util096.jsonl
mixed_chat5_rag5_longctx1500_util07.jsonl
eviction_mixed_chat5_rag5_longctx1500_util07.jsonl
mixed_chat5_rag5_longctx1500_util06.jsonl
eviction_mixed_chat5_rag5_longctx1500_util06.jsonl
mixed_chat5_rag5_longctx1500_util05.jsonl
eviction_mixed_chat5_rag5_longctx1500_util05.jsonl
```

실행:

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation/scripts/figure

python plot_capacity_sweep.py
```

출력:

```text
hypothesis_validation/results/figures/capacity_sweep.png
hypothesis_validation/results/figures/capacity_sweep_summary.csv
```

주요 지표:

- Chat token-weighted hit rate
- Chat/RAG SLO attainment
- UsefulCrossEviction(chat <- RAG)

## 3. Turn-Level SLO

Chat victim의 turn이 깊어질수록 mixed workload에서 SLO가 어떻게 무너지는지 본다.

비교 대상:

```text
isolated
ctx500
ctx1500
ctx3000
```

필요 파일:

```text
chat_isolated_turn9_qps5.jsonl
mixed_chat5_rag5_longctx500_util06.jsonl
mixed_chat5_rag5_longctx1500_util06.jsonl
mixed_chat5_rag5_longctx3000_util06.jsonl
```

실행:

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation/scripts/figure

python plot_turn_level_slo.py
```

출력:

```text
hypothesis_validation/results/figures/turn_level_slo.png
hypothesis_validation/results/figures/turn_level_slo_summary.csv
```

## 4. Text Summaries for Reproduction Checks

다른 사람이 결과를 재현했을 때 빠르게 확인하기 위한 텍스트 요약이다.

Turn별 Chat SLO / TTFT:

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation/scripts/figure

python summarize_turn_level_slo.py
```

Useful eviction attribution:

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation/scripts/figure

python summarize_useful_evictions.py
```

둘을 한 번에 실행하고 텍스트 파일로 남기려면:

```bash
cd /home/ubuntu/JJ-distributed-LLM-inference/hypothesis_validation/scripts/figure

bash run_result_summaries.sh
```

출력:

```text
hypothesis_validation/results/figures/turn_level_slo_observation.txt
hypothesis_validation/results/figures/useful_eviction_observation.txt
```

주요 확인 흐름:

```text
RAG context length 증가
-> useful chat<-rag eviction 증가
-> Chat later-turn SLO 감소 / TTFT P95 증가
```

## Windows 예시

```powershell
cd C:\JJ-distributed-LLM-inference\hypothesis_validation\scripts\figure

python plot_context_length_sweep.py
python plot_capacity_sweep.py
python plot_turn_level_slo.py
python run_result_summaries.py --output-dir .
```

다른 위치에 저장하려면 `--results-dir`, `--output-dir`를 직접 지정한다.

```powershell
python plot_context_length_sweep.py `
  --results-dir C:\JJ-distributed-LLM-inference\hypothesis_validation\results `
  --output-dir C:\JJ-distributed-LLM-inference\hypothesis_validation\results\figures
```
