# Prefill Compute-Bound 탐색: Batch Size별 Lm 측정

### 배경 및 동기 (DistServe 논문 기반)

DistServe 논문은 Prefill 인스턴스에서 **batch size가 증가함에 따라 throughput이 증가하다가 GPU 연산 능력이 포화 상태에 도달**하는 문제를 제기합니다.

- **Prefill**: 입력 토큰 전체를 한 번에 처리하는 compute-bound 연산. GPU 연산량이 집중되며 batch size와 prompt 길이가 늘어날수록 연산량이 급증합니다.
- **Compute-bound 포화**: batch size가 커질수록 throughput이 증가하다가 GPU 연산 능력이 포화되면 throughput 증가폭이 둔화되고 latency는 계속 증가합니다.

논문에서는 GPU 포화 지점에서 compute-bound 상태로 만드는 임계 프롬프트 길이 **Lm**을 찾아내어, Prefill 인스턴스에서 Lm보다 짧은 요청은 일괄 처리하고 긴 요청은 개별 스케줄링하는 전략을 제안합니다.

본 실험은 단일 GPU 환경에서 **batch size별로 compute-bound 전환 구간(Lm)을 탐색**하고, 해당 구간에서 throughput·latency·GPU utilization의 변화를 실측하는 것을 목표로 합니다.

---

### 실험 목표

GPU 1장 인스턴스에서 다양한 (batch size, prompt length) 조합을 실험하여 아래를 측정합니다.

| 측정 항목 | 설명 |
|---|---|
| **Prefill latency** | `forward` 1회 완료까지의 GPU 시간 (`torch.cuda.synchronize()` 기준) |
| **Throughput** | `(B × L) / mean_latency_sec` (tokens/sec) |
| **GPU utilization** | `nvidia-smi` 수집 |
| **GPU memory usage** | `nvidia-smi` 수집 |

각 batch size B에 대해 prompt length를 늘려가며 **throughput 증가폭이 둔화되고 GPU utilization이 높은 수준에 도달하는 최초의 길이**를 Lm(B)로 정의합니다.

---

### 실험 환경

- **인스턴스**: AWS EC2 `g4dn.xlarge` (NVIDIA T4 GPU, 16GB VRAM) — Spot Instance
- **모델**: `facebook/opt-6.7b` (8-bit 양자화, ~6.7GB)
- **프레임워크**: `transformers` + `torch`
- **OS**: Ubuntu 22.04 LTS / CUDA 12.1
- **프로파일링**: `nvidia-smi`

---

### 실험 설계

#### 입력 데이터 생성

Tokenization overhead를 배제하고 prefill 연산 특성만 측정하기 위해, **고정 랜덤 시드(seed=42)** 로 생성한 `input_ids`를 사용합니다.

- shape: `[B, L]`
- 토큰 값: vocabulary 범위 내 정수
- 생성 방식: seed 고정 후 1회 생성, 이후 반복 재사용 (run-to-run noise 최소화)

#### 실험 범위

| 단계 | Batch size (B) | Prompt length (L) | Iteration |
|---|---|---|---|
| **Phase 1** (기능 검증) | 1, 2, 4 | 128, 512, 1024 | 8회 (워밍업) |
| **Phase 2** (본 실험) | 1, 2, 4, 8 | 128, 256, 512, 1024, 1536 | 20회 |
| **Phase 2 추가** | 8 | 2048 | 20회 |

- `use_cache=False` (prefill-only, KV cache 비활성화)
- OOM 발생 시 해당 (B, L) 조합은 실패로 기록하고 compute saturation이 아닌 **VRAM 한계**로 해석

#### VRAM 사용량 추정 (모델 8-bit 기준)

| B \ L | 128 | 256 | 512 | 1024 | 1536 | 2048 |
|---|---|---|---|---|---|---|
| **1** | 7.3–8.0 | 7.4–8.1 | 7.5–8.2 | 7.8–8.6 | 8.1–9.0 | 8.5–9.5 |
| **2** | 7.4–8.1 | 7.5–8.3 | 7.8–8.7 | 8.3–9.3 | 8.9–10.2 | 9.5–11.0 |
| **4** | 7.6–8.4 | 7.9–8.8 | 8.4–9.6 | 9.3–11.0 | 10.3–12.3 | 11.4–13.8 |
| **8** | 8.1–9.0 | 8.6–9.8 | 9.6–11.2 | 11.3–13.6 | 12.9–15.5 | 14.5–17.5 |

#### 측정 방법

```python
torch.cuda.synchronize()
start = time.time()

outputs = model(input_ids, use_cache=False)

torch.cuda.synchronize()
end = time.time()
```

GPU 상태는 별도 프로세스에서 `nvidia-smi`로 1초 간격 수집합니다.

```bash
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv -l 1
```

---

### Lm 판정 기준

단일 지표로 판정하지 않고, 아래 조건을 함께 봅니다.

| 조건 | 설명 |
|---|---|
| **Throughput 증가폭 둔화** | prompt length를 늘려도 tokens/sec가 거의 오르지 않음 |
| **Latency 지속 증가** | 처리 시간은 늘어나는데 처리 효율은 정체 |
| **GPU utilization 고수준 유지** | 지속적으로 높은 수준 유지 |

위 조건이 동시에 나타나는 최초의 prompt length를 해당 batch size의 **Lm**으로 추정합니다.

---

### 예상 결과

| 단계 | Throughput | Latency |
|---|---|---|
| 초기 | 빠르게 증가 | 완만히 증가 |
| 전환점 근처 | 증가폭 둔화 | 계속 증가 |
| 포화 이후 | 거의 정체 | 급증 |

**얻을 수 있는 것**
- batch size별 prompt length 임계값 Lm(B)
- Lm 전후의 throughput / latency / GPU utilization 변화 패턴

---

### 디렉토리 구조 (예정)

```
dist_serve_yj/
├── README.md
├── requirements.txt
├── engine.py                  # OPT-6.7B 모델 로딩 및 prefill forward pass 래퍼
│                              #   - prefill_step(): 단일 prefill 실행 및 latency 반환
├── experiment.py              # 실험 진입점
│                              #   - run_phase1(): 기능 검증 (B=[1,2,4], L=[128,512,1024])
│                              #   - run_phase2(): 본 실험 (B=[1,2,4,8], L=[128,...,1536])
│                              #   - record_oom(): OOM 발생 시 실패 기록
├── measure.py                 # 측정 유틸리티
│                              #   - GPUMonitor: nvidia-smi 백그라운드 수집
│                              #   - summarize(): 평균/표준편차 산출
└── results/                   # 실험 결과 저장
    ├── phase1_sanity.json
    ├── phase2_grid.json
    └── phase2_oom_log.json
```
