# AGENTS.md

Operational guide and invariants for autonomous AI coding agents working in this repository or applying this ACPI DSDT battery fix on a target Linux system.

---

## 1. Problem Signature & Root Cause

On affected Lenovo laptops (Yoga, IdeaPad, Legion, ThinkBook), `/sys/class/power_supply/BAT*/model_name` and/or `manufacturer` expose space-separated hex bytes:

- `model_name`: `0x4C 0x32 0x31 0x4D 0x34 0x50 0x45 0x32` (ASCII: `L21M4PE2`)
- `manufacturer`: `0x53 0x4D 0x50 0x00 0x32 0x30 0x32 0x31 0x00 0x00 0x00 0x00` (ASCII up to NUL: `SMP`)

**Root cause in AML:**
- The DSDT declares Revision `0x01` (32-bit `Integer` width).
- Battery helper methods (`\_SB.PCI0.LPC0.EC0.GBMN`, `GUBI`, or equivalents called by `_BIF` / `_BIX`) read an SMBus block (`SMD0`) into a 32-byte `Buffer` (`BFWB`), slice it with `CreateField (BFWB, Zero, <bits>, <field>)` where `<bits>` > `32` (e.g. `0x40` = 8 bytes, `0x60` = 12 bytes), and store `<field>` into a String-initialized `Name` object (`CMMA = FB70` / `BRMN = FBB0`).
- Because `<bits>` > 32 on an ACPI v1 table, `<field>` evaluates as `ACPI_TYPE_BUFFER`. Storing a `Buffer` into a `String` triggers ACPICA's implicit `Buffer -> String` conversion, formatting bytes as `"0x%02X "`.
- Replacing the assignment with explicit `ToString (BFWB, <byte_len>)` (`byte_len = bits // 8`) interprets the raw bytes as an ASCII string and stops at the first `0x00` NUL byte.

---

## 2. Repository Architecture

- [`patch_dsdt.py`](patch_dsdt.py): Python 3.10+ stdlib-only CLI with subcommands:
  - `check`: Read-only diagnostics (DMI, kernel lockdown, `CONFIG_ACPI_TABLE_UPGRADE`, battery sysfs, tools, initramfs hooks).
  - `decode`: Decodes `0xXX ...` strings or live `/sys/class/power_supply/BAT*` attributes.
  - `dump`: Copies `/sys/firmware/acpi/tables/{DSDT,SSDT*}` via `pkexec` (or root/`--sudo`).
  - `patch`: Decompiles `dsdt.dat` (`-e ssdt*.dat`), patches `_BIF`/`_BIX` helper methods, bumps `DefinitionBlock` OEM Revision (`+1`), compiles with `iasl -ve -tc` (recovering `Error 6022` duplicate `Case` blocks safely), runs round-trip disassembly in `verify/`, and smoke-tests in `acpiexec`.
  - `build-cpio`: Creates an uncompressed early `cpio` archive containing `kernel/firmware/acpi/dsdt.aml`.
- [`tests/test_patch_dsdt.py`](tests/test_patch_dsdt.py): Hermetic `unittest` suite (no root, no `/sys/firmware/acpi`, no `/boot` access).
- [`examples/yoga-6-13abr8-L5CN24WW.diff`](examples/yoga-6-13abr8-L5CN24WW.diff): Reference unified diff from a verified Yoga 6 13ABR8 (`83B2`, BIOS `L5CN24WW`).

---

## 3. Mandatory Pre-Flight Checks & Abort Conditions

Before patching a user's machine, run `./patch_dsdt.py check` and abort / switch to a userspace alternative if **any** of the following holds:

1. **Kernel Lockdown is active**: `/sys/kernel/security/lockdown` does NOT contain `[none]` (e.g., `[integrity]` or `[confidentiality]` with Secure Boot enabled). The Linux kernel blocks `CONFIG_ACPI_TABLE_UPGRADE` under lockdown.
2. **Kernel config lacks ACPI upgrade**: `CONFIG_ACPI_TABLE_UPGRADE` is not `y`.
3. **Missing tools**: `iasl` (`acpica`) is unavailable and cannot be installed.
4. **No hex battery strings**: Neither `model_name` nor `manufacturer` matches `0x[0-9A-Fa-f]{2}(?:\s+0x[0-9A-Fa-f]{2})+` (or the table already uses `ToString`).

---

## 4. Hard Safety Invariants

Agents working on this repository or running it on a live host MUST obey these rules:

1. **Never commit or distribute proprietary firmware dumps**: `.gitignore` excludes `*.dat`, `*.aml`, `*.dsl`, `*.hex`, `*.cpio`, `*.img`, `acpi_work/`, `work/`, and `verify/`. Never force-add them or reuse one laptop's compiled `dsdt.aml` on another model or BIOS version.
2. **Always increment the `DefinitionBlock` OEM Revision**: The kernel's `acpi_table_upgrade` ignores override tables whose OEM revision is not strictly greater than the firmware table's revision (`0x00000001` → `0x00000002`).
3. **Use `CreateField` bit-width ÷ 8 as the `ToString` length**: Never pass `Ones` to `ToString (BFWB, Ones)` when `BFWB` is a 32-byte shared `SMD0` buffer—unterminated strings like `L21M4PE2` (8 chars, no NUL at byte 8) would read trailing garbage bytes from `SMD0` bytes 8..31. Always pass `byte_len = bit_len // 8` (e.g., `0x08`, `0x0C`) and only patch `CreateField` slices starting at offset `Zero`.
4. **Handle `iasl` `Error 6022` conservatively**: Only remove a duplicate `Case (<val>)` block if both the original and duplicate `Case` share the exact same value AND lie directly inside the same brace-matched `Switch` block. Never suppress arbitrary compiler errors with `iasl -f`.
5. **Prefer `pkexec` on Linux desktops** for privileged commands (`dump`, copying to `/etc/initcpio/acpi_override/`, `mkinitcpio -P`) unless running headless or as root.
6. **Back up before modifying boot configs**: Back up `/etc/mkinitcpio.conf` (e.g., to `/etc/mkinitcpio.conf.bak`) or bootloader configs before editing them, and verify `kernel/firmware/acpi/dsdt.aml` exists in the generated initramfs/CPIO (`lsinitcpio` or `cpio -t`) before advising a reboot.
7. **Keep unit tests hermetic & stdlib-only**: `patch_dsdt.py` and `tests/test_patch_dsdt.py` must use only the Python standard library and never read `/sys/firmware/acpi` or touch `/boot` during unit tests.

---

## 5. Verification Commands

```bash
# Run hermetic unit tests (must pass with 0 failures)
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

# Read-only host diagnostics & decoding sanity check
./patch_dsdt.py check
./patch_dsdt.py decode "0x4C 0x32 0x31 0x4D 0x34 0x50 0x45 0x32" "0x53 0x4D 0x50 0x00 0x32 0x30 0x32 0x31 0x00 0x00 0x00 0x00"
```

After patching on a live machine and rebooting:

```bash
pkexec dmesg | grep -i "Table Upgrade"
cat /sys/class/power_supply/BAT*/model_name /sys/class/power_supply/BAT*/manufacturer
fastfetch -s battery
```
