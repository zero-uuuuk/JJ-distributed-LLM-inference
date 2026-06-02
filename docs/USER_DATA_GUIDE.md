# USER DATA GUIDE

> [!IMPORTANT]
> **vLLM 및 JJ-Distributed-LLM-Inference 환경 구성 가이드**

>
> EC2 인스턴스 시작 시 `User Data` 섹션에 입력하여 vLLM 및 프로젝트 환경을 구축을 위한 스크립트입니다.


```bash
#!/bin/bash
set -e

# 로그 설정: /var/log/user-data.log 에 실행 내용을 남김 (디버깅용)
exec > >(tee /var/log/user-data.log|logger -t user-data -s 2>/dev/console) 2>&1

echo "=========================================================="
echo ">>> Start User Data Configuration..."
echo "=========================================================="

# 1. 시스템 패키지 설치 (root 권한 실행)
echo ">>> [1/3] Updating and Installing System Packages..."
apt-get update
apt-get install -y git build-essential curl nvtop
echo ">>> [SUCCESS] System Packages Installed."

# 2. uv 설치 (ubuntu 계정으로 실행)
echo ">>> [2/3] Installing uv for user 'ubuntu'..."
su - ubuntu -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
echo ">>> [SUCCESS] uv Installed."

# 3. Git Clone 및 Python 환경 설정 (ubuntu 계정으로 실행)
# 주의: 모든 작업은 ubuntu 사용자의 홈 디렉토리 및 권한으로 수행되어야 함
echo ">>> [3/3] Setting up Project Environment for user 'ubuntu'..."

# 4. ubuntu 유저로 접속해 권한 에러 방지
su - ubuntu -c '
    # 홈 디렉토리로 이동
    cd /home/ubuntu

    # Git Clone (1)
    # 이미 폴더가 있으면 에러가 날 수 있으니 체크
    if [ ! -d "JJ-Distributed-LLM-Inference" ]; then
        echo ">>> Cloning JJ-Distributed-LLM-Inference..."
        git clone -b research/QuotaServe-supplement https://<your_github_token>@github.com/zero-uuuuk/JJ-Distributed-LLM-Inference.git
        echo ">>> [SUCCESS] JJ-Distributed-LLM-Inference cloned."
    fi

    # Git Clone (2)
    if [ ! -d "vllm" ]; then
        echo ">>> Cloning vllm..."
        git clone -b research/QuotaServe-temp https://github.com/zero-uuuuk/vllm
        echo ">>> [SUCCESS] vllm cloned."
    fi

    cd vllm

    # 가상환경 생성 (Python 3.12)
    echo ">>> Creating Virtual Environment with uv (Python 3.12)..."
    uv venv --python 3.12
    echo ">>> [SUCCESS] Virtual Environment Created."

    # 가상환경 활성화 및 패키지 설치
    echo ">>> Activating Virtual Environment and Installing Packages..."
    source .venv/bin/activate

    # vLLM 설치
    VLLM_USE_PRECOMPILED=1 uv pip install -e .

    # 설치 확인
    python -c "import vllm; print(vllm.__version__)"

    echo ">>> Python Environment Setup Complete"
'

# 5. LD_LIBRARY_PATH 환경 변수 영구 등록
# 스크립트 실행 중에는 적용되지 않으므로, 유저가 접속할 때마다 적용되도록 .bashrc에 기록
LIB_PATH="/home/ubuntu/vllm/.venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib"

# 이미 등록되어 있는지 확인 후 추가
if ! grep -q "$LIB_PATH" /home/ubuntu/.bashrc; then
    echo "export LD_LIBRARY_PATH=\$LD_LIBRARY_PATH:$LIB_PATH" >> /home/ubuntu/.bashrc
    echo ">>> LD_LIBRARY_PATH added to .bashrc"
fi

echo "✅ User Data Script Finished Successfully!"
```

> [!TIP]
> **로그 확인 및 디버깅**
>
> 설정 과정에서 오류가 발생할 경우 `/var/log/user-data.log` 로그 파일을 확인하여 원인을 파악하고 디버깅하면 됩니다.
