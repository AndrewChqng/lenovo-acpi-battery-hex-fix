#!/usr/bin/env python3
"""Patch and verify Lenovo ACPI DSDT tables for hex-encoded battery strings.

Fixes firmware bugs where AML battery methods (_BIF/_BIX helpers like GBMN
and GUBI) implicitly convert an EC Buffer (>32 bits on ACPI v1 DSDTs) to a
String, producing space-separated "0xXX 0xYY ..." strings in sysfs instead of
plain ASCII strings.

Stdlib-only; requires external ACPICA tools (`iasl`, optionally `acpiexec`)
and `cpio` only for decompilation/compilation/verification subcommands.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import gzip
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


HEX_TOKEN_RE = re.compile(r"(?:0x[0-9A-Fa-f]{2})(?:\s+0x[0-9A-Fa-f]{2})+")
DEF_BLOCK_RE = re.compile(
    r'(DefinitionBlock\s*\(\s*"[^"]*"\s*,\s*"([^"]+)"\s*,\s*[^,]+\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*)(0x[0-9A-Fa-f]+|\d+)(\s*\))'
)
METHOD_HDR_RE = re.compile(
    r"\bMethod\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*[^)]*\)\s*\{"
)
CALL_RE = re.compile(r"(?:[A-Za-z0-9_^\\]+\.)*([A-Za-z_][A-Za-z0-9_]{0,3})\s*\(")
NAME_STR_RE = re.compile(
    r'\bName\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*"[^"]*"\s*\)'
)
NAME_BUF_RE = re.compile(
    r"\bName\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*Buffer\s*\([^)]*\)\s*\{[^}]*\}\s*\)"
)
CREATE_FIELD_RE = re.compile(
    r"\bCreateField\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*(?:Zero|0x0+|0)\s*,\s*(0x[0-9A-Fa-f]+|\d+)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)"
)
ASSIGN_RE = re.compile(
    r"^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*([A-Za-z_][A-Za-z0-9_]*)(?:[ \t]*/\*.*?\*/)?[ \t]*$",
    re.MULTILINE,
)
STORE_RE = re.compile(
    r"^([ \t]*)Store[ \t]*\([ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*,[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*\)(?:[ \t]*/\*.*?\*/)?[ \t]*$",
    re.MULTILINE,
)
CASE_LINE_RE = re.compile(r"^\s*Case\s*\(\s*([^)]+?)\s*\)\s*$")
ERROR_6022_RE = re.compile(
    r"^[^\n]*?\s+(\d+):\s+Case\s*\([^\n]+\nError\s+6022\s+-\s+Case value already specified"
    r".*?Original Case value below:\s*\n[^\n]*?\s+(\d+):\s+Case\s*\(",
    re.MULTILINE | re.DOTALL,
)

KNOWN_BATTERY_HELPERS = frozenset({"GBMN", "GUBI"})


@dataclass(frozen=True)
class PatchRecord:
    """One AML buffer-to-string assignment replaced with ToString."""

    method: str
    target_var: str
    source_field: str
    buffer_var: str
    byte_length: int


def decode_hex_string(value: str) -> tuple[bool, str]:
    """Decode space-separated 0xXX hex bytes into ASCII up to the first NUL.

    Returns (was_hex, decoded_text). If value is not a hex-byte string,
    returns (False, value.strip()).
    """
    text = value.strip()
    if not HEX_TOKEN_RE.fullmatch(text):
        return False, text

    raw_bytes: list[int] = []
    for token in text.split():
        byte_val = int(token, 16)
        if byte_val == 0:
            break
        raw_bytes.append(byte_val)

    decoded = bytes(raw_bytes).decode("utf-8", errors="replace")
    return True, decoded


def _skip_non_code(text: str, idx: int) -> int:
    """Advance past string literals or comments starting at idx, else return idx."""
    n = len(text)
    ch = text[idx]
    if ch == '"':
        i = idx + 1
        while i < n:
            if text[i] == "\\" and i + 1 < n:
                i += 2
                continue
            if text[i] == '"':
                return i + 1
            i += 1
        return n
    if ch == "/" and idx + 1 < n:
        nxt = text[idx + 1]
        if nxt == "*":
            end = text.find("*/", idx + 2)
            return end + 2 if end != -1 else n
        if nxt == "/":
            end = text.find("\n", idx + 2)
            return end + 1 if end != -1 else n
    return idx


def _find_matching_brace(text: str, open_idx: int) -> int:
    """Return index of matching '}' for '{' at open_idx, or -1 if unmatched."""
    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != "{":
        return -1

    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        after = _skip_non_code(text, i)
        if after != i:
            i = after
            continue
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _enclosing_brace(text: str, target_idx: int) -> int:
    """Return index of innermost '{' enclosing target_idx, or -1."""
    stack: list[int] = []
    i = 0
    while i < target_idx:
        after = _skip_non_code(text, i)
        if after != i:
            i = after
            continue
        ch = text[i]
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            stack.pop()
        i += 1
    return stack[-1] if stack else -1


def _iter_method_spans(dsl: str) -> list[tuple[str, int, int]]:
    """Return (method_name, open_brace_idx, close_brace_idx) for all AML Methods."""
    spans: list[tuple[str, int, int]] = []
    for match in METHOD_HDR_RE.finditer(dsl):
        open_idx = match.end() - 1
        close_idx = _find_matching_brace(dsl, open_idx)
        if close_idx != -1:
            spans.append((match.group(1), open_idx, close_idx))
    return spans


def bump_oem_revision(dsl: str) -> tuple[str, int, int]:
    """Increment the DefinitionBlock OEM revision by 1 and format as 0x%08X."""
    match = DEF_BLOCK_RE.search(dsl)
    if not match:
        raise ValueError("Could not locate DefinitionBlock OEM revision in DSL.")

    old_rev = int(match.group(3), 0)
    new_rev = old_rev + 1
    replacement = f"{match.group(1)}0x{new_rev:08X}{match.group(4)}"
    updated = dsl[: match.start()] + replacement + dsl[match.end() :]
    return updated, old_rev, new_rev


def find_battery_helper_methods(dsl: str) -> set[str]:
    """Discover AML helper methods called by _BIF/_BIX (plus known Lenovo helpers)."""
    methods_by_name: dict[str, list[str]] = {}
    for name, open_idx, close_idx in _iter_method_spans(dsl):
        methods_by_name.setdefault(name, []).append(dsl[open_idx + 1 : close_idx])

    discovered: set[str] = set()
    frontier = [name for name in ("_BIF", "_BIX") if name in methods_by_name]
    visited: set[str] = set()

    for _depth in range(3):
        next_frontier: list[str] = []
        for method_name in frontier:
            if method_name in visited:
                continue
            visited.add(method_name)
            for body in methods_by_name.get(method_name, ()):
                for call_match in CALL_RE.finditer(body):
                    callee = call_match.group(1)
                    if callee in methods_by_name and callee not in visited:
                        discovered.add(callee)
                        next_frontier.append(callee)
        frontier = next_frontier
        if not frontier:
            break

    discovered.update(KNOWN_BATTERY_HELPERS & set(methods_by_name))
    return discovered


def _patch_method_body(method_name: str, body: str) -> tuple[str, list[PatchRecord]]:
    """Patch implicit Buffer->String assignments inside a single method body."""
    string_vars = set(NAME_STR_RE.findall(body))
    buffer_vars = set(NAME_BUF_RE.findall(body))
    if not string_vars or not buffer_vars:
        return body, []

    fields: dict[str, tuple[str, int]] = {}
    for m in CREATE_FIELD_RE.finditer(body):
        buf_var, bit_len_str, field_var = m.group(1), m.group(2), m.group(3)
        if buf_var not in buffer_vars:
            continue
        bit_len = int(bit_len_str, 0)
        if bit_len > 32 and bit_len % 8 == 0:
            fields[field_var] = (buf_var, bit_len // 8)

    if not fields:
        return body, []

    records: list[PatchRecord] = []

    def replace_assign(match: re.Match[str]) -> str:
        indent, lhs, rhs = match.group(1), match.group(2), match.group(3)
        if lhs not in string_vars or rhs not in fields:
            return match.group(0)
        buf_var, byte_len = fields[rhs]
        records.append(
            PatchRecord(
                method=method_name,
                target_var=lhs,
                source_field=rhs,
                buffer_var=buf_var,
                byte_length=byte_len,
            )
        )
        return f"{indent}{lhs} = ToString ({buf_var}, 0x{byte_len:02X})"

    def replace_store(match: re.Match[str]) -> str:
        indent, src, dst = match.group(1), match.group(2), match.group(3)
        if dst not in string_vars or src not in fields:
            return match.group(0)
        buf_var, byte_len = fields[src]
        records.append(
            PatchRecord(
                method=method_name,
                target_var=dst,
                source_field=src,
                buffer_var=buf_var,
                byte_length=byte_len,
            )
        )
        return f"{indent}{dst} = ToString ({buf_var}, 0x{byte_len:02X})"

    updated = ASSIGN_RE.sub(replace_assign, body)
    updated = STORE_RE.sub(replace_store, updated)
    return updated, records


def patch_battery_methods(
    dsl: str, all_methods: bool = False
) -> tuple[str, list[PatchRecord]]:
    """Replace implicit Buffer->String copies in battery helper methods with ToString."""
    targets = None if all_methods else find_battery_helper_methods(dsl)
    spans = _iter_method_spans(dsl)

    chunks: list[str] = []
    records: list[PatchRecord] = []
    cursor = 0

    for name, open_idx, close_idx in spans:
        if targets is not None and name not in targets:
            continue
        body_start = open_idx + 1
        body = dsl[body_start:close_idx]
        patched_body, body_records = _patch_method_body(name, body)
        if body_records:
            chunks.append(dsl[cursor:body_start])
            chunks.append(patched_body)
            cursor = close_idx
            records.extend(body_records)

    if not records:
        return dsl, []

    chunks.append(dsl[cursor:])
    return "".join(chunks), records


def remove_duplicate_case_at_line(
    dsl: str, err_line: int, orig_line: int
) -> tuple[str, bool]:
    """Remove an unreachable duplicate Case block reported by iasl Error 6022."""
    lines = dsl.splitlines(keepends=True)
    if not (1 <= orig_line < err_line <= len(lines)):
        return dsl, False

    orig_match = CASE_LINE_RE.match(lines[orig_line - 1])
    err_match = CASE_LINE_RE.match(lines[err_line - 1])
    if not orig_match or not err_match:
        return dsl, False
    if orig_match.group(1) != err_match.group(1):
        return dsl, False

    orig_offset = sum(len(line) for line in lines[: orig_line - 1])
    err_offset = sum(len(line) for line in lines[: err_line - 1])
    orig_scope = _enclosing_brace(dsl, orig_offset)
    err_scope = _enclosing_brace(dsl, err_offset)
    if orig_scope == -1 or orig_scope != err_scope:
        return dsl, False

    pos = err_offset + len(lines[err_line - 1])
    while pos < len(dsl) and dsl[pos] in " \t\r\n":
        pos += 1
    if pos >= len(dsl) or dsl[pos] != "{":
        return dsl, False

    close_idx = _find_matching_brace(dsl, pos)
    if close_idx == -1:
        return dsl, False

    end = close_idx + 1
    while end < len(dsl) and dsl[end] in " \t":
        end += 1
    if end < len(dsl) and dsl[end : end + 2] == "\r\n":
        end += 2
    elif end < len(dsl) and dsl[end] == "\n":
        end += 1

    return dsl[:err_offset] + dsl[end:], True


def _run_cmd(
    cmd: list[str], cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=check,
    )


def _read_first_line(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _check_kernel_config_flag(flag: str) -> str:
    proc_cfg = Path("/proc/config.gz")
    if proc_cfg.exists():
        try:
            with gzip.open(proc_cfg, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith(f"{flag}=") or line.startswith(f"# {flag} is not set"):
                        return line.strip()
        except OSError:
            pass
    boot_cfg = Path(f"/boot/config-{os.uname().release}")
    if boot_cfg.exists():
        try:
            for line in boot_cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith(f"{flag}=") or line.startswith(f"# {flag} is not set"):
                    return line.strip()
        except OSError:
            pass
    return "unknown (no /proc/config.gz)"


def cmd_decode(args: argparse.Namespace) -> int:
    """Decode provided hex byte strings or live sysfs battery strings."""
    if args.strings:
        for raw in args.strings:
            was_hex, decoded = decode_hex_string(raw)
            tag = "hex->ascii" if was_hex else "plain"
            print(f"[{tag}] {raw} -> {decoded!r}")
        return 0

    bats = sorted(Path("/sys/class/power_supply").glob("BAT*"))
    if not bats:
        print("No /sys/class/power_supply/BAT* entries found.", file=sys.stderr)
        return 1

    for bat in bats:
        print(f"== {bat.name} ==")
        for attr in ("model_name", "manufacturer", "serial_number", "technology"):
            raw = _read_first_line(bat / attr)
            if raw is None:
                continue
            was_hex, decoded = decode_hex_string(raw)
            if was_hex:
                print(f"  {attr}: {raw!r} -> {decoded!r} [HEX BUG]")
            else:
                print(f"  {attr}: {raw!r} [OK]")
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    """Run read-only pre-flight checks on firmware, kernel, and tools."""
    print("== System & Firmware ==")
    dmi = Path("/sys/devices/virtual/dmi/id")
    for name in ("sys_vendor", "product_name", "product_version", "bios_version", "bios_date"):
        val = _read_first_line(dmi / name)
        if val:
            print(f"  {name}: {val}")
    print(f"  kernel: {os.uname().release}")

    print("\n== Kernel ACPI Override Readiness ==")
    lockdown = _read_first_line(Path("/sys/kernel/security/lockdown")) or "not exposed"
    print(f"  lockdown: {lockdown}")
    if "[none]" not in lockdown and lockdown != "not exposed":
        print("  WARNING: Kernel lockdown is active; ACPI_TABLE_UPGRADE is blocked by Secure Boot/lockdown.")
    print(f"  CONFIG_ACPI_TABLE_UPGRADE: {_check_kernel_config_flag('CONFIG_ACPI_TABLE_UPGRADE')}")

    print("\n== Battery Sysfs ==")
    bats = sorted(Path("/sys/class/power_supply").glob("BAT*"))
    if not bats:
        print("  No BAT* devices found.")
    for bat in bats:
        for attr in ("model_name", "manufacturer"):
            raw = _read_first_line(bat / attr)
            if raw is None:
                continue
            was_hex, decoded = decode_hex_string(raw)
            status = f"HEX BUG (decodes to {decoded!r})" if was_hex else "OK"
            print(f"  {bat.name}/{attr}: {raw!r} -> {status}")

    print("\n== Toolchain & Initramfs ==")
    for tool in ("iasl", "acpiexec", "cpio", "mkinitcpio", "dracut", "update-initramfs"):
        print(f"  {tool}: {shutil.which(tool) or 'missing'}")

    override_dir = Path("/etc/initcpio/acpi_override")
    if override_dir.exists():
        amls = sorted(p.name for p in override_dir.glob("*.aml"))
        print(f"  /etc/initcpio/acpi_override: {amls or 'empty'}")
    mk_conf = Path("/etc/mkinitcpio.conf")
    if mk_conf.exists():
        for line in mk_conf.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("HOOKS="):
                print(f"  /etc/mkinitcpio.conf: {line}")
                break
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    """Dump /sys/firmware/acpi/tables/{DSDT,SSDT*} into work-dir."""
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    gid = os.getgid()

    script = (
        "set -e\n"
        f"cp /sys/firmware/acpi/tables/DSDT {shlex.quote(str(work_dir / 'dsdt.dat'))}\n"
        "for f in /sys/firmware/acpi/tables/SSDT*; do\n"
        '  [ -e "$f" ] || continue\n'
        '  base=$(basename "$f" | tr "[:upper:]" "[:lower:]")\n'
        f'  cp "$f" {shlex.quote(str(work_dir))}/"${{base}}.dat"\n'
        "done\n"
        f"chown -R {uid}:{gid} {shlex.quote(str(work_dir))}\n"
        f"chmod 0644 {shlex.quote(str(work_dir))}/*.dat\n"
    )

    if os.geteuid() == 0:
        cmd = ["sh", "-c", script]
    elif args.sudo:
        cmd = ["sudo", "sh", "-c", script]
    else:
        cmd = ["pkexec", "sh", "-c", script]

    print(f"Dumping ACPI tables into {work_dir} ...")
    res = _run_cmd(cmd, check=False)
    if res.returncode != 0:
        if res.stderr:
            print(res.stderr, file=sys.stderr, end="")
        print(f"Dump failed with exit code {res.returncode}.", file=sys.stderr)
        return res.returncode

    dumped = sorted(p.name for p in work_dir.glob("*.dat"))
    print(f"Dumped {len(dumped)} table(s): {', '.join(dumped)}")
    return 0


def _decompile_dsdt(work_dir: Path) -> Path:
    dsdt_dat = work_dir / "dsdt.dat"
    if not dsdt_dat.exists():
        raise FileNotFoundError(f"Missing {dsdt_dat}; run 'dump' first or pass --dsl.")

    ssdts = sorted(p.name for p in work_dir.glob("ssdt*.dat"))
    cmd = ["iasl"]
    if ssdts:
        cmd.extend(["-e", *ssdts])
    cmd.extend(["-d", "dsdt.dat"])

    res = _run_cmd(cmd, cwd=work_dir, check=False)
    if res.returncode != 0:
        print(res.stdout, end="")
        print(res.stderr, file=sys.stderr, end="")
        raise RuntimeError(f"iasl disassembly failed (exit {res.returncode}).")

    dsdt_dsl = work_dir / "dsdt.dsl"
    orig_dsl = work_dir / "dsdt.orig.dsl"
    shutil.copy2(dsdt_dsl, orig_dsl)
    return dsdt_dsl


def _compile_with_recovery(
    dsl_path: Path, allow_fix_dup_case: bool
) -> tuple[str, int]:
    """Compile DSL with iasl, optionally removing unreachable Error 6022 duplicate Cases."""
    work_dir = dsl_path.parent
    removed_cases = 0

    for _attempt in range(6):
        res = _run_cmd(["iasl", "-ve", "-tc", dsl_path.name], cwd=work_dir, check=False)
        combined = (res.stdout or "") + "\n" + (res.stderr or "")
        if res.returncode == 0:
            return combined, removed_cases

        if not allow_fix_dup_case:
            print(combined, file=sys.stderr)
            raise RuntimeError(f"iasl compilation failed (exit {res.returncode}).")

        current_text = dsl_path.read_text(encoding="utf-8", errors="replace")
        err_match = ERROR_6022_RE.search(combined)
        if not err_match:
            print(combined, file=sys.stderr)
            raise RuntimeError(f"iasl compilation failed (exit {res.returncode}).")

        err_line = int(err_match.group(1))
        orig_line = int(err_match.group(2))
        updated, changed = remove_duplicate_case_at_line(
            current_text, err_line=err_line, orig_line=orig_line
        )
        if not changed:
            print(combined, file=sys.stderr)
            raise RuntimeError(
                f"Could not safely remove Error 6022 duplicate Case at line {err_line}."
            )
        dsl_path.write_text(updated, encoding="utf-8")
        removed_cases += 1
        print(
            f"Removed unreachable duplicate Case block at line {err_line} "
            f"(shadowed by line {orig_line})."
        )

    raise RuntimeError("Exceeded maximum Error 6022 recovery attempts.")


def _verify_roundtrip(work_dir: Path, aml_path: Path, expected_rev: int) -> None:
    verify_dir = work_dir / "verify"
    if verify_dir.exists():
        shutil.rmtree(verify_dir)
    verify_dir.mkdir(parents=True)

    shutil.copy2(aml_path, verify_dir / "dsdt.aml")
    ssdts = sorted(work_dir.glob("ssdt*.dat"))
    for ssdt in ssdts:
        shutil.copy2(ssdt, verify_dir / ssdt.name)

    cmd = ["iasl"]
    if ssdts:
        cmd.extend(["-e", *(p.name for p in ssdts)])
    cmd.extend(["-d", "dsdt.aml"])
    res = _run_cmd(cmd, cwd=verify_dir, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Round-trip verification disassembly failed:\n{res.stderr}")

    re_dsl = (verify_dir / "dsdt.dsl").read_text(encoding="utf-8", errors="replace")
    def_match = DEF_BLOCK_RE.search(re_dsl)
    if not def_match or int(def_match.group(3), 0) != expected_rev:
        raise RuntimeError("Round-trip DSDT OEM revision mismatch.")
    if "ToString (" not in re_dsl:
        raise RuntimeError("Round-trip DSDT does not contain ToString () calls.")


def _verify_acpiexec(work_dir: Path, aml_path: Path, dsl_text: str) -> None:
    acpiexec = shutil.which("acpiexec")
    if not acpiexec:
        print("acpiexec not found; skipping AML emulator smoke test.")
        return

    ssdts = sorted(p.name for p in work_dir.glob("ssdt*.dat"))
    bat_match = re.search(r"Device\s*\(\s*(BAT[0-9A-Za-z_]*)\s*\)", dsl_text)
    if bat_match:
        bat_dev = bat_match.group(1)
        batch = f"find {bat_dev}; quit"
    else:
        batch = "quit"

    cmd = [acpiexec, "-b", batch, aml_path.name, *ssdts]
    res = _run_cmd(cmd, cwd=work_dir, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"acpiexec verification failed:\n{res.stdout}\n{res.stderr}")
    print(f"acpiexec loaded {aml_path.name} + {len(ssdts)} SSDT(s) cleanly.")


def cmd_patch(args: argparse.Namespace) -> int:
    """Decompile (if needed), patch DSDT battery methods, compile, and verify."""
    if not shutil.which("iasl"):
        print("Error: `iasl` not found. Install the `acpica` package.", file=sys.stderr)
        return 1

    work_dir = Path(args.work_dir).resolve()
    if args.dsl:
        dsl_path = Path(args.dsl).resolve()
        work_dir = dsl_path.parent
        orig_path = work_dir / f"{dsl_path.stem}.orig{dsl_path.suffix}"
        if not orig_path.exists():
            shutil.copy2(dsl_path, orig_path)
    else:
        dsl_path = _decompile_dsdt(work_dir)
        orig_path = work_dir / "dsdt.orig.dsl"

    orig_text = orig_path.read_text(encoding="utf-8", errors="replace")
    patched_text, records = patch_battery_methods(orig_text, all_methods=args.all_methods)
    if not records:
        print(
            "No implicit Buffer->String battery assignments found to patch.",
            file=sys.stderr,
        )
        return 1

    patched_text, old_rev, new_rev = bump_oem_revision(patched_text)
    print(f"OEM revision: 0x{old_rev:08X} -> 0x{new_rev:08X}")
    for rec in records:
        print(
            f"  Patched {rec.method}: {rec.target_var} = {rec.source_field} "
            f"-> ToString ({rec.buffer_var}, 0x{rec.byte_length:02X})"
        )

    if args.dry_run:
        diff = difflib.unified_diff(
            orig_text.splitlines(keepends=True),
            patched_text.splitlines(keepends=True),
            fromfile=f"a/{dsl_path.name}",
            tofile=f"b/{dsl_path.name}",
        )
        sys.stdout.writelines(diff)
        return 0

    dsl_path.write_text(patched_text, encoding="utf-8")
    compile_out, removed_cases = _compile_with_recovery(
        dsl_path, allow_fix_dup_case=not args.no_fix_dup_case
    )
    for line in compile_out.splitlines():
        if line.startswith(("ASL Input:", "AML Output:", "Compilation successful")):
            print(f"  {line}")

    final_text = dsl_path.read_text(encoding="utf-8", errors="replace")
    print("\n== Unified Diff ==")
    diff = difflib.unified_diff(
        orig_text.splitlines(keepends=True),
        final_text.splitlines(keepends=True),
        fromfile=f"a/{dsl_path.name}",
        tofile=f"b/{dsl_path.name}",
    )
    sys.stdout.writelines(diff)

    aml_path = dsl_path.with_suffix(".aml")
    _verify_roundtrip(work_dir, aml_path, expected_rev=new_rev)
    print(f"Round-trip verification passed ({aml_path}, {aml_path.stat().st_size} bytes).")

    if not args.no_verify_exec:
        _verify_acpiexec(work_dir, aml_path, final_text)

    if removed_cases:
        print(f"Note: removed {removed_cases} unreachable Error 6022 Case block(s).")
    return 0


def build_cpio(aml_path: Path, output_path: Path) -> list[str]:
    """Package compiled dsdt.aml into an uncompressed early CPIO archive."""
    if not shutil.which("cpio"):
        raise RuntimeError("`cpio` binary not found in PATH.")
    if not aml_path.is_file():
        raise FileNotFoundError(f"AML file not found: {aml_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "kernel" / "firmware" / "acpi" / "dsdt.aml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(aml_path, target)

        cpio_input = "kernel\nkernel/firmware\nkernel/firmware/acpi\nkernel/firmware/acpi/dsdt.aml\n"
        res = subprocess.run(
            ["cpio", "-H", "newc", "-o", "--quiet"],
            input=cpio_input.encode("utf-8"),
            cwd=str(root),
            capture_output=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"cpio creation failed: {res.stderr.decode('utf-8', errors='replace')}")
        output_path.write_bytes(res.stdout)

    verify = subprocess.run(
        ["cpio", "-t", "--quiet"],
        input=output_path.read_bytes(),
        capture_output=True,
        check=False,
    )
    if verify.returncode != 0:
        raise RuntimeError("Failed to verify generated CPIO archive.")
    entries = verify.stdout.decode("utf-8", errors="replace").splitlines()
    if "kernel/firmware/acpi/dsdt.aml" not in entries:
        raise RuntimeError("kernel/firmware/acpi/dsdt.aml missing from CPIO archive.")
    return entries


def cmd_build_cpio(args: argparse.Namespace) -> int:
    """CLI handler to build an early uncompressed ACPI override CPIO archive."""
    entries = build_cpio(Path(args.aml).resolve(), Path(args.output).resolve())
    print(f"Created {Path(args.output).resolve()} ({Path(args.output).stat().st_size} bytes):")
    for entry in entries:
        print(f"  {entry}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diagnose, patch, and package Lenovo DSDT battery hex string overrides."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Inspect system, lockdown, sysfs, and tools.")
    p_check.set_defaults(func=cmd_check)

    p_decode = sub.add_parser(
        "decode", help="Decode 0xXX hex byte strings or live BAT* sysfs attributes."
    )
    p_decode.add_argument("strings", nargs="*", help="Optional hex strings to decode.")
    p_decode.set_defaults(func=cmd_decode)

    p_dump = sub.add_parser("dump", help="Dump /sys/firmware/acpi/tables/{DSDT,SSDT*}.")
    p_dump.add_argument("--work-dir", default="acpi_work", help="Output working directory.")
    p_dump.add_argument(
        "--sudo", action="store_true", help="Use sudo instead of pkexec when non-root."
    )
    p_dump.set_defaults(func=cmd_dump)

    p_patch = sub.add_parser(
        "patch", help="Decompile, patch battery ToString(), compile, and verify DSDT."
    )
    p_patch.add_argument("--work-dir", default="acpi_work", help="Working directory.")
    p_patch.add_argument("--dsl", help="Patch an existing .dsl file instead of decompiling.")
    p_patch.add_argument(
        "--all-methods",
        action="store_true",
        help="Scan all AML methods instead of only _BIF/_BIX helpers.",
    )
    p_patch.add_argument(
        "--no-fix-dup-case",
        action="store_true",
        help="Do not auto-remove unreachable iasl Error 6022 duplicate Case blocks.",
    )
    p_patch.add_argument(
        "--no-verify-exec",
        action="store_true",
        help="Skip acpiexec namespace load verification.",
    )
    p_patch.add_argument(
        "--dry-run", action="store_true", help="Print unified diff without writing/compiling."
    )
    p_patch.set_defaults(func=cmd_patch)

    p_cpio = sub.add_parser(
        "build-cpio",
        help="Build an uncompressed early CPIO containing kernel/firmware/acpi/dsdt.aml.",
    )
    p_cpio.add_argument(
        "--aml", default="acpi_work/dsdt.aml", help="Path to compiled dsdt.aml."
    )
    p_cpio.add_argument(
        "--output",
        default="acpi_work/acpi_override.cpio",
        help="Output early CPIO image path.",
    )
    p_cpio.set_defaults(func=cmd_build_cpio)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
