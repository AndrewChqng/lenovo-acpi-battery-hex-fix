"""Unit tests for patch_dsdt.py."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import patch_dsdt


SAMPLE_DSL = textwrap.dedent(
    """\
    /*
     * Header comment
     */
    DefinitionBlock ("", "DSDT", 1, "LENOVO", "CB-01   ", 0x00000001)
    {
        Scope (_SB.PCI0.LPC0)
        {
            Scope (EC0)
            {
                Method (GBMN, 0, NotSerialized)
                {
                    Name (CMMA, "         ")
                    Name (BFWB, Buffer (0x20){})
                    CreateField (BFWB, Zero, 0x40, FB70)
                    If (ECOK ())
                    {
                        Local0 = ECSM (0x0B, 0x16, 0x21)
                        If (Local0)
                        {
                            BFWB = SMD0 /* \\_SB_.PCI0.LPC0.EC0_.SMD0 */
                            CMMA = FB70 /* \\_SB_.PCI0.LPC0.EC0_.GBMN.FB70 */
                        }
                    }
                    Return (CMMA)
                }

                Method (GUBI, 0, NotSerialized)
                {
                    Name (BRMN, "UNKNOWN ")
                    Name (BFWB, Buffer (0x20){})
                    CreateField (BFWB, Zero, 0x60, FBB0)
                    If (ECOK ())
                    {
                        Local0 = ECSM (0x0B, 0x16, 0x20)
                        If (Local0)
                        {
                            BFWB = SMD0
                            Store (FBB0, BRMN)
                        }
                    }
                    Return (BRMN)
                }

                Method (UNRL, 0, NotSerialized)
                {
                    Name (STR0, "        ")
                    Name (BUF0, Buffer (0x10){})
                    CreateField (BUF0, Zero, 0x40, FLD0)
                    STR0 = FLD0
                    Return (STR0)
                }
            }

            Device (BAT1)
            {
                Method (_BIF, 0, NotSerialized)
                {
                    Return (BIFX (Zero))
                }

                Method (_BIX, 0, NotSerialized)
                {
                    Return (BIFX (One))
                }

                Method (BIFX, 1, NotSerialized)
                {
                    Local0 = ^^EC0.GBMN ()
                    Local1 = ^^EC0.GUBI ()
                    Return (Local0)
                }
            }

            Method (DUPC, 1, NotSerialized)
            {
                Switch (Arg0)
                {
                    Case (Zero)
                    {
                        Local0 = One
                    }
                    Case (0x3000)
                    {
                        Local0 = 0x02
                    }
                    Case (Zero)
                    {
                        Local0 = 0x03
                    }
                    Default
                    {
                        Local0 = 0x04
                    }
                }
                Return (Local0)
            }
        }
    }
    """
)


class DecodeHexStringTests(unittest.TestCase):
    def test_decodes_model_name_and_nul_terminated_manufacturer(self) -> None:
        self.assertEqual(
            patch_dsdt.decode_hex_string("0x4C 0x32 0x31 0x4D 0x34 0x50 0x45 0x32"),
            (True, "L21M4PE2"),
        )
        self.assertEqual(
            patch_dsdt.decode_hex_string(
                "0x53 0x4D 0x50 0x00 0x32 0x30 0x32 0x31 0x00 0x00 0x00 0x00"
            ),
            (True, "SMP"),
        )

    def test_ignores_plain_strings(self) -> None:
        self.assertEqual(patch_dsdt.decode_hex_string("L21M4PE2"), (False, "L21M4PE2"))
        self.assertEqual(patch_dsdt.decode_hex_string("5900"), (False, "5900"))
        self.assertEqual(patch_dsdt.decode_hex_string("0x4C"), (False, "0x4C"))


class OemRevisionTests(unittest.TestCase):
    def test_bumps_hex_revision_and_decimal_revision(self) -> None:
        updated, old_rev, new_rev = patch_dsdt.bump_oem_revision(SAMPLE_DSL)
        self.assertEqual(old_rev, 1)
        self.assertEqual(new_rev, 2)
        self.assertIn(
            'DefinitionBlock ("", "DSDT", 1, "LENOVO", "CB-01   ", 0x00000002)',
            updated,
        )

        dec_dsl = 'DefinitionBlock ("", "DSDT", 1, "LENOVO", "TP-N1   ", 41)\n{\n}\n'
        updated2, old2, new2 = patch_dsdt.bump_oem_revision(dec_dsl)
        self.assertEqual((old2, new2), (41, 42))
        self.assertIn("0x0000002A", updated2)


class BatteryMethodPatchTests(unittest.TestCase):
    def test_discovers_only_battery_helpers(self) -> None:
        helpers = patch_dsdt.find_battery_helper_methods(SAMPLE_DSL)
        self.assertEqual(helpers, {"BIFX", "GBMN", "GUBI"})

    def test_patches_both_assign_and_store_and_leaves_unrelated_untouched(self) -> None:
        patched, records = patch_dsdt.patch_battery_methods(SAMPLE_DSL)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [(r.method, r.target_var, r.byte_length) for r in records],
            [("GBMN", "CMMA", 8), ("GUBI", "BRMN", 12)],
        )
        self.assertIn("CMMA = ToString (BFWB, 0x08)", patched)
        self.assertIn("BRMN = ToString (BFWB, 0x0C)", patched)
        self.assertIn("STR0 = FLD0", patched)

        # Idempotent when run again on already patched DSL
        patched_again, records_again = patch_dsdt.patch_battery_methods(patched)
        self.assertEqual(records_again, [])
        self.assertEqual(patched_again, patched)

    def test_all_methods_flag_patches_unrelated(self) -> None:
        patched, records = patch_dsdt.patch_battery_methods(SAMPLE_DSL, all_methods=True)
        self.assertEqual(len(records), 3)
        self.assertIn("STR0 = ToString (BUF0, 0x08)", patched)


class DuplicateCaseRecoveryTests(unittest.TestCase):
    def test_removes_duplicate_case_in_same_switch(self) -> None:
        lines = SAMPLE_DSL.splitlines()
        orig_line = next(i + 1 for i, ln in enumerate(lines) if "Case (Zero)" in ln)
        err_line = next(
            i + 1
            for i, ln in enumerate(lines)
            if i + 1 > orig_line and "Case (Zero)" in ln
        )
        updated, changed = patch_dsdt.remove_duplicate_case_at_line(
            SAMPLE_DSL, err_line=err_line, orig_line=orig_line
        )
        self.assertTrue(changed)
        self.assertEqual(updated.count("Case (Zero)"), 1)
        self.assertIn("Local0 = One", updated)
        self.assertNotIn("Local0 = 0x03", updated)
        self.assertIn("Default", updated)


class BuildCpioTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cpio"), "cpio binary required")
    def test_build_cpio_creates_early_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aml = root / "dsdt.aml"
            aml.write_bytes(b"DSDT_TEST_BYTES")
            out = root / "acpi_override.cpio"

            entries = patch_dsdt.build_cpio(aml, out)
            self.assertIn("kernel/firmware/acpi/dsdt.aml", entries)
            res = subprocess.run(
                ["cpio", "-t", "--quiet"],
                input=out.read_bytes(),
                capture_output=True,
                check=True,
            )
            self.assertIn(
                "kernel/firmware/acpi/dsdt.aml",
                res.stdout.decode("utf-8").splitlines(),
            )


if __name__ == "__main__":
    unittest.main()
