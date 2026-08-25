<div align="center">

# JJ Distributed LLM Inference

**클라우드 GPU 클러스터에서의 LLM 서빙 연구 · 한양대학교 졸업 프로젝트**

_Hanyang University · Data Science · 2026_

[![Branch](https://img.shields.io/badge/branch-research%2Fpaper--reading-blue?style=flat-square&logo=git)](https://github.com/zero-uuuuk/JJ-distributed-LLM-inference/tree/research/paper-reading-experiments)
[![vLLM](https://img.shields.io/badge/based%20on-vLLM-6c47ff?style=flat-square&logo=pytorch)](https://github.com/zero-uuuuk/vllm)
[![Status](https://img.shields.io/badge/status-In%20Progress-orange?style=flat-square)](#)

</div>

---

## 프로젝트 개요

클라우드 GPU 클러스터 환경에서 LLM Serving의 성능 한계를 분석하고, 이를 개선하는 시스템 연구를 진행하는 한양대학교 졸업 프로젝트입니다.

단순한 분산 추론 구현을 넘어, **LLM Serving 내부의 Cache 동작 · 스케줄링 · 워크로드 간 간섭** 등의 문제를 정량화하고 새로운 정책을 제안하는 것을 목표로 합니다.

---

## 연구 주제 및 브랜치 구조

```
main
│
├── research/paper-reading-experiments  [기록용]
│   └── Phase 1: Distributed Inference 논문 리딩 & 초기 실험 아카이브
│       (DistServe · SplitWise · SpotServe)
│
├── research/CCD_archive  [폐기 · 기록용]
│   └── Phase 2: Cache-Conditioned Disaggregation
│       APC 환경에서 PD가 항상 최적이 아님을 관찰,
│       cache state 기반으로 PD / Mixed / Chunked 모드를 선택하는 스케줄러 제안
│       → motivation 불충분으로 폐기, 실험 기록 보관용
│
└── research/QuotaServe 
    └── Phase 3: Cross-Workload Prefix Cache Pollution
        Mixed Serving에서 워크로드 간 prefix KV cache 간섭 문제 정의,
        SLO-aware soft quota policy로 fairness & goodput 동시 확보
```

---

## 리포지토리 운영 규칙

> [!IMPORTANT]
> **코드 & vLLM 싱크 가이드**
>
> - **이 레포**: 직접 작성한 실험 스크립트, 벤치마크 도구, 관련 문서만 관리합니다.
> - **vLLM 수정**: 오픈소스 내부 로직 수정은 **[zero-uuuuk/vllm](https://github.com/zero-uuuuk/vllm.git)** 독립 레포에서 진행합니다.
> - **EC2 작업**: 인스턴스에서 테스트 시 위 vLLM 레포를 별도로 `clone`하여 환경을 구성합니다.

---

## 저자

| 이름 | GitHub |
|:---:|:---:|
| 장영욱 | [@zero-uuuuk](https://github.com/zero-uuuuk) |
| 정유진 | [@yvz1225](https://github.com/yvz1225) |

---

<div align="center">
<sub>Hanyang University · Data Science · 2026</sub>
</div>