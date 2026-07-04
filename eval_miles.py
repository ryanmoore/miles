#!/usr/bin/env python3
"""
Analyst eval harness. Runs a question through the miles analyst and saves a full transcript.

Usage:
    uv run python eval_miles.py "Your question here"
    uv run python eval_miles.py "Your question here" --label pace-filter-tweak

Results saved to: eval_results/YYYYMMDD-HHMMSS[-label].md
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ANALYST_SYSTEM_FILE = Path(".claude/commands/miles.md")
RESULTS_DIR = Path("eval_results")
MCP_PREFIX = "mcp__miles__"


def _fmt_json_block(raw: str) -> str:
    try:
        parsed = json.loads(raw)
        return "```json\n" + json.dumps(parsed, indent=2) + "\n```"
    except Exception:
        return f"```\n{raw}\n```"


def run_eval(question: str, label: str | None) -> Path:
    analyst_system = ANALYST_SYSTEM_FILE.read_text()

    cmd = [
        "claude", "-p", question,
        "--system-prompt", analyst_system,
        "--output-format", "stream-json",
        "--verbose",
        # Non-interactive runs can't answer permission prompts; pre-allow the
        # miles MCP server so every tool is exercisable in evals.
        "--allowedTools", "mcp__miles",
    ]

    print(f"Question: {question}", flush=True)
    print("Running...", flush=True)

    proc = subprocess.run(cmd, capture_output=True, text=True)

    tool_log: list[dict[str, object]] = []
    # MCP tool_use blocks waiting to be paired with results (positional queue)
    pending_mcp: list[dict[str, object]] = []
    final_text = ""

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = ev.get("type")

        if t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                bt = block.get("type")
                if bt == "tool_use":
                    name: str = block.get("name", "")
                    # Skip internal Claude Code tools (ToolSearch, etc.)
                    if not name.startswith(MCP_PREFIX):
                        continue
                    short_name = name[len(MCP_PREFIX):]
                    tool_input = block.get("input", {})
                    print(f"  → {short_name}({json.dumps(tool_input, separators=(',', ':'))})", flush=True)
                    pending_mcp.append({"name": short_name, "input": tool_input})
                elif bt == "text":
                    text = block.get("text", "")
                    if text:
                        final_text = text

        elif t == "user":
            meta = ev.get("tool_use_result", {})
            # MCP results have "content"; ToolSearch results have "matches" — skip those
            if "content" not in meta:
                continue
            content = meta.get("content", "")
            if pending_mcp:
                entry = pending_mcp.pop(0)
                entry["result"] = content
                tool_log.append(entry)

        elif t == "result":
            # result.result is the consolidated final text
            result_text: str = ev.get("result", "")
            if result_text:
                final_text = result_text

    if proc.returncode != 0 and not final_text:
        final_text = f"**Error (exit {proc.returncode}):**\n```\n{proc.stderr.strip()}\n```"

    # Build transcript
    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = f"{ts}-{label}" if label else ts
    out_path = RESULTS_DIR / f"{slug}.md"

    lines: list[str] = [
        f"# Analyst Eval — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"**Label:** {label or '(none)'}",
        "",
        "## Question",
        "",
        question,
        "",
        "---",
        "",
    ]

    if tool_log:
        lines.append("## Tool Calls")
        lines.append("")
        for i, tc in enumerate(tool_log, 1):
            lines.append(f"### {i}. `{tc['name']}`")
            lines.append("")
            lines.append("**Input:**")
            lines.append(_fmt_json_block(json.dumps(tc["input"])))
            lines.append("")
            lines.append("**Output:**")
            raw_result = tc.get("result", "")
            lines.append(_fmt_json_block(str(raw_result)))
            lines.append("")
        lines += ["---", ""]

    lines += [
        "## Analyst Response",
        "",
        final_text,
    ]

    out_path.write_text("\n".join(lines))
    return out_path


def main() -> None:
    if not ANALYST_SYSTEM_FILE.exists():
        print(f"Error: {ANALYST_SYSTEM_FILE} not found. Run from the miles project root.", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Run a question through the miles analyst and save the transcript."
    )
    parser.add_argument("question", help="The question to ask the analyst")
    parser.add_argument("--label", "-l", help="Short label for this run (e.g. 'pace-filter-v2')")
    args = parser.parse_args()

    out_path = run_eval(args.question, args.label)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
