#!/usr/bin/env python3
"""Static, GenVM-free structural checks for a GenLayer Intelligent Contract
file, aimed squarely at the failure classes actually hit while building
this contract against a real Studio deployment:

  - the `Depends` magic comment must be line 1;
  - exactly one class must extend `*.Contract`;
  - `__init__` must exist, be undecorated, and be a direct method of that
    class;
  - every function decorated with a `gl.public.*` decorator, ANYWHERE in
    the file, must have that contract class as its actual lexical parent
    -- this is the check that would have caught the real bug hit during
    development, where two module-level helper functions sat physically
    between two class methods with matching 4-space indentation, and
    every method defined after them silently became a nested function
    inside the last helper instead of a method of the contract class.
    That produced a contract with an empty Studio method panel and NO
    Python exception anywhere -- exactly the kind of bug a runtime
    traceback can never surface, because nothing ever raised.
  - no public method signature uses a bare forbidden type (int, list,
    dict, Optional, Union, tuple, Any) as an argument or return
    annotation.

This does NOT replace `genvm-lint` or an actual Studio deploy -- it has no
access to the real GenVM type system and cannot check things like
whether nested @allow_storage graphs are supported on a given build. It
only catches the structural classes of bug above, cheaply, before ever
touching Studio.

Usage:
    python scripts/preflight.py contracts/blamecourt.py
"""

import ast
import sys


def _unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _decorator_is_gl_public(dec) -> bool:
    text = _unparse(dec)
    return text.startswith("gl.public.") or text.startswith("public.")


class _ParentVisitor(ast.NodeVisitor):
    """Walks the whole module tracking each node's lexical parent, so we
    can tell whether a `@gl.public.*`-decorated function's ACTUAL parent
    scope is the contract class or something else (e.g. another function
    it got silently nested inside)."""

    def __init__(self):
        self.stack = []
        self.public_funcs = []  # list of (FunctionDef, parent_node)

    def generic_visit(self, node):
        self.stack.append(node)
        super().generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        for dec in node.decorator_list:
            if _decorator_is_gl_public(dec):
                parent = self.stack[-1] if self.stack else None
                self.public_funcs.append((node, parent))
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


FORBIDDEN_TYPES = ("int", "list", "dict", "Optional", "Union", "tuple", "Any")


def _uses_forbidden_type(annotation_node) -> str:
    """Returns the forbidden type name if the annotation uses one (bare or
    as a generic base like `list[str]`), else ''."""
    text = _unparse(annotation_node)
    for forbidden in FORBIDDEN_TYPES:
        if text == forbidden or text.startswith(forbidden + "["):
            return forbidden
    return ""


def check(path: str):
    errors = []
    warnings = []

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if src.startswith("\ufeff"):
        errors.append("File has a BOM at byte 0; the Depends comment must be the literal first bytes.")

    first_line = src.split("\n", 1)[0]
    if not (first_line.strip().startswith("#") and '"Depends"' in first_line):
        errors.append(f"Line 1 must be the Depends magic comment; got: {first_line!r}")

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        errors.append(f"File does not parse as Python: {e}")
        return errors, warnings, []

    contract_classes = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if _unparse(base).endswith("Contract"):
                    contract_classes.append(node)
                    break

    if len(contract_classes) != 1:
        errors.append(
            "Expected exactly one top-level class extending *.Contract, found "
            + str(len(contract_classes))
            + "."
        )
        return errors, warnings, []

    cls = contract_classes[0]

    init_nodes = [
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    ]
    if not init_nodes:
        errors.append("__init__ not found as a direct method of the contract class.")
    else:
        init = init_nodes[0]
        if init.decorator_list:
            errors.append("__init__ must be undecorated.")
        arg_names = [a.arg for a in init.args.args]
        if arg_names != ["self"]:
            warnings.append(
                "__init__ takes arguments beyond `self` ("
                + str(arg_names)
                + ") -- confirm every one is a schema-safe primitive (str/bool/u256/u32)."
            )

    visitor = _ParentVisitor()
    visitor.visit(tree)

    public_methods = []
    for node, parent in visitor.public_funcs:
        if parent is not cls:
            parent_desc = getattr(parent, "name", type(parent).__name__)
            errors.append(
                f"'{node.name}' is decorated as a public GenLayer method but its "
                f"actual lexical parent is '{parent_desc}', not the contract class "
                f"'{cls.name}' -- it will be invisible in Studio's method panel "
                f"even though the file parses and imports cleanly. This is caused "
                f"by a module-level statement sitting between two class methods "
                f"at matching indentation; move every module-level helper "
                f"function to before the class definition."
            )
        else:
            public_methods.append(node.name)

        for a in node.args.args:
            if a.annotation is not None:
                forbidden = _uses_forbidden_type(a.annotation)
                if forbidden:
                    errors.append(
                        f"{node.name}({a.arg}: {_unparse(a.annotation)}) uses "
                        f"non-ABI-safe type '{forbidden}' in a public signature."
                    )
        if node.returns is not None:
            forbidden = _uses_forbidden_type(node.returns)
            if forbidden:
                errors.append(
                    f"{node.name}(...) -> {_unparse(node.returns)} uses "
                    f"non-ABI-safe return type '{forbidden}'."
                )

    if not public_methods:
        errors.append("No @gl.public.* methods resolved as direct children of the contract class.")

    return errors, warnings, sorted(public_methods)


def main():
    if len(sys.argv) != 2:
        print("usage: preflight.py <contract.py>")
        sys.exit(2)

    errors, warnings, public_methods = check(sys.argv[1])

    for w in warnings:
        print("WARNING:", w)
    for e in errors:
        print("ERROR:", e)

    if not errors:
        print(f"OK -- {len(public_methods)} public method(s) resolved as direct class members: {public_methods}")

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
