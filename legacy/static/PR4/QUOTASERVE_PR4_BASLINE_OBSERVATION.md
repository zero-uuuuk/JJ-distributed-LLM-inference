# QuotaServe PR4 Baseline Observation

이 문서는 PR4 static quota victim selection의 실행 전 목표와 실행 후 결과를 기록한다.
실제 실행 명령은 [README.md](./README.md)에 둔다.

## 1. PR4 목표

PR4에서는 PR3에서 계산한 workload별 `occupancy_w`와 config의 `quota_w`를 사용해
victim selection 순서를 바꾼다.

```text
occupancy_w > quota_w인 workload가 있으면
→ 해당 workload의 evictable cached prefix block 중 LRU 선택

over-quota workload가 없으면
→ global LRU fallback
```

기존 global LRU 자체를 삭제하는 것이 아니라, quota를 초과한 workload를 우선하는
선택 경로를 추가하는 단계다.

## 2. PR4 범위

포함:

```text
quota_ratio를 quota_base_blocks 기준의 절대 quota_w로 변환
occupancy_w > quota_w workload 계산
over-quota workload 내부에서 LRU victim 선택
num_blocks > 1일 때 block을 하나씩 선택하고 queue에서 제거
selection_reason 및 victim selection metadata 기록
cross-workload eviction 여부 기록
```

이번 단계에 포함하지 않음:

```text
dynamic quota 조정
ratio_low / ratio_high feedback controller
floor / cap 계산
shadow cache 기반 useful eviction ratio controller
```

## 3. 실제 선택 흐름

```text
새 KV block 필요
↓
free queue에 block이 있는가?
  아니오 → allocation 실패 후 상위 scheduler가 처리
  예
↓
QuotaServe static selector active인가?
  아니오 → 기존 global LRU head 사용
  예
↓
head가 uncached block인가?
  예 → eviction 없이 해당 block 재사용
  아니오
↓
occupancy_w > quota_w인 workload가 있는가?
  아니오 → global LRU fallback
  예 → 해당 workload의 evictable cached block 중 LRU 선택
↓
BlockPool이 free queue에서 block 제거
↓
cache map에서 block 제거 확정
↓
eviction log 기록 후 block metadata 초기화
```

`num_blocks > 1`이면 위 선택 과정을 block마다 반복한다. 따라서 앞에서 선택한
block을 queue에서 먼저 제거한 뒤 다음 block을 선택한다.

## 4. 로그에서 확인할 항목

PR4 eviction event에는 다음 필드가 있어야 한다.

```text
evicted_workload
trigger_workload
is_cross_workload
selection_reason
victim_occupancy
victim_quota
occupancy_snapshot
scan_steps
```

`is_cross_workload`는 다음처럼 해석한다.

```text
trigger_workload != evicted_workload → true
trigger_workload == evicted_workload → false
둘 중 하나가 없거나 unknown             → null
```

`selection_reason`의 의미:

```text
over_quota_selected  : over-quota workload의 block을 선택
fallback_no_over_quota: over-quota workload가 없어 global LRU 사용
baseline_lru_off_mode: QuotaServe off 상태의 baseline LRU
external_eviction    : selector를 거치지 않은 외부 eviction
```

`uncached_head`는 eviction이 아니므로 eviction log에는 기록하지 않는다.

## 5. 검증 기준

### 5.1 기능 검증

- static 서버 시작 로그에서 `mode=static`, `active=True` 확인
- `selection_reason`이 누락되지 않음
- static 로그에 `over_quota_selected` 또는 `fallback_no_over_quota`가 관측됨
- `occupancy_snapshot`이 workload별 dictionary로 기록됨
- `scan_steps`가 양의 정수로 기록됨
- cross-workload eviction이면 `is_cross_workload=true`
- `num_blocks > 1`에서도 block별 selection metadata가 섞이지 않음
- over-quota workload가 존재하는데 후보 block을 찾지 못하면 조용히 LRU로 fallback하지 않고 오류 발생

### 5.2 Baseline 비교

동일 workload와 동일 서버 설정으로 `off`와 `static`을 각각 실행한다.

| 항목 | off baseline | static | 비교 목적 |
|---|---:|---:|---|
| 성공 요청 수 |  |  | 기능 오류 확인 |
| Chat hit rate |  |  | 보호 효과 확인 |
| Chat TTFT p95 |  |  | latency 영향 확인 |
| Chat SLO attainment |  |  | SLO 영향 확인 |
| Longctx/Agent SLO |  |  | antagonist 손상 확인 |
| 총 eviction 수 |  |  | eviction 규모 비교 |
| cross-workload eviction 수 |  |  | pollution 확인 |
| useful eviction 수 |  |  | eviction 품질 확인 |
| `over_quota_selected` 비율 | 해당 없음 |  | 정책 개입 정도 |
| `scan_steps` 평균/p95 | 해당 없음 |  | CPU 탐색 비용 |

## 6. 결과 기록

### Chat + Longctx

```text
실행 결과 파일: pr4_chat_longctx_static.jsonl
비교 baseline: static/PR0/raw_results_backup/pr0_chat_longctx_apc_on_len8192.jsonl
Eviction log: pr4_eviction_chat_longctx_static.jsonl

결과:
- 성공/실패: 2000/0
- Chat hit rate: PR0 8.21% → PR4 13.48% (+5.28%p)
- Chat TTFT p95: PR0 1197.6ms → PR4 1200.8ms (거의 동일)
- Chat SLO attainment: PR0 47.9% → PR4 48.4%
- Longctx hit rate: PR0 2.90% → PR4 2.88% (거의 동일)
- Longctx SLO attainment: PR0 100% → PR4 100%
- 총 eviction: PR0 182,328건 → PR4 181,099건
- cross-workload eviction: PR0 64,620건 → PR4 61,067건
- longctx → chat eviction: 30,118건 → 28,375건
- longctx → chat useful eviction: 24,099건 → 22,346건
- 전체 useful eviction ratio: 18.78% → 18.22%
- over_quota_selected: 159,142건 (87.87%)
- fallback_no_over_quota: 21,957건 (12.13%)
- scan_steps 평균/p95: 604.29 / 1137
- quota: chat=1227, longctx=409
- `occupancy > quota` 위반 기록: 0건
```

Chat hit rate가 증가했고 Longctx SLO는 유지되었다. 또한 longctx가 Chat block을
evict한 건수도 감소했다. 다만 전체 useful eviction ratio는 baseline보다
높아지지 않았으므로, eviction 품질 개선까지 단정하지는 않는다.

### Chat + Agent

```text
실행 결과 파일: pr4_chat_agent_static.jsonl
비교 baseline: static/PR0/raw_results_backup/pr0_chat_agent_apc_on_len8192.jsonl
요약 파일: pr4_chat_agent_static_summary.json
Eviction log: pr4_eviction_chat_agent_static.jsonl

결과:
- 성공/실패: 2000/0
- Chat hit rate: PR0 12.40% → PR4 12.30% (-0.10%p)
- Chat TTFT p95: PR0 491.2ms → PR4 535.6ms
- Chat SLO attainment: PR0 90.3% → PR4 88.4%
- Agent hit rate: PR0 36.14% → PR4 32.59%
- Agent SLO attainment: PR0 48.6% → PR4 44.4%
- 총 eviction: PR0 96,838건 → PR4 99,603건
- cross-workload eviction: 33.06% → 31.60%
- 전체 useful eviction ratio: 70.28% → 71.14%
- over_quota_selected: 46,767건
- fallback_no_over_quota: 52,836건
- quota: chat=1227, agent=1022
- `occupancy > quota` 위반 기록: 0건
```

Chat+Agent에서도 selector와 quota metadata는 정상적으로 동작했다. 다만 이번
실험에서는 PR0 대비 Chat/Agent 성능 개선이 확인되지 않았다. 이 결과는
구현 실패가 아니라 workload별 quota 비율을 추가로 조정해야 할 가능성을
보여준다.


## 7. 결론

PR4의 1차 결론은 다음 질문에 답하는 것이다.

```text
고정 quota와 workload 내부 LRU만으로
global LRU보다 Chat prefix pollution을 줄일 수 있는가?
```

PR4 기능 검증은 통과했다. `mode=static`에서 selector가 실제로 호출되었고,
`over_quota_selected`, `fallback_no_over_quota`, `victim_quota`,
`occupancy_snapshot`, `scan_steps`가 기록되었다. over-quota workload가 있는데
후보를 찾지 못한 오류도 없었다.

특히 Chat+Longctx에서는 Chat hit rate가 개선되고 Longctx SLO가 유지되어
Chat 보호 효과를 확인했다. 다만 useful eviction ratio가 개선된 것은 아니므로
quota 비율이 최적이라고 결론내리지는 않는다. Chat+Agent는 selector 동작은
확인했지만 성능 개선은 확인하지 못했다.

quota sweep은 PR4 구현 검증의 일부가 아니라 후속 profile 실험이다. sweep 결과는
이후 dynamic 단계의 `ratio_low/high`, `floor/cap` 후보를 정하는 데 사용한다.
