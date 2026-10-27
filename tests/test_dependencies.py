"""Component boundaries: who may import whom, checked from the source
(every import statement, including lazy ones inside functions).

    sources  <-  engine  <-  billing / notify  <-  app

notify and billing are stricter than the arrows allow: they import
nothing from the repo, so each can be lifted out on its own. deploy/
(the backup tool) is standalone too. Test code is exempt: app/tests may
reach into engine/tests helpers, etc.
"""

import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COMPONENTS = ("sources", "engine", "notify", "billing", "app", "deploy")
ALLOWED = {
    "sources": set(),
    "engine": {"sources"},
    "notify": set(),
    "billing": set(),
    "app": {"sources", "engine", "notify", "billing"},
    "deploy": set(),
}
# The arrows themselves; ALLOWED must never exceed them.
DIRECTION = {
    "sources": set(),
    "engine": {"sources"},
    "notify": {"sources", "engine"},
    "billing": {"sources", "engine"},
    "app": {"sources", "engine", "notify", "billing"},
    "deploy": set(),
}
REPO_TOP_LEVEL = set(COMPONENTS) | {"platformd", "adapters", "sentry",
                                    "watchdog", "report", "preflight",
                                    "startup_check", "tests"}


def _source_files(component):
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, component)):
        dirnames[:] = [d for d in dirnames
                       if d not in ("tests", "__pycache__")]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _imported_top_levels(path):
    with open(path) as fh:
        tree = ast.parse(fh.read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: stays inside its own package
                continue
            yield node.module.split(".")[0]


def import_graph():
    graph = {}
    for comp in COMPONENTS:
        deps = {}
        for path in _source_files(comp):
            for top in _imported_top_levels(path):
                if top in REPO_TOP_LEVEL and top != comp:
                    deps.setdefault(top, set()).add(os.path.relpath(path, ROOT))
        graph[comp] = deps
    return graph


class DependencyDirectionTest(unittest.TestCase):
    def test_allowed_is_within_the_documented_direction(self):
        for comp, allowed in ALLOWED.items():
            self.assertLessEqual(allowed, DIRECTION[comp], comp)

    def test_every_component_imports_only_what_it_may(self):
        for comp, deps in import_graph().items():
            bad = {dep: sorted(files) for dep, files in deps.items()
                   if dep not in ALLOWED[comp]}
            self.assertEqual(bad, {}, "%s imports a component it may not" % comp)

    def test_no_cycles(self):
        graph = {c: set(d) for c, d in import_graph().items()}
        state = {}

        def visit(node, stack):
            state[node] = "open"
            for nxt in graph.get(node, ()):
                if state.get(nxt) == "open":
                    self.fail("import cycle: %s" % " -> ".join(stack + [nxt]))
                if nxt not in state:
                    visit(nxt, stack + [nxt])
            state[node] = "done"

        for comp in graph:
            if comp not in state:
                visit(comp, [comp])

    def test_nothing_imports_the_retired_packages(self):
        for comp, deps in import_graph().items():
            for retired in ("platformd", "adapters", "sentry"):
                self.assertNotIn(retired, deps, comp)

    @unittest.skipUnless(hasattr(sys, "stdlib_module_names"), "Python 3.10+")
    def test_stdlib_only(self):
        allowed = set(sys.stdlib_module_names) | REPO_TOP_LEVEL
        for comp in COMPONENTS:
            for path in _source_files(comp):
                for top in _imported_top_levels(path):
                    self.assertIn(top, allowed,
                                  "%s imports third-party %r"
                                  % (os.path.relpath(path, ROOT), top))

    def test_every_component_has_a_readme_and_tests(self):
        for comp in COMPONENTS:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, comp, "README.md")), comp)
            tests = os.path.join(ROOT, comp, "tests")
            self.assertTrue(any(n.startswith("test_") for n in os.listdir(tests)), comp)


if __name__ == "__main__":
    unittest.main()
