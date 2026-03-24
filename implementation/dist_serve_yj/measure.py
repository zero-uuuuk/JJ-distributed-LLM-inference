"""
measure.py
실험 측정 유틸리티.

- GPUMonitor : nvidia-smi 백그라운드 폴링 (utilization, memory)
- summarize() : latency 목록 → 평균/표준편차/throughput 계산
"""

import subprocess
import threading
import time
import statistics
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# GPU 모니터
# ---------------------------------------------------------------------------

@dataclass
class GPUSample:
    timestamp: float          # time.perf_counter() 기준
    utilization_pct: int      # GPU utilization (%)
    memory_used_mb: int       # GPU memory used (MiB)


class GPUMonitor:
    """
    백그라운드 스레드에서 `nvidia-smi` 를 폴링하여 GPU 상태를 수집한다.

    사용 예:
        monitor = GPUMonitor(interval=0.5)
        monitor.start()
        # ... 실험 실행 ...
        monitor.stop()
        samples = monitor.samples
    """

    def __init__(self, interval: float = 0.5) -> None:
        """
        Args:
            interval: 폴링 간격 (초). 기본 0.5초.
        """
        self.interval = interval
        self.samples: List[GPUSample] = []
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """백그라운드 폴링 시작."""
        self._stop_event.clear()
        thread = threading.Thread(target=self._poll, daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        """폴링 중지 후 스레드 종료 대기."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def _poll(self) -> None:
        query = "utilization.gpu,memory.used"
        fmt = "csv,noheader,nounits"
        cmd = ["nvidia-smi", f"--query-gpu={query}", f"--format={fmt}"]

        while not self._stop_event.is_set():
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    parts = result.stdout.strip().split(",")
                    if len(parts) == 2:
                        util = int(parts[0].strip())
                        mem = int(parts[1].strip())
                        self.samples.append(
                            GPUSample(
                                timestamp=time.perf_counter(),
                                utilization_pct=util,
                                memory_used_mb=mem,
                            )
                        )
            except Exception:
                pass  # nvidia-smi 실패 시 무시하고 계속 폴링
            time.sleep(self.interval)

    def aggregate(self) -> dict:
        """수집된 샘플을 집계하여 dict 반환."""
        if not self.samples:
            return {}
        utils = [s.utilization_pct for s in self.samples]
        mems = [s.memory_used_mb for s in self.samples]
        return {
            "gpu_util_mean_pct": statistics.mean(utils),
            "gpu_util_max_pct": max(utils),
            "gpu_mem_mean_mb": statistics.mean(mems),
            "gpu_mem_max_mb": max(mems),
            "n_samples": len(self.samples),
        }


# ---------------------------------------------------------------------------
# latency / throughput 집계
# ---------------------------------------------------------------------------

def summarize(latencies_sec: List[float], batch_size: int, seq_len: int) -> dict:
    """
    latency 측정값 목록으로 평균/표준편차/throughput 을 계산한다.

    throughput = (B × L) / mean_latency   [tokens/sec]

    Args:
        latencies_sec: 각 iteration의 forward pass latency (초) 목록.
        batch_size   : 배치 크기 B.
        seq_len      : 프롬프트 길이 L.

    Returns:
        dict with keys:
            mean_latency_sec, std_latency_sec,
            min_latency_sec, max_latency_sec,
            throughput_tokens_per_sec,
            n_iters
    """
    n = len(latencies_sec)
    mean_lat = statistics.mean(latencies_sec)
    std_lat = statistics.stdev(latencies_sec) if n > 1 else 0.0
    throughput = (batch_size * seq_len) / mean_lat if mean_lat > 0 else 0.0

    return {
        "mean_latency_sec": mean_lat,
        "std_latency_sec": std_lat,
        "min_latency_sec": min(latencies_sec),
        "max_latency_sec": max(latencies_sec),
        "throughput_tokens_per_sec": throughput,
        "n_iters": n,
    }
