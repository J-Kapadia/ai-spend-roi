"""The truth file is out of reach of the pipeline: a static import-graph test, a scan of the source
for the truth path, and a process-level test that a `roi build` process never loads the truth modules.

Only `roi.verify_truth` reads the truth file, and only the `roi verify` command (and tests) reach it.
`roi.gen` writes the file with its own serializer and imports no truth module.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

TRUTH = "roi.truth"
VERIFY_TRUTH = "roi.verify_truth"
VERIFY_COMMAND = "roi.commands.verify"
FORBIDDEN = {TRUTH, VERIFY_TRUTH, VERIFY_COMMAND}  # what no entry point but `roi verify` may reach

# Source files that name the truth path: the work-dir layout, and the generators that write the file.
TRUTH_PATH_WRITERS = {"roi/workdir.py", "roi/gen/books.py", "roi/gen/toy.py", "roi/commands/gen.py"}
TRUTH_PATH_READERS = {"roi/verify_truth.py"}
# Files that import modules by a computed name: the command loader and the step and check registries.
DYNAMIC_IMPORTERS = {"roi/cli.py", "roi/steps/__init__.py", "roi/checks/__init__.py"}


# ---- the graph ---------------------------------------------------------------------------------

def module_files(src: Path, package: str) -> dict[str, Path]:
    """Dotted name -> file, for every module under src/<package>; a package is its __init__."""
    out = {}
    for path in sorted((src / package).rglob("*.py")):
        parts = list(path.relative_to(src).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        out[".".join(parts)] = path
    return out


def imported(name: str, path: Path, known: dict[str, Path]) -> set[str]:
    """The modules of this package that `name` imports, anywhere in its body (function bodies too),
    with the packages that importing a submodule executes."""
    is_package = path.name == "__init__.py"
    found: set[str] = set()

    def add(dotted: str) -> None:
        parts = dotted.split(".")
        found.update(p for p in (".".join(parts[: i + 1]) for i in range(len(parts))) if p in known)

    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                pkg = name.split(".") if is_package else name.split(".")[:-1]
                pkg = pkg[: len(pkg) - (node.level - 1)]
                base = ".".join(pkg + ([node.module] if node.module else []))
            add(base)
            for alias in node.names:  # `from pkg import module`
                if f"{base}.{alias.name}" in known:
                    add(f"{base}.{alias.name}")
    found.discard(name)
    return found


def graph(src: Path, package: str, registries: set[str] = frozenset()) -> dict[str, set[str]]:
    """Import edges between the modules of a package. A registry package (one that loads all its
    submodules by name, like the build steps) also reaches every one of its modules."""
    known = module_files(src, package)
    edges = {name: imported(name, path, known) for name, path in known.items()}
    for reg in registries:
        edges[reg] |= {m for m in known if m.startswith(reg + ".")}
    return edges


def closure(edges: dict[str, set[str]], start: str) -> set[str]:
    seen, todo = {start}, [start]
    while todo:
        for nxt in edges.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


@pytest.fixture(scope="module")
def edges():
    return graph(SRC, "roi", registries={"roi.steps", "roi.checks"})


# ---- the boundary ------------------------------------------------------------------------------

def test_no_entry_point_but_verify_reaches_the_truth_modules(edges):
    """Every module is an entry point (the CLI, each command, each step, the library) except the
    verify command itself and the truth modules; none may reach them through any chain of imports."""
    entry_points = sorted(set(edges) - FORBIDDEN)
    assert "roi.cli" in entry_points and "roi.commands.build" in entry_points and "roi.steps.roi" in entry_points
    for entry in entry_points:
        reached = closure(edges, entry) & FORBIDDEN
        assert not reached, f"{entry} reaches {sorted(reached)}"


def test_the_graph_walk_sees_what_it_should(edges):
    verify = closure(edges, VERIFY_COMMAND)
    assert VERIFY_TRUTH in verify, "the verify command must reach the verification of the truth"
    assert "roi.verify_books" in verify and "roi.estimator" in verify

    # a build loads every step through the registry, and the steps reach the estimator and the ROI code
    build = closure(edges, "roi.commands.build")
    assert {"roi.steps.estimate", "roi.steps.roi", "roi.estimator", "roi.roi", "roi.gen.panel"} <= build
    assert VERIFY_TRUTH not in build and VERIFY_COMMAND not in build

    # the CLI loads one command per invocation, by name, so it reaches none of them statically
    assert not {m for m in closure(edges, "roi.cli") if m.startswith("roi.commands.")}


def test_only_the_verify_command_imports_the_verification_of_the_truth(edges):
    importers = {name for name, deps in edges.items() if VERIFY_TRUTH in deps}
    assert importers == {VERIFY_COMMAND}


def test_nothing_but_the_verification_of_the_truth_imports_the_truth_module(edges):
    importers = {name for name, deps in edges.items() if TRUTH in deps}
    assert importers <= {VERIFY_TRUTH}


def test_computed_imports_are_confined_to_the_command_loader_and_the_registries():
    found = set()
    for path in (SRC / "roi").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "import_module" in text or "__import__" in text:
            found.add(path.relative_to(SRC).as_posix())
    assert found == DYNAMIC_IMPORTERS


def test_the_walk_catches_a_planted_import(tmp_path):
    """The walk is not vacuous: in a made-up package it follows plain, aliased, relative, function-local
    and registry imports to a planted module, and stays out when nothing leads there."""
    pkg = tmp_path / "pkg"
    (pkg / "steps").mkdir(parents=True)
    files = {
        "__init__.py": "",
        "entry.py": "from pkg import mid\n",
        "mid.py": "import pkg.leaf as leaf\n",
        "leaf.py": "def f():\n    from . import secret\n",
        "secret.py": "",
        "clean.py": "import pkg.other\n",
        "other.py": "",
        "loader.py": "from pkg import steps\n",
        "steps/__init__.py": "",
        "steps/one.py": "from pkg import secret\n",
    }
    for rel, text in files.items():
        (pkg / rel).write_text(text)
    plain = graph(tmp_path, "pkg")
    assert "pkg.secret" in closure(plain, "pkg.entry")
    assert "pkg.secret" not in closure(plain, "pkg.clean")
    assert "pkg.secret" not in closure(plain, "pkg.loader")  # a registry is not followed without being declared
    registry = graph(tmp_path, "pkg", registries={"pkg.steps"})
    assert "pkg.secret" in closure(registry, "pkg.loader")


# ---- the truth path in the source --------------------------------------------------------------

def truth_path_references(path: Path) -> list[str]:
    """Places in code (not prose) that name the truth file or directory or import a truth module."""
    refs = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute) and node.attr == "truth":
            refs.append(f"attribute truth, line {node.lineno}")
        elif isinstance(node, ast.FunctionDef) and node.name == "truth":
            refs.append(f"definition of truth, line {node.lineno}")
        elif isinstance(node, ast.Constant) and node.value == "truth.json":
            refs.append(f"the file name, line {node.lineno}")
        elif isinstance(node, ast.Import) and any(a.name.split(".")[:2] == ["roi", "truth"] for a in node.names):
            refs.append(f"import of roi.truth, line {node.lineno}")
        elif isinstance(node, ast.ImportFrom) and (
            (node.module or "").split(".")[:2] == ["roi", "truth"]
            or (node.module == "roi" and any(a.name == "truth" for a in node.names))
        ):
            refs.append(f"import of roi.truth, line {node.lineno}")
    return refs


def test_only_the_generators_and_the_verification_name_the_truth_path():
    named = {}
    for path in sorted((SRC / "roi").rglob("*.py")):
        refs = truth_path_references(path)
        if refs:
            named[path.relative_to(SRC).as_posix()] = refs
    assert set(named) == TRUTH_PATH_WRITERS | TRUTH_PATH_READERS, named


def test_the_generators_import_no_truth_module():
    for rel in sorted(TRUTH_PATH_WRITERS):
        refs = truth_path_references(SRC / rel)
        assert not [r for r in refs if r.startswith("import")], rel


# ---- a build process ---------------------------------------------------------------------------

DUMP = """
import json, sys
from roi.cli import main
try:
    main(sys.argv[3:], standalone_mode=False)
except BaseException as e:
    print(f"{type(e).__name__}: {e}", file=sys.stderr)
finally:
    loaded = sorted(m for m in sys.modules if m == "roi" or m.startswith("roi."))
    json.dump(loaded, open(sys.argv[2], "w"))
"""


def modules_loaded_by(work: Path, tag: str, *args: str) -> list[str]:
    out = work / f"{tag}.modules.json"
    subprocess.run([sys.executable, "-c", DUMP, str(work), str(out), "--work-dir", str(work), *args],
                   capture_output=True, text=True, check=False)
    return json.loads(out.read_text())


@pytest.fixture(scope="module")
def toy_build(tmp_path_factory):
    """A toy build in its own process: its work dir and the roi modules it loaded."""
    work = tmp_path_factory.mktemp("toy")
    loaded = modules_loaded_by(work, "build", "build", "--toy")
    assert (work / "warehouse" / "roi.duckdb").exists(), "the build did not finish"
    return work, loaded


def test_a_build_process_does_not_load_the_truth_modules(toy_build):
    _, loaded = toy_build
    assert {"roi.commands.build", "roi.steps.roi", "roi.estimator", "roi.gen.toy"} <= set(loaded)
    assert not set(loaded) & FORBIDDEN, sorted(set(loaded) & FORBIDDEN)


def test_a_verify_process_does_load_the_verification_of_the_truth(toy_build):
    work, _ = toy_build
    loaded = modules_loaded_by(work, "verify", "verify", "--through", "2025-06")
    assert {VERIFY_COMMAND, VERIFY_TRUTH} <= set(loaded)
