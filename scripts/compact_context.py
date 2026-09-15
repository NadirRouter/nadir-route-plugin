#!/usr/bin/env python3
"""Preview a successful tool's UTF-8 output; keep omitted bytes locally.

No model, network, dependencies, or conversation-history edits. Stdout is the
preview; stderr is JSON character/byte accounting, never a billed-token claim.
"""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


def python_view(text, keep_symbols=(), *, lean=False):
    """A source view, never replacement code. Keep named bodies verbatim.

    ponytail: Python's stdlib parser only; unsupported syntax stays exact until
    another language has a measured benefit that justifies shipping its parser.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    if not tree.body:
        return None
    lines = text.splitlines(keepends=True)
    rows, found = [], set()

    def emit(first, last):
        if last >= first:
            location = f"[L{first}-{last}]" if lean else f"[source lines {first}-{last}]"
            rows.append(location + "\n" + "".join(lines[first - 1:last]).rstrip("\r\n"))

    def is_doc(node):
        return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)

    def emit_doc(node):
        if lean:
            return
        last = min(node.end_lineno, node.lineno + 2)
        emit(node.lineno, last)
        if last < node.end_lineno:
            rows.append(f"[docstring remainder omitted; source lines {last + 1}-{node.end_lineno}]")

    def walk(nodes, prefix=""):
        for node in nodes:
            if is_doc(node):
                emit_doc(node)
                continue
            named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            first = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            name = prefix + node.name if named else ""
            matches = {s for s in keep_symbols if s == name or (named and s == node.name)}
            found.update(matches)
            if named and not matches:
                child = node.body[0]
                body_start = min([child.lineno] + [d.lineno for d in getattr(child, "decorator_list", [])])
                # One-line definitions include their body; never split a line.
                emit(first, max(node.lineno, body_start - 1))
                if body_start == node.lineno:
                    continue
                if isinstance(node, ast.ClassDef):
                    walk(node.body, name + ".")
                else:
                    # Docstrings describe the contract; retain them in the view.
                    first_stmt = node.body[0]
                    if is_doc(first_stmt):
                        emit_doc(first_stmt)
                        body_start = first_stmt.end_lineno + 1
                    if body_start <= node.end_lineno:
                        rows.append(f"[body omitted L{body_start}-{node.end_lineno}]" if lean else
                                    f"[body omitted: {name}; source lines {body_start}-{node.end_lineno}]")
            else:
                if lean and isinstance(node, (ast.Assign, ast.AnnAssign)) and sum(len(line) for line in lines[first - 1:node.end_lineno]) > 256:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names = ", ".join(ast.unparse(target) for target in targets)
                    rows.append(f"[data omitted: {names}; source lines {first}-{node.end_lineno}]")
                else:
                    emit(first, node.end_lineno)

    walk(tree.body)
    if set(keep_symbols) - found:
        return None
    return ("[Lean Python view: L = original source lines; docstrings omitted. Recover exact source before editing.]\n\n" if lean else "") + "\n\n".join(rows)


def compact_file(path, *, exit_code, max_chars=8000, archive_dir=None, summarize_tests=False,
                 code_language=None, keep_symbols=()):
    if max_chars < 1024:
        raise ValueError("max_chars must be at least 1024 (includes retrieval instructions)")
    if not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
        raise ValueError("exit_code must be the command's actual status, from 0 to 255")
    if not Path(path).is_file():
        raise ValueError("path must be a regular captured-output file")
    if code_language not in (None, "python") or (keep_symbols and code_language != "python"):
        raise ValueError("symbol selection requires code_language='python'")
    if summarize_tests and code_language:
        raise ValueError("choose test summaries or code views, not both")
    # ponytail: buffer one output, capped at 32 MiB; stream if larger logs matter.
    with Path(path).open("rb") as source:
        raw = source.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("output exceeds 32 MiB; narrow the command or read it in chunks")
    original = raw.decode("utf-8")  # reject binary; never silently discard bytes
    report = {
        "compacted": False,
        "exit_code": exit_code,
        "original_chars": len(original),
        "returned_chars": len(original),
        "original_bytes": len(raw),
        "returned_bytes": len(raw),
        "archive_path": None,
        "sha256": None,
    }
    if exit_code or len(original) <= max_chars:
        report["reason"] = "failed_command_preserved" if exit_code else "below_limit"
        return original, report

    code = python_view(original, keep_symbols) if code_language else None
    if code_language and code is None:
        report["reason"] = "code_structure_unavailable"
        return original, report
    summary = None
    if summarize_tests:
        # Adapted from OmniRoute's testGreen.ts at d6f315018af6ed59ff0df253f857ca17abad4974.
        # Copyright (c) 2026 diegosouzapw; MIT notice in THIRD_PARTY_NOTICES.md.
        stripped = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", original)
        # Conservative: even '0 failed' keeps diagnostics. Never trust a green
        # summary from one runner when another runner reported a failure.
        if re.search(r"\b(?:fail(?:ed|ure|ures)?|errors?|traceback|assertionerror|xfailed|xpassed|skipped|warnings?)\b|✖", stripped, re.I):
            report["reason"] = "test_diagnostics_preserved"
            return original, report
        # ponytail: recognize pure pytest/Jest/Vitest summaries only; unknown
        # formats keep the existing recoverable preview, never guessed success.
        summaries = []
        for line in stripped.splitlines():
            line = line.strip()
            if re.fullmatch(r"(?:={3,}\s*)?[1-9]\d* passed in \d+(?:\.\d+)?s(?: \([\d:]+\))?(?:\s*={3,})?", line):
                summaries.append(line)
            match = re.fullmatch(r"Tests:\s*([1-9]\d*) passed,\s*(\d+) total", line)
            vitest = re.fullmatch(r"Tests\s+([1-9]\d*) passed \((\d+)\)", line)
            if match or vitest:
                match = match or vitest
                if match[1] != match[2]:
                    report["reason"] = "ambiguous_test_summary_preserved"
                    return original, report
                summaries.append(line)
        if len(summaries) > 1:
            report["reason"] = "ambiguous_test_summary_preserved"
            return original, report
        summary = summaries[0] if summaries else None

    # A private, unique directory avoids collisions, symlinks and overwrites.
    directory = Path(tempfile.mkdtemp(prefix="nadir-context-", dir=archive_dir)).resolve()
    archive = directory / "output.txt"
    digest = hashlib.sha256(raw).hexdigest()
    omission = "code view; omitted bodies are not safe to edit" if code is not None else "test log summarized" if summary else "middle omitted"
    marker = (
        f"\n\n[Nadir tool-output preview: {omission}; command exit status 0.\n"
        f"Full original (local): {archive}\n"
        f"SHA-256: {digest}\n"
        "Search/read the original before relying on omitted evidence. "
        "This is incomplete tool data, not a task summary.]\n\n"
    )
    available = max_chars - len(marker)
    if available < 2:
        directory.rmdir()
        raise ValueError("archive path leaves no preview space; increase max_chars")
    detail = "standard"
    if code is not None and len(code) > available:
        lean = python_view(original, keep_symbols, lean=True)
        if lean is not None and len(lean) < len(code):
            code, detail = lean, "lean"
    # Never truncate the structural view: a selected body or a declaration
    # must not disappear just because the view exceeded its budget.
    if code is not None and (len(code) > available or len(code + marker) >= len(original)):
        directory.rmdir()
        report["reason"] = "code_view_exceeds_budget"
        return original, report
    head = available // 2
    preview = code + marker if code is not None else summary + marker if summary and len(summary) <= available else original[:head] + marker + original[-(available - head):]
    try:
        with os.fdopen(os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            out.write(raw)
    except Exception:
        archive.unlink(missing_ok=True)
        directory.rmdir()
        raise
    report.update(
        compacted=True,
        returned_chars=len(preview),
        returned_bytes=len(preview.encode("utf-8")),
        archive_path=str(archive),
        sha256=digest,
        reason="recoverable_code_view" if code is not None else "recoverable_test_summary" if summary else "recoverable_preview",
    )
    if code is not None:
        report["code_detail"] = detail
    return preview, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="File containing captured tool output")
    parser.add_argument("--exit-code", type=int, required=True, help="Actual source command status")
    parser.add_argument("--max-chars", type=int, default=8000, help="Preview budget, including retrieval pointer")
    parser.add_argument("--archive-dir", type=Path, help="Existing local parent directory; default: OS temp directory")
    parser.add_argument("--summarize-tests", action="store_true", help="Summarize recognized passing tests; preserve diagnostics in full")
    parser.add_argument("--code-language", choices=["python"], help="Outline a complete captured Python file; unsupported syntax stays exact")
    parser.add_argument("--keep-symbol", action="append", default=[], help="Keep this function or Class.method body exact; repeat for multiple symbols")
    args = parser.parse_args()
    try:
        content, report = compact_file(
            args.path, exit_code=args.exit_code, max_chars=args.max_chars, archive_dir=args.archive_dir,
            summarize_tests=args.summarize_tests,
            code_language=args.code_language, keep_symbols=args.keep_symbol,
        )
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(2, f"nadir compact: {error}. Original file is unchanged.\n")
    # Binary stdout preserves CRLF and UTF-8 regardless of the host's locale.
    sys.stdout.buffer.write(content.encode("utf-8"))
    print(json.dumps(report), file=sys.stderr)
    return args.exit_code


if __name__ == "__main__":
    sys.exit(main())
