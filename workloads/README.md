<div align="center">

# Workloads

**LLM Serving 실험용 JSONL trace 생성 도구 모음**

_JSONL trace · Prefix cache analysis · QuotaServe_

</div>

---

## 개요

`hypothesis_validation`의 Chat + RAG mixed 실험에 쓰는 trace를 생성합니다. 각 워크로드의 상세(인자·스키마·옵션)는 해당 디렉터리의 README를 보세요.

| 워크로드 | 디렉터리 | 역할 | 상세 |
|---|---|---|---|
| **MS MARCO** | [`msmarco/`](msmarco/) | RAG — prefill-heavy antagonist | [README](msmarco/README.md) |
| **ShareGPT** | [`sharegpt/`](sharegpt/) | Chat — multi-turn, decode-heavy victim | [README](sharegpt/README.md) |

---

## 환경 준비

레포지터리 루트에서 의존성을 설치합니다.

```bash
uv pip install -r requirements.txt
```

---

<div align="center">
<sub>MS MARCO · ShareGPT · Workloads · JJ Distributed LLM Inference</sub>
</div>
