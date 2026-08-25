"""세 workload 원본을 로드하고 공통 request record로 정규화한다."""

import json
import random
from pathlib import Path
from typing import Any

from datasets import load_dataset as hf_load_dataset
from huggingface_hub import hf_hub_download
from tqdm import tqdm


# ---------------------------------------------------------------------------
# 기본값
# ---------------------------------------------------------------------------


CHAT_REPO = "anon8231489123/ShareGPT_Vicuna_unfiltered"
CHAT_FILE = "ShareGPT_V3_unfiltered_cleaned_split.json"
RAG_DATASET = "microsoft/ms_marco"
AGENT_DATASET = "yoonholee/terminalbench-trajectories"

RAG_INSTRUCTION = """You are a RAG question-answering assistant.
Use only the retrieved context to answer the question.
If the context does not contain enough evidence, say you do not know.
Keep the answer concise."""

AGENT_INSTRUCTION = "You are an autonomous terminal agent. Continue the task using the trajectory context."


# ---------------------------------------------------------------------------
# 데이터셋 로드
# ---------------------------------------------------------------------------


def load_dataset(config: dict[str, Any]) -> list[dict[str, Any]]:
    """세 원본 데이터셋을 workload type이 붙은 하나의 list로 로드한다."""
    records: list[dict[str, Any]] = []

    for spec in config.get("workloads", []):
        workload = spec.get("type")

        # Chat 원본 파일 다운로드
        if workload == "chat":
            path = Path(
                hf_hub_download(
                    repo_id=CHAT_REPO,
                    filename=CHAT_FILE,
                    repo_type="dataset",
                )
            )
            # 다운로드한 Chat JSON 파일 읽기
            with path.open(encoding="utf-8") as input_file:
                value = json.load(input_file)
            rows = value
            
        # RAG, Agent 원본 파일 다운로드
        else:
            dataset_name = RAG_DATASET if workload == "rag" else AGENT_DATASET
            split = "validation" if workload == "rag" else "train"
            dataset_config = "v2.1" if workload == "rag" else None
            rows = hf_load_dataset(
                dataset_name,
                dataset_config,
                split=split,
                streaming=True,
            )

        # 원본 row의 공통 raw record 변환
        for row in tqdm(rows, desc=f"load:{workload}"):
            records.append({"workload": workload, "row": row})

    return records


# ---------------------------------------------------------------------------
# workload별 정규화
# ---------------------------------------------------------------------------


def _normalize_chat(
    rows: list[dict[str, Any]],
    spec: dict[str, Any],
) -> list[dict[str, Any]]:
    """ShareGPT row를 누적 history request로 변환한다."""
    count = spec["count"]
    min_turns, max_turns = spec["min_turns"], spec["max_turns"]
    chat_records: list[dict[str, Any]] = []

    for index, row in enumerate(tqdm(rows, desc="normalize:chat")):
        # 대화 메시지 조회
        messages = row.get("conversations") or []

        # 사용자·답변 쌍 검증
        if len(messages) % 2 or not all("value" in message for message in messages):
            continue

        # turn 상·하한 적용
        messages = messages[: max_turns * 2]
        if len(messages) // 2 < min_turns:
            continue

        # 대화 ID·history 초기화
        conversation_id = str(row.get("id", index))
        history: list[dict[str, str]] = []

        # turn별 request 생성
        for turn, (user, answer) in enumerate(
            zip(messages[::2], messages[1::2]), start=1
        ):
            user, answer = str(user["value"]), str(answer["value"])
            chat_records.append(
                {
                    "workload": "chat",
                    "request_id": f"{conversation_id}_turn_{turn}",
                    "group_id": conversation_id,
                    "step_id": turn,
                    "messages": history + [{"role": "user", "content": user}],
                    "output_text": answer,
                    "metadata": {"source_dataset": "ShareGPT"},
                }
            )

            # 현재 turn history 추가
            history += [
                {"role": "user", "content": user},
                {"role": "assistant", "content": answer},
            ]

        # conversation 단위 상한 확인
        if len(chat_records) >= count:
            break

    return chat_records[:count]


def _normalize_rag(
    rows: list[dict[str, Any]],
    spec: dict[str, Any],
) -> list[dict[str, Any]]:
    """MS MARCO row를 retrieved context request로 변환한다."""
    count = spec["count"]
    rag_records: list[dict[str, Any]] = []

    for row in tqdm(rows, desc="normalize:rag"):
        # 질문·답변 조회
        question = str(row.get("query", "")).strip()
        candidates = (
            str(value).strip()
            for key in ("answers", "wellFormedAnswers")
            for value in row.get(key) or []
        )
        answer = next(
            (value for value in candidates if value and value != "No Answer Present."),
            "",
        )

        # retrieved passage 정리
        passages = row.get("passages") or {}
        texts = [
            stripped
            for text in passages.get("passage_text", [])
            if (stripped := str(text).strip())
        ]

        # 유효 row 필터
        if not question or not answer or not texts:
            continue

        # prompt 직렬화
        context = "\n\n".join(
            f"[{index}] {text}" for index, text in enumerate(texts, start=1)
        )
        prompt = (
            f"[Retrieved context]\n{context}"
            f"\n\n[Question]\n{question}\n\n[Answer]\n"
        )

        # request record 추가
        request_id = f"msmarco-rag-{len(rag_records):06d}"
        rag_records.append(
            {
                "workload": "rag",
                "request_id": request_id,
                "group_id": str(row.get("query_id", request_id)),
                "step_id": None,
                "messages": [
                    {"role": "system", "content": RAG_INSTRUCTION},
                    {"role": "user", "content": prompt},
                ],
                "output_text": answer,
                "metadata": {
                    "source_dataset": "MS MARCO v2.1",
                },
            }
        )

        # request 단위 상한 확인
        if len(rag_records) >= count:
            break

    return rag_records


def _normalize_agent(
    rows: list[dict[str, Any]],
    spec: dict[str, Any],
) -> list[dict[str, Any]]:
    """Terminal-Bench row를 tool request와 final response로 변환한다."""
    count = spec["count"]
    min_steps, max_steps = spec["min_steps"], spec["max_steps"]
    max_prompt_chars = spec["max_prompt_chars"]
    agent_records: list[dict[str, Any]] = []

    for row_index, row in enumerate(tqdm(rows, desc="normalize:agent")):
        # trajectory step 조회
        steps = json.loads(row["steps"])
        if not steps:
            continue

        # step별 message·tool index 정리
        step_records: list[tuple[str, list[dict[str, str]]]] = []
        indices: list[int] = []
        for index, step in enumerate(steps):
            text = str(step.get("msg") or "").strip()
            observation = str(step.get("obs") or "").strip()
            source = str(step.get("src", "user")).lower()

            # agent step의 output·observation 구성
            if source == "agent":
                # tool 호출 step 식별
                if step.get("tools"):
                    indices.append(index)
                    tools = "[Tool calls]\n" + json.dumps(
                        step["tools"], ensure_ascii=False
                    )
                    text = f"{text}\n\n{tools}" if text else tools

                # 빈 Agent 본문 보완
                text = text or json.dumps(step, ensure_ascii=False)

                # assistant message 구성
                messages = [{"role": "assistant", "content": text}]

                # tool observation turn 추가
                if observation:
                    messages.append(
                        {"role": "user", "content": f"Observation:\n{observation}"}
                    )

            # user·system message 구성
            else:
                messages = [
                    {
                        "role": "system" if source == "system" else "user",
                        "content": text or json.dumps(step, ensure_ascii=False),
                    }
                ]

            step_records.append((text, messages))

        # tool step·trajectory 필터
        if not indices or not min_steps <= len(indices) <= max_steps:
            continue

        # 마지막 tool 이후 final response 확인
        final_index = next(
            (
                index
                for index in range(indices[-1] + 1, len(steps))
                if str(steps[index].get("src", "user")).lower() == "agent"
                and str(steps[index].get("msg") or "").strip()
                and steps[index].get("tools") is None
                and steps[index].get("obs") is None
            ),
            None,
        )
        if final_index is None:
            continue

        # session ID·request 목록 초기화
        session_id = f"session_{row_index}"
        session_records: list[dict[str, Any]] = []

        # tool request·final response prompt 구성
        for step_id, source_index in enumerate(indices + [final_index], start=1):
            messages = [
                {"role": "system", "content": AGENT_INSTRUCTION},
                {"role": "user", "content": f"task: {row.get('task_name', '')}"},
            ]
            for _, previous in step_records[:source_index]:
                messages += previous

            # prompt 길이 필터
            if sum(len(message["content"]) for message in messages) > max_prompt_chars:
                session_records = []
                break

            # request record 추가
            session_records.append(
                {
                    "workload": "agent",
                    "request_id": f"{session_id}_step_{step_id}",
                    "group_id": session_id,
                    "step_id": step_id,
                    "messages": messages,
                    "output_text": step_records[source_index][0],
                    "metadata": {
                        "source_dataset": "Terminal-Bench trajectories"
                    },
                }
            )

        agent_records.extend(session_records)

        # session 단위 상한 확인
        if len(agent_records) >= count:
            break

    return agent_records[:count]


# ---------------------------------------------------------------------------
# 공통 정규화 진입점
# ---------------------------------------------------------------------------


def normalize_dataset(
    records: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """raw records를 Chat/RAG/Agent 공통 request schema로 변환한다."""
    # workload 및 샘플링 설정
    specs = {spec["type"]: spec for spec in config["workloads"]}
    sampling = config["sampling"]
    rng = random.Random(sampling["seed"])
    result: list[dict[str, Any]] = []

    # workload별 row 분류
    rows_by_workload = {workload: [] for workload in ("chat", "rag", "agent")}
    for record in records:
        rows_by_workload[record["workload"]].append(record["row"])

    # workload별 row 정규화
    normalizers = {
        "chat": _normalize_chat,
        "rag": _normalize_rag,
        "agent": _normalize_agent,
    }
    for workload, normalize in normalizers.items():
        rows = rows_by_workload[workload]
        if sampling["random"]:
            rng.shuffle(rows)
        result.extend(normalize(rows, specs[workload]))

    return result
