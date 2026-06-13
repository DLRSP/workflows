#!/usr/bin/env python3
"""Build a GitHub Actions matrix from tox envlist definitions."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from configparser import ConfigParser
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

ACTION_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ACTION_ROOT / "config" / "default-matrix.json"

PY_DJANGO_ENV = re.compile(r"^py(\d+)-django(\d+)$")
PY_ONLY_ENV = re.compile(r"^py(\d+)$")


class MatrixMode(str, Enum):
    AUTO = "auto"
    PYTHON_ONLY = "python-only"


@dataclass(frozen=True)
class PythonPolicy:
    name: str
    flags: dict[str, bool]
    min_version: tuple[int, int] | None = None
    max_version: tuple[int, int] | None = None


@dataclass(frozen=True)
class DjangoPolicy:
    name: str
    versions: tuple[str, ...]
    flags: dict[str, bool]


@dataclass(frozen=True)
class CompatibilityRule:
    django_versions: tuple[str, ...]
    min_python: tuple[int, int] | None = None
    max_python: tuple[int, int] | None = None


@dataclass(frozen=True)
class MatrixConfig:
    fallback_include: list[dict[str, Any]]
    python_policies: tuple[PythonPolicy, ...]
    django_policies: tuple[DjangoPolicy, ...]
    compatibility_rules: tuple[CompatibilityRule, ...]
    unknown_env_python_version: str

    def flags_for_python(self, python_version: str) -> dict[str, bool]:
        version = parse_version(python_version)
        flags: dict[str, bool] = {}
        for policy in self.python_policies:
            if policy.min_version is not None and version >= policy.min_version:
                flags.update(policy.flags)
            if policy.max_version is not None and version <= policy.max_version:
                flags.update(policy.flags)
        return flags

    def flags_for_django(self, django_version: str) -> dict[str, bool]:
        flags: dict[str, bool] = {}
        for policy in self.django_policies:
            if django_version in policy.versions:
                flags.update(policy.flags)
        return flags

    def flags_for_env(
        self,
        python_version: str,
        django_version: str | None = None,
    ) -> dict[str, bool]:
        flags = self.flags_for_python(python_version)
        if django_version is not None:
            flags.update(self.flags_for_django(django_version))
        return flags

    def is_compatible(
        self,
        python_version: str,
        django_version: str | None,
    ) -> bool:
        if django_version is None:
            return True

        py = parse_version(python_version)
        for rule in self.compatibility_rules:
            if django_version not in rule.django_versions:
                continue
            if rule.min_python is not None and py < rule.min_python:
                return False
            if rule.max_python is not None and py > rule.max_python:
                return False
        return True


@dataclass(frozen=True)
class MatrixResult:
    include: list[dict[str, Any]]
    mode: str

    @property
    def count(self) -> int:
        return len(self.include)

    def as_github_matrix(self) -> dict[str, Any]:
        return {"include": self.include}


def parse_version(value: str) -> tuple[int, int]:
    major, minor = value.split(".", maxsplit=1)
    return int(major), int(minor)


def tox_factor_to_version(tag: str) -> str:
    if len(tag) == 2:
        return f"{tag[0]}.{tag[1]}"
    return f"{tag[0]}.{tag[1:]}"


def load_config(path: Path) -> MatrixConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    policies = tuple(
        PythonPolicy(
            name=item["name"],
            flags=dict(item["flags"]),
            min_version=(
                parse_version(item["min_version"]) if "min_version" in item else None
            ),
            max_version=(
                parse_version(item["max_version"]) if "max_version" in item else None
            ),
        )
        for item in payload.get("python_policies", [])
    )
    django_policies = tuple(
        DjangoPolicy(
            name=item["name"],
            versions=tuple(item["versions"]),
            flags=dict(item["flags"]),
        )
        for item in payload.get("django_policies", [])
    )
    compatibility_rules = tuple(
        CompatibilityRule(
            django_versions=tuple(item["django_versions"]),
            min_python=(
                parse_version(item["min_python"]) if "min_python" in item else None
            ),
            max_python=(
                parse_version(item["max_python"]) if "max_python" in item else None
            ),
        )
        for item in payload.get("compatibility_rules", [])
    )
    defaults = payload.get("defaults", {})
    fallback = payload.get("fallback", {})
    return MatrixConfig(
        fallback_include=list(fallback.get("include", [])),
        python_policies=policies,
        django_policies=django_policies,
        compatibility_rules=compatibility_rules,
        unknown_env_python_version=defaults.get("unknown_env_python_version", "3.11"),
    )


def expand_braces(value: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", value)
    if not match:
        return [value]

    prefix = value[: match.start()]
    suffix = value[match.end() :]
    options = match.group(1).split(",")
    suffixes = expand_braces(suffix) if "{" in suffix else [suffix]

    expanded: list[str] = []
    for option in options:
        for tail in suffixes:
            expanded.extend(expand_braces(prefix + option + tail))
    return expanded


def split_envlist_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    depth = 0

    for char in value:
        if char == "{":
            depth += 1
            current.append(char)
        elif char == "}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            token = "".join(current).strip()
            if token:
                tokens.append(token)
            current = []
        else:
            current.append(char)

    token = "".join(current).strip()
    if token:
        tokens.append(token)
    return tokens


def parse_envlist_from_tox_ini(path: Path) -> list[str]:
    parser = ConfigParser()
    parser.read(path, encoding="utf-8")
    if not parser.has_section("tox") or not parser.has_option("tox", "envlist"):
        return []

    raw = parser.get("tox", "envlist")
    # Newlines in tox.ini are env separators; without this, adjacent factors merge
    # (e.g. py{310}-django{42} + py{312}-django{52} -> py310-django42py312-django52).
    normalized = re.sub(r"\s+", "", raw.replace("\n", ",").replace("\r", ","))
    if not normalized:
        return []

    envs: list[str] = []
    for token in split_envlist_tokens(normalized):
        envs.extend(expand_braces(token))
    return sorted(dict.fromkeys(envs))


def list_tox_envs(workspace: Path) -> list[str]:
    tox_ini = workspace / "tox.ini"
    if not tox_ini.is_file():
        return []

    try:
        completed = subprocess.run(
            ["tox", "-l"],
            check=True,
            capture_output=True,
            text=True,
            cwd=workspace,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return parse_envlist_from_tox_ini(tox_ini)

    envs = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return envs or parse_envlist_from_tox_ini(tox_ini)


class MatrixBuilder:
    def __init__(self, config: MatrixConfig, workspace: Path) -> None:
        self._config = config
        self._workspace = workspace

    def build(self, mode: MatrixMode | str) -> MatrixResult:
        if MatrixMode(str(mode).strip().lower()) is MatrixMode.PYTHON_ONLY:
            return self._python_only_result()

        envs = list_tox_envs(self._workspace)
        if not envs:
            return self._python_only_result()

        include: list[dict[str, Any]] = []
        for env in envs:
            entry = self._entry_for_env(env)
            django_version = entry.get("django-version")
            if not self._config.is_compatible(
                entry["python-version"],
                django_version if isinstance(django_version, str) else None,
            ):
                continue
            include.append(entry)

        return MatrixResult(include=include, mode="tox-env")

    def _python_only_result(self) -> MatrixResult:
        return MatrixResult(
            include=[dict(entry) for entry in self._config.fallback_include],
            mode="python-only",
        )

    def _entry_for_env(self, env: str) -> dict[str, Any]:
        match = PY_DJANGO_ENV.match(env)
        if match:
            python_version = tox_factor_to_version(match.group(1))
            django_version = tox_factor_to_version(match.group(2))
            entry: dict[str, Any] = {
                "tox-env": env,
                "python-version": python_version,
                "django-version": django_version,
                "matrix-mode": "tox-env",
            }
            entry.update(self._config.flags_for_env(python_version, django_version))
            return entry

        match = PY_ONLY_ENV.match(env)
        if match:
            python_version = tox_factor_to_version(match.group(1))
            entry = {
                "tox-env": env,
                "python-version": python_version,
                "matrix-mode": "tox-env",
            }
            entry.update(self._config.flags_for_env(python_version))
            return entry

        return {
            "tox-env": env,
            "python-version": self._config.unknown_env_python_version,
            "matrix-mode": "tox-env",
        }


def emit_result(result: MatrixResult) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        matrix_payload = json.dumps(result.as_github_matrix())
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={matrix_payload}\n")
            handle.write(f"mode={result.mode}\n")
            handle.write(f"count={result.count}\n")
        return

    print(
        json.dumps(
            {"include": result.include, "mode": result.mode, "count": result.count},
            indent=2,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a GitHub Actions matrix from tox.ini envlist.",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--mode",
        choices=[item.value for item in MatrixMode],
        default=os.environ.get("TOX_MATRIX_MODE", MatrixMode.AUTO.value),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = MatrixBuilder(load_config(args.config), args.workspace).build(args.mode)
    emit_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
