# QuotaServe PR2 Baseline Observation

이 문서는 QuotaServe PR2의 목표, 범위, 검증 기준을 기록한다.

실제 실행 명령은 [README.md](./README.md)에 둔다.

## 1. PR2 목표

PR2의 목표는 QuotaServe가 request를 workload 단위로 구분할 수 있게 만드는 것이다.

QuotaServe static/dynamic 정책은 workload별로 다음 정보를 알아야 한다.

```text
이 request는 어느 workload인가?
이 cached block은 어느 workload가 만든 것인가?
이 eviction은 어느 workload가 유발했는가?
```

PR2에서는 이 중 첫 번째 기반을 만든다.

```text
request_id에서 workload tag를 추론할 수 있어야 한다.
```

## 2. 핵심 결정

PR2에서는 OpenAI `user` 필드를 QuotaServe 기준으로 사용하지 않는다.

대신 클라이언트 runner가 `X-Request-Id` 헤더에 workload tag prefix를 넣는다.

```text
X-Request-Id: chat-<uuid>
X-Request-Id: longctx-<uuid>
X-Request-Id: agent-<uuid>
```

vLLM은 이 값을 OpenAI request id에 반영한다. vLLM 내부 request id에는 `chatcmpl-` 또는 `cmpl-` 같은 engine prefix가 붙을 수 있다.

따라서 서버 쪽 workload 추론은 다음 순서로 처리한다.

```text
request_id = chatcmpl-chat-<uuid>
↓
engine prefix 제거
↓
chat-<uuid>
↓
첫 token 추출
↓
workload = chat
```

## 3. 기준 경로

PR2의 기준 전달 경로는 다음이다.

```text
runner
  X-Request-Id: chat-<uuid>
        ↓
vLLM OpenAI server
  request_id = chatcmpl-chat-<uuid>
        ↓
vllm.quota_serve.workload.infer_workload(request_id)
        ↓
workload = chat
```

이 경로가 안정적으로 동작해야 후속 PR에서 block owner, occupancy, victim attribution을 붙일 수 있다.

## 4. PR2 범위

PR2에 포함되는 것:

```text
request_id 기반 workload tag 추론
X-Request-Id 기반 workload tag 전달
chat / longctx / agent / rag tag mapping
unknown workload 처리
runner header 확인
raw result request_id prefix 확인
eviction log가 발생한 경우 workload attribution 확인
```

PR2에 포함되지 않는 것:

```text
OpenAI user 필드 core plumbing
Request에 workload_id 필드 추가
KVCacheBlock owner metadata 추가
occupancy counter 구현
quota-aware victim selection 구현
dynamic controller 구현
```

## 5. 관련 파일

vLLM:

```text
vllm/quota_serve/workload.py
```

JJ repo:

```text
static/run_mixed_c2.py
static/run_mixed_agent_c2.py
```

`workload.py`는 request_id 문자열에서 workload tag를 추론한다.

runner는 각 요청에 다음 헤더를 붙인다.

```python
headers = {"X-Request-Id": f"{workload_tag}-{uuid.uuid4().hex}"}
```

## 6. tag mapping

현재 workload tag mapping은 다음 의미를 갖는다.

```text
chat     -> chat
longctx  -> longctx
hotpotqa -> longctx
agent    -> agent
rag      -> rag
msmarco  -> rag
```

알 수 없는 prefix는 `unknown`으로 둔다.

## 7. PR2 통과 기준

아래 항목이 모두 만족되면 PR2 workload tag 단계는 통과로 본다.

```text
1. infer_workload unit smoke 통과
2. runner가 user 필드가 아니라 X-Request-Id 헤더를 사용
3. Chat request_id에 chat prefix가 남음
4. Longctx request_id에 longctx prefix가 남음
5. Agent runner도 동일하게 X-Request-Id 헤더 사용
6. eviction log가 발생한 경우 evicted_workload / trigger_workload unknown 없음
```

작은 smoke에서는 eviction이 안 날 수 있다. 이 경우 PR2 실패가 아니다. PR2의 핵심은 request tag 전달/추론이고, eviction attribution은 PR0 본 실행 또는 후속 PR 실험에서 다시 확인한다.

## 8. 주의 사항

```text
1. PR2는 tag 전달/추론 단계다.
2. block owner나 occupancy는 아직 구현하지 않는다.
3. OpenAI user 필드는 vLLM KV cache 경로까지 안정적으로 내려오지 않으므로 사용하지 않는다.
4. request_id는 vLLM에서 chatcmpl- prefix가 붙을 수 있으므로 infer_workload가 이를 제거해야 한다.
5. runner의 --quota-mode는 결과 메타데이터 라벨이다. 실제 정책은 서버 env로 켠다.
6. static quota 효과 검증은 PR4에서 한다.
```
