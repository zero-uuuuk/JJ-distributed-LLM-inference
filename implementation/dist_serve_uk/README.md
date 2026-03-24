# Prefill-Decoding Interference 측정

### 배경 및 동기 (DistServe 논문 기반)

DistServe 논문은 LLM 서빙에서 **Prefill phase**와 **Decoding phase**가 서로 다른 자원 특성을 가진다는 점을 핵심 문제로 제기합니다.

- **Prefill**: 입력 토큰 전체를 한 번에 처리하는 compute-bound 연산. GPU 연산량이 집중되며 짧은 시간에 큰 부하를 유발합니다.
- **Decoding**: 토큰을 하나씩 자기회귀적으로 생성하는 memory-bandwidth-bound 연산. 지속적으로 KV Cache를 읽고 쓰며 낮은 연산 밀도를 가집니다.

두 phase를 동일한 GPU에서 함께 실행(co-location)하면, Prefill의 연산 폭발이 Decoding 배치의 **TPOT(Time Per Output Token)** 지연을 유발합니다. DistServe는 이를 해결하기 위해 Prefill과 Decoding을 물리적으로 분리된 인스턴스에 배치하는 **Disaggregated Serving** 아키텍처를 제안합니다.

본 실험은 이 간섭 현상을 **실측**하는 것을 목표로 합니다.

---

### 실험 목표

동일한 GPU에서 아래 세 조건을 비교하여 Prefill이 Decoding에 미치는 **순수 간섭 효과**를 정량화합니다.

| 조건 | 구성 | 목적 |
|------|------|------|
| **Baseline** | Decoding N개 (iter 0~199) | 기준 TPOT |
| **Baseline+1** | Decoding N+1개 (iter 0~199) | 배치 크기 증가 효과만 분리 |
| **Treatment** | iter 0: Decoding N개 + Prefill 1개 / iter 1~199: Decoding N+1개 | Prefill 간섭 효과 측정 |

- **Treatment[iter 0] - Baseline[iter 0]** = 배치 크기 효과 + prefill 간섭 효과
- **Treatment[iter 0] - Baseline+1[iter 0]** = 순수 prefill 간섭 효과 (배치 크기 효과 통제)
- iter 1~199에서는 Treatment와 Baseline+1이 동일한 구성(decoding N+1개)이므로 비교 의미 없음

측정 지표:
- **TPOT (Time Per Output Token)**: Decoding 배치의 iteration별 토큰 생성 시간. iteration 0에서의 spike와 이후 회복을 시계열로 관찰.

---

### 실험 환경

- **인스턴스**: AWS EC2 `g5.xlarge` (NVIDIA A10G GPU, 24GB VRAM) — Spot Instance
- **모델**: `facebook/opt-6.7b` (fp16, ~13.4GB)
  - 가용 VRAM: ~10.6GB (KV Cache용)
- **프레임워크**: `transformers` + `torch`

---

### 실험 설계

#### 데이터셋 및 워크로드 파라미터

| 파라미터 | 값 |
|---|---|
| 입력 데이터 | Random token IDs (`seed=42` 고정) |
| Decoding batch size N | 8, 16, 32 sweep |
| Input tokens (decoding 요청) | 256 tokens |
| Output tokens (decoding 요청) | 200 tokens |
| Prefill 요청 input tokens | 512 tokens |
| Prefill 요청 output tokens | 1 token (prefill 연산만 유발하고 이후 decoding 부담 최소화) |
| Run 반복 횟수 | 10회 (평균 및 표준편차 산출) |

**VRAM 사용량 추정 (batch=32 기준)**
- 공식: `2(K+V) × layers × batch × seq_len × num_heads × head_dim × 2B(fp16)`
- Decoding KV Cache: `2 × 32 × 32 × 456 × 32 × 128 × 2B` ≈ 7.65GB
- Prefill KV Cache: `2 × 32 × 1 × 512 × 32 × 128 × 2B` ≈ 0.27GB
- 합계: 13.4 + 7.65 + 0.27 ≈ **21.3GB** → A10G 24GB 내 수용 가능
- batch=32 Baseline+1(N+1=33)은 OOM 위험 → **Baseline+1은 N=8, 16까지만 적용**

#### 측정 방법

TPOT는 각 decoding iteration의 wall-clock time으로 측정합니다.

```python
for iteration in range(output_tokens):
    t0 = time.perf_counter()
    # forward pass
    t1 = time.perf_counter()
    tpot_per_iter[iteration] = t1 - t0
```

10회 반복 후 iteration별 TPOT의 평균과 표준편차를 기록합니다.

#### 실험 절차

**준비 단계 (측정 제외)**
- Decoding batch 내 N개 요청을 모두 prefill 완료 → KV Cache 확보
- 양쪽 조건에 동일하게 적용되므로 통제됨

**측정 시작 (iteration 0부터)**

LLM 서빙은 매 iteration마다 forward pass를 1번 실행하며, 해당 iteration에 스케줄된 모든 요청을 동시에 처리합니다.

```
[Baseline]
iteration 0:   forward pass (decoding N개)           → TPOT 기록
iteration 1:   forward pass (decoding N개)           → TPOT 기록
...
iteration 199: forward pass (decoding N개)           → TPOT 기록

[Baseline+1]  (N=8, 16만 적용)
iteration 0:   forward pass (decoding N+1개)         → TPOT 기록
iteration 1:   forward pass (decoding N+1개)         → TPOT 기록
...
iteration 199: forward pass (decoding N+1개)         → TPOT 기록

[Treatment]
iteration 0:   forward pass (decoding N개 + prefill 512 tokens)  → TPOT 기록  ← 간섭 발생
               prefill 완료 → 신규 요청이 decoding 단계로 전환
iteration 1:   forward pass (decoding N+1개)         → TPOT 기록
...
iteration 199: forward pass (decoding N+1개)         → TPOT 기록
```

- prefill은 iteration 0의 forward pass 1번으로 완료되며, 이 iteration의 TPOT가 spike
- iteration 1부터는 신규 요청도 decoding으로 전환되어 Baseline+1과 동일한 구성
- `Treatment[iter 0] - Baseline[iter 0]` = prefill 간섭의 직접적인 크기
- `Treatment[iter 1~] - Baseline+1[iter 1~]` ≈ 0 이어야 정상 (sanity check)
- batch size N을 8, 16, 32로 sweep하여 간섭 강도 변화를 관찰

---

### 예상 결과 (DistServe 논문 기반 가설)

- Prefill 삽입 직후 iteration에서 TPOT가 **수 배 이상** 증가할 것으로 예상
- Prefill 완료 후 이후 iteration에서는 TPOT가 정상 수준으로 회복
- 이 현상이 DistServe가 주장하는 **co-location interference**의 실증적 근거가 됨

---

### 디렉토리 구조 (예정)

```
dist_serve_uk/
├── README.md
├── requirements.txt
├── engine.py                  # OPT-6.7B 모델 로딩 및 forward pass 래퍼
│                              #   - prefill_and_cache(): 준비 단계용 (측정 제외)
│                              #   - decode_step(): 단일 decoding step 실행
│                              #   - prefill_step(): 단일 prefill step 실행 (Treatment용)
├── experiment.py              # 실험 진입점
│                              #   - run_baseline(batch_size, n_runs)
│                              #   - run_baseline_plus1(batch_size, n_runs)
│                              #   - run_treatment(batch_size, n_runs)
├── measure.py                 # 측정 유틸리티
│                              #   - IterTimer: perf_counter 기반 iteration별 TPOT 기록
│                              #   - summarize(): 평균/표준편차 산출
└── results/                   # 실험 결과 저장
    ├── tpot_batch8.json
    ├── tpot_batch16.json
    └── tpot_batch32.json
```
