from __future__ import annotations

import re
import shlex
import subprocess  # nosec B404
import sys
import typing as t
from collections.abc import Iterable, Sequence

DEFAULT_SOURCES: tuple[str, ...] = ("src", "bin", "examples", "tests")
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?$")
SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/*-]*$")
SOURCES_HELP = f"源码路径，支持多个路径或文件；未传入时默认使用 {' '.join(DEFAULT_SOURCES)}。"
PKG_HELP = "要升级的单个依赖名，例如：redis。"
_FIND_BASE = ("find", ".", "-not", "-path", "./.venv/*", "-not", "-path", "./.git/*")
_SOURCES_ARG_CONFIG = {
    "sources": {
        "help": SOURCES_HELP,
        "default": "",
        "positional": True,
        "multiple": True,
    }
}

_PARALLEL_SOURCES_ARG_CONFIG = {
    "sources": {
        "help": SOURCES_HELP,
        "default": " ".join(DEFAULT_SOURCES),
        "positional": True,
        "multiple": True,
    }
}

_PKG_ARG_CONFIG = {"pkg": {"help": PKG_HELP, "default": ""}}
_SOURCE_TASKS: tuple[tuple[str, str, str], ...] = (
    ("lint-mypy", "类型注解检查", "lint_mypy"),
    ("lint-black", "格式化检查", "lint_black"),
    ("lint-isort", "import排序检查", "lint_isort"),
    ("lint-flake8", "flake8检查", "lint_flake8"),
    ("lint-pylint", "pylint检查", "lint_pylint"),
    ("lint-ruff", "ruff检查", "lint_ruff"),
    ("lint-bandit", "代码风险检查", "lint_bandit"),
    ("lint-codespell", "拼写检查", "lint_codespell"),
    ("lint-vulture", "未使用代码检查", "lint_vulture"),
    ("lint", "串行检查代码", "lint"),
    ("format-autoflake", "autoflake移除未使用的import和变量", "format_autoflake"),
    ("format-isort", "isort import排序", "format_isort"),
    ("format-black", "black代码格式化", "format_black"),
    ("format-ruff", "ruff代码格式化", "format_ruff"),
    ("format", "代码格式化", "format"),
)
_SIMPLE_TASKS: tuple[tuple[str, str, str], ...] = (
    ("test", "单元测试", "test"),
    ("clean-pyc", "清理 Python 运行文件", "clean_pyc"),
    ("clean-test", "清理测试文件", "clean_test"),
    ("clean", "清理所有不该进代码库的文件", "clean"),
    ("upgrade-deps", "依赖文件升级", "upgrade_deps"),
    ("pyupgrade", "升级Python syntax。需要安装fd", "pyupgrade"),
)


def _exit_with_error(message: str) -> t.NoReturn:
    print(f"\033[31m❌️ {message}\033[0m", file=sys.stderr)
    raise SystemExit(1)


def _resolve_sources(sources: str | Sequence[str] | None = None) -> tuple[str, ...]:
    if not sources:
        return DEFAULT_SOURCES
    if isinstance(sources, str):
        parsed_sources = tuple(shlex.split(sources))
    elif isinstance(sources, (list, tuple)):
        parsed_sources = tuple(sources)
    else:
        parsed_sources = (str(sources),)

    normalized_sources = parsed_sources or DEFAULT_SOURCES
    for source in normalized_sources:
        if source.startswith("-") or not SOURCE_PATTERN.fullmatch(source):
            _exit_with_error(f"源码路径不合法: {source}")
    return normalized_sources


def _quote_args(args: Iterable[str]) -> str:
    return shlex.join(args)


def _run(args: Sequence[str], check: bool = True, **kwags: t.Any) -> subprocess.CompletedProcess:
    print(f"\033[32m{_quote_args(args)}\033[0m", flush=True)
    try:
        return subprocess.run(args, check=check, **kwags)  # nosec B603
    except subprocess.CalledProcessError as e:
        _exit_with_error(f"命令执行失败: {e.returncode}")


def _source_args(sources: str | Sequence[str] | None = None) -> tuple[str, ...]:
    return _resolve_sources(sources)


def _validate_package_name(pkg: str | None) -> str:
    normalized_pkg = (pkg or "").strip()
    if not normalized_pkg:
        _exit_with_error("必须提供要升级的依赖名")
    if not PACKAGE_NAME_PATTERN.fullmatch(normalized_pkg):
        _exit_with_error(f"依赖名不合法: {pkg}")
    return normalized_pkg


def _find_delete(names: Iterable[str], *, recursive: bool) -> None:
    expr: list[str] = []
    for index, name in enumerate(names):
        if index:
            expr.append("-o")
        expr.extend(("-name", name))
    rm_flag = "-rf" if recursive else "-f"
    _run((*_FIND_BASE, "(", *expr, ")", "-exec", "rm", rm_flag, "{}", "+"))


def generate_poe_config() -> dict[str, t.Any]:
    tasks: dict[str, dict[str, t.Any]] = {}

    for task_name, help_text, function_name in _SOURCE_TASKS:
        tasks[task_name] = {
            "help": help_text,
            "script": f"bin.poe_tasks:{function_name}(sources)",
            "args": _SOURCES_ARG_CONFIG,
        }

    for task_name, help_text, function_name in _SIMPLE_TASKS:
        tasks[task_name] = {
            "help": help_text,
            "script": f"bin.poe_tasks:{function_name}",
        }

    pre_commit_parallel = [
        {"script": "bin.poe_tasks:lint_codespell(sources)"},
        {"script": "bin.poe_tasks:lint_mypy(sources)"},
        {"script": "bin.poe_tasks:lint_ruff(sources)"},
        {"script": "bin.poe_tasks:lint_bandit(sources)"},
    ]

    tasks["lint-parallel"] = {
        "help": "并行检查代码",
        "args": _PARALLEL_SOURCES_ARG_CONFIG,
        "parallel": pre_commit_parallel
        + [
            {"script": "bin.poe_tasks:lint_black(sources)"},
            {"script": "bin.poe_tasks:lint_isort(sources)"},
            {"script": "bin.poe_tasks:lint_flake8(sources)"},
            {"script": "bin.poe_tasks:lint_pylint(sources)"},
        ],
        "ignore_fail": "return_non_zero",
    }
    tasks["upgrade-pkg"] = {
        "help": "升级单个依赖，例如：poe upgrade-pkg --pkg redis",
        "script": "bin.poe_tasks:upgrade_pkg",
        "args": _PKG_ARG_CONFIG,
    }
    return {"tasks": tasks}


def lint_mypy(sources: str = "") -> None:
    _run(("mypy", *_source_args(sources)))


def lint_black(sources: str = "") -> None:
    _run(("black", *_source_args(sources), "--check"))


def lint_isort(sources: str = "") -> None:
    _run(("isort", "--check-only", *_source_args(sources)))


def lint_flake8(sources: str = "") -> None:
    _run(("flake8", *_source_args(sources), "--jobs=0"))


def lint_pylint(sources: str = "") -> None:
    _run(("pylint", *_source_args(sources), "--jobs=0"))


def lint_ruff(sources: str = "") -> None:
    _run(("ruff", "check", *_source_args(sources), "--fix"))


def lint_bandit(sources: str = "") -> None:
    _run(("bandit", "-c", "pyproject.toml", "-r", *_source_args(sources)))


def lint_codespell(sources: str = "") -> None:
    _run(("codespell", *_source_args(sources)))


def lint_vulture(sources: str = "") -> None:
    _run(("vulture", *_source_args(sources)))


def lint(sources: str = "") -> None:
    lint_codespell(sources)
    lint_mypy(sources)
    lint_black(sources)
    lint_isort(sources)
    lint_flake8(sources)
    lint_ruff(sources)
    lint_pylint(sources)
    lint_bandit(sources)


def format_autoflake(sources: str = "") -> None:
    _run(
        (
            "autoflake",
            "--remove-all-unused-imports",
            "--recursive",
            "--remove-unused-variables",
            "--in-place",
            *_source_args(sources),
            "--exclude=__init__.py",
        )
    )


def format_isort(sources: str = "") -> None:
    _run(("isort", *_source_args(sources)))


def format_black(sources: str = "") -> None:
    _run(("black", *_source_args(sources)))


def format_ruff(sources: str = "") -> None:
    _run(("ruff", "format", *_source_args(sources)))


def format(sources: str = "") -> None:
    format_autoflake(sources)
    format_isort(sources)
    format_black(sources)
    format_ruff(sources)


def test() -> None:
    _run(
        (
            "pytest",
            "-svv",
            "--cov=src",
            "--cov-fail-under=50",
            "--cov-report=term-missing",
            "tests",
        )
    )


def clean_pyc() -> None:
    tmp_files = (
        "*.pyc",
        "*.pyo",
        "*~",
        "*.log",
        "*.log.*",
        ".DS_Store",
        "redbeat.RedBeatScheduler",
        "celerybeat-schedule",
        "celerybeat.pid",
    )
    tmp_dirs = (
        "__pycache__",
        ".mypy_cache",
        "*.egg-info",
    )
    _find_delete(tmp_files, recursive=False)
    _find_delete(tmp_dirs, recursive=True)


def clean_test() -> None:
    tmp_files = (
        "pytest_coverage*.xml",
        "pytest_result*.xml",
    )
    tmp_dirs = (
        ".ruff_cache",
        ".pytest_cache",
        ".coverage*",
        "htmlcov",
        ".benchmarks",
    )
    _find_delete(tmp_files, recursive=False)
    _find_delete(tmp_dirs, recursive=True)


def clean() -> None:
    clean_pyc()
    clean_test()


def upgrade_deps() -> None:
    _run(("uv", "lock", "--upgrade"))


def upgrade_pkg(pkg: str = "") -> None:
    normalized_pkg = _validate_package_name(pkg)
    _run(("uv", "add", normalized_pkg, "--upgrade-package", normalized_pkg))


def pyupgrade() -> None:
    result = _run(
        ("fd", "-e", "py", "-t", "f", "-E", ".venv", "-E", ".git"),
        check=True,
        capture_output=True,
        text=True,
    )
    files = tuple(line for line in result.stdout.splitlines() if line)
    if not files:
        return
    _run(("uv", "tool", "run", "pyupgrade", "--py312-plus", "--exit-zero-even-if-changed", *files))
