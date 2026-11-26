"""MIT Axiomatic Design architecture matrix gates.

Enforces two structural invariants across ``src/virgo``:
1. Macro lower-triangular layering (unidirectional DAG across layers, including
   function-local lazy imports, with zero runtime cycles).
2. Block-diagonal peer independence across sibling plugins, extensions, tools,
   execution leaf components, and AI provider families.

Known baseline violations are tracked as exact sets so each decoupling phase
must shrink them monotonically until they reach empty.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "virgo"

# Strict layer hierarchy (lower rank must never import higher rank).
PACKAGE_LAYERS: dict[str, int] = {
    # Layer 0: pure leaf foundations
    "virgo": 0,
    "virgo._gitignore": 0,
    "virgo._win32": 0,
    "virgo.i18n": 0,
    "virgo.diagnostics": 0,
    "virgo.execution_policy": 0,
    # Layer 1: execution contracts/processes & core LLM/agent abstractions
    "virgo.execution_types": 1,
    "virgo.process": 1,
    "virgo.ai": 1,
    "virgo.agent": 1,
    # Layer 2a: diagonal execution leaf components
    "virgo.file_access": 2,
    "virgo.output_store": 2,
    "virgo.sandbox": 2,
    # Layer 2b: execution composition facade & built-in tools
    "virgo.execution": 3,
    "virgo.tools": 3,
    # Layer 3: session harness
    "virgo.harness": 4,
    # Layer 4: transactional plugins & extensions
    "virgo.plugins": 5,
    "virgo.extensions": 5,
    # Layer 5: terminal presentation & entry points
    "virgo.cli": 6,
    "virgo.__main__": 6,
}

# --- Exact baseline violation sets (shrunk to empty across Phases 1-5) ------

KNOWN_PACKAGE_RUNTIME_SCCS: set[tuple[str, ...]] = {
    ("virgo.extensions", "virgo.harness"),
}

KNOWN_MODULE_RUNTIME_SCCS: set[tuple[str, ...]] = {
    (
        "virgo.plugins.web",
        "virgo.plugins.web.access",
        "virgo.plugins.web.fetch",
        "virgo.plugins.web.search",
    ),
}

KNOWN_LAYER_VIOLATIONS: set[tuple[str, str]] = {
    ("virgo.harness", "virgo.extensions"),
}

KNOWN_TC_REVERSE_EDGES: set[tuple[str, str]] = set()

KNOWN_PRIVATE_INTRUSIONS: set[str] = {
    "plugins/web/__init__.py:runtime._web_backend_config",
    "plugins/web/__init__.py:runtime._web_backends",
    "plugins/web/__init__.py:runtime._web_models",
    "plugins/web/__init__.py:runtime._web_paths",
    "harness/runtime.py:services.web",
    "harness/session.py:services.subagents",
    "harness/extensions.py:sandbox._result",
    "extensions/todo.py:compaction_internals",
    "cli/tui/app.py:bus.emit",
    "cli/tui/app.py:services.execution",
    "cli/repl.py:services.execution",
}


def _collect_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT.parent).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _resolve_relative(cur_mod: str, cur_path: Path, module_name: str | None, level: int) -> str:
    parts = cur_mod.split(".")
    if cur_path.name != "__init__.py":
        parts = parts[:-1]
    if level > 1:
        parts = parts[: -(level - 1)]
    if module_name:
        parts.extend(module_name.split("."))
    return ".".join(parts)


def _top_package(mod: str) -> str:
    parts = mod.split(".")
    return mod if len(parts) <= 2 else ".".join(parts[:2])


def _extract_edges(modules: dict[str, Path]) -> dict[tuple[str, str], set[str]]:
    edges: dict[tuple[str, str], set[str]] = defaultdict(set)

    def normalize(dst: str) -> str | None:
        while dst and dst not in modules:
            if "." not in dst:
                return None
            dst = dst.rsplit(".", 1)[0]
        return dst if dst in modules else None

    def visit(node: ast.AST, cur_mod: str, cur_path: Path, in_tc: bool = False, in_fn: bool = False) -> None:
        if isinstance(node, ast.If):
            is_tc = "TYPE_CHECKING" in ast.unparse(node.test)
            for child in node.body:
                visit(child, cur_mod, cur_path, in_tc or is_tc, in_fn)
            for child in node.orelse:
                visit(child, cur_mod, cur_path, in_tc, in_fn)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.iter_child_nodes(node):
                visit(child, cur_mod, cur_path, in_tc, True)
            return
        if isinstance(node, ast.Import):
            kind = "TC" if in_tc else ("LOCAL" if in_fn else "TOP")
            for alias in node.names:
                if alias.name.startswith("virgo"):
                    dst = normalize(alias.name)
                    if dst and dst != cur_mod:
                        edges[(cur_mod, dst)].add(kind)
            return
        if isinstance(node, ast.ImportFrom):
            base = (
                _resolve_relative(cur_mod, cur_path, node.module, node.level)
                if node.level > 0
                else (node.module or "")
            )
            if base.startswith("virgo"):
                kind = "TC" if in_tc else ("LOCAL" if in_fn else "TOP")
                for alias in node.names:
                    cand = f"{base}.{alias.name}"
                    dst = cand if cand in modules else normalize(base)
                    if dst and dst != cur_mod:
                        edges[(cur_mod, dst)].add(kind)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, cur_mod, cur_path, in_tc, in_fn)

    for mod, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visit(tree, mod, path)
    return edges


def _tarjan_sccs(nodes: set[str], adj: dict[str, set[str]]) -> set[tuple[str, ...]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    cycles: set[tuple[str, ...]] = set()

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(adj.get(v, ())):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                cycles.add(tuple(sorted(comp)))

    for node in sorted(nodes):
        if node not in indices:
            strongconnect(node)
    return cycles


def test_every_package_has_assigned_layer() -> None:
    modules = _collect_modules()
    pkgs = {_top_package(m) for m in modules}
    missing = pkgs - set(PACKAGE_LAYERS)
    assert not missing, f"Unlayered packages found: {sorted(missing)}"


def test_package_runtime_graph_is_acyclic() -> None:
    modules = _collect_modules()
    edges = _extract_edges(modules)
    adj: dict[str, set[str]] = defaultdict(set)
    pkgs = {_top_package(m) for m in modules}
    for (src, dst), kinds in edges.items():
        if not (kinds & {"TOP", "LOCAL"}):
            continue
        ps, pd = _top_package(src), _top_package(dst)
        if ps != pd:
            adj[ps].add(pd)
    actual = _tarjan_sccs(pkgs, adj)
    assert actual == KNOWN_PACKAGE_RUNTIME_SCCS, (
        f"Package runtime SCCs diverged.\n"
        f"  Unexpected new cycles: {sorted(actual - KNOWN_PACKAGE_RUNTIME_SCCS)}\n"
        f"  Resolved (remove from KNOWN_PACKAGE_RUNTIME_SCCS): {sorted(KNOWN_PACKAGE_RUNTIME_SCCS - actual)}"
    )


def test_module_runtime_graph_is_acyclic() -> None:
    modules = _collect_modules()
    edges = _extract_edges(modules)
    adj: dict[str, set[str]] = defaultdict(set)
    for (src, dst), kinds in edges.items():
        if kinds & {"TOP", "LOCAL"}:
            adj[src].add(dst)
    actual = _tarjan_sccs(set(modules), adj)
    assert actual == KNOWN_MODULE_RUNTIME_SCCS, (
        f"Module runtime SCCs diverged.\n"
        f"  Unexpected new cycles: {sorted(actual - KNOWN_MODULE_RUNTIME_SCCS)}\n"
        f"  Resolved (remove from KNOWN_MODULE_RUNTIME_SCCS): {sorted(KNOWN_MODULE_RUNTIME_SCCS - actual)}"
    )


def test_package_layer_hierarchy_is_lower_triangular() -> None:
    modules = _collect_modules()
    edges = _extract_edges(modules)
    violations: set[tuple[str, str]] = set()
    for (src, dst), kinds in edges.items():
        if not (kinds & {"TOP", "LOCAL"}):
            continue
        ps, pd = _top_package(src), _top_package(dst)
        if ps != pd and PACKAGE_LAYERS[pd] > PACKAGE_LAYERS[ps]:
            violations.add((ps, pd))
    assert violations == KNOWN_LAYER_VIOLATIONS, (
        f"Layer hierarchy violations diverged.\n"
        f"  Unexpected upward imports: {sorted(violations - KNOWN_LAYER_VIOLATIONS)}\n"
        f"  Resolved (remove from KNOWN_LAYER_VIOLATIONS): {sorted(KNOWN_LAYER_VIOLATIONS - violations)}"
    )


def test_leaf_packages_have_no_type_checking_upward_imports() -> None:
    modules = _collect_modules()
    edges = _extract_edges(modules)
    actual: set[tuple[str, str]] = set()
    for (src, dst), kinds in edges.items():
        if "TC" not in kinds:
            continue
        ps, pd = _top_package(src), _top_package(dst)
        if PACKAGE_LAYERS[ps] == 0 and PACKAGE_LAYERS[pd] > 0:
            actual.add((src, dst))
    assert actual == KNOWN_TC_REVERSE_EDGES, (
        f"Leaf TYPE_CHECKING upward imports diverged.\n"
        f"  Unexpected: {sorted(actual - KNOWN_TC_REVERSE_EDGES)}\n"
        f"  Resolved: {sorted(KNOWN_TC_REVERSE_EDGES - actual)}"
    )


def test_peer_diagonal_blocks_are_mutually_independent() -> None:
    modules = _collect_modules()
    edges = _extract_edges(modules)
    runtime_edges = {(s, d) for (s, d), ks in edges.items() if ks & {"TOP", "LOCAL"}}

    # 1. Peer plugins never import each other.
    def plugin_id(mod: str) -> str | None:
        parts = mod.split(".")
        if len(parts) >= 3 and parts[:2] == ["virgo", "plugins"]:
            return parts[2]
        return None

    for src, dst in runtime_edges:
        ps, pd = plugin_id(src), plugin_id(dst)
        assert not (ps and pd and ps != pd), f"Cross-plugin dependency: {src} -> {dst}"

    # 2. Peer extensions never import each other or plugins.
    def ext_id(mod: str) -> str | None:
        parts = mod.split(".")
        if len(parts) >= 3 and parts[:2] == ["virgo", "extensions"]:
            return parts[2]
        return None

    for src, dst in runtime_edges:
        es, ed = ext_id(src), ext_id(dst)
        assert not (es and ed and es != ed), f"Cross-extension dependency: {src} -> {dst}"
        assert not (es and plugin_id(dst)), f"Extension depends on plugin: {src} -> {dst}"
        assert not (plugin_id(src) and ed), f"Plugin depends on extension: {src} -> {dst}"

    # 3. Execution leaf components (file_access, output_store, sandbox) are mutually diagonal.
    exec_leaves = {"virgo.file_access", "virgo.output_store", "virgo.sandbox"}
    for src, dst in runtime_edges:
        ps, pd = _top_package(src), _top_package(dst)
        assert not (ps in exec_leaves and pd in exec_leaves and ps != pd), (
            f"Execution leaf cross-dependency: {src} -> {dst}"
        )

    # 4. Built-in tool families (fs, shell/bash, web, web_fetch) are mutually diagonal
    #    (tools.output is the shared output-budget helper).
    def tool_family(mod: str) -> str | None:
        if mod in {"virgo.tools.fs"}:
            return "fs"
        if mod in {"virgo.tools.shell", "virgo.tools.bash", "virgo.tools.shell_env"}:
            return "shell"
        if mod in {"virgo.tools.web"}:
            return "web_search"
        if mod in {"virgo.tools.web_fetch"}:
            return "web_fetch"
        return None

    for src, dst in runtime_edges:
        fs, fd = tool_family(src), tool_family(dst)
        assert not (fs and fd and fs != fd), f"Cross-tool-family dependency: {src} -> {dst}"

    # 5. AI provider families (anthropic vs openai-family vs faux) are mutually diagonal.
    def provider_family(mod: str) -> str | None:
        if mod == "virgo.ai.providers.anthropic":
            return "anthropic"
        if mod in {"virgo.ai.providers.openai", "virgo.ai.providers.responses", "virgo.ai.providers.codex"}:
            return "openai"
        if mod == "virgo.ai.providers.faux":
            return "faux"
        return None

    for src, dst in runtime_edges:
        fs, fd = provider_family(src), provider_family(dst)
        assert not (fs and fd and fs != fd), f"Cross-provider-family dependency: {src} -> {dst}"


def test_no_implicit_cross_boundary_intrusions() -> None:
    found: set[str] = set()

    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))

        # 1. Plugins/extensions must not write/read private runtime._* or session._* attrs
        #    or reach directly into virgo.ai.providers.*.
        if rel.startswith(("plugins/", "extensions/")):
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id in {"runtime", "rt", "session"} and node.attr.startswith("_"):
                        found.add(f"{rel}:{node.value.id}.{node.attr}")
                if isinstance(node, ast.ImportFrom) and node.module and "ai.providers" in node.module:
                    found.add(f"{rel}:{node.module.split('ai.', 1)[-1]}")
                if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module and "providers.web" in node.module:
                    found.add(f"{rel}:providers.web")

        # 2. Extension todo must not inspect compaction's internal message[0].kind == "summary".
        if rel == "extensions/todo.py":
            if 'getattr(first, "kind", None) != "summary"' in src:
                found.add(f"{rel}:compaction_internals")

        # 3. Layer 1 agent/loop.py must not hardcode Layer 2/3 status strings or harness.* i18n keys.
        if rel == "agent/loop.py":
            if any(tok in src for tok in ('"execution_unavailable"', '"resource_denied"', '"high_risk_denied"')):
                found.add(f"{rel}:hardcoded_layer_status")
            if '"harness.agent.' in src:
                found.add(f"{rel}:harness_i18n_key")

        # 4. Harness core must not hardcode optional plugin services or poke private internals of stores/sandbox/agent.
        if rel.startswith("harness/"):
            if 'services.get("web")' in src or 'services["web"]' in src:
                found.add(f"{rel}:services.web")
            if 'services.get("subagents")' in src or 'services["subagents"]' in src:
                found.add(f"{rel}:services.subagents")
            if "bound._lock" in src or "store._lock" in src:
                if rel != "harness/session.py":  # session trash locks multiple stores atomically
                    found.add(f"{rel}:store._lock")
            if "failure_signals" in src and rel != "harness/store.py":
                found.add(f"{rel}:store.failure_signals")
            if "sandbox, \"_result\"" in src or "sandbox._result" in src:
                found.add(f"{rel}:sandbox._result")
            if "agent._emit" in src:
                found.add(f"{rel}:agent._emit")

        # 5. CLI must not directly emit on session.bus or reach into session.services["execution"].
        if rel.startswith("cli/"):
            if '.bus.emit(' in src:
                found.add(f"{rel}:bus.emit")
            if 'services["execution"]' in src or 'services.get("execution")' in src:
                found.add(f"{rel}:services.execution")

    assert found == KNOWN_PRIVATE_INTRUSIONS, (
        f"Implicit cross-boundary intrusions diverged.\n"
        f"  Unexpected new intrusions: {sorted(found - KNOWN_PRIVATE_INTRUSIONS)}\n"
        f"  Resolved (remove from KNOWN_PRIVATE_INTRUSIONS): {sorted(KNOWN_PRIVATE_INTRUSIONS - found)}"
    )
