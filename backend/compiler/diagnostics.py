"""Diagnostic: one error or warning reported by any compiler phase."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Diagnostic:
    severity: str          # "error" or "warning"
    code: str              # e.g. "AF-E201"
    phase: str             # "lexer", "parser", "semantic", "verify", ...
    message: str
    line: int
    col: int
    path: Optional[list] = None   # witness path of node ids, for verification errors
    node: Optional[str] = None    # IR node this is about (used to color the graph, not sent to the UI)

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "phase": self.phase,
            "message": self.message,
            "line": self.line,
            "col": self.col,
            "path": self.path,
        }


def error(code, phase, message, line, col, path=None, node=None) -> Diagnostic:
    return Diagnostic("error", code, phase, message, line, col, path, node)


def warning(code, phase, message, line, col, path=None, node=None) -> Diagnostic:
    return Diagnostic("warning", code, phase, message, line, col, path, node)


def has_errors(diagnostics) -> bool:
    return any(d.is_error for d in diagnostics)


def format_diagnostic(d: Diagnostic, source: str = "", filename: str = "<input>") -> str:
    """Format like a classic compiler: file:line:col: error AF-E201: message, then the line and a caret."""
    text = f"{filename}:{d.line}:{d.col}: {d.severity} {d.code}: {d.message}"
    if d.path:
        text += "\n    path: " + " -> ".join(d.path)
    lines = source.splitlines()
    if source and 1 <= d.line <= len(lines):
        src_line = lines[d.line - 1]
        text += "\n    " + src_line + "\n    " + " " * (d.col - 1) + "^"
    return text
