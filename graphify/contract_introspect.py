"""contract_introspect.py — cross-service edge inference via a contract pivot.

SPIKE. Proves that a consumer's HTTP calls can be matched to producer endpoints
across services (and languages) by ``(method, normalized-path)`` against a
GLOBAL endpoint catalog — WITHOUT resolving hosts. Service identity is derived
from where the controller lives (the service directory), matching the estate's
"derive from controllers" constraint (no gateway/service-registry assumed).

Producer frameworks (decorator/annotation based → route→handler binds locally
in source): FastAPI (Python), NestJS (TypeScript), Spring (Java: every
``@*Mapping`` form, type-level prefixes, and the ``server.servlet.context-path``
from the service's own or config-server YAML/properties). Consumer calls: fetch /
axios (JS/TS), requests / httpx (Python), Feign interfaces, RestTemplate, and
WebClient/RestClient including URLs built by string concatenation (Java). Test
sources are skipped.

Static extraction only (tier 3). Regexes target the common shapes and are
not a bulletproof parser. Emits the standard graphify
``{nodes, edges}``; cross-service edges are tagged INFERRED (unique match) or
AMBIGUOUS (path collision across services — kept, never dropped, per the
demote-not-delete recall policy). Unmatched calls are treated as external and
reported in ``stats``, not silently dropped. Recall is measured on real
estates by ``benchmarks/vs_upstream``.

Known limitations (deferred): gRPC and other non-HTTP transports; gateway
route config (Spring Cloud Gateway YAML); FastAPI ``include_router(prefix=...)``
composition; non-GET fetch options; Spring mapping arguments spread over
several lines (the path is read, a ``method=`` on a later line is not).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── data ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Endpoint:
    method: str        # GET/POST/...
    path: str          # normalized, e.g. "/users/{}"
    raw_path: str
    service: str
    handler: str
    source_file: str
    line: int


@dataclass(frozen=True)
class Call:
    method: str
    path: str          # normalized
    raw_url: str
    caller: str
    service: str
    source_file: str
    line: int
    target_service: str = ""   # named target (Feign) → near-exact resolution
    # "host" when target_service was read off a service-discovery URL host: it
    # narrows matches when it names a known service, but unlike a Feign name it
    # never turns a path match into "external".
    target_source: str = ""


# ── path normalization (the validated core) ─────────────────────────────────

_SCHEME_HOST = re.compile(r"^[a-zA-Z][\w+.-]*://[^/]+")


def normalize_path(raw: str) -> str:
    """Canonicalize a route/URL to ``/seg/seg`` with every path parameter
    collapsed to ``{}``. Strips scheme+host, query, and fragment.

    Handles param shapes across frameworks: ``{id}`` (FastAPI/Spring),
    ``:id`` (Nest/Express), ``<id>`` (Flask), and ``${id}`` (JS template).
    """
    s = raw.strip().strip('`"\'')
    s = _SCHEME_HOST.sub("", s)                 # drop scheme://host
    s = s.split("?", 1)[0].split("#", 1)[0]     # drop query/fragment
    out: list[str] = []
    for seg in s.split("/"):
        if not seg:
            continue
        if (
            (seg.startswith("{") and seg.endswith("}"))
            or seg.startswith(":")
            or (seg.startswith("<") and seg.endswith(">"))
            or "${" in seg
        ):
            out.append("{}")
        else:
            out.append(seg)
    return "/" + "/".join(out)


def _join(prefix: str, path: str) -> str:
    a = (prefix or "").strip("/")
    b = (path or "").strip("/")
    joined = "/".join(p for p in (a, b) if p)
    return "/" + joined


# ── enclosing-function tracking ──────────────────────────────────────────────

_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
# TS/JS definition forms, tried most-specific first so the broad class-method form
# doesn't mis-capture the leading keyword of an `export async function foo(` line:
#   1. a `function` declaration (export/default/async/generator variants),
#   2. an arrow function bound to a const/let/var (export variant), and
#   3. a class method / object-shorthand (`name(` — the original, broad, form).
_TS_FUNCTION = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\(")
_TS_ARROW = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*(?::[^={]+)?=>"
)
_TS_METHOD = re.compile(r"^\s*(?:public |private |protected )?(?:static )?(?:async )?(\w+)\s*\(")
_JAVA_METHOD = re.compile(r"^\s*(?:public|private|protected)\s+[\w<>\[\], ?]+\s+(\w+)\s*\(")
# An interface's abstract method (a Feign client's `String getAccount(...);`) has no
# access modifier and ends in `;`. Only used on interface files, where the broad
# shape can't be mistaken for a statement.
_JAVA_ABSTRACT = re.compile(
    r"^\s*(?:public\s+)?(?!return\b|throw\b|new\b)[\w<>\[\],.? ]+?\s+(\w+)\s*\([^;{=]*\)\s*(?:throws\s+[\w., ]+)?;\s*$"
)
_NON_FN = {"if", "for", "while", "switch", "catch", "return", "constructor", "class", "new"}


def _defs_for(lang: str) -> tuple[re.Pattern, ...]:
    return {
        "python": (_PY_DEF,),
        "ts": (_TS_FUNCTION, _TS_ARROW, _TS_METHOD),
        "java": (_JAVA_METHOD,),
        "java-interface": (_JAVA_METHOD, _JAVA_ABSTRACT),
    }[lang]


def _function_index(lines: list[str], lang: str) -> list[tuple[int, str]]:
    """Ordered (line_no, name) of function/method definitions in a file. Each line is
    matched against the language's def forms in priority order; the first that hits
    wins, so specific forms (``function``/arrow) beat the broad method form."""
    pats = _defs_for(lang)
    out: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        for pat in pats:
            m = pat.match(line)
            if m:
                if m.group(1) not in _NON_FN:
                    out.append((i, m.group(1)))
                break
    return out


def _enclosing(defs: list[tuple[int, str]], line_no: int) -> str:
    """Name of the nearest def at or above ``line_no``."""
    name = "<module>"
    for dl, dn in defs:
        if dl <= line_no:
            name = dn
        else:
            break
    return name


def _next_def_after(defs: list[tuple[int, str]], line_no: int) -> str:
    for dl, dn in defs:
        if dl >= line_no:
            return dn
    return "<unknown>"


# ── producer extraction ──────────────────────────────────────────────────────

_FASTAPI = re.compile(r'@\w+\.(get|post|put|patch|delete|head|options)\(\s*["\']([^"\']+)["\']')
_NEST_CTRL = re.compile(r'@Controller\(\s*["\']?([^"\')]*)["\']?\s*\)')
_NEST_ROUTE = re.compile(r'@(Get|Post|Put|Patch|Delete)\(\s*["\']?([^"\')]*)["\']?\s*\)')
# Any Spring mapping annotation, bare (`@GetMapping`) or with arguments. Only the
# annotation's own line is read: an argument list continuing onto later lines still
# yields its path, but a `method=` given there is missed (the mapping is ANY).
_SPRING_MAPPING = re.compile(r'@(Get|Post|Put|Patch|Delete|Request)Mapping\b(?:\s*\(([^)]*)\)?)?')
_ANNOT_PATH_POSITIONAL = re.compile(r'^\s*\{?\s*"([^"]*)"')
_ANNOT_PATH_KEYWORD = re.compile(r'\b(?:value|path)\s*=\s*\{?\s*"([^"]*)"')
_REQUEST_METHOD = re.compile(r'RequestMethod\.(\w+)')
_JAVA_TYPE_DECL = re.compile(r'^\s*(?:(?:public|protected|private|abstract|final|static)\s+)*(?:class|interface)\s+\w+')
# A method-level @RequestMapping without `method=` serves every HTTP method.
ANY_METHOD = "*"


def _parse_spring_mapping(line: str) -> tuple[str, str, str] | None:
    """``(verb, http_method, path)`` of a Spring mapping annotation on ``line``.

    Handles ``@GetMapping`` (bare), ``@GetMapping("/x")``, ``@PostMapping(value = "/x")``,
    ``@RequestMapping(path = {"/x"}, method = RequestMethod.PUT)`` in any argument
    order. ``verb`` is the annotation stem (``Get``…``Request``); ``path`` is ``""``
    when the annotation names none; ``http_method`` is ``ANY_METHOD`` for a
    ``@RequestMapping`` without ``method=``."""
    m = _SPRING_MAPPING.search(line)
    if not m:
        return None
    verb, args = m.group(1), m.group(2) or ""
    pm = _ANNOT_PATH_POSITIONAL.match(args) or _ANNOT_PATH_KEYWORD.search(args)
    path = pm.group(1) if pm else ""
    if verb == "Request":
        rm = _REQUEST_METHOD.search(args)
        method = rm.group(1).upper() if rm else ANY_METHOD
    else:
        method = verb.upper()
    return verb, method, path


def _spring_class_prefix(lines: list[str]) -> tuple[str, int]:
    """``(prefix, decl_line)``: the type-level ``@RequestMapping`` path (annotations
    above the class/interface declaration) and the declaration's line index. Only
    annotations above the declaration count, so a controller with no type-level
    mapping doesn't borrow its first method's path."""
    decl = next((i for i, ln in enumerate(lines) if _JAVA_TYPE_DECL.match(ln)), len(lines))
    prefix = ""
    for ln in lines[:decl]:
        parsed = _parse_spring_mapping(ln)
        if parsed and parsed[0] == "Request":
            prefix = parsed[2]
    return prefix, decl


def _detect_lang(path: Path) -> str | None:
    return {".py": "python", ".ts": "ts", ".java": "java"}.get(path.suffix)


def _extract_endpoints(path: Path, service: str, lines: list[str], lang: str) -> list[Endpoint]:
    defs = _function_index(lines, lang)
    eps: list[Endpoint] = []
    if lang == "python":
        for i, line in enumerate(lines):
            m = _FASTAPI.search(line)
            if m:
                eps.append(Endpoint(m.group(1).upper(), normalize_path(m.group(2)), m.group(2),
                                    service, _next_def_after(defs, i), str(path), i + 1))
    elif lang == "ts":
        prefix = ""
        cm = next((_NEST_CTRL.search(ln) for ln in lines if _NEST_CTRL.search(ln)), None)
        if cm:
            prefix = cm.group(1)
        for i, line in enumerate(lines):
            m = _NEST_ROUTE.search(line)
            if m:
                full = _join(prefix, m.group(2))
                eps.append(Endpoint(m.group(1).upper(), normalize_path(full), full,
                                    service, _next_def_after(defs, i), str(path), i + 1))
    elif lang == "java":
        # A @FeignClient interface uses the same @*Mapping annotations but is a
        # CONSUMER, not a producer — its mappings are harvested in _extract_calls.
        if any(_FEIGN_CLIENT.search(ln) for ln in lines):
            return eps
        cprefix, decl = _spring_class_prefix(lines)
        for i in range(decl + 1, len(lines)):
            parsed = _parse_spring_mapping(lines[i])
            if parsed:
                full = _join(cprefix, parsed[2])
                eps.append(Endpoint(parsed[1], normalize_path(full), full,
                                    service, _next_def_after(defs, i), str(path), i + 1))
    return eps


# ── consumer extraction ──────────────────────────────────────────────────────

_FETCH = re.compile(r'fetch\(\s*[`"\']([^`"\']+)[`"\']')
_AXIOS = re.compile(r'axios\.(get|post|put|patch|delete)\(\s*[`"\']([^`"\']+)[`"\']')
_PY_HTTP = re.compile(
    r'(?:requests|httpx|client|session|self\.\w+)\.(get|post|put|patch|delete)\(\s*f?["\']([^"\']+)["\']'
)
# Java consumers
_FEIGN_CLIENT = re.compile(r'@FeignClient\(\s*(?:[^)]*?\b(?:name|value)\s*=\s*)?["\']([^"\']+)["\']')
_FEIGN_PATH = re.compile(r'@FeignClient\([^)]*?\bpath\s*=\s*["\']([^"\']*)["\']')
# Literal-first URL shapes, kept for plan_benchmark's grep baseline.
_RESTTEMPLATE = re.compile(
    r'\.(getForObject|getForEntity|postForObject|postForEntity|put|delete|exchange)\(\s*["\']([^"\']+)["\']'
)
_WEBCLIENT_URI = re.compile(r'\.uri\(\s*["\']([^"\']+)["\']')
_RT_CALL = re.compile(r'\.(getForObject|getForEntity|postForObject|postForEntity|put|delete|exchange)\(')
_URI_CALL = re.compile(r'\.uri\(')
_RT_METHOD = {"getForObject": "GET", "getForEntity": "GET", "postForObject": "POST",
              "postForEntity": "POST", "put": "PUT", "delete": "DELETE", "exchange": "GET"}
# WebClient / RestClient pick the verb before `.uri(`: `.post().uri(...)` or
# `.method(HttpMethod.PUT).uri(...)`, often on the previous line.
_FLUENT_VERB = re.compile(r'\.(get|post|put|patch|delete)\(\s*\)|\.method\(\s*HttpMethod\.(\w+)\s*\)')


def _first_arg(text: str) -> str:
    """The first argument of a call whose ``(`` was just consumed: everything up to the
    first top-level ``,`` or the closing ``)``, respecting string literals."""
    depth, quote, out = 0, "", []
    for ch in text:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        out.append(ch)
    return "".join(out)


def _concat_url(arg: str) -> tuple[str, bool] | None:
    """Reduce a string-concatenation URL expression to a path template.

    String literals are kept, non-literal operands between them become ``{}``, and a
    leading non-literal (the host: ``getCustomerServiceUri()``, ``hostname``) is
    dropped. ``"/owners/" + ownerId + "/pets"`` → ``/owners/{}/pets``. Returns
    ``(template, host_dropped)``, or ``None`` when the expression has no literal."""
    parts, depth, quote, cur = [], 0, "", []
    for ch in arg:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "+" and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur).strip())

    def _literal(p: str) -> str | None:
        return p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else None

    out: list[str] = []
    host_dropped = False
    for p in parts:
        lit = _literal(p)
        if lit is not None:
            out.append(lit)
        elif out:
            out.append("{}")
        else:
            host_dropped = True
    if not out:
        return None
    return "".join(out), host_dropped


def _looks_like_url(template: str, host_dropped: bool) -> bool:
    """A real URL argument, not ``Map.put("date", …)``: it starts with ``/``, a scheme,
    or a placeholder, or follows a host expression that was dropped."""
    return host_dropped or template.startswith(("/", "http://", "https://", "{", "${"))


def _fluent_verb(lines: list[str], i: int, upto: int) -> str:
    """The WebClient/RestClient verb set before the ``.uri(`` at ``lines[i][upto]``,
    looking back up to three lines; GET when none is visible."""
    window = "\n".join(lines[max(0, i - 3):i] + [lines[i][:upto]])
    found = list(_FLUENT_VERB.finditer(window))
    if not found:
        return "GET"
    last = found[-1]
    return (last.group(1) or last.group(2)).upper()


def _url_calls(lines: list[str], i: int, pattern: re.Pattern) -> list[tuple[re.Match, str, bool]]:
    """``(match, template, host_dropped)`` for each ``pattern`` call on ``lines[i]``
    whose first argument reduces to a URL. The argument may continue onto the next
    two lines."""
    out = []
    rest = "\n".join(lines[i:i + 3])
    for m in pattern.finditer(lines[i]):
        reduced = _concat_url(_first_arg(rest[m.end():]))
        if reduced and _looks_like_url(*reduced):
            out.append((m, *reduced))
    return out


def _host_target(raw_url: str) -> str:
    """A bare service-name host (``http://customers-service/…``, service discovery
    style) as a named target; ``""`` for real hostnames and ``localhost``."""
    host = host_of(raw_url).split(":", 1)[0]
    return host if host and "." not in host and host != "localhost" else ""


def _extract_java_consumers(path: Path, service: str, lines: list[str]) -> list[Call]:
    """Feign clients (near-exact: the client names its target service) + RestTemplate
    / WebClient / RestClient URL calls, including URLs built by concatenation."""
    calls: list[Call] = []
    fm = next((_FEIGN_CLIENT.search(ln) for ln in lines if _FEIGN_CLIENT.search(ln)), None)
    feign_svc = fm.group(1) if fm else ""
    if feign_svc:
        defs = _function_index(lines, "java-interface")
        pm = next((_FEIGN_PATH.search(ln) for ln in lines if _FEIGN_PATH.search(ln)), None)
        cprefix, decl = _spring_class_prefix(lines)
        base = _join(pm.group(1) if pm else "", cprefix)
        for i in range(decl + 1, len(lines)):
            parsed = _parse_spring_mapping(lines[i])
            if parsed:
                full = _join(base, parsed[2])
                method = "GET" if parsed[1] == ANY_METHOD else parsed[1]
                calls.append(Call(method, normalize_path(full), full,
                                  _next_def_after(defs, i), service, str(path), i + 1,
                                  target_service=feign_svc))
        return calls
    defs = _function_index(lines, "java")
    for i in range(len(lines)):
        for m, template, _ in _url_calls(lines, i, _RT_CALL):
            # `.put(`/`.delete(` are also Map/collection methods: require a path or
            # scheme in the literal even when a host expression was dropped.
            if m.group(1) in ("put", "delete") and not template.startswith(("/", "http://", "https://")):
                continue
            calls.append(Call(_RT_METHOD.get(m.group(1), "GET"), normalize_path(template),
                              template, _enclosing(defs, i), service, str(path), i + 1,
                              target_service=_host_target(template), target_source="host"))
        for m, template, _ in _url_calls(lines, i, _URI_CALL):
            calls.append(Call(_fluent_verb(lines, i, m.start()), normalize_path(template),
                              template, _enclosing(defs, i), service, str(path), i + 1,
                              target_service=_host_target(template), target_source="host"))
    return calls


def _extract_calls(path: Path, service: str, lines: list[str], lang: str) -> list[Call]:
    defs = _function_index(lines, lang)
    calls: list[Call] = []
    if lang == "java":
        return _extract_java_consumers(path, service, lines)
    for i, line in enumerate(lines):
        if lang in ("ts", "python"):
            for m in _FETCH.finditer(line):
                calls.append(Call("GET", normalize_path(m.group(1)), m.group(1),
                                  _enclosing(defs, i), service, str(path), i + 1))
            for m in _AXIOS.finditer(line):
                calls.append(Call(m.group(1).upper(), normalize_path(m.group(2)), m.group(2),
                                  _enclosing(defs, i), service, str(path), i + 1))
        if lang == "python":
            for m in _PY_HTTP.finditer(line):
                calls.append(Call(m.group(1).upper(), normalize_path(m.group(2)), m.group(2),
                                  _enclosing(defs, i), service, str(path), i + 1))
    return calls


# ── orchestration ────────────────────────────────────────────────────────────

def _ep_id(e: Endpoint) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", e.path.lower()).strip("_")
    return f"svc_{e.service}_ep_{e.method.lower()}_{slug}"


def _fn_id(service: str, fn: str) -> str:
    return f"svc_{service}_fn_{fn}".lower()


_TEST_DIRS = {"test", "tests", "__tests__", "spec", "e2e"}
_TEST_FILE = re.compile(r"(?:\.(?:spec|test)\.[jt]sx?|_test\.py|Tests?\.java)$|^test_.*\.py$")
_SKIP_DIRS = {".git", "node_modules", "target", "build", "dist", ".venv", "venv", "__pycache__"}


def _is_test_path(rel: Path) -> bool:
    """Test sources call endpoints too (MockMvc, WebTestClient, fixtures); they are not
    production consumers, so their calls must not become cross-service edges."""
    return any(p in _TEST_DIRS for p in rel.parts[:-1]) or bool(_TEST_FILE.search(rel.name))


def scan_service(service_dir: Path, service: str) -> tuple[list[Endpoint], list[Call]]:
    endpoints: list[Endpoint] = []
    calls: list[Call] = []
    for f in sorted(service_dir.rglob("*")):
        if not f.is_file():
            continue
        lang = _detect_lang(f)
        if lang is None or _is_test_path(f.relative_to(service_dir)):
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        endpoints += _extract_endpoints(f, service, lines, lang)
        calls += _extract_calls(f, service, lines, lang)
    return endpoints, calls


# ── Spring service config: names and servlet context path ─────────────────────
#
# A Spring service serves every route under `server.servlet.context-path`, set in its
# own application.yml/.properties or in a config-server file named after the
# application (`config/.../shared/account-service.yml`). Clients call the prefixed
# path, so endpoints must carry it. `spring.application.name` is also the name Feign
# clients and service-discovery URLs use for the service, which may differ from its
# directory (`customers-service` in `spring-petclinic-customers-service/`).

_CONFIG_SUFFIXES = (".yml", ".yaml", ".properties")
_CONTEXT_PATH_KEYS = ("server.servlet.context-path", "server.context-path")
_PROPERTY_LINE = re.compile(r"^([^=:\s]+)\s*[=:]\s*(.*)$")


def _flatten_config(path: Path) -> dict[str, str]:
    """Dotted-key view of a Spring config file; the first document/occurrence wins."""
    import yaml

    text = path.read_text(encoding="utf-8", errors="replace")
    out: dict[str, str] = {}
    if path.suffix == ".properties":
        for ln in text.splitlines():
            ln = ln.strip()
            m = _PROPERTY_LINE.match(ln) if ln and ln[0] not in "#!" else None
            if m:
                out.setdefault(m.group(1), m.group(2).strip())
        return out

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif node is not None and not isinstance(node, list):
            out.setdefault(prefix, str(node))

    try:
        for doc in yaml.safe_load_all(text):
            walk(doc, "")
    except yaml.YAMLError:
        pass
    return out


def _config_files(root: Path) -> dict[str, list[Path]]:
    """Non-test Spring config files under ``root``, keyed by stem."""
    import os

    by_stem: dict[str, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.suffix in _CONFIG_SUFFIXES and not _is_test_path(p.relative_to(root)):
                by_stem.setdefault(p.stem, []).append(p)
    return by_stem


def _concrete(value: str | None) -> str:
    return value.strip().strip("\"'") if value and "${" not in value else ""


def _service_configs(root: Path, service_dirs: list[Path]) -> dict[str, tuple[set[str], str]]:
    """``{service_dir_name: (names, context_path)}``. Names are the directory name plus
    ``spring.application.name``; the context path comes from the service's own
    application/bootstrap config, else from a config-server file named after it."""
    files = _config_files(root)
    out: dict[str, tuple[set[str], str]] = {}
    for d in service_dirs:
        own: dict[str, str] = {}
        mine = [p for stem, ps in files.items() for p in ps
                if stem.split("-", 1)[0] in ("application", "bootstrap") and d in p.parents]
        # base files (application.yml) before profile variants (application-docker.yml)
        for p in sorted(mine, key=lambda p: (p.stem not in ("application", "bootstrap"), str(p))):
            for k, v in _flatten_config(p).items():
                own.setdefault(k, v)
        names = {d.name}
        app = _concrete(own.get("spring.application.name"))
        if app:
            names.add(app)
        ctx = next((_concrete(own.get(k)) for k in _CONTEXT_PATH_KEYS if _concrete(own.get(k))), "")
        if not ctx:
            for name in sorted(names):
                for p in files.get(name, []):
                    flat = _flatten_config(p)
                    ctx = next((_concrete(flat.get(k)) for k in _CONTEXT_PATH_KEYS if _concrete(flat.get(k))), "")
                    if ctx:
                        break
                if ctx:
                    break
        out[d.name] = (names, ctx if ctx.startswith("/") else "")
    return out


def _collect(root: Path) -> tuple[list[Endpoint], list[Call], dict[str, set[str]]]:
    """Scan every immediate subdirectory of ``root`` as a service. Endpoints carry
    their service's context path; returns ``(endpoints, calls, service_names)``."""
    from dataclasses import replace

    svc_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    configs = _service_configs(root, svc_dirs)
    all_eps: list[Endpoint] = []
    all_calls: list[Call] = []
    for svc_dir in svc_dirs:
        eps, calls = scan_service(svc_dir, svc_dir.name)
        ctx = configs[svc_dir.name][1]
        if ctx:
            eps = [replace(e, raw_path=_join(ctx, e.raw_path), path=normalize_path(_join(ctx, e.raw_path)))
                   for e in eps]
        all_eps += eps
        all_calls += calls
    return all_eps, all_calls, {svc: names for svc, (names, _) in configs.items()}


def _catalog(endpoints: list[Endpoint]) -> dict[tuple[str, str], list[Endpoint]]:
    catalog: dict[tuple[str, str], list[Endpoint]] = {}
    for e in endpoints:
        catalog.setdefault((e.method, e.path), []).append(e)
    return catalog


def _candidates(c: Call, catalog: dict[tuple[str, str], list[Endpoint]],
                names: dict[str, set[str]]) -> tuple[list[Endpoint], bool]:
    """Endpoints in other services that ``c`` can reach, and whether a named target
    picked them. A Feign name is strict (no endpoint in the named service → none);
    a URL-host name only narrows when it names a known service."""
    cands = [e for e in catalog.get((c.method, c.path), []) + catalog.get((ANY_METHOD, c.path), [])
             if e.service != c.service]
    if not c.target_service:
        return cands, False
    named = [e for e in cands if c.target_service in names.get(e.service, {e.service})]
    if named or c.target_source != "host":
        return named, True
    return cands, False


def cross_service_graph(root: str | Path) -> dict[str, Any]:
    """Scan every immediate subdirectory of ``root`` as a service; build a global
    endpoint catalog and match consumer calls against it to emit cross-service
    edges. Returns ``{"nodes", "edges", "stats"}`` in graphify schema."""
    all_eps, all_calls, names = _collect(Path(root))
    catalog = _catalog(all_eps)

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def _add_node(nid: str, label: str, kind: str, sf: str, line: int, **meta):
        nodes.setdefault(nid, {
            "id": nid, "label": label, "file_type": "code", "kind": kind,
            "source_file": sf, "source_location": f"L{line}",
            "metadata": {k: v for k, v in meta.items() if v is not None},
        })

    # producer nodes: endpoint + handler, handler --handles--> endpoint
    for e in all_eps:
        eid, hid = _ep_id(e), _fn_id(e.service, e.handler)
        _add_node(eid, f"{e.method} {e.path}", "route", e.source_file, e.line,
                  service=e.service, method=e.method, path=e.path)
        # anchor: the route annotation sits just above the handler's definition
        _add_node(hid, f"{e.handler}()", "function", e.source_file, e.line, service=e.service,
                  anchor="next")
        edges.append({"source": hid, "target": eid, "relation": "handles",
                      "confidence": "EXTRACTED", "confidence_score": 1.0,
                      "source_file": e.source_file, "source_location": f"L{e.line}",
                      "context": "contract"})

    stats = {"endpoints": len(all_eps), "calls": len(all_calls),
             "matched_unique": 0, "matched_ambiguous": 0, "matched_named": 0, "external": 0}

    for c in all_calls:
        # A named target (Feign client, service-discovery host) settles a path
        # collision across services, so the match is NOT ambiguous.
        cands, named = _candidates(c, catalog, names)
        cid = _fn_id(c.service, c.caller)
        if not cands:
            stats["external"] += 1
            continue
        # anchor: the call site sits inside the caller's definition (Feign: the
        # mapping annotation sits just above the interface method)
        _add_node(cid, f"{c.caller}()", "function", c.source_file, c.line, service=c.service,
                  anchor="next" if c.target_source != "host" and c.target_service else "enclosing")
        ambiguous = len(cands) > 1 and not named
        stats["matched_named" if named else
              "matched_ambiguous" if ambiguous else "matched_unique"] += 1
        for e in cands:
            hid = _fn_id(e.service, e.handler)
            edges.append({
                "source": cid, "target": hid, "relation": "calls_service",
                "confidence": "AMBIGUOUS" if ambiguous else "INFERRED",
                "confidence_score": 0.95 if named else 0.2 if ambiguous else 0.85,
                "source_file": c.source_file, "source_location": f"L{c.line}",
                "context": "cross_service",
                "metadata": {"method": c.method, "path": c.path,
                             "to_service": e.service, "via_endpoint": _ep_id(e),
                             "ambiguous": ambiguous, "named_target": named,
                             "raw_url": c.raw_url},
            })

    return {"nodes": list(nodes.values()), "edges": edges, "stats": stats}


def host_of(raw_url: str) -> str:
    """Host of an absolute URL (``https://api.stripe.com/v1/charges`` → ``api.stripe.com``);
    ``""`` for a relative path. The third-party signal: a call to an absolute host
    that no internal endpoint claims is an outbound dependency."""
    m = _SCHEME_HOST.match((raw_url or "").strip().strip('`"\''))
    if not m:
        return ""
    return m.group(0).split("://", 1)[1]


def external_calls(root: str | Path, services: set[str] | None = None) -> list[Call]:
    """Third-party (outbound) HTTP calls across the estate: consumer calls whose
    ``(method, path)`` matches **no** internal endpoint and whose URL names an
    absolute host — i.e. Stripe/GitHub/etc., not another indexed service. Reuses
    the same scan + catalog as :func:`cross_service_graph`. ``services`` optionally
    restricts to calls originating in those services (e.g. a plan's blast radius)."""
    all_eps, all_calls, names = _collect(Path(root))
    catalog = _catalog(all_eps)

    out: list[Call] = []
    for c in all_calls:
        if services is not None and c.service not in services:
            continue
        cands, _ = _candidates(c, catalog, names)
        if cands:
            continue                      # matched an internal endpoint → not third-party
        if not host_of(c.raw_url):
            continue                      # relative/unknown target → can't call it third-party
        out.append(c)
    return out


# ── reconciliation with the tree-sitter AST graph ─────────────────────────────
#
# cross_service_graph emits handler/consumer function nodes under a self-contained
# ``svc_*`` id scheme, so cross-service edges form a subgraph disconnected from the
# tree-sitter AST nodes for the SAME handlers. reconcile_contract joins them on
# identity so blast radius can traverse from a real AST function node into the
# cross-service edges — the contract analog of scip_ingest.reconcile_scip.
#
# Join key = (path-suffix, normalized-function-name). SCIP joins on a definition
# line; contract handler nodes carry the route-decorator line, not the def line,
# so the name (with the file) is the reliable key.


def _norm_fn_name(label: str) -> str:
    """Canonical function name from a node label: drop a leading ``.`` (method
    qualifier) and a trailing ``()`` call marker, case-fold. ``get_user()`` →
    ``get_user``; ``.getOrder()`` → ``getorder``."""
    s = (label or "").strip()
    if s.startswith("."):
        s = s[1:]
    if s.endswith("()"):
        s = s[:-2]
    return s.strip().lower()


def _path_suffix_match(a: str, b: str) -> bool:
    """True if two paths share a trailing path-component suffix (either endswith
    the other), so same-named handlers in different services don't cross-match."""
    pa = [p for p in a.replace("\\", "/").split("/") if p]
    pb = [p for p in b.replace("\\", "/").split("/") if p]
    n = min(len(pa), len(pb))
    return n > 0 and pa[-n:] == pb[-n:]


# Emitted node kinds that have a tree-sitter AST twin (fold onto it). Synthetic
# construct nodes (route/schedule/event/topic) do not — they are kept as new.
_FOLDABLE_KINDS = {"function", "class"}
# Edge relations whose target is a raw *name* (not an emitted node id) and so is
# resolved against AST nodes by name: dependency injection (a type name) and
# cloud-scheduler triggers (a Lambda handler function name). In-code `triggers`
# targets are already emitted fn-node ids and resolve via id_map first, so adding
# `triggers` here only affects the raw-name (cloud) case.
_TYPE_TARGET_RELATIONS = {"injects", "triggers"}


def _line_of(node: dict) -> int | None:
    m = re.match(r"L(\d+)", str(node.get("source_location") or ""))
    return int(m.group(1)) if m else None


def _by_anchor(cands: list[dict], emitted: dict) -> list[dict]:
    """Pick among same-named AST nodes in one file (overloads) by line. An emitted
    handler is anchored at the route annotation just above its definition
    (``anchor="next"``: the nearest definition at or after that line); a caller at
    its call site (``"enclosing"``: the nearest definition at or before it). Leaves
    ``cands`` unchanged when the anchor or lines are missing."""
    anchor = (emitted.get("metadata") or {}).get("anchor")
    line = _line_of(emitted)
    lined = [(ln, n) for n in cands if (ln := _line_of(n)) is not None]
    if anchor not in ("next", "enclosing") or line is None or len(lined) != len(cands):
        return cands
    if anchor == "next":
        after = [(ln, n) for ln, n in lined if ln >= line]
        return [min(after, key=lambda t: t[0])[1]] if after else cands
    before = [(ln, n) for ln, n in lined if ln <= line]
    return [max(before, key=lambda t: t[0])[1]] if before else cands


def reconcile_contract(base: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Merge an introspector's emitted nodes/edges onto the tree-sitter (``base``)
    graph on ``(path-suffix, symbol-name)`` identity. Generic over the emitter —
    cross-service contract handlers, scheduler/event handlers, and DI class nodes.

    A foldable emitted node (``kind`` in ``function``/``class``) that lands on
    exactly one base AST node is folded into it: the AST node stays canonical and
    is stamped ``metadata.service``; the emitted id maps to it and its edges are
    rewritten onto it. Synthetic construct nodes (``route``/``schedule``/``event``/
    ``topic``) have no AST twin and are kept as new; foldable nodes matching 0 or
    >1 base nodes are also kept — never name-guessed — mirroring ``reconcile_scip``.

    ``injects`` edges carry a raw *type-name* target (dependency injection): it is
    resolved to a base node by name when exactly one matches (INFERRED), else kept
    raw (demote-not-delete; SCIP can resolve it later).

    Returns ``{"nodes": <emitted nodes to ADD>, "edges": <rewritten edges>,
    "reconciliation": {...}}``. ``base`` node dicts are mutated in place (service
    stamped) but NOT returned — the caller already holds them.
    """
    from collections import defaultdict

    base_nodes = list(base.get("nodes", []))
    c_nodes = list(contract.get("nodes", []))
    c_edges = list(contract.get("edges", contract.get("links", [])))

    by_name: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_bare_name: dict[str, list[dict]] = defaultdict(list)
    for n in base_nodes:
        base_name = (n.get("source_file") or "").replace("\\", "/").rsplit("/", 1)[-1]
        label = n.get("label") or ""
        if label == base_name:
            continue  # file-container node, not a symbol definition
        norm = _norm_fn_name(label)
        by_name[(base_name, norm)].append(n)
        by_bare_name[norm].append(n)

    id_map: dict[str, str] = {}
    keep: list[dict] = []
    matched = ambiguous = unmatched = 0

    for cn in c_nodes:
        cid = cn["id"]
        if cn.get("kind") not in _FOLDABLE_KINDS:
            id_map[cid] = cid            # synthetic construct node — no AST twin
            keep.append(cn)
            continue
        cfile = cn.get("source_file") or ""
        key = (cfile.replace("\\", "/").rsplit("/", 1)[-1], _norm_fn_name(cn.get("label") or ""))
        cand = [
            bn for bn in by_name.get(key, [])
            if _path_suffix_match(cfile, bn.get("source_file") or "")
        ]
        if len(cand) > 1:
            cand = _by_anchor(cand, cn)
        if len(cand) == 1:
            canon = cand[0]
            id_map[cid] = canon["id"]
            meta = canon.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
            svc = (cn.get("metadata") or {}).get("service")
            if svc and not meta.get("service"):
                meta["service"] = svc
            canon["metadata"] = meta
            matched += 1
        else:
            id_map[cid] = cid            # 0 or >1 — keep as new, never guess
            keep.append(cn)
            if cand:
                ambiguous += 1
            else:
                unmatched += 1

    def _resolve_type_target(name: str) -> str:
        cands = by_bare_name.get(_norm_fn_name(name), [])
        return cands[0]["id"] if len(cands) == 1 else name

    seen: set[tuple] = set()
    edges_out: list[dict] = []
    type_resolved = 0
    for e in c_edges:
        e = dict(e)
        e["source"] = id_map.get(e.get("source"), e.get("source"))
        tgt = e.get("target")
        if tgt in id_map:
            e["target"] = id_map[tgt]
        elif e.get("relation") in _TYPE_TARGET_RELATIONS and isinstance(tgt, str):
            resolved = _resolve_type_target(tgt)
            if resolved != tgt:
                type_resolved += 1
            e["target"] = resolved
        k = (e.get("source"), e.get("target"), e.get("relation"))
        if k in seen:
            continue
        seen.add(k)
        edges_out.append(e)

    return {
        "nodes": keep,
        "edges": edges_out,
        "reconciliation": {
            "contract_fn_nodes": sum(1 for n in c_nodes if n.get("kind") in _FOLDABLE_KINDS),
            "matched": matched,
            "kept_new": unmatched,
            "ambiguous_kept_new": ambiguous,
            "type_targets_resolved": type_resolved,
        },
    }
