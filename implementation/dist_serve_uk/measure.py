"""
measure.py
----------
TPOT(Time Per Output Token) 측정 유틸리티.

- IterTimer : iteration별 wall-clock time을 perf_counter로 기록
- summarize  : 여러 run의 iteration별 TPOT를 집계하여 평균·표준편차 반환
"""

import time
import numpy as np


class IterTimer:
    """
    단일 run의 iteration별 TPOT를 기록하는 타이머.

    사용법:
        timer = IterTimer(n_iters=200)
        for i in range(200):
            timer.start()
            # forward pass
            timer.stop()
        tpot = timer.tpot  # shape: (200,)
    """

    def __init__(self, n_iters: int):
        self.n_iters = n_iters
        self.tpot = np.zeros(n_iters, dtype=np.float64)
        self._cursor = 0
        self._t0 = None

    def start(self):
        """iteration 시작 시점 기록."""
        self._t0 = time.perf_counter()

    def stop(self):
        """iteration 종료 시점 기록 후 TPOT 저장."""
        assert self._t0 is not None, "start()를 먼저 호출해야 합니다."
        self.tpot[self._cursor] = time.perf_counter() - self._t0
        self._cursor += 1
        self._t0 = None


def summarize(tpot_runs: list[np.ndarray]) -> dict:
    """
    여러 run의 iteration별 TPOT를 집계한다.

    Args:
        tpot_runs: shape (n_iters,) 인 np.ndarray의 리스트. 길이 = run 횟수.

    Returns:
        {
            "mean": shape (n_iters,) — iteration별 평균 TPOT (초),
            "std":  shape (n_iters,) — iteration별 표준편차 TPOT (초),
        }
    """
    stacked = np.stack(tpot_runs, axis=0)  # shape: (n_runs, n_iters)
    return {
        "mean": stacked.mean(axis=0).tolist(),
        "std": stacked.std(axis=0).tolist(),
    }
