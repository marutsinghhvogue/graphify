"""pdg.py — intra-procedural data dependence (the PDG's data half) for Python.

Parses a function with tree-sitter, walks its statements in source order, and adds
a ``data_dep`` edge from the statement that last defined a variable to the
statement that uses it (last-write def→use). Emits graphify ``{nodes, edges}``:
``statement`` nodes + ``data_dep`` edges, so it composes with the rest of the
graph and feeds taint analysis (``taint.py``).

Scope (v1): Python, intra-procedural, last-write over statement order — exact for
straight-line code, approximate around branches/loops (a CFG-based
reaching-definitions pass is the productionization step). Findings built on this
are therefore INFERRED. See docs/pdg-taint-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _parser():
    import tree_sitter_python as tspy
    from tree_sitter import Language, Parser
    return Parser(Language(tspy.language()))


@dataclass(frozen=True)
class Arg:
    var: str | None             # variable name if the arg is a plain identifier
    text: str                   # the arg's source text (for source-pattern matching)
    callee: str | None          # callee name if the arg is itself a call (nested)


@dataclass(frozen=True)
class CallSite:
    callee: str                 # last name of the called function (g / obj.g -> g)
    args: tuple                 # tuple[Arg] — one per positional argument


@dataclass
class Stmt:
    id: str
    line: int          # 1-based
    text: str
    defs: list[str]
    uses: list[str]
    node_type: str
    func: str
    file: str = ""
    calls: list = field(default_factory=list)              # list[CallSite]
    params: list[str] = field(default_factory=list)        # set on the function_entry stmt
    tainted: dict[str, Any] = field(default_factory=dict)  # filled by taint.py


def _read(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")


def _target_names(node) -> list[str]:
    """Identifiers bound by an assignment/for/with target. Plain identifiers and
    tuple/list patterns bind; subscript/attribute targets bind nothing simple."""
    if node is None:
        return []
    if node.type == "identifier":
        return [node.text.decode()]
    if node.type in ("pattern_list", "tuple_pattern", "list_pattern"):
        out: list[str] = []
        for c in node.named_children:
            out += _target_names(c)
        return out
    return []


def _use_names(node) -> list[str]:
    """Variable *uses* under ``node``: identifiers read, excluding attribute names
    (``x.attr`` → attr is not a var) and keyword-argument names (``f(k=v)`` → k)."""
    out: list[str] = []
    if node is None:
        return out

    def _is_field(parent, field: str, child) -> bool:
        # tree-sitter returns fresh node wrappers, so compare by stable node id.
        f = parent.child_by_field_name(field)
        return f is not None and f.id == child.id

    def walk(n) -> None:
        if n.type == "identifier":
            p = n.parent
            if p is not None and p.type == "attribute" and _is_field(p, "attribute", n):
                return  # method / field name, not a variable use
            if p is not None and p.type == "keyword_argument" and _is_field(p, "name", n):
                return  # keyword-arg name
            out.append(n.text.decode())
            return
        for c in n.children:
            walk(c)
    walk(node)
    return out


def _base_use_of_target(node) -> list[str]:
    """A subscript/attribute assignment target uses its base: ``a[i] = x`` uses a,i."""
    if node is not None and node.type in ("subscript", "attribute"):
        return _use_names(node)
    return []


def _callee_name(fn) -> str:
    if fn is None:
        return ""
    if fn.type == "identifier":
        return fn.text.decode()
    if fn.type == "attribute":
        a = fn.child_by_field_name("attribute")
        return a.text.decode() if a is not None else ""
    return ""


def _call_args(src: bytes, call) -> tuple:
    args = call.child_by_field_name("arguments")
    out: list = []
    if args is not None:
        for c in args.named_children:
            if c.type == "keyword_argument":
                c = c.child_by_field_name("value") or c
            var = c.text.decode() if c.type == "identifier" else None
            callee = _callee_name(c.child_by_field_name("function")) if c.type == "call" else None
            out.append(Arg(var, _read(src, c), callee))
    return tuple(out)


def _stmt_calls(src: bytes, stmt) -> list:
    """Every call in a statement (outer + nested), for inter-procedural taint."""
    return [
        CallSite(_callee_name(c.child_by_field_name("function")), _call_args(src, c))
        for c in _descendants(stmt, "call")
    ]


def _stmt_def_use(stmt) -> tuple[list[str], list[str]]:
    t = stmt.type
    if t == "expression_statement" and stmt.named_child_count:
        inner = stmt.named_children[0]
        if inner.type in ("assignment", "augmented_assignment"):
            left = inner.child_by_field_name("left")
            right = inner.child_by_field_name("right")
            defs = _target_names(left)
            uses = _use_names(right)
            uses += _base_use_of_target(left)
            if inner.type == "augmented_assignment":
                uses += _use_names(left)   # x += y reads x too
            return defs, uses
        return [], _use_names(inner)       # bare call/expression — a sink candidate
    if t == "for_statement":
        return _target_names(stmt.child_by_field_name("left")), _use_names(stmt.child_by_field_name("right"))
    if t == "with_statement":
        defs: list[str] = []
        uses: list[str] = []
        for wi in _descendants(stmt, "with_item"):
            val = wi.child_by_field_name("value") or (wi.named_children[0] if wi.named_children else None)
            asn = wi.child_by_field_name("alias")
            uses += _use_names(val)
            defs += _target_names(asn)
        return defs, uses
    # if / while / return / assert / raise / etc. — header uses only
    return [], _use_names(_header_of(stmt))


# Compound statements whose header (condition/target) we treat as one unit, then
# recurse into the body block(s) for the nested statements.
_COMPOUND = {"if_statement", "elif_clause", "else_clause", "for_statement",
             "while_statement", "with_statement", "try_statement", "except_clause",
             "finally_clause"}
_LEAF = {"expression_statement", "return_statement", "assert_statement",
         "raise_statement", "delete_statement", "print_statement"}


def _header_of(stmt):
    """The condition/subject part of a compound statement (excludes its body)."""
    cond = stmt.child_by_field_name("condition")
    return cond if cond is not None else stmt


def _descendants(node, kind: str):
    out = []
    def walk(n):
        for c in n.children:
            if c.type == kind:
                out.append(c)
            walk(c)
    walk(node)
    return out


def _iter_statements(block):
    """Statement units in source order: each leaf statement, and the header of each
    compound statement, recursing into compound bodies."""
    for c in block.named_children:
        if c.type in _LEAF or c.type in ("for_statement", "with_statement"):
            yield c
            body = c.child_by_field_name("body")
            if body is not None:
                yield from _iter_statements(body)
        elif c.type in _COMPOUND:
            yield c
            for field_name in ("consequence", "body"):
                b = c.child_by_field_name(field_name)
                if b is not None:
                    yield from _iter_statements(b)
            for alt in c.children:
                if alt.type in ("elif_clause", "else_clause", "except_clause", "finally_clause"):
                    yield from _iter_statements_wrap(alt)
        elif c.type == "block":
            yield from _iter_statements(c)


def _iter_statements_wrap(clause):
    yield clause
    b = clause.child_by_field_name("consequence") or clause.child_by_field_name("body")
    if b is not None:
        yield from _iter_statements(b)


def _analyze_function(fn, src: bytes, path: str, findings_ns: str) -> tuple[list[Stmt], list[dict]]:
    name = ""
    n = fn.child_by_field_name("name")
    if n is not None:
        name = n.text.decode()
    stmts: list[Stmt] = []
    edges: list[dict] = []
    last_def: dict[str, str] = {}

    # parameters are definitions at entry
    params = fn.child_by_field_name("parameters")
    param_names: list[str] = []
    if params is not None:
        for p in params.named_children:
            pid = p if p.type == "identifier" else p.child_by_field_name("name")
            if pid is not None and pid.type == "identifier":
                param_names.append(pid.text.decode())
    body = fn.child_by_field_name("body")
    if body is None:
        return [], []

    def sid(i: int) -> str:
        return f"stmt_{findings_ns}_{name}_{i}".lower().replace(" ", "_")

    entry = Stmt(sid(0), (fn.start_point[0] + 1), f"def {name}(...)", list(param_names), [],
                 "function_entry", name, file=path, params=list(param_names))
    stmts.append(entry)
    for p in param_names:
        last_def[p] = entry.id

    for i, st in enumerate(_iter_statements(body), start=1):
        defs, uses = _stmt_def_use(st)
        text = _read(src, st) if st.end_byte > st.start_byte else ""
        s = Stmt(sid(i), st.start_point[0] + 1, text, defs, uses, st.type, name,
                 file=path, calls=_stmt_calls(src, st))
        stmts.append(s)
        for u in uses:
            d = last_def.get(u)
            if d and d != s.id:
                edges.append({
                    "source": d, "target": s.id, "relation": "data_dep",
                    "confidence": "INFERRED", "confidence_score": 0.7,
                    "source_file": path, "source_location": f"L{s.line}",
                    "context": "pdg", "metadata": {"var": u},
                })
        for d in defs:
            last_def[d] = s.id
    return stmts, edges


def build_pdg(source: str, *, path: str = "", ns: str = "m") -> dict[str, Any]:
    """Build the intra-procedural data-dependence graph for Python ``source``.

    Returns ``{nodes, edges, statements}`` — ``statement`` nodes + ``data_dep``
    edges (graphify schema), plus the ``Stmt`` objects for taint analysis."""
    parser = _parser()
    src = source.encode("utf-8")
    tree = parser.parse(src)
    all_stmts: list[Stmt] = []
    edges: list[dict] = []
    for fn in _descendants(tree.root_node, "function_definition"):
        s, e = _analyze_function(fn, src, path, ns)
        all_stmts += s
        edges += e
    nodes = [{
        "id": s.id,
        "label": (s.text.splitlines()[0].strip()[:160] if s.text else s.node_type),
        "file_type": "code", "kind": "statement",
        "source_file": path, "source_location": f"L{s.line}",
        "metadata": {"func": s.func, "defs": s.defs, "uses": s.uses, "node_type": s.node_type},
    } for s in all_stmts]
    return {"nodes": nodes, "edges": edges, "statements": all_stmts}


def build_pdg_file(path: str | Path, *, ns: str | None = None) -> dict[str, Any]:
    p = Path(path)
    return build_pdg(p.read_text(encoding="utf-8", errors="replace"),
                     path=str(p), ns=ns or p.stem)
