"""Tests for Taug's access library as Svar's answer function packs it: it stands without Svar,
and the package Lambda gets imports the handler and changes with the library."""

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_lambda as lambda_

from svar.infra.answer_function import ANSWER_PACKAGE, answer_code, copy_answer_package
from taug.infra.shared import REPOSITORY_ROOT

ACCESS_FOLDER = REPOSITORY_ROOT / "taug" / "access"

#: Every module of Svar's function, by the plain name Lambda imports it under.
SVAR_MODULES = {path.stem for path in (REPOSITORY_ROOT / "svar" / "app").glob("*.py")}


def run_isolated(code: str, path: Path) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter with ``path`` first on its import path, and
    nothing of the tests' setup (-I: no PYTHONPATH, no current folder, no user
    site-packages). The interpreter's own site-packages stay, as Lambda's runtime brings
    boto3."""
    setup = f"import sys; sys.path.insert(0, {str(path)!r})\n"
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", setup + code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_library_imports_without_svar_on_the_path() -> None:
    """Only the repository root on the path: taug.access loads, and no module of Svar's
    function comes along."""
    result = run_isolated(
        "import json\n"
        "import taug.access.aws_errors, taug.access.keys, taug.access.workspaces\n"
        "print(json.dumps(sorted(sys.modules)))\n",
        REPOSITORY_ROOT,
    )
    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout))
    assert "taug.access.keys" in loaded
    assert "svar" not in loaded
    assert not loaded & SVAR_MODULES - {"__init__"}


def test_the_library_imports_nothing_from_svar() -> None:
    """Read, not run: an import inside a function would escape the test above."""
    for path in sorted(ACCESS_FOLDER.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                assert top != "svar" and top not in SVAR_MODULES, f"{path.name} imports {name}"


def test_the_package_holds_svar_app_at_the_top_and_taug_access_below(tmp_path: Path) -> None:
    copy_answer_package(tmp_path)
    assert (tmp_path / "handler.py").is_file()
    assert (tmp_path / "taug" / "__init__.py").is_file()
    assert (tmp_path / "taug" / "access" / "keys.py").is_file()
    assert not (tmp_path / "keys.py").exists()
    assert not list(tmp_path.rglob("__pycache__"))
    assert not list(tmp_path.rglob("README.md"))
    assert [source for source, _ in ANSWER_PACKAGE] == [
        "svar/app",
        "taug/__init__.py",
        "taug/access",
    ]


def test_the_packaged_handler_imports_as_lambda_imports_it(tmp_path: Path) -> None:
    """Only the package on the path, as in Lambda: ``handler`` loads, and with it the key
    check from the package, not from the repository."""
    copy_answer_package(tmp_path)
    result = run_isolated(
        "import handler, taug.access.keys as keys\n"
        "print(keys.__file__)\n"
        "print(handler.keys is keys)\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    module_file, same = result.stdout.splitlines()
    assert Path(module_file).resolve().is_relative_to(tmp_path.resolve())
    assert same == "True"


def code_key(root: Path, outdir: Path) -> str:
    """The S3 key of the answer function's package, synthesized from ``root``."""
    app = cdk.App(outdir=str(outdir))
    stack = cdk.Stack(app, "PackageStack")
    lambda_.Function(
        stack,
        "AnswerFunction",
        runtime=lambda_.Runtime.PYTHON_3_14,
        handler="handler.handler",
        code=answer_code(root),
    )
    template = app.synth().get_stack_by_name("PackageStack").template
    (function,) = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
    ]
    return function["Properties"]["Code"]["S3Key"]


def copy_of_the_package_sources(root: Path) -> Path:
    for source, _ in ANSWER_PACKAGE:
        if (REPOSITORY_ROOT / source).is_dir():
            shutil.copytree(REPOSITORY_ROOT / source, root / source)
        else:
            (root / source).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY_ROOT / source, root / source)
    return root


def test_a_change_in_the_library_alone_changes_the_package(tmp_path: Path) -> None:
    """Hashed by its source folder, svar/app, the package would keep its key when only
    taug/access/ changed, and Lambda would never get the change."""
    root = copy_of_the_package_sources(tmp_path / "repo")
    before = code_key(root, tmp_path / "out-1")
    assert code_key(root, tmp_path / "out-2") == before

    workspaces = root / "taug" / "access" / "workspaces.py"
    workspaces.write_bytes(workspaces.read_bytes() + b"\n# changed\n")
    assert code_key(root, tmp_path / "out-3") != before
