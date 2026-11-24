"""Report numerical diagnostics from local Codex rollout logs."""
import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sys
import uuid


FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
          "reasoning_output_tokens", "total_tokens")
INPUT, CACHE, OUTPUT, REASONING, TOTAL = FIELDS
START = {"task_started", "turn_started", "turn.started"}
COMPLETE = {"task_complete", "task_completed", "turn_completed", "turn.completed"}
ABORT = {"turn_aborted", "task_aborted", "turn_failed", "turn.failed"}
CATEGORIES = (
    "System instructions", "Developer instructions", "AGENTS.md", "Skill instructions",
    "User prompts", "Assistant responses", "Source code and files read", "Tool calls",
    "Tool results", "Terminal command output", "MCP tool results", "Conversation history",
    "Compaction summaries", "Other context",
)


class SelectionError(Exception):
    pass


def mapping(value):
    return value if isinstance(value, dict) else {}


def sequence(value):
    return value if isinstance(value, list) else []


def number(value):
    return value if type(value) is int and value >= 0 else None


def usage(value, includes_reasoning=True):
    result = {key: number(mapping(value).get(key)) for key in FIELDS}
    if includes_reasoning is False:
        output, reasoning = result[OUTPUT], result[REASONING]
        if output is not None and reasoning is not None:
            result[OUTPUT] = output + reasoning
        else:
            result[OUTPUT] = None
    if result[TOTAL] is None and result[INPUT] is not None and result[OUTPUT] is not None:
        result[TOTAL] = result[INPUT] + result[OUTPUT]
    return result if any(value is not None for value in result.values()) else {}


def subtract(after, before):
    return {key: after[key] - before[key]
            if after.get(key) is not None and before.get(key) is not None and after[key] >= before[key]
            else None for key in FIELDS}


def add(usages):
    return {key: sum(row[key] for row in usages)
            if usages and all(row.get(key) is not None for row in usages) else None
            for key in FIELDS}


def records(path):
    with path.open(encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    yield row
            except (ValueError, RecursionError):
                continue


def unpack(row):
    kind = row.get("type")
    payload = mapping(row.get("payload")) if "payload" in row else row
    kind = payload.get("type") if kind == "event_msg" else kind
    return (kind if isinstance(kind, str) else None), payload


def session_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def cwd_key(value):
    return os.path.normcase(os.path.realpath(value))


@dataclass
class Candidate:
    path: Path
    identity: str | None = None
    cwd: str | None = None
    active: bool = False
    aliases: set = field(default_factory=set)


def inspect(path):
    candidate = Candidate(path)
    for row in records(path):
        kind, data = unpack(row)
        if kind == "session_meta":
            candidate.identity = session_id(data.get("id")) or session_id(data.get("session_id"))
            candidate.aliases.update(filter(None, (session_id(data.get(key)) for key in
                                                    ("id", "session_id", "thread_id"))))
        if kind in {"session_meta", "turn_context"} and isinstance(data.get("cwd"), str) and data["cwd"]:
            candidate.cwd = data["cwd"]
        if kind in START:
            candidate.active = True
        elif kind in COMPLETE or kind in ABORT:
            candidate.active = False
    return candidate


def select(home, cwd, requested=None, env=None):
    env = os.environ if env is None else env
    explicit = requested or env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")
    if explicit and not session_id(explicit):
        raise SelectionError("Session ID is invalid. Use --session-id with a Codex thread UUID.")
    paths = (home / "sessions").rglob("*.jsonl")
    if explicit:
        identity = session_id(explicit)
        matches = []
        for path in paths:
            if path.stem.lower().endswith(identity):
                try:
                    candidate = inspect(path)
                except OSError:
                    continue
                if candidate.identity is None or identity in candidate.aliases:
                    matches.append(candidate)
        if not matches:
            for path in (home / "sessions").rglob("*.jsonl"):
                try:
                    candidate = inspect(path)
                except OSError:
                    continue
                if identity in candidate.aliases:
                    matches.append(candidate)
        if len(matches) != 1:
            raise SelectionError("Session ID did not resolve to exactly one readable local rollout.")
        candidate = matches[0]
        candidate.identity = candidate.identity or identity
        return candidate, "session ID"

    matches = []
    for path in paths:
        try:
            candidate = inspect(path)
            if candidate.cwd and cwd_key(candidate.cwd) == cwd_key(cwd):
                matches.append((path.stat().st_mtime_ns, candidate))
        except (OSError, ValueError):
            continue
    matches.sort(key=lambda match: match[0], reverse=True)
    if not matches:
        raise SelectionError("No recorded session matches this directory. Use --session-id.")
    if len(matches) > 1 and (any(c.active for _, c in matches) or matches[0][0] == matches[1][0]):
        raise SelectionError("Several sessions match this directory and ownership is ambiguous. Use --session-id.")
    return matches[0][1], "latest rollout matching CWD"


def text_tokens(text):
    return (len(text.encode("utf-8", errors="replace")) + 3) // 4


def content_texts(value):
    if isinstance(value, str):
        return [value]
    return [item["text"] for item in sequence(value)
            if isinstance(item, dict) and isinstance(item.get("text"), str)]


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


INSTRUCTION_BLOCK = re.compile(
    r"(<skills_instructions>.*?</skills_instructions>|<skill>.*?</skill>)", re.DOTALL
)
FILE_READ = re.compile(
    r"\b(Get-Content|cat|head|tail|sed|type)\s+|(?:^|[\"'])\s*rg\s+[^;\n]*--files-with-matches",
    re.IGNORECASE,
)
TERMINAL = {"exec_command", "write_stdin", "shell", "shell_command", "terminal", "local_shell"}


class Context:
    def __init__(self):
        self.base = ()
        self.items = []
        self.available = True

    def set_base(self, value):
        text = value if isinstance(value, str) else mapping(value).get("text")
        self.base = tuple(content_texts(text))

    def replace(self, data):
        self.items = []
        replacement = data.get("replacement_history")
        self.available = isinstance(replacement, list)
        if self.available:
            for item in replacement:
                if isinstance(item, dict):
                    self.items.append((None, mapping(item.get("payload", item))))
        if not any(item.get("type") in ("compaction", "compaction_summary") for _, item in self.items):
            message = data.get("message")
            if isinstance(message, str) and message:
                self.items.append((None, {"type": "compaction_summary", "content": message}))

    def snapshot(self, turn):
        return self.base, self.items, len(self.items), turn, self.available

    def summarize(self, turn):
        return self.summarize_snapshot(self.snapshot(turn))

    @staticmethod
    def summarize_snapshot(snapshot):
        base, all_items, count, turn, _ = snapshot
        items = all_items[:count]
        counts = Counter()
        largest = Counter()
        calls = {}
        explicit_system = {text for _, item in items if item.get("role") == "system"
                           for text in content_texts(item.get("content"))}
        for text in base:
            if text not in explicit_system:
                counts["System instructions"] += text_tokens(text)
        for owner, item in items:
            kind = item.get("type")
            role = item.get("role")
            pieces = content_texts(item.get("content"))
            category = "Other context"
            if kind == "message":
                if role == "system":
                    category = "System instructions"
                elif role == "developer":
                    category = "Developer instructions"
                elif role == "user":
                    category = "User prompts" if owner == turn and owner is not None else "Conversation history"
                elif role == "assistant":
                    category = "Assistant responses" if owner == turn and owner is not None else "Conversation history"
                for text in pieces:
                    if role in ("user", "developer") and text.lstrip().startswith("# AGENTS.md instructions"):
                        counts["AGENTS.md"] += text_tokens(text)
                    elif role in ("user", "developer"):
                        for part in INSTRUCTION_BLOCK.split(text):
                            if part:
                                label = "Skill instructions" if part.startswith(("<skill>", "<skills_instructions>")) else category
                                if part.startswith(("<environment_context>", "<external_")):
                                    label = "Other context"
                                counts[label] += text_tokens(part)
                    else:
                        counts[category] += text_tokens(text)
                continue
            if kind in ("compaction", "compaction_summary"):
                category = "Compaction summaries"
            elif isinstance(kind, str) and kind.endswith(("_call_output", "_tool_result")):
                call_id = item.get("call_id")
                category = calls.get(call_id, "Tool results") if isinstance(call_id, str) else "Tool results"
                output = item.get("output", item.get("content"))
                pieces = [output if isinstance(output, str) else json_text(output)] if output is not None else []
            elif isinstance(kind, str) and kind.endswith("_call"):
                category = "Tool calls"
                name = item.get("name")
                name = name if isinstance(name, str) else ""
                arguments = item.get("arguments", item.get("input", item.get("action", "")))
                args = arguments if isinstance(arguments, str) else json_text(arguments)
                namespace = item.get("namespace")
                terminal = (name in TERMINAL or kind == "local_shell_call"
                            or (name in {"exec", "functions.exec"} and "tools.exec_command" in args))
                mcp = name.startswith(("mcp__", "mcp.")) or kind == "mcp_tool_call" or (
                    isinstance(namespace, str) and namespace.startswith(("mcp__", "mcp.")))
                result_type = "Tool results"
                if mcp or (name in {"exec", "functions.exec"} and "tools.mcp__" in args and not terminal):
                    result_type = "MCP tool results"
                elif terminal or (name in {"exec", "functions.exec"} and "tools.write_stdin" in args):
                    result_type = "Source code and files read" if FILE_READ.search(args) else "Terminal command output"
                if isinstance(item.get("call_id"), str):
                    calls[item["call_id"]] = result_type
                pieces = [part for part in (name, args) if part]
            elif kind == "additional_tools":
                pieces = [json_text(item.get("tools"))] if item.get("tools") is not None else []
            elif kind == "reasoning":
                pieces = [entry["text"] for entry in sequence(item.get("summary"))
                          if isinstance(entry, dict) and isinstance(entry.get("text"), str)]
            if pieces:
                size = sum(map(text_tokens, pieces))
                counts[category] += size
                largest[category] = max(largest[category], size)
        return counts, largest


@dataclass
class Turn:
    baseline: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)
    recorded: dict = field(default_factory=dict)
    legacy_end: dict = field(default_factory=dict)
    direct: dict = field(default_factory=dict)

    def totals(self):
        if self.direct:
            return self.direct
        if self.recorded:
            return self.recorded
        if self.legacy_end:
            return subtract(self.legacy_end, self.baseline)
        return add(self.requests)


class Report:
    def __init__(self):
        self.context = Context()
        self.model = None
        self.capacity = None
        self.turn_capacity = None
        self.latest = {}
        self.total = {}
        self.legacy = {}
        self.turns = {}
        self.active = None
        self.completed = []
        self.seen = set()
        self.compactions = 0
        self.compaction_counts = Counter()
        self.snapshot = None
        self.snapshot_invalid = False
        self.requests = []
        self.native_seen = False
        self.sequence = 0
        self.direct_usage = []

    def request(self, values, owner):
        if values:
            self.latest = values
            self.snapshot_invalid = False
            self.snapshot = self.context.snapshot(owner)
            self.requests.append(values)

    def digest(self, row):
        kind, data = unpack(row)
        turn_id = data.get("turn_id")
        owner = turn_id if isinstance(turn_id, str) and turn_id else self.active
        if kind == "session_meta":
            base = data.get("base_instructions")
            self.context.set_base(base)
            self.model = mapping(mapping(base).get("provenance")).get("model") or data.get("model") or self.model
            self.capacity = number(data.get("model_context_window")) or number(data.get("context_window")) or self.capacity
        elif kind == "turn_context" or kind in START:
            model = data.get("model")
            if isinstance(model, str) and model != self.model:
                self.model = model
                self.snapshot_invalid = bool(self.latest)
                self.capacity = self.turn_capacity if kind == "turn_context" else None
            self.capacity = number(data.get("model_context_window")) or self.capacity
            if kind in START:
                self.turn_capacity = number(data.get("model_context_window"))
                self.sequence += 1
                self.active = owner or f"turn-{self.sequence}"
                zero = dict.fromkeys(FIELDS, 0) if not self.turns else {}
                self.turns.setdefault(self.active, Turn(baseline=self.legacy or zero))
            elif owner and self.active is None and owner not in self.completed:
                self.active = owner
                self.turns.setdefault(owner, Turn(baseline=self.legacy))
        elif kind == "response_item":
            self.context.items.append((self.active, data))
        elif kind == "token_usage_record":
            response_id = data.get("response_id")
            if isinstance(response_id, str) and response_id:
                if response_id in self.seen:
                    return
                self.seen.add(response_id)
            self.native_seen = True
            values = usage(data.get("usage"))
            self.total = usage(data.get("thread_token_usage")) or self.total
            if owner:
                turn = self.turns.setdefault(owner, Turn())
                turn.recorded = usage(data.get("turn_token_usage")) or turn.recorded
                if values:
                    turn.requests.append(values)
            self.request(values, owner)
        elif kind == "token_count":
            info = mapping(data.get("info"))
            self.capacity = number(info.get("model_context_window")) or self.capacity
            cumulative = usage(info.get("total_token_usage"))
            last = usage(info.get("last_token_usage"), info.get("last_usage_includes_reasoning"))
            repeated = cumulative and cumulative == self.legacy
            if cumulative:
                self.legacy = cumulative
                if owner:
                    self.turns.setdefault(owner, Turn()).legacy_end = cumulative
            # Current clients mirror the preceding request record in this event.
            mirrored = cumulative and cumulative == self.total and bool(self.requests)
            # A differing legacy cumulative counter may include branch-level adjustments.
            # It cannot supersede the native thread total when both streams are available.
            if not mirrored and not self.native_seen and not repeated and last:
                self.request(last, owner)
                if owner and not cumulative:
                    self.turns.setdefault(owner, Turn()).requests.append(last)
        elif kind in COMPLETE:
            if owner is None:
                self.sequence += 1
                owner = f"turn-{self.sequence}"
            turn = self.turns.setdefault(owner, Turn())
            if owner not in self.completed:
                turn.direct = usage(data.get("usage"))
                if turn.direct:
                    self.direct_usage.append(turn.direct)
                self.completed.append(owner)
            if owner == self.active:
                self.active = None
        elif kind in ABORT:
            if owner == self.active:
                self.active = None
        elif kind in {"compacted", "context_compacted"}:
            self.compaction_counts[kind] += 1
            self.compactions = max(self.compaction_counts.values())
            if kind == "compacted" or self.compaction_counts[kind] > self.compaction_counts["compacted"]:
                self.snapshot_invalid = bool(self.latest)
            if kind == "compacted":
                self.context.replace(data)
            elif self.compaction_counts[kind] > self.compaction_counts["compacted"]:
                self.context.replace({})

    def session_total(self):
        if self.total or self.legacy:
            return self.total or self.legacy
        if self.direct_usage and not self.requests:
            return add(self.direct_usage)
        if self.requests and not self.direct_usage:
            return add(self.requests)
        return {}



def formatted(value):
    return f"{value:,}" if value is not None else "N/A"


def percentage(numerator, denominator):
    return f"{numerator / denominator:.1%}" if numerator is not None and denominator else "N/A"


def recommendations(counts, largest, visible_total, used, capacity):
    advice = []
    for category, label, action in (
        ("Terminal command output", "Terminal output", "Filter commands at the source or cap output to relevant lines."),
        ("Source code and files read", "File-read output", "Read specific files and line ranges instead of full files."),
        ("MCP tool results", "MCP output", "Narrow queries and request fewer results or fields."),
        ("Tool results", "Tool output", "Reduce result limits and keep only relevant fields."),
        ("Tool calls", "Tool arguments", "Reuse existing scripts and narrow arguments for repeated commands."),
    ):
        if largest[category] >= 2000 or counts[category] >= 4000:
            advice.append((counts[category], f"{label} (~{counts[category]:,} tokens). {action}"))
    history = counts["Conversation history"]
    if history >= 8000 and history >= visible_total * 0.25:
        advice.append((history, f"Conversation history (~{history:,} tokens). Keep follow-ups focused and reuse a short task summary."))
    instructions = sum(counts[key] for key in ("AGENTS.md", "Skill instructions"))
    if instructions >= 4000 and instructions >= visible_total * 0.20:
        advice.append((instructions, "AGENTS.md and skill instructions are sizable. Remove duplication or move details into on-demand references."))
    lines = [text for _, text in sorted(advice, reverse=True)[:4]]
    if capacity and used is not None and used >= capacity * 0.85:
        lines.append("The latest request used at least 85% of the window. Consider manual compaction if this task must continue, or a new session for a separate task.")
    return lines or ["No high-volume component crossed the available evidence thresholds."]


def render(report, candidate, selection):
    latest = report.latest
    inputs, cached = latest.get(INPUT), latest.get(CACHE)
    uncached = inputs - cached if inputs is not None and cached is not None and cached <= inputs else None
    model = report.model
    if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,99}", model):
        model = "N/A"
    used = None if report.snapshot_invalid else latest.get(INPUT)
    remaining = report.capacity - used if report.capacity and used is not None and used <= report.capacity else None
    lines = [
        "## Recorded token usage",
        f"- Model: {model} | Session ID: {candidate.identity or 'N/A'} | Selection: {selection}",
        f"- Context capacity: {formatted(report.capacity)} | Estimated utilization: {percentage(used, report.capacity)} | Estimated remaining: {formatted(remaining)}",
        f"- Latest model request: input {formatted(latest.get(INPUT))} | cached {formatted(latest.get(CACHE))} | uncached {formatted(uncached)} | output {formatted(latest.get(OUTPUT))} | reasoning {formatted(latest.get(REASONING))}",
        f"- Latest request cache hit: {percentage(latest.get(CACHE) if uncached is not None else None, latest.get(INPUT))} | Recorded session total: {formatted(report.session_total().get(TOTAL))} | Compactions logged: {report.compactions}",
        "",
        "## Estimated visible context",
    ]
    if report.snapshot and not report.snapshot_invalid:
        counts, largest = report.context.summarize_snapshot(report.snapshot)
        complete = report.snapshot[-1]
        lines.append("Visible items near the latest model request.")
    else:
        counts, largest = report.context.summarize(report.active)
        complete = report.context.available
        lines.append("Visible rollout items at report time. A current request snapshot is unavailable.")
    lines.append("Counts use UTF-8 bytes / 4. Shares use the visible subtotal. Hidden or opaque items and unrecognized categories can be unavailable.")
    if not complete:
        lines.append("Partial reconstruction. The compaction log omitted replacement history.")
    visible_total = sum(counts.values())
    lines.extend(["", "| Component | Est. tokens | Visible share | Bar |", "|---|---:|---:|---|"])
    for category in sorted(CATEGORIES, key=lambda key: (-counts[key], CATEGORIES.index(key))):
        size = counts[category]
        bar = "#" * max(1, round(16 * size / visible_total)) if size else ""
        lines.append(f"| {category} | {formatted(size or None)} | {percentage(size or None, visible_total)} | {bar} |")
    lines.extend([
        f"| **Visible subtotal** | **{formatted(visible_total or None)}** | | |",
        "", "## Last five completed turns",
        "", "| Completed turn | Input | Cached | Output | Total |",
        "|---|---:|---:|---:|---:|",
    ])
    for index, turn_id in list(enumerate(report.completed, 1))[-5:]:
        values = report.turns[turn_id].totals()
        line = " | ".join(formatted(values.get(key)) for key in (INPUT, CACHE, OUTPUT, TOTAL))
        lines.append(f"| {index} | {line} |")
    if not report.completed:
        lines.append("| N/A | N/A | N/A | N/A | N/A |")
    lines.extend(["", "## Recommendations", ""])
    lines.extend(f"- {item}" for item in recommendations(counts, largest, visible_total, used, report.capacity))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", help="Codex thread UUID")
    parser.add_argument("--cwd", type=Path, help="Project directory for fallback matching")
    args = parser.parse_args()
    try:
        cwd = args.cwd or Path.cwd()
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        candidate, selection = select(home, cwd, args.session_id)
        report = Report()
        for row in records(candidate.path):
            report.digest(row)
        print(render(report, candidate, selection))
    except SelectionError as error:
        print(f"Token report unavailable. {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RecursionError):
        print("Token report unavailable. Could not process the local rollout.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
