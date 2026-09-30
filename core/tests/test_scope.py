from __future__ import annotations

import re
from pathlib import Path

import pytest
from review_ctx.scope import (
    JAVA,
    TYPESCRIPT,
    Scope,
    enclosing_scope,
    find_scopes,
    language_of,
    mask,
)

CORPUS = Path(__file__).parent / "corpus"
MARKER = re.compile(r"/\*([<>?])(\w+)\*/")
REQUIRED_ACCURACY = 0.90


def _queries(path: Path) -> list[tuple[str, int, tuple[int, int] | None]]:
    """Read the labels of a corpus file: ``(file, line, expected range or None)``."""
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    asked: list[tuple[int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for kind, name in MARKER.findall(line):
            if kind == "<":
                starts[name] = number
            elif kind == ">":
                ends[name] = number
            else:
                asked.append((number, name))
    return [
        (path.name, line, None if name == "none" else (starts[name], ends[name]))
        for line, name in asked
    ]


def _run(path: Path) -> list[tuple[str, int, tuple[int, int] | None, tuple[int, int] | None]]:
    language = language_of(path.name)
    scopes = find_scopes(path.read_text(encoding="utf-8"), language)
    results = []
    for name, line, expected in _queries(path):
        found = enclosing_scope(scopes, line)
        actual = (found.start, found.end) if found else None
        results.append((name, line, expected, actual))
    return results


CORPUS_FILES = sorted(p for p in CORPUS.iterdir() if p.is_file())


def test_corpus_has_enough_labelled_hunks() -> None:
    total = sum(len(_queries(path)) for path in CORPUS_FILES)
    assert total >= 30


def test_enclosing_range_is_exact_for_at_least_ninety_percent_of_hunks() -> None:
    results = [row for path in CORPUS_FILES for row in _run(path)]
    misses = [row for row in results if row[2] != row[3]]
    accuracy = 1 - len(misses) / len(results)
    detail = "\n".join(f"{n}:{line} expected {e} got {a}" for n, line, e, a in misses)
    assert accuracy >= REQUIRED_ACCURACY, f"{accuracy:.1%} exact\n{detail}"


def test_known_limitations_are_only_the_documented_ones() -> None:
    results = [row for path in CORPUS_FILES for row in _run(path)]
    missed = {(name, line) for name, line, expected, actual in results if expected != actual}
    kinds = {"OrderController.java": "record compact constructor", "orders.service.ts": "IIFE"}
    assert {name for name, _ in missed} <= set(kinds)
    assert len(missed) <= 2


def _scopes(source: str, language: str = TYPESCRIPT) -> list[Scope]:
    return find_scopes(source, language)


def test_destructured_parameters_do_not_break_the_function_header() -> None:
    source = "export function App({ title, items }) {\n  const a = 1;\n  return a;\n}\n"
    (scope,) = _scopes(source)
    assert (scope.name, scope.kind, scope.start, scope.end) == ("App", "function", 1, 4)
    assert scope.signature == "App({ title, items })"


def test_arrow_component_with_destructured_props() -> None:
    source = "const Row = ({ a, b }) => {\n  const x = 1;\n  return x;\n};\n"
    (scope,) = _scopes(source)
    assert (scope.name, scope.kind) == ("Row", "arrow")


def test_callbacks_passed_as_arguments_are_not_scopes() -> None:
    source = "useEffect(() => {\n  a();\n  b();\n  c();\n  d();\n}, []);\n"
    assert _scopes(source) == []


def test_functions_inside_a_callback_body_are_scopes() -> None:
    source = (
        "describe('x', () => {\n  function helper() {\n    a();\n    b();\n    c();\n  }\n});\n"
    )
    (scope,) = _scopes(source)
    assert (scope.name, scope.start, scope.end) == ("helper", 2, 6)


def test_control_flow_blocks_are_not_scopes() -> None:
    source = "function f() {\n  if (a) {\n    b();\n  } else {\n    c();\n  }\n}\n"
    assert [s.name for s in _scopes(source)] == ["f"]


def test_java_synchronized_method_is_a_method_but_synchronized_block_is_not() -> None:
    source = (
        "class A {\n  synchronized void m() {\n    synchronized (this) {\n"
        "      x();\n    }\n  }\n}\n"
    )
    assert [(s.name, s.start, s.end) for s in _scopes(source, JAVA)] == [("m", 2, 6)]


def test_java_lambda_blocks_are_not_scopes() -> None:
    source = "class A {\n  void m() {\n    run(() -> {\n      x();\n    });\n  }\n}\n"
    assert [s.name for s in _scopes(source, JAVA)] == ["m"]


def test_java_anonymous_class_methods_are_scopes_inside_the_outer_method() -> None:
    source = (
        "class A {\n  void m() {\n    x(new B() {\n      public void run() {\n"
        "        a();\n        b();\n        c();\n      }\n    });\n  }\n}\n"
    )
    scopes = _scopes(source, JAVA)
    assert [(s.name, s.start, s.end) for s in scopes] == [("m", 2, 10), ("run", 4, 8)]


def test_short_inner_scope_yields_to_its_enclosing_scope() -> None:
    source = (
        "function outer() {\n  a();\n  const inner = () => {\n    b();\n  };\n  c();\n  d();\n}\n"
    )
    scopes = _scopes(source)
    assert enclosing_scope(scopes, 4).name == "outer"
    assert enclosing_scope(scopes, 2).name == "outer"


def test_line_outside_any_function_has_no_scope() -> None:
    assert enclosing_scope(_scopes("const a = 1;\nfunction f() {\n  x();\n}\n"), 1) is None


def test_braces_in_strings_comments_and_regexes_are_ignored() -> None:
    source = (
        "function f() {\n"
        "  const a = '{';\n"
        '  const b = "}";\n'
        "  // }\n"
        "  /* { */\n"
        "  const c = /}/;\n"
        "  return `${a}}`;\n"
        "}\n"
    )
    assert [(s.name, s.start, s.end) for s in _scopes(source)] == [("f", 1, 8)]


def test_mask_keeps_offsets_and_line_breaks() -> None:
    source = 'a = "x{y"; // c\nb = 1;\n'
    masked = mask(source, JAVA)
    assert len(masked) == len(source) and masked.count("\n") == 2
    assert "{" not in masked and masked.startswith('a = "   "; ')


def test_apostrophe_without_a_closing_quote_is_not_a_string() -> None:
    source = "function f() {\n  return <p>Don't stop</p>;\n}\nfunction g() {\n  x();\n}\n"
    assert [s.name for s in _scopes(source)] == ["f", "g"]


def test_signatures_are_truncated() -> None:
    params = ", ".join(f"argument{i}: string" for i in range(20))
    (scope,) = _scopes(f"function big({params}) {{\n  x();\n}}\n")
    assert len(scope.signature) == 100 and scope.signature.endswith("...")


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("A.java", JAVA),
        ("src/a.ts", TYPESCRIPT),
        ("a.TSX", TYPESCRIPT),
        ("a.jsx", TYPESCRIPT),
        ("a.py", None),
        ("Makefile", None),
    ],
)
def test_language_is_detected_by_extension(path: str, language: str | None) -> None:
    assert language_of(path) == language


def test_unbalanced_closing_braces_do_not_raise() -> None:
    assert _scopes("}\n}\nfunction f() {\n  x();\n}\n")[0].name == "f"
