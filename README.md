# JJ-distributed-LLM-inference

이 프로젝트는 이기종 GPU 환경에서의 효율적인 LLM 분산 추론을 연구하고 구현하는 과제입니다.

> [!IMPORTANT]
> **리포지토리 운영 및 vLLM 싱크 가이드**
> *   **리포지토리 용도**: 본 `JJ-distributed-LLM-inference` 리포지토리는 직접 작성된 코드(실험 스크립트, 벤치마크 툴 등)와 관련 문서(docs) 위주로 관리합니다.
> *   **vLLM 소스 수정**: vLLM 오픈소스의 내부 로직을 직접 수정하는 작업은 [zero-uuuuk/vllm](https://github.com/zero-uuuuk/vllm.git) 독립 리포지토리에서 진행해 주세요.
> *   **EC2 작업**: EC2 인스턴스에서 테스트를 수행할 때도 위 vLLM 리포지토리를 별도로 `clone`하여 작업 환경을 구축해 주시기 바랍니다.