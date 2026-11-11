"""Post-build checks on a .deb: control metadata, ELF architecture, and an
apt resolver dry-run proving the declared dependencies can be satisfied."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from xarch_pkg.chroot import Chroot
from xarch_pkg.config import check_relation_field
from xarch_pkg.util import BuildError, log

REQUIRED_FIELDS = ("Package", "Version", "Architecture", "Maintainer", "Description")
RELATION_FIELDS = ("Depends", "Pre-Depends", "Recommends", "Suggests", "Conflicts", "Breaks")
CHECK_DIR = "build/check"


@dataclass
class Report:
    deb: Path
    fields: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def parse_control(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and key:
            fields[key] += "\n" + line
        elif ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


def elf_machine(path: Path) -> tuple[int, int] | None:
    """Return (EI_CLASS, e_machine) for an ELF file, or None if it is not ELF."""
    with path.open("rb") as fh:
        head = fh.read(20)
    if len(head) < 20 or head[:4] != b"\x7fELF":
        return None
    order = "little" if head[5] == 1 else "big"
    return head[4], int.from_bytes(head[18:20], order)


def validate(deb: Path, ch: Chroot) -> Report:
    report = Report(deb)
    check = ch.root / CHECK_DIR
    if check.exists():
        shutil.rmtree(check)
    check.mkdir(parents=True)
    shutil.copyfile(deb, check / deb.name)
    inside = f"/{CHECK_DIR}/{deb.name}"

    # 1. control metadata, read back out of the archive with dpkg-deb --info
    control = ch.sh(f"dpkg-deb --info {inside} control", capture=True).stdout
    report.fields = fields = parse_control(control)
    for name in REQUIRED_FIELDS:
        if not fields.get(name):
            report.problems.append(f"missing control field {name}")
    if fields.get("Architecture") not in (ch.target.name, "all"):
        report.problems.append(
            f"Architecture is {fields.get('Architecture')!r}, expected {ch.target.name!r}"
        )
    for name in RELATION_FIELDS:
        if name in fields:
            report.problems += [f"{name}: {p}" for p in check_relation_field(fields[name])]

    # 2. every ELF object in the payload must match the target architecture
    ch.sh(f"dpkg-deb -x {inside} /{CHECK_DIR}/root")
    for p in sorted((check / "root").rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        info = elf_machine(p)
        if info and info != (ch.target.elf_class, ch.target.elf_machine):
            rel = p.relative_to(check / "root")
            report.problems.append(
                f"/{rel} is ELF class {info[0]} machine {info[1]}, expected "
                f"class {ch.target.elf_class} machine {ch.target.elf_machine} ({ch.target.name})"
            )

    # 3. apt resolver dry-run against the target suite's archive
    sim = ch.sh(f"apt-get install --simulate -q {inside} 2>&1; echo \"rc=$?\"", capture=True).stdout
    if not sim.rstrip().endswith("rc=0"):
        report.problems.append("apt could not satisfy dependencies:\n" + sim.strip())
    else:
        report.resolved = [ln.split()[1] for ln in sim.splitlines() if ln.startswith("Inst ")]

    shutil.rmtree(check)
    status = "OK" if report.ok else "FAILED"
    log(f"validate {deb.name}: {status}; apt would install: {', '.join(report.resolved) or '-'}")
    for p in report.problems:
        log(f"  problem: {p}")
    return report


def require_valid(deb: Path, ch: Chroot) -> Report:
    report = validate(deb, ch)
    if not report.ok:
        raise BuildError(f"{deb.name} failed validation")
    return report
