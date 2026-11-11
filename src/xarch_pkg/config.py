"""Load and validate the per-project ``xpkg.toml`` package description."""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from xarch_pkg.util import BuildError

CONFIG_NAME = "xpkg.toml"
MAINTAINER_SCRIPTS = ("preinst", "postinst", "prerm", "postrm")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
_VERSION_RE = re.compile(r"^(\d+:)?[0-9][A-Za-z0-9.+~:-]*$")
# One relation: "name[:arch] [(op version)] [[arch list]]"
_RELATION_RE = re.compile(
    r"^[a-z0-9][a-z0-9+.-]+(:[a-z0-9-]+)?"
    r"(\s*\(\s*(<<|<=|=|>=|>>)\s*[A-Za-z0-9.+~:-]+\s*\))?"
    r"(\s*\[[^\]]+\])?$"
)


def check_relation_field(value: str) -> list[str]:
    """Return a list of problems with a Depends-style field ("" means none)."""
    problems = []
    for group in filter(None, (g.strip() for g in value.split(","))):
        for alt in (a.strip() for a in group.split("|")):
            if not _RELATION_RE.match(alt):
                problems.append(f"malformed relation {alt!r}")
    return problems


@dataclass
class PackageConfig:
    source_dir: Path
    name: str
    version: str
    maintainer: str
    description: str
    long_description: str = ""
    section: str = "misc"
    priority: str = "optional"
    homepage: str = ""
    relations: dict[str, list[str]] = field(default_factory=dict)
    maintainer_scripts: dict[str, Path] = field(default_factory=dict)
    build_command: str = "make && make install"
    build_depends: list[str] = field(default_factory=list)
    build_env: dict[str, str] = field(default_factory=dict)
    suite: str = "trixie"
    mirror: str = "http://deb.debian.org/debian"
    toolchain: dict[str, str] = field(default_factory=dict)
    source_date_epoch: int = 0

    def deb_filename(self, arch: str) -> str:
        version = self.version.split(":", 1)[-1]  # epochs are not part of file names
        return f"{self.name}_{version}_{arch}.deb"


_RELATION_KEYS = {
    "depends": "Depends",
    "pre_depends": "Pre-Depends",
    "recommends": "Recommends",
    "suggests": "Suggests",
    "conflicts": "Conflicts",
    "breaks": "Breaks",
    "provides": "Provides",
    "replaces": "Replaces",
}


def _git_epoch(source_dir: Path) -> int | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(source_dir), "log", "-1", "--format=%ct"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return int(out) if out else None
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def load(source_dir: Path) -> PackageConfig:
    source_dir = source_dir.resolve()
    path = source_dir / CONFIG_NAME
    if not path.is_file():
        raise BuildError(f"no {CONFIG_NAME} found in {source_dir}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    pkg = raw.get("package", {})
    build = raw.get("build", {})
    env = raw.get("environment", {})
    errors: list[str] = []

    for key in ("name", "version", "maintainer", "description"):
        if not pkg.get(key):
            errors.append(f"[package].{key} is required")
    if pkg.get("name") and not _NAME_RE.match(pkg["name"]):
        errors.append(f"[package].name {pkg['name']!r} is not a valid Debian package name")
    if pkg.get("version") and not _VERSION_RE.match(pkg["version"]):
        errors.append(f"[package].version {pkg['version']!r} is not a valid Debian version")

    relations: dict[str, list[str]] = {}
    for key, field_name in _RELATION_KEYS.items():
        values = pkg.get(key, [])
        if not isinstance(values, list):
            errors.append(f"[package].{key} must be a list")
            continue
        for problem in check_relation_field(", ".join(values)):
            errors.append(f"[package].{key}: {problem}")
        if values:
            relations[field_name] = values

    scripts: dict[str, Path] = {}
    for name, rel in raw.get("maintainer_scripts", {}).items():
        if name not in MAINTAINER_SCRIPTS:
            errors.append(f"[maintainer_scripts].{name}: must be one of {MAINTAINER_SCRIPTS}")
            continue
        script = source_dir / rel
        if not script.is_file():
            errors.append(f"[maintainer_scripts].{name}: {script} does not exist")
        elif not script.read_text().startswith("#!"):
            errors.append(f"[maintainer_scripts].{name}: {script} has no #! line")
        scripts[name] = script

    toolchain = raw.get("toolchain", {})
    if not all(isinstance(v, str) for v in toolchain.values()):
        errors.append("[toolchain] values must be exact version strings")

    epoch = env.get("source_date_epoch") or _git_epoch(source_dir)
    if epoch is None:
        errors.append("set [environment].source_date_epoch (or build from a git checkout)")

    if errors:
        raise BuildError(f"invalid {path}:\n  " + "\n  ".join(errors))

    return PackageConfig(
        source_dir=source_dir,
        name=pkg["name"],
        version=pkg["version"],
        maintainer=pkg["maintainer"],
        description=pkg["description"],
        long_description=pkg.get("long_description", "").strip(),
        section=pkg.get("section", "misc"),
        priority=pkg.get("priority", "optional"),
        homepage=pkg.get("homepage", ""),
        relations=relations,
        maintainer_scripts=scripts,
        build_command=build.get("command", "make && make install"),
        build_depends=list(build.get("depends", [])),
        build_env=dict(build.get("env", {})),
        suite=env.get("suite", "trixie"),
        mirror=env.get("mirror", "http://deb.debian.org/debian").rstrip("/"),
        toolchain=dict(toolchain),
        source_date_epoch=int(epoch),
    )
