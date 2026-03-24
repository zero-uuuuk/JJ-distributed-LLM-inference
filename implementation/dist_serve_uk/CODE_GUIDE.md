# 코드 구조 설명

## measure.py

### IterTimer

한 번의 run(200번 forward pass)에서 iteration별 소요 시간을 기록한다.

```
timer = IterTimer(n_iters=200)

for i in range(200):
    timer.start()   # 시작 시각 기록
    # forward pass
    timer.stop()    # tpot[i] = 종료 - 시작

timer.tpot  # shape (200,) — iteration별 TPOT (초)
```

내부적으로 `_cursor`가 `stop()` 호출마다 1씩 증가하며 배열의 올바른 위치에 저장한다.

---

### summarize

10회 run의 결과를 집계한다.

```
run 1:  [0.05, 0.04, ...]  shape (200,)
run 2:  [0.05, 0.04, ...]
...
run 10: [0.06, 0.04, ...]

np.stack → shape (10, 200)
axis=0으로 mean/std → shape (200,) 각각
```

반환값: `{"mean": [200개], "std": [200개]}`

- `mean[0]` = iter 0의 10회 평균 TPOT → Treatment에서 이 값이 spike해야 간섭이 실증됨
- 이 딕셔너리가 `results/tpot_batch{N}.json`으로 저장됨

---

## engine.py

### prefill() — 준비 단계 (측정 제외)

측정 시작 전 decoding 배치의 KV Cache를 확보한다.
요청마다 seq_len이 다를 수 있으므로 개별 forward pass로 처리한다.

```
요청1 [A,B,...,256개] → forward → kv1  shape: (1, n_heads, 256, head_dim)
요청2 [D,E,...,256개] → forward → kv2
반환: [kv1, kv2, ..., kvN]
```

---

### run_iteration() — 측정 구간 핵심

매 iteration마다 호출. 내부 흐름 4단계:

**① input_ids 구성**
- decoding 요청: next token 1개
- prefill 요청: 전체 512 tokens

**② KV Cache padding (`_pad_and_merge_kv`)**

요청마다 past_seq_len이 달라 배치로 직접 합칠 수 없다.
max_past_seq_len에 맞춰 left zero-padding으로 길이를 통일한다.
prefill 요청은 past_kv가 없으므로 전체 zero 텐서로 채운다.

```
kv1:       [■■■■■■■■]  past_seq_len=256
kv2:       [■■■■■■■■]  past_seq_len=256
kv_prefill:[00000000]  past_seq_len=0 → zero padding
→ cat → (batch, n_heads, 256, head_dim)
```

**③ forward pass 1번**

attention_mask와 position_ids로 padding 위치를 마스킹.
`use_cache=True`로 갱신된 KV Cache를 반환받는다.

**④ 출력 분리 (`_split_outputs`)**

forward pass를 배치로 돌렸으므로 출력도 배치로 한 덩어리로 나온다.
다음 iteration에서 각 요청에 맞는 KV Cache를 따로 전달해야 하므로 요청별로 쪼개야 한다.
쪼개지 않으면 다음 iteration에서 요청1에게 전체 배치의 KV Cache가 넘어가게 된다.

```
out.past_key_values: (batch, n_heads, max_past + max_new, head_dim)  ← 한 덩어리
요청1 KV: [-257:] → (1, n_heads, 257, head_dim)  ← 256 + 1
prefill KV: [-512:] → (1, n_heads, 512, head_dim)
```

---

### 전체 흐름

```
준비:      prefill() → kv_list 확보

iter 0:    run_iteration(next_tokens, kv_list, prefill_seq=512tokens)
           → KV Cache padding → forward 1번 → kv_list 갱신 + prefill_kv 반환

iter 1~199: run_iteration(next_tokens, kv_list)
            → KV Cache padding → forward 1번 → kv_list 갱신
```

매 iteration마다 kv_list의 각 KV Cache seq_len이 1씩 늘어난다.
