"""
engine.py
OPT-6.7B 모델 로딩 및 prefill forward pass 래퍼.

- 8-bit 양자화 (bitsandbytes) 로 모델 로드
- prefill_step(input_ids) : 단일 prefill 실행 → latency(sec) 반환
"""

import time
import torch
from transformers import AutoModelForCausalLM, BitsAndBytesConfig


class PrefillEngine:
    """OPT-6.7B prefill-only 실행 엔진."""

    def __init__(
        self,
        model_name: str = "facebook/opt-6.7b",
        load_in_8bit: bool = True,
    ) -> None:
        """
        Args:
            model_name  : Hugging Face 모델 ID 또는 로컬 경로.
            load_in_8bit: True면 bitsandbytes 8-bit 양자화 적용.
        """
        print(f"[engine] 모델 로딩 중: {model_name} (8-bit={load_in_8bit})")
        quantization_config = BitsAndBytesConfig(load_in_8bit=True) if load_in_8bit else None
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            quantization_config=quantization_config,
            device_map="auto",           # GPU 자동 배치
            dtype=torch.float16,         # 8-bit 로더와 혼용 가능
            use_safetensors=True,        # torch.load CVE-2025-32434 우회
        )
        self.model.eval()
        print("[engine] 모델 로딩 완료")

    # ------------------------------------------------------------------
    # 공개 인터페이스
    # ------------------------------------------------------------------

    def prefill_step(self, input_ids: torch.Tensor) -> float:
        """
        단일 prefill forward pass를 실행하고 wall-clock latency를 반환한다.

        CPU 타이밍이 아닌 GPU 완료 시각으로 측정하기 위해
        시작/종료 양 쪽에서 `torch.cuda.synchronize()` 를 호출한다.

        측정 구간:
            [synchronize → forward → synchronize]
            tokenization 및 데이터 이동은 측정 구간 밖에서 수행할 것.

        Args:
            input_ids: shape [B, L], dtype torch.long.
                       device 이동은 호출 전에 완료되어 있어야 한다.

        Returns:
            latency_sec (float): forward pass 소요 시간 (초).
        """
        torch.cuda.synchronize()
        t0 = time.perf_counter()

        with torch.no_grad():
            _ = self.model(input_ids, use_cache=False)

        torch.cuda.synchronize()
        t1 = time.perf_counter()

        return t1 - t0

    def warmup(self, warmup_input_ids: torch.Tensor, n: int = 3) -> None:
        """
        CUDA JIT 컴파일 및 메모리 할당 안정화를 위한 워밍업.

        Args:
            warmup_input_ids: 워밍업에 사용할 입력 텐서 (측정 제외).
            n               : 워밍업 반복 횟수.
        """
        print(f"[engine] 워밍업 {n}회 실행 중...")
        for _ in range(n):
            self.prefill_step(warmup_input_ids)
        print("[engine] 워밍업 완료")
