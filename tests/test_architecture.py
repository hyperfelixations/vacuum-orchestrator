"""Enforce dependency direction and exclusive persistence/device ownership."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1] / "custom_components/vacuum_orchestrator"


def test_core_imports_neither_home_assistant_nor_concrete_adapters():
    for layer in ("domain", "application", "ports"):
        for path in (ROOT / layer).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                forbidden = {
                    "homeassistant",
                    "adapters",
                    "infrastructure",
                    "api",
                    "runtime",
                }
                if layer in {"domain", "ports"}:
                    forbidden.add("application")
                assert not any(
                    set(module.split(".")) & forbidden for module in modules
                ), path


def test_repository_and_physical_calls_have_single_owners():
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr == "async_commit":
                assert relative in {
                    "application/orchestrator.py",
                    "infrastructure/critical_repository.py",
                }, relative
            if node.func.attr == "async_save_raw":
                assert relative == "infrastructure/critical_repository.py", relative
            if node.func.attr == "async_call":
                assert relative.startswith("adapters/"), relative
