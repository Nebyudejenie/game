"""Structural enforcement of the spec's own Prompt 5 requirement: 'no
string in any handler that isn't an i18n key.' Walks the whole handlers.py
AST for string literals containing real alphabetic content, and fails on
any that isn't specifically exempted: a t(...) call's translation key, a
Command(...)/Router(...) protocol identifier, a dict/Record subscript key
(row["seq"]), a ledger account "kind" (an internal enum value, not user
text), or an f-string's literal fragments (used here only for URL
building, never message text -- messages always go through t(...)).

None of these exemptions were guessed in advance -- they're exactly the
false positives this checker actually produced against the real file
while writing it, each one individually verified to be a non-user-facing
identifier before being added.
"""

import ast
from pathlib import Path

HANDLERS_PATH = Path(__file__).parent.parent.parent / "services" / "bot" / "handlers.py"

# Constructors whose string argument is a protocol identifier (a command
# name, a router name), never user-facing text. CommandObject is the same
# category as Command -- a synthetic command dispatch (on_menu_text builds
# one to route a ReplyKeyboard press through the same handler a typed
# "/deposit" would hit) carries a bot-command name, not display text.
_EXEMPT_CALL_NAMES = {"Command", "CommandObject", "Router"}

# Database methods whose first argument is a SQL string, not user text.
_SQL_METHOD_NAMES = {"fetchrow", "fetch", "fetchval", "execute"}

# Calls whose string arguments are internal domain identifiers (ledger
# account kinds), not user-facing text.
_DOMAIN_ENUM_CALL_NAMES = {"get_or_create_account"}


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class _LiteralTextFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[ast.Constant] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)

        if name == "t":
            # First positional arg is the translation key -- exempt only
            # that one; format kwargs and everything else still get
            # checked (this is what catches a literal hardcoded directly
            # into a t(...) call's format argument).
            for arg in node.args[1:]:
                self.visit(arg)
            for kw in node.keywords:
                self.visit(kw.value)
            return

        if name in _EXEMPT_CALL_NAMES or name in _DOMAIN_ENUM_CALL_NAMES:
            return

        if name in _SQL_METHOD_NAMES:
            for arg in node.args[1:]:
                self.visit(arg)
            for kw in node.keywords:
                self.visit(kw.value)
            return

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # row["seq"], row["won"], etc. -- a data-structure key, not text.
        # Still visit the value being subscripted (normally just a Name).
        self.visit(node.value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        # An f-string. In this file, only used for URL construction
        # (cmd_invite's referral link) -- never message text, which always
        # goes through t(...) with {}-style .format() placeholders instead.
        # Skip the literal fragments; still check any interpolated
        # sub-expressions in case one hides a real violation.
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                self.visit(value.value)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and any(ch.isalpha() for ch in node.value):
            self.violations.append(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # skip the docstring
        for stmt in body:
            self.visit(stmt)
        for decorator in node.decorator_list:
            self.visit(decorator)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Module(self, node: ast.Module) -> None:
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # skip the module docstring
        for stmt in body:
            self.visit(stmt)


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
        "async def handler(message, notifier):\n"
        '    await notifier.send(message.chat.id, "This is not translated")\n'
    )
    finder = _LiteralTextFinder()
    finder.visit(tree)
    assert finder.violations


def test_the_checker_still_catches_a_literal_hidden_in_a_t_format_kwarg():
    tree = ast.parse(
        "async def handler(message, notifier, language):\n"
        '    await notifier.send(message.chat.id, t("key", language, outcome="won"))\n'
    )
    finder = _LiteralTextFinder()
    finder.visit(tree)
    assert finder.violations
