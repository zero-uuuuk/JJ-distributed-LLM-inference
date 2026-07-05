# Raw Results

Case 1 실험의 원본 실행 결과를 두는 디렉터리입니다.

## 파일 구조

```text
raw_results/
├── *_summary.json
├── *.jsonl
└── eviction_logs/
    └── *.jsonl
```

| 경로 | 용도 | Git 업로드 |
|---|---|---|
| `*_summary.json` | 실험 결과 요약. 분석 문서에서 주로 참조하는 파일입니다. | 업로드 가능 |
| `*.jsonl` | 요청별 raw 결과.| 업로드하지 않음 |
| `eviction_logs/*.jsonl` | vLLM eviction 계측 로그.| 업로드하지 않음 |

## Eviction log 위치

> [!NOTE]
> Mixed APC ON처럼 eviction을 계측하는 실험은 아래 위치에 로그를 이동시키면 됩니다.

```text
hypothesis_validation/case1_validation/raw_results/eviction_logs/
```
