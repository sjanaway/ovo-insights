"""Static policies over the source tree, enforced as tests so CI fails on a violation."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "ovo_insights"
SQL_METHODS = {"execute", "executemany", "executescript"}


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _calls(tree: ast.AST, names: set[str]) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in names
    ]


def _is_literal_sql(node: ast.expr, constants: dict[str, ast.expr]) -> bool:
    """SQL must be a string literal, a concatenation of literals, or a module constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_literal_sql(node.left, constants) and _is_literal_sql(node.right, constants)
    if isinstance(node, ast.Name) and node.id in constants:
        return _is_literal_sql(constants[node.id], constants)
    return False


def _module_constants(tree: ast.Module) -> dict[str, ast.expr]:
    constants: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (isinstance(target, ast.Name) and target.id.isupper()) or (
                isinstance(target, ast.Name) and target.id.startswith("_")
            ):
                constants[target.id] = node.value
    return constants


# executescript runs a migration file's contents; that variable is the one allowed exception.
ALLOWED_DYNAMIC_SQL = {("storage/migrator.py", "executescript", "migration.sql")}


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_sql_is_never_built_dynamically(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = _module_constants(tree)
    relative = str(path.relative_to(SRC))

    for call in _calls(tree, SQL_METHODS):
        assert isinstance(call.func, ast.Attribute)
        if not call.args:
            continue
        sql = call.args[0]
        if (relative, call.func.attr, ast.unparse(sql)) in ALLOWED_DYNAMIC_SQL:
            continue
        assert _is_literal_sql(sql, constants), (
            f"{relative}:{call.lineno} builds SQL dynamically ({ast.unparse(sql)!r}). "
            "Use a literal string with ? placeholders."
        )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_regexes_are_ascii_only(path: Path) -> None:
    # \d and \w match non-ASCII digits and letters unless re.ASCII is set; Decimal accepts
    # full-width digits too, so a non-ASCII regex is a validation bypass.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    relative = str(path.relative_to(SRC))

    for call in _calls(tree, {"compile"}):
        if not (isinstance(call.func, ast.Attribute) and ast.unparse(call.func.value) == "re"):
            continue
        flags = " ".join(ast.unparse(arg) for arg in call.args[1:])
        flags += " ".join(ast.unparse(kw.value) for kw in call.keywords if kw.arg == "flags")
        pattern = ast.unparse(call.args[0])
        uses_unicode_classes = any(c in pattern for c in (r"\d", r"\w", r"\s", r"\b"))
        if uses_unicode_classes:
            assert "re.ASCII" in flags, f"{relative}:{call.lineno} regex needs re.ASCII"


def test_policy_detects_violations() -> None:
    bad = ast.parse('conn.execute(f"SELECT * FROM t WHERE id = {x}")\nre.compile(r"\\d+")')
    sql_call = _calls(bad, SQL_METHODS)[0]

    assert not _is_literal_sql(sql_call.args[0], {})
    assert _is_literal_sql(ast.parse('"a" + "b"', mode="eval").body, {})


# -- templates (M6): autoescaping and the strict Content-Security-Policy --------------------

TEMPLATES = SRC / "web" / "templates"
_TEMPLATE_RULES = {
    r"\|\s*safe\b": "the |safe filter disables autoescaping",
    r"autoescape\s+false": "autoescaping must stay on",
    r"<script(?![^>]*\bsrc=)[^>]*>": "inline scripts are blocked by the CSP",
    r"<style\b": "inline styles are blocked by the CSP",
    r"\sstyle\s*=": "style attributes are blocked by the CSP",
    r"\son[a-z]+\s*=": "inline event handlers are blocked by the CSP",
    r"\shx-on": "hx-on attributes need eval",
    r"javascript:": "javascript: URLs",
    r"""(src|href)\s*=\s*["']?(https?:)?//""": "external resources (no CDNs)",
}


def _templates() -> list[Path]:
    return sorted(TEMPLATES.glob("*.html"))


def test_there_are_templates_to_check() -> None:
    assert len(_templates()) >= 5


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_templates_keep_autoescaping_and_the_csp(path: Path) -> None:
    import re

    text = path.read_text(encoding="utf-8")
    for pattern, why in _TEMPLATE_RULES.items():
        match = re.search(pattern, text, re.IGNORECASE | re.ASCII)
        assert match is None, f"{path.name}: {match.group(0)!r} ({why})"


def test_template_rules_catch_violations() -> None:
    import re

    bad = [
        "{{ x|safe }}",
        "<script>alert(1)</script>",
        '<p style="color:red">',
        '<a onclick="x()">',
        '<div hx-on:click="x">',
        '<script src="https://cdn.example/x.js"></script>',
    ]
    for sample in bad:
        assert any(re.search(p, sample, re.IGNORECASE | re.ASCII) for p in _TEMPLATE_RULES), sample
    assert not any(
        re.search(p, '<script src="/static/vendor/x.js" defer></script>', re.IGNORECASE)
        for p in _TEMPLATE_RULES
    )


def test_the_template_environment_always_autoescapes() -> None:
    from ovo_insights.web.app import templates

    env = templates()
    assert env.autoescape is True
    assert env.from_string("{{ x }}").render(x="<b>") == "&lt;b&gt;"
