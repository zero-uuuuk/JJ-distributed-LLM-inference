# AWS 요구사항 명세서 — dist_serve_yj

Prefill Compute-Bound 탐색 실험 (`dist_serve_yj`) 을 AWS에서 수행하기 위한 인프라 요구사항을 정의합니다.

---

## 1. 인스턴스 사양

| 항목 | 값 |
|---|---|
| **인스턴스 타입** | `g4dn.xlarge` |
| **구매 방식** | Spot Instance |
| **GPU** | NVIDIA T4 (16GB VRAM) |
| **vCPU** | 4 |
| **RAM** | 16GB |
| **스토리지** | 50GB gp3 EBS (모델 체크포인트 + 실험 결과 저장) |
| **리전** | `ap-northeast-2` (서울) 또는 Spot 가용성 높은 리전 |

> **Spot Instance 선택 이유**
> 현재 계정 Quota: `G / VT Spot Instance vCPU per Region = 8`
> g4dn.xlarge(vCPU 4)는 Quota 내에서 실행 가능하며, 단일 인스턴스로 충분합니다.

---

## 2. AMI / OS 요구사항

| 항목 | 값 |
|---|---|
| **AMI** | Ubuntu 22.04 LTS (Deep Learning Base OSS Nvidia Driver AMI 권장) |
| **NVIDIA Driver** | 535 계열 이상 |
| **CUDA Toolkit** | 12.1 |
| **cuDNN** | CUDA 12.x 호환 버전 |

> AWS Marketplace의 **"Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)"** 를 사용하면 드라이버·CUDA 사전 설치 상태로 시작할 수 있습니다.

---

## 3. 네트워크 / 보안 그룹

| 규칙 | 프로토콜 | 포트 | 소스 |
|---|---|---|---|
| SSH 접속 | TCP | 22 | 내 IP (또는 팀 IP 대역) |

- 본 실험은 외부 트래픽을 수신하지 않으므로 인바운드는 SSH만 허용합니다.
- 아웃바운드는 전체 허용 (Hugging Face 모델 다운로드, pip 패키지 설치용).

---

## 4. IAM 요구사항

본 실험에서는 AWS 서비스 API 호출이 없으므로 별도 IAM Role이 불필요합니다.

단, 실험 결과를 S3에 업로드할 경우 아래 최소 권한을 부여합니다.

```json
{
  "Effect": "Allow",
  "Action": ["s3:PutObject", "s3:GetObject"],
  "Resource": "arn:aws:s3:::your-bucket-name/dist_serve_yj/*"
}
```

---

## 5. 소프트웨어 스택

인스턴스 기동 후 아래 순서로 환경을 구성합니다.

### 5-1. 시스템 패키지

```bash
sudo apt-get update && sudo apt-get install -y \
    git \
    python3.10 \
    python3-pip \
    nvidia-utils-535   # nvidia-smi 포함
```

### 5-2. Python 패키지

```bash
pip install \
    torch --index-url https://download.pytorch.org/whl/cu121 \
    transformers \
    accelerate \
    bitsandbytes \   # 8-bit 양자화용
    numpy
```

### 5-3. 모델 다운로드

```bash
# Hugging Face CLI로 사전 다운로드 (실험 중 네트워크 의존 제거)
pip install huggingface_hub
huggingface-cli download facebook/opt-6.7b --local-dir ./models/opt-6.7b
```

> `facebook/opt-6.7b` 전체 파일 크기 약 **13GB**.
> EBS 50GB 기준 모델 + 결과 파일 수용 가능합니다.

---

## 6. 스토리지 요구사항

| 경로 | 용도 | 필요 용량 |
|---|---|---|
| `./models/opt-6.7b/` | 모델 체크포인트 | ~13GB |
| `./results/` | 실험 결과 JSON | ~수 MB |
| 시스템 + 패키지 | OS, CUDA, pip | ~20GB |
| **합계** | | **~35GB** → 50GB gp3으로 충분 |

---

## 7. VRAM 요구사항 및 OOM 기준

모델 8-bit 양자화 기준 VRAM 사용량 추정 (단위: GB):

| B \ L | 128 | 256 | 512 | 1024 | 1536 | 2048 |
|---|---|---|---|---|---|---|
| **1** | 7.3–8.0 | 7.4–8.1 | 7.5–8.2 | 7.8–8.6 | 8.1–9.0 | 8.5–9.5 |
| **2** | 7.4–8.1 | 7.5–8.3 | 7.8–8.7 | 8.3–9.3 | 8.9–10.2 | 9.5–11.0 |
| **4** | 7.6–8.4 | 7.9–8.8 | 8.4–9.6 | 9.3–11.0 | 10.3–12.3 | 11.4–13.8 |
| **8** | 8.1–9.0 | 8.6–9.8 | 9.6–11.2 | 11.3–13.6 | 12.9–15.5 | **14.5–17.5 ⚠️** |

- T4 VRAM 16GB 기준, `B=8, L=2048` 구간에서 OOM 가능성 있음
- OOM 발생 시 compute saturation이 아닌 **VRAM 한계**로 기록하고 실험 계속

---

## 8. Spot Instance 중단 대응

Spot Instance는 AWS 회수 시 2분 전 중단 알림이 발생합니다.

### 권장 대응

1. **체크포인트 저장**: 각 (B, L) 조합 완료 시마다 `results/` 에 즉시 JSON 저장
2. **재시작 스크립트**: 이미 완료된 (B, L) 조합은 건너뛰는 resume 로직 구현
3. **중단 감지 (선택)**: EC2 메타데이터 엔드포인트 폴링

```bash
# Spot 중단 알림 감지 (2분 내 저장 트리거)
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/spot/termination-time
```

---

## 9. 비용 추정

| 항목 | 단가 (서울 리전 기준) | 예상 사용 시간 | 예상 비용 |
|---|---|---|---|
| g4dn.xlarge Spot | ~$0.16/hr | 3–5시간 | ~$0.5–0.8 |
| EBS gp3 50GB | ~$0.08/GB-month | 1일 이내 | < $0.1 |
| 데이터 전송 | 무료 (S3 업로드 아웃바운드) | — | ~$0 |
| **합계** | | | **~$1 이내** |

---

## 10. 실행 체크리스트

```
[ ] g4dn.xlarge Spot 요청 생성 (ap-northeast-2 또는 us-east-1)
[ ] 보안 그룹: SSH(22) 인바운드만 허용
[ ] EBS 50GB gp3 연결
[ ] Deep Learning Base AMI (Ubuntu 22.04) 선택
[ ] SSH 접속 후 python 패키지 설치 (torch, transformers, bitsandbytes)
[ ] 모델 다운로드: facebook/opt-6.7b (~13GB)
[ ] nvidia-smi 동작 확인: nvidia-smi --query-gpu=name,memory.total --format=csv
[ ] Phase 1 기능 검증 실행 (B=[1,2,4], L=[128,512,1024])
[ ] Phase 2 본 실험 실행
[ ] 결과 파일 로컬 또는 S3로 백업
[ ] 인스턴스 종료 (비용 절감)
```
