# Lenovo ACPI Battery Hex String Fix (`DSDT` Override)

Fixes the Linux battery reporting bug on Lenovo laptops (Yoga, IdeaPad, Legion, ThinkBook) where `/sys/class/power_supply/BAT*/model_name` and `manufacturer` display raw space-separated hex bytes instead of ASCII text:

| Field | Before (Firmware Bug) | After (Patched DSDT) |
| :--- | :--- | :--- |
| **`fastfetch -s battery`** | `Battery (0x4C 0x32 0x31 0x4D 0x34 0x50 0x45 0x32): 55%` | `Battery (L21M4PE2): 55%` |
| **`model_name`** | `0x4C 0x32 0x31 0x4D 0x34 0x50 0x45 0x32` | `L21M4PE2` |
| **`manufacturer`** | `0x53 0x4D 0x50 0x00 0x32 0x30 0x32 0x31 0x00 0x00 0x00 0x00` | `SMP` |

Unlike a shell alias or `fastfetch` config override, overriding the ACPI `DSDT` fixes `/sys/class/power_supply/BAT*/` **system-wide** for `fastfetch`, `upower`, `inxi`, `powerprofilesctl`, and desktop battery widgets.

---

## Why This Happens (Root Cause)

In affected Lenovo BIOS firmware, the `DSDT` table header declares **Revision 1** (32-bit integer semantics), and the Embedded Controller battery methods (`GBMN` for model name and `GUBI` for manufacturer) read SMBus string blocks into a 32-byte `Buffer` and copy an 8-byte (`0x40` bits) or 12-byte (`0x60` bits) `BufferField` directly into a `Name` string object:

```asl
Name (CMMA, "         ")
Name (BFWB, Buffer (0x20){})
CreateField (BFWB, Zero, 0x40, FB70)
...
BFWB = SMD0
CMMA = FB70   // Implicit Buffer -> String conversion!
```

Because `0x40` bits (64 bits) and `0x60` bits (96 bits) exceed 32 bits, ACPICA evaluates `FB70` / `FBB0` as an `ACPI_TYPE_BUFFER`. Assigning a `Buffer` into an existing `String` object triggers ACPICA's implicit `Buffer -> String` conversion (`acpi_ex_convert_to_string`), which formats each byte as space-separated hex (`0x4C 0x32 ...`).

Replacing those implicit assignments with explicit **`ToString (BFWB, <byte_length>)`** tells ACPICA to interpret the buffer bytes as a NUL-terminated ASCII string (`L21M4PE2` and `SMP`, stopping cleanly at the `0x00` byte after `SMP`).

---

## Verified Hardware & Environment

Tested end-to-end across reboot on:

- **Laptop**: Lenovo **Yoga 6 13ABR8** (`83B2`)
- **BIOS**: `L5CN24WW` (`09/26/2024`)
- **OS / Kernel**: CachyOS Linux (`7.3.5-2-cachyos`, `CONFIG_ACPI_TABLE_UPGRADE=y`)
- **Initramfs / Bootloader**: `mkinitcpio` (`acpi_override` hook) + `systemd-boot`
- **Reference Diff**: [`examples/yoga-6-13abr8-L5CN24WW.diff`](examples/yoga-6-13abr8-L5CN24WW.diff)

---

## Prerequisites & Important Warnings

> [!CAUTION]
> **Never share or flash another machine's compiled `dsdt.aml`.** ACPI tables are specific to your exact laptop model, hardware configuration, and BIOS version. Always dump, patch, and compile your own system's `/sys/firmware/acpi/tables/DSDT`.

1. **Kernel support (`CONFIG_ACPI_TABLE_UPGRADE=y`)**: Enabled on most Arch/CachyOS/Fedora/Ubuntu kernels.
2. **Kernel Lockdown / Secure Boot**: `CONFIG_ACPI_TABLE_UPGRADE` is **disabled by the kernel when Lockdown is in `integrity` or `confidentiality` mode** (commonly triggered by UEFI Secure Boot). `/sys/kernel/security/lockdown` must show `[none]`.
3. **BIOS updates**: Re-dump and re-patch your DSDT if a BIOS firmware update changes your ACPI tables.
4. **Required tools**: `iasl` (from `acpica`), `cpio`, and Python 3.10+ (stdlib only — no third-party Python dependencies).

Run the built-in pre-flight check first:

```bash
git clone https://github.com/AndrewChqng/lenovo-acpi-battery-hex-fix.git
cd lenovo-acpi-battery-hex-fix
./patch_dsdt.py check
```

---

## Quick Start (Automated Script)

[`patch_dsdt.py`](patch_dsdt.py) automates dumping your live ACPI tables, locating the `_BIF`/`_BIX` battery helpers (`GBMN`, `GUBI`, etc.), patching implicit `Buffer -> String` assignments to `ToString (...)`, incrementing the `DefinitionBlock` OEM revision, compiling with `iasl`, and running round-trip + `acpiexec` verification.

### 1. Install `acpica`

```bash
# Arch Linux / CachyOS
pkexec pacman -S --needed acpica

# Fedora
sudo dnf install acpica-tools

# Debian / Ubuntu
sudo apt install acpica-tools
```

### 2. Dump Your ACPI Tables

```bash
./patch_dsdt.py dump
# Creates acpi_work/dsdt.dat and acpi_work/ssdt*.dat (uses pkexec; pass --sudo if headless)
```

### 3. Patch, Compile, and Verify

```bash
./patch_dsdt.py patch
```

Preview the patch first without writing or compiling by passing `--dry-run`.

What `patch` does automatically:
- Disassembles `acpi_work/dsdt.dat` using `ssdt*.dat` for external symbol resolution (`iasl -e ssdt*.dat -d dsdt.dat`).
- Bumps the `DefinitionBlock` OEM Revision by `+1` (required for the kernel to accept the override).
- Replaces `CMMA = FB70` -> `CMMA = ToString (BFWB, 0x08)` and `BRMN = FBB0` -> `BRMN = ToString (BFWB, 0x0C)` (deriving byte widths directly from `CreateField`).
- If Lenovo's decompiled DSDT hits `iasl` `Error 6022 - Case value already specified` (a known BIOS AML quirk where a redundant `Case (Zero)` appears alongside `Default` in the same `Switch`), safely removes the shadowed duplicate block.
- Compiles `acpi_work/dsdt.aml`, re-disassembles it into `acpi_work/verify/` to confirm round-trip integrity, and loads it in `acpiexec` if installed.

---

## Bootloader / Initramfs Integration

### Option A: Arch Linux / CachyOS (`mkinitcpio` `acpi_override` hook — Verified)

1. Copy the compiled AML to `/etc/initcpio/acpi_override/`:
   ```bash
   pkexec sh -c '
     mkdir -p /etc/initcpio/acpi_override
     cp acpi_work/dsdt.aml /etc/initcpio/acpi_override/dsdt.aml
     chmod 644 /etc/initcpio/acpi_override/dsdt.aml
   '
   ```
2. Back up `/etc/mkinitcpio.conf` and add `acpi_override` to `HOOKS` (after `microcode`):
   ```bash
   # Example:
   HOOKS=(base systemd autodetect microcode acpi_override kms modconf block keyboard sd-vconsole plymouth filesystems)
   ```
3. Regenerate your initramfs and verify `kernel/firmware/acpi/dsdt.aml` is included in the early CPIO:
   ```bash
   pkexec sh -c 'mkinitcpio -P && lsinitcpio /boot/initramfs-linux-cachyos.img | grep -i acpi'
   ```
   Expected line in output:
   ```text
   kernel/firmware/acpi/dsdt.aml
   ```

### Option B: Universal Standalone Early CPIO (`systemd-boot`, `GRUB`, `Limine`)

For distributions without `mkinitcpio`'s `acpi_override` hook, build a standalone uncompressed early CPIO image:

```bash
./patch_dsdt.py build-cpio --aml acpi_work/dsdt.aml --output acpi_work/acpi_override.cpio
```

Then copy `acpi_work/acpi_override.cpio` to `/boot/acpi_override.cpio` and prepend it as the **first** `initrd` before your main initramfs:

- **`systemd-boot`** (`/boot/loader/entries/*.conf`):
  ```text
  initrd  /acpi_override.cpio
  initrd  /initramfs-linux.img
  ```
- **`GRUB`** (`/boot/grub/grub.cfg` or `/etc/grub.d/40_custom`):
  ```text
  initrd  /acpi_override.cpio /initramfs-linux.img
  ```
- **`Limine`** (`limine.conf`):
  ```text
  module_path: boot():/acpi_override.cpio
  module_path: boot():/initramfs-linux.img
  ```

### Option C: Fedora / openSUSE (`dracut`)

Dracut supports ACPI overrides directly via `/etc/dracut.conf.d/acpi.conf`:

```text
acpi_override="yes"
acpi_table_dir="/etc/acpi_override"
```

Place `acpi_work/dsdt.aml` in `/etc/acpi_override/dsdt.aml` and run `sudo dracut --force --regenerate-all`.

---

## Using with AI Coding Agents

This repository includes [`AGENTS.md`](AGENTS.md) with the exact ACPI diagnostics, safety invariants (lockdown checks, OEM revision rules, `CreateField` bit-to-byte math, `Error 6022` recovery rules, and initramfs verification), and commands so an autonomous coding agent (Claude Code, Codex, Antigravity, Gemini CLI, etc.) can safely inspect your laptop and apply or adapt this fix.

---

## Manual Step-by-Step Guide (Without `patch_dsdt.py`)

<details>
<summary>Expand manual iasl commands</summary>

1. Dump tables:
   ```bash
   mkdir -p acpi_work && cd acpi_work
   pkexec sh -c "cp /sys/firmware/acpi/tables/DSDT dsdt.dat; for f in /sys/firmware/acpi/tables/SSDT*; do cp \"\$f\" \$(basename \"\$f\" | tr A-Z a-z).dat; done; chown -R $USER:$USER ."
   ```
2. Decompile with SSDT external resolution:
   ```bash
   iasl -e ssdt*.dat -d dsdt.dat
   ```
3. Edit `dsdt.dsl`:
   - Increment the 6th argument of `DefinitionBlock` (`0x00000001` → `0x00000002`).
   - Inside `Method (GBMN)` (model name), change `CMMA = FB70` to `CMMA = ToString (BFWB, 0x08)` (or byte count matching `CreateField` bit width `÷ 8`).
   - Inside `Method (GUBI)` (manufacturer), change `BRMN = FBB0` to `BRMN = ToString (BFWB, 0x0C)`.
   - If `iasl` fails with `Error 6022 - Case value already specified`, check that the later `Case` is shadowed by an identical earlier `Case` in the same `Switch` and remove the unreachable duplicate block.
4. Compile and verify:
   ```bash
   iasl -ve -tc dsdt.dsl
   ```
5. Install `dsdt.aml` into your initramfs using Option A, B, or C above.

</details>

---

## Verification & Rollback

### Verify After Reboot

```bash
# Confirm kernel loaded the overridden DSDT (OEM revision bumped)
pkexec dmesg | grep -i "Table Upgrade"

# Confirm clean ASCII strings in sysfs and fastfetch
cat /sys/class/power_supply/BAT*/model_name /sys/class/power_supply/BAT*/manufacturer
fastfetch -s battery
```

### Rollback

- **Arch / CachyOS (`mkinitcpio`)**:
  ```bash
  pkexec sh -c '
    rm -f /etc/initcpio/acpi_override/dsdt.aml
    cp /etc/mkinitcpio.conf.bak /etc/mkinitcpio.conf
    mkinitcpio -P
  '
  ```
- **Standalone CPIO**: Remove the `acpi_override.cpio` `initrd` / `module_path` line from your bootloader entry and delete `/boot/acpi_override.cpio`.

---

## Development & Tests

```bash
# Run unit tests (hermetic, stdlib-only)
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

---

## Credits & AI Disclaimer

Diagnosed, implemented, verified, and documented with assistance from **Antigravity (Google DeepMind)**:

- Traced the Lenovo Yoga 6 13ABR8 (`L5CN24WW`) hex battery strings (`0x4C 0x32 ...` and `0x53 0x4D 0x50 0x00 ...`) by decompiling `/sys/firmware/acpi/tables/DSDT` with `iasl` cross-referenced against `SSDT*` tables, identifying implicit `BufferField -> String` conversions in `\_SB.PCI0.LPC0.EC0.GBMN` (`0x40` bits) and `GUBI` (`0x60` bits) on an ACPI v1 table, patching them to `ToString (BFWB, 0x08)` / `ToString (BFWB, 0x0C)`, bumping the DSDT OEM revision, verifying via round-trip disassembly + `acpiexec` + reboot on CachyOS (`7.3.5-2-cachyos`), and packaging the reusable [`patch_dsdt.py`](patch_dsdt.py) utility.

> [!WARNING]
> **Experimental / AI-Assisted Firmware Tool Disclaimer:** This repository contains experimental, AI-assisted ACPI table analysis and patching tooling provided "as-is" without warranty of any kind. Incorrect ACPI DSDT overrides can prevent a system from booting or cause power-management issues; always maintain a recovery boot entry or live USB and verify your compiled table before rebooting.
