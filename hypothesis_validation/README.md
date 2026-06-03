<div align="center">

# Hypothesis Validation

**QuotaServe 가설 검증 문서 인덱스**

_Hypothesis · Case 1 · Single/Mixed workload · APC ON/OFF_

</div>

---

## 개요

이 디렉터리는 QuotaServe 가설을 세우고, Case 1 실험을 실행하고, 결과를 해석하기 위한 문서를 모아둔 곳입니다.

> [!NOTE]
> 실험 실행 전에 [`workloads/`](../workloads/)를 먼저 확인해주세요. Chat/RAG trace의 생성 방식과 token 분포 분석을 함께 볼 수 있습니다.

| 하고 싶은 일 | 먼저 볼 문서 | 실행 대상 |
|---|---|---|
| 가설과 실험 설계를 이해하기 | [`HYPOTHESIS.md`](HYPOTHESIS.md) | - |
| Chat-only/RAG-only baseline 돌리기 | [`RUN_TRACE.md`](case1_validation/RUN_TRACE.md) | [`run_trace.py`](case1_validation/run_trace.py) |
| Chat + RAG mixed 실험 돌리기 | [`RUN_MIXED.md`](case1_validation/RUN_MIXED.md) | [`run_mixed.py`](case1_validation/run_mixed.py) |
| raw 결과 파일 배치 확인하기 | [`raw_results/README.md`](case1_validation/raw_results/README.md) | - |
| Case 1 결과 해석하기 | [`CASE1_ANALYSIS.md`](case1_validation/analysis_results/CASE1_ANALYSIS.md) | - |

## 파일 구조

```text
hypothesis_validation/
├── HYPOTHESIS.md          # QuotaServe 가설과 Case 1 실험 설계
└── case1_validation/
    ├── RUN_TRACE.md      # Chat-only/RAG-only 실행 방법
    ├── RUN_MIXED.md      # Chat + RAG mixed 실행 방법
    ├── raw_results/      # summary JSON과 로컬 raw JSONL 위치
    └── analysis_results/ # Case 1 결과 해석 문서와 그림
```

---

<div align="center">
<sub>Hypothesis Validation · Case 1 · JJ Distributed LLM Inference</sub>
</div>
