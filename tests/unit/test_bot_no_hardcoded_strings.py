"""Structural enforcement of the spec's own Prompt 5 requirement: 'no
string in any handler that isn't an i18n key.' Walks the whole handlers.py
AST for string literals containing real alphabetic content, and fails on
any that isn't specifically exempted (a t(...) call's translation key, or a
Command(...) filter's command name -- protocol identifiers, not user-facing
text). This catches a hardcoded literal wherever it appears in the file,
not just at a notifier.send() call site -- including one buried in a
kwarg passed to t(...) itself, which is exactly the bug this test caught
for real during development (see DECISIONS.md).
"""

import ast
from pathlib import Path

HANDLERS_PATH = Path(__file__).parent.parent.parent / "services" / "bot" / "handlers.py"

# Constructors whose string argument is a protocol identifier (a command
# name, a router name), never user-facing text.
_EXEMPT_CALL_NAMES = {"Command", "Router"}

# Database methods whose first argument is a SQL string, not user text.
# Later arguments (query parameters) are still checked -- they're normally
# Name references anyway, never literals.
_SQL_METHOD_NAMES = {"fetchrow", "fetch", "fetchval", "execute"}


class _LiteralTextFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[ast.Constant] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "t":
            # First positional arg is the translation key -- exempt only
            # that one; format kwargs and everything else still get
            # checked (this is what catches outcome="won" hardcoded
            # directly into a t(...) call's format argument).
            for arg in node.args[1:]:
                self.visit(arg)
            for kw in node.keywords:
                self.visit(kw.value)
            return
        if isinstance(func, ast.Name) and func.id in _EXEMPT_CALL_NAMES:
            return
        if isinstance(func, ast.Attribute) and func.attr in _SQL_METHOD_NAMES:
            for arg in node.args[1:]:
                self.visit(arg)
            for kw in node.keywords:
                self.visit(kw.value)
            return
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and any(ch.isalpha() for ch in node.value):
            self.violations.append(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Skip the docstring (first statement, if it's a bare string expr)
        # but still walk the rest of the function body.
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        for stmt in body:
            self.visit(stmt)
        for decorator in node.decorator_list:
            self.visit(decorator)


def test_no_hardcoded_user_facing_strings_in_handlers():
    tree = ast.parse(HANDLERS_PATH.read_text(encoding="utf-8"))
    finder = _LiteralTextFinder()
    finder.visit(tree)

    assert not finder.violations, "\n".join(
        f"handlers.py:{node.lineno}: hardcoded string literal {node.value!r} -- "
        f"every user-facing string must come from i18n.t(...)"
        for node in finder.violations
    )


def test_the_checker_actually_catches_a_hardcoded_string():
    # A regression guard for the checker itself: if this ever stops
    # failing, the checker has gone blind, not the codebase gone clean.
    tree = ast.parse(
        'async def handler(message, notifier):\n'
        '    await notifier.send(message.chat.id, "This is not translated")\n'
    )
    finder = _LiteralTextFinder()
    finder.visit(tree)
    assert finder.violations
