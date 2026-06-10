"""Build a tau2-bench based multi-step agent workload JSONL trace.

The builder intentionally does not import tau2-bench. It reads the public data
files and tool source either from a local checkout or from GitHub raw URLs, then
turns reference tool actions into replay-style agent steps.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


DEFAULT_REPO_ID = "sierra-research/tau2-bench"
DEFAULT_REF = "main"
DEFAULT_DOMAIN = "telecom"
DEFAULT_SPLIT = "base"
DEFAULT_TASKS_FILE = "tasks.json"
DEFAULT_TOKENIZER = "meta-llama/Llama-3.2-3B-Instruct"
DEFAULT_OUTPUT_TEXT = {
    "type": "function",
    "function": {"name": "", "arguments": {}},
}

SYSTEM_PREFIX = """You are a customer-support tool-using agent.
Follow the domain policy and use the available tools.
For this workload trace, return exactly one JSON object for the next tool action.
The JSON object must have this shape:
{"type":"function","function":{"name":"tool_name","arguments":{...}}}
Do not include prose outside the JSON object."""

NEXT_ACTION_PROMPT = "Produce the next tool action as a single JSON object."


# ---------------------------------------------------------------------------
# JSON IO
# ---------------------------------------------------------------------------


def load_json(path: Path) -> Any:
    resolved_path = path.expanduser().resolve()
    with resolved_path.open("r", encoding="utf-8") as input_file:
        return json.load(input_file)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    with resolved_path.open("w", encoding="utf-8") as output_file:
        for row in rows:
            output_file.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_text(path: Path, text: str) -> None:
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("w", encoding="utf-8") as output_file:
        output_file.write(text)


def read_text(path: Path) -> str:
    resolved_path = path.expanduser().resolve()
    with resolved_path.open("r", encoding="utf-8") as input_file:
        return input_file.read()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a tau2-bench agent multi-step JSONL workload trace.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help="Optional local tau2-bench checkout. If omitted, GitHub raw files are used.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(__file__).resolve().parent / ".cache",
        help="Cache directory for downloaded GitHub raw files.",
    )
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--tasks-file", default=DEFAULT_TASKS_FILE)
    parser.add_argument("--split-file", default="split_tasks.json")
    parser.add_argument(
        "--policy-file",
        default=None,
        help="Domain policy file. If omitted, common tau2 policy names are tried.",
    )
    parser.add_argument(
        "--tools-file",
        default=None,
        help="Domain tools.py path. If omitted, src/tau2/domains/<domain>/tools.py is used.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--num-sessions", type=int, default=100)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument(
        "--min-steps",
        type=int,
        default=None,
        help="Minimum assistant actions required per session. Defaults to --max-steps.",
    )
    parser.add_argument(
        "--order",
        choices=("step-major", "session-major"),
        default="step-major",
        help="JSONL row order. Runtime scheduling should still use session semantics.",
    )
    parser.add_argument(
        "--action-requestor",
        choices=("assistant", "user", "all"),
        default="assistant",
        help="Which reference actions to turn into model steps.",
    )
    parser.add_argument(
        "--max-policy-chars",
        type=int,
        default=12_000,
        help="Maximum policy characters included in the system prompt. 0 means no limit.",
    )
    parser.add_argument(
        "--max-tool-doc-chars",
        type=int,
        default=500,
        help="Maximum docstring characters per tool. 0 means no limit.",
    )
    parser.add_argument(
        "--tokenizer",
        default=DEFAULT_TOKENIZER,
        help="Tokenizer for output_token_len. Use 'approx' to avoid transformers.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=0,
        help="Clamp output_token_len to this value. 0 means no clamp.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------


def repo_raw_base(repo_id: str, ref: str) -> str:
    return f"https://raw.githubusercontent.com/{repo_id}/{ref}"


def cache_name(relative_path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", relative_path)


def download_text(raw_base_url: str, relative_path: str, cache_dir: Path) -> str:
    cache_path = cache_dir.expanduser().resolve() / cache_name(relative_path)
    if cache_path.is_file():
        return read_text(cache_path)

    url = f"{raw_base_url.rstrip('/')}/{relative_path.lstrip('/')}"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            text = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(
            f"Failed to download {url}: {exc}\n"
            "Use --source-root with a local tau2-bench checkout if network is unavailable."
        ) from exc

    write_text(cache_path, text)
    return text


def load_source_text(
    source_root: Path | None,
    raw_base_url: str,
    relative_path: str,
    cache_dir: Path,
    required: bool = True,
) -> str:
    if source_root is not None:
        local_path = source_root.expanduser().resolve() / relative_path
        if local_path.is_file():
            return read_text(local_path)
        if required:
            raise SystemExit(f"Required tau2 file not found: {local_path}")
        return ""

    try:
        return download_text(raw_base_url, relative_path, cache_dir)
    except SystemExit:
        if required:
            raise
        return ""


def load_source_json(
    source_root: Path | None,
    raw_base_url: str,
    relative_path: str,
    cache_dir: Path,
    required: bool = True,
) -> Any:
    text = load_source_text(
        source_root=source_root,
        raw_base_url=raw_base_url,
        relative_path=relative_path,
        cache_dir=cache_dir,
        required=required,
    )
    if not text and not required:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Failed to parse JSON from {relative_path}: {exc}") from exc


def domain_data_path(domain: str, filename: str) -> str:
    return f"data/tau2/domains/{domain}/{filename}"


def domain_tools_path(domain: str) -> str:
    return f"src/tau2/domains/{domain}/tools.py"


def resolve_policy_text(
    source_root: Path | None,
    raw_base_url: str,
    domain: str,
    policy_file: str | None,
    cache_dir: Path,
) -> tuple[str, str]:
    candidates = (
        [policy_file]
        if policy_file is not None
        else ["main_policy.md", "policy.md", "main_policy_solo.md"]
    )
    for candidate in candidates:
        if candidate is None:
            continue
        relative_path = domain_data_path(domain, candidate)
        text = load_source_text(
            source_root=source_root,
            raw_base_url=raw_base_url,
            relative_path=relative_path,
            cache_dir=cache_dir,
            required=False,
        )
        if text:
            return text, relative_path
    raise SystemExit(
        f"Could not find a policy file for domain '{domain}'. "
        "Pass --policy-file explicitly."
    )


# ---------------------------------------------------------------------------
# Tool schema extraction
# ---------------------------------------------------------------------------


def annotation_to_string(annotation: ast.AST | None) -> str:
    if annotation is None:
        return "Any"
    try:
        return ast.unparse(annotation)
    except Exception:
        return "Any"


def default_to_string(default: ast.AST | None) -> str | None:
    if default is None:
        return None
    try:
        return ast.unparse(default)
    except Exception:
        return "..."


def decorator_tool_type(decorator: ast.AST) -> str | None:
    if not isinstance(decorator, ast.Call):
        return None

    func = decorator.func
    if isinstance(func, ast.Name) and func.id != "is_tool":
        return None
    if isinstance(func, ast.Attribute) and func.attr != "is_tool":
        return None
    if not isinstance(func, (ast.Name, ast.Attribute)):
        return None

    if not decorator.args:
        return "UNKNOWN"
    first_arg = decorator.args[0]
    if isinstance(first_arg, ast.Attribute):
        return first_arg.attr
    if isinstance(first_arg, ast.Name):
        return first_arg.id
    return "UNKNOWN"


def truncate_text(text: str, max_chars: int) -> str:
    clean_text = inspect.cleandoc(text).strip()
    if max_chars <= 0 or len(clean_text) <= max_chars:
        return clean_text
    return clean_text[:max_chars].rstrip() + " [truncated]"


def extract_tool_schemas(tools_source: str, max_doc_chars: int) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(tools_source)
    except SyntaxError as exc:
        raise SystemExit(f"Failed to parse tools.py: {exc}") from exc

    schemas: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            tool_type = None
            for decorator in item.decorator_list:
                tool_type = decorator_tool_type(decorator)
                if tool_type is not None:
                    break
            if tool_type is None:
                continue

            args = item.args.args
            defaults = [None] * (len(args) - len(item.args.defaults)) + list(
                item.args.defaults
            )
            parameters: list[dict[str, Any]] = []
            for arg, default in zip(args, defaults):
                if arg.arg == "self":
                    continue
                parameters.append(
                    {
                        "name": arg.arg,
                        "type": annotation_to_string(arg.annotation),
                        "default": default_to_string(default),
                    }
                )

            schemas.append(
                {
                    "name": item.name,
                    "tool_type": tool_type,
                    "parameters": parameters,
                    "description": truncate_text(
                        ast.get_docstring(item) or "", max_doc_chars
                    ),
                }
            )
    return schemas


def build_tool_schema_text(tools: list[dict[str, Any]]) -> str:
    return json.dumps(tools, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# tau2 task handling
# ---------------------------------------------------------------------------


def normalize_tasks(raw_tasks: Any) -> list[dict[str, Any]]:
    if isinstance(raw_tasks, list):
        return [task for task in raw_tasks if isinstance(task, dict)]
    if isinstance(raw_tasks, dict):
        for key in ("tasks", "data", "items"):
            value = raw_tasks.get(key)
            if isinstance(value, list):
                return [task for task in value if isinstance(task, dict)]
    raise SystemExit("Unsupported tasks JSON shape. Expected a list or {tasks: [...]} object.")


def find_split_list(split_data: Any, split: str) -> list[Any] | None:
    if isinstance(split_data, dict):
        value = split_data.get(split)
        if isinstance(value, list):
            return value
        for nested_value in split_data.values():
            found = find_split_list(nested_value, split)
            if found is not None:
                return found
    return None


def filter_tasks_by_split(
    tasks: list[dict[str, Any]],
    split_data: Any | None,
    split: str,
) -> list[dict[str, Any]]:
    if split_data is None:
        return tasks
    split_ids = find_split_list(split_data, split)
    if split_ids is None:
        raise SystemExit(f"Split '{split}' was not found in split file.")
    allowed_ids = {str(task_id) for task_id in split_ids}
    return [task for task in tasks if str(task.get("id")) in allowed_ids]


def task_actions(task: dict[str, Any], action_requestor: str) -> list[dict[str, Any]]:
    criteria = task.get("evaluation_criteria") or {}
    actions = criteria.get("actions") or []
    selected_actions: list[dict[str, Any]] = []

    for action in actions:
        if not isinstance(action, dict):
            continue
        requestor = str(action.get("requestor", "assistant"))
        if action_requestor != "all" and requestor != action_requestor:
            continue
        if not action.get("name"):
            continue
        selected_actions.append(action)
    return selected_actions


def select_session_tasks(
    tasks: list[dict[str, Any]],
    num_sessions: int,
    min_steps: int,
    action_requestor: str,
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    selected: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for task in tasks:
        actions = task_actions(task, action_requestor)
        if len(actions) < min_steps:
            continue
        selected.append((task, actions))
        if num_sessions > 0 and len(selected) >= num_sessions:
            break

    if num_sessions > 0 and len(selected) < num_sessions:
        raise SystemExit(
            f"Only found {len(selected)} tasks with at least {min_steps} "
            f"{action_requestor} actions; requested {num_sessions} sessions. "
            "Lower --num-sessions or --min-steps."
        )
    return selected


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def pretty_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def structured_instructions_to_text(instructions: Any) -> str:
    if isinstance(instructions, str):
        return instructions.strip()
    if not isinstance(instructions, dict):
        return str(instructions)

    labels = [
        ("domain", "Domain"),
        ("reason_for_call", "Reason for call"),
        ("known_info", "Known info"),
        ("unknown_info", "Unknown info"),
        ("task_instructions", "Task instructions"),
    ]
    lines: list[str] = []
    for key, label in labels:
        value = instructions.get(key)
        if value is not None and str(value).strip():
            lines.append(f"{label}:\n{str(value).strip()}")
    return "\n\n".join(lines) if lines else pretty_json(instructions)


def user_scenario_to_text(task: dict[str, Any]) -> str:
    scenario = task.get("user_scenario") or {}
    if isinstance(scenario, str):
        return scenario.strip()
    if not isinstance(scenario, dict):
        return str(scenario)

    lines: list[str] = []
    persona = scenario.get("persona")
    if persona:
        lines.append(f"Persona:\n{persona}")
    if "instructions" in scenario:
        lines.append(f"Instructions:\n{structured_instructions_to_text(scenario['instructions'])}")
    return "\n\n".join(lines) if lines else pretty_json(scenario)


def initial_message_history(task: dict[str, Any]) -> list[dict[str, str]]:
    initial_state = task.get("initial_state") or {}
    if not isinstance(initial_state, dict):
        return []

    messages = initial_state.get("message_history") or []
    if not isinstance(messages, list):
        return []

    normalized: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user"))
        content = message.get("content")
        if content is None:
            content = compact_json(message)
        normalized.append({"role": role, "content": str(content)})
    return normalized


def build_system_prompt(policy_text: str, tool_schema_text: str) -> str:
    return (
        f"{SYSTEM_PREFIX}\n\n"
        "[Domain policy]\n"
        f"{policy_text}\n\n"
        "[Available tools]\n"
        f"{tool_schema_text}"
    )


def build_session_user_prompt(task: dict[str, Any], domain: str) -> str:
    return (
        f"[Domain]\n{domain}\n\n"
        f"[Task ID]\n{task.get('id')}\n\n"
        "[User scenario]\n"
        f"{user_scenario_to_text(task)}"
    )


def action_output_text(action: dict[str, Any]) -> str:
    output = dict(DEFAULT_OUTPUT_TEXT)
    output["function"] = {
        "name": action.get("name", ""),
        "arguments": action.get("arguments") or {},
    }
    return compact_json(output)


def observation_text(action: dict[str, Any]) -> str:
    return (
        "Observation: reference tool execution completed. "
        f"action_id={action.get('action_id')} "
        f"name={action.get('name')} "
        f"arguments={compact_json(action.get('arguments') or {})}"
    )


def sanitize_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "unknown"


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


class ApproxTokenizer:
    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        # A lightweight fallback for planning traces when transformers is unavailable.
        approx_tokens = max(1, (len(text) + 3) // 4)
        return list(range(approx_tokens))


def load_tokenizer(tokenizer_name: str) -> Any:
    if tokenizer_name.lower() in {"approx", "none"}:
        return ApproxTokenizer()

    try:
        from transformers import AutoTokenizer
    except ImportError:
        print(
            "warning: transformers is not installed; using approximate token counts",
            file=sys.stderr,
        )
        return ApproxTokenizer()

    try:
        return AutoTokenizer.from_pretrained(tokenizer_name)
    except Exception as exc:
        raise SystemExit(
            f"Failed to load tokenizer '{tokenizer_name}': {exc}\n"
            "Use --tokenizer approx to build a planning trace without transformers."
        ) from exc


def count_output_tokens(tokenizer: Any, text: str, max_output_tokens: int) -> int:
    token_count = len(tokenizer.encode(text, add_special_tokens=False))
    if max_output_tokens > 0:
        return min(token_count, max_output_tokens)
    return token_count


# ---------------------------------------------------------------------------
# Workload construction
# ---------------------------------------------------------------------------


def build_session_rows(
    task: dict[str, Any],
    actions: list[dict[str, Any]],
    domain: str,
    system_prompt: str,
    max_steps: int,
    tokenizer: Any,
    max_output_tokens: int,
    source_paths: dict[str, str],
) -> list[dict[str, Any]]:
    task_id = sanitize_id(task.get("id"))
    session_id = f"tau2-{domain}-{task_id}"
    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_session_user_prompt(task, domain)},
    ]
    history_messages = initial_message_history(task)
    session_rows: list[dict[str, Any]] = []
    step_actions = actions[:max_steps]

    for step_index, action in enumerate(step_actions, start=1):
        output_text = action_output_text(action)
        messages = (
            base_messages
            + history_messages
            + [{"role": "user", "content": NEXT_ACTION_PROMPT}]
        )
        session_rows.append(
            {
                "request_id": f"{session_id}_step_{step_index}",
                "session_id": session_id,
                "task_id": task.get("id"),
                "domain": domain,
                "step_id": step_index,
                "session_step_count": len(step_actions),
                "messages": messages,
                "output_text": output_text,
                "output_token_len": count_output_tokens(
                    tokenizer, output_text, max_output_tokens
                ),
                "tool_name": action.get("name"),
                "tool_arguments": action.get("arguments") or {},
                "action_id": action.get("action_id"),
                "action_requestor": action.get("requestor", "assistant"),
                "reference_action_index": step_index - 1,
                "tool_gap_seconds": None,
                "source_dataset": "tau2-bench",
                "cache_pattern": "agent_multi_step",
                "source_paths": source_paths,
            }
        )

        history_messages.append({"role": "assistant", "content": output_text})
        history_messages.append({"role": "user", "content": observation_text(action)})

    return session_rows


def flatten_sessions(
    session_rows: list[list[dict[str, Any]]],
    order: str,
) -> list[dict[str, Any]]:
    if order == "session-major":
        return [row for rows in session_rows for row in rows]

    max_steps = max((len(rows) for rows in session_rows), default=0)
    rows: list[dict[str, Any]] = []
    for step_offset in range(max_steps):
        for session in session_rows:
            if step_offset < len(session):
                rows.append(session[step_offset])
    return rows


def build_workload_rows(
    tasks: list[dict[str, Any]],
    domain: str,
    policy_text: str,
    tools_source: str,
    num_sessions: int,
    max_steps: int,
    min_steps: int,
    order: str,
    action_requestor: str,
    max_tool_doc_chars: int,
    tokenizer: Any,
    max_output_tokens: int,
    source_paths: dict[str, str],
) -> list[dict[str, Any]]:
    tool_schemas = extract_tool_schemas(tools_source, max_tool_doc_chars)
    if not tool_schemas:
        raise SystemExit("No @is_tool-decorated tools were found in tools.py.")

    selected_tasks = select_session_tasks(
        tasks=tasks,
        num_sessions=num_sessions,
        min_steps=min_steps,
        action_requestor=action_requestor,
    )
    system_prompt = build_system_prompt(policy_text, build_tool_schema_text(tool_schemas))

    all_session_rows = [
        build_session_rows(
            task=task,
            actions=actions,
            domain=domain,
            system_prompt=system_prompt,
            max_steps=max_steps,
            tokenizer=tokenizer,
            max_output_tokens=max_output_tokens,
            source_paths=source_paths,
        )
        for task, actions in selected_tasks
    ]
    return flatten_sessions(all_session_rows, order)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    if args.max_steps < 1:
        raise SystemExit("--max-steps must be at least 1")
    min_steps = args.min_steps if args.min_steps is not None else args.max_steps
    if min_steps < 1:
        raise SystemExit("--min-steps must be at least 1")
    if min_steps > args.max_steps:
        raise SystemExit("--min-steps cannot be greater than --max-steps")

    raw_base_url = repo_raw_base(args.repo_id, args.ref)
    tasks_path = domain_data_path(args.domain, args.tasks_file)
    split_path = domain_data_path(args.domain, args.split_file)
    tools_path = args.tools_file or domain_tools_path(args.domain)

    print(f"repo: {args.repo_id}@{args.ref}", flush=True)
    print(f"domain: {args.domain}", flush=True)
    print(f"tasks_file: {args.tasks_file}", flush=True)
    print(f"split: {args.split}", flush=True)
    print(f"tokenizer: {args.tokenizer}", flush=True)

    raw_tasks = load_source_json(
        source_root=args.source_root,
        raw_base_url=raw_base_url,
        relative_path=tasks_path,
        cache_dir=args.cache_dir,
    )
    split_data = load_source_json(
        source_root=args.source_root,
        raw_base_url=raw_base_url,
        relative_path=split_path,
        cache_dir=args.cache_dir,
        required=False,
    )
    policy_text, policy_path = resolve_policy_text(
        source_root=args.source_root,
        raw_base_url=raw_base_url,
        domain=args.domain,
        policy_file=args.policy_file,
        cache_dir=args.cache_dir,
    )
    policy_text = truncate_text(policy_text, args.max_policy_chars)
    tools_source = load_source_text(
        source_root=args.source_root,
        raw_base_url=raw_base_url,
        relative_path=tools_path,
        cache_dir=args.cache_dir,
    )

    tasks = normalize_tasks(raw_tasks)
    split_tasks = filter_tasks_by_split(tasks, split_data, args.split)
    tokenizer = load_tokenizer(args.tokenizer)
    rows = build_workload_rows(
        tasks=split_tasks,
        domain=args.domain,
        policy_text=policy_text,
        tools_source=tools_source,
        num_sessions=args.num_sessions,
        max_steps=args.max_steps,
        min_steps=min_steps,
        order=args.order,
        action_requestor=args.action_requestor,
        max_tool_doc_chars=args.max_tool_doc_chars,
        tokenizer=tokenizer,
        max_output_tokens=args.max_output_tokens,
        source_paths={
            "tasks": tasks_path,
            "split": split_path,
            "policy": policy_path,
            "tools": tools_path,
        },
    )
    write_jsonl(args.output, rows)

    output_lens = [row["output_token_len"] for row in rows]
    prompt_lens = [sum(len(message["content"]) for message in row["messages"]) for row in rows]
    sessions = {row["session_id"] for row in rows}
    print(f"loaded_tasks: {len(tasks)}")
    print(f"split_tasks: {len(split_tasks)}")
    print(f"sessions: {len(sessions)}")
    print(f"requests: {len(rows)}")
    print(f"order: {args.order}")
    print(f"min_steps: {min_steps}")
    print(f"max_steps: {args.max_steps}")
    print(
        "output_token_len min/avg/max: "
        f"{min(output_lens) if output_lens else 0}/"
        f"{sum(output_lens) / len(output_lens) if output_lens else 0:.1f}/"
        f"{max(output_lens) if output_lens else 0}"
    )
    print(
        "prompt_char_len min/avg/max: "
        f"{min(prompt_lens) if prompt_lens else 0}/"
        f"{sum(prompt_lens) / len(prompt_lens) if prompt_lens else 0:.1f}/"
        f"{max(prompt_lens) if prompt_lens else 0}"
    )
    print(f"output: {args.output}")


if __name__ == "__main__":
    main()
