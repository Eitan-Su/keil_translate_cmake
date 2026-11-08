import io
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from slib_layout import parse_layout, read_symdefs, render_linker, runtime_source
from keil_to_cmake import GenerationOptions, KeilProjectToCMake


PRODUCER = """
LR_IROM1 0x08000000 0x3c000 {
 ER_IROM1 0x08000000 0x3c000 {
  *.o (RESET, +First) *(InRoot$$Sections) .ANY (+RO)
 }
 RW_IRAM1 0x20000000 0x1000 { fir_filter.o (+RW +ZI) }
 RW_IRAM2 0x20001000 0x7000 { .ANY (+RW +ZI) }
}
LR_SLIB_C_LIB 0x0803c000 0x2000 {
 ER_SLIB_C_LIB 0x0803c000 0x2000 { *armlib* (+RO) }
}
LR_SLIB_INST 0x0803e000 0x1000 {
 ER_SLIB_INST 0x0803e000 0x1000 { fir_filter.o (+XO) arm_fir.o (+XO) }
}
LR_SLIB_DATA 0x0803f000 0x1000 {
 ER_SLIB_DATA 0x0803f000 0x1000 { coefficients.o (+RO) }
}
"""
CONSUMER = """
LR_IROM1 0x08000000 0x3c000 {
 ER_IROM1 0x08000000 0x3c000 {
  *.o (RESET, +First) *(InRoot$$Sections) .ANY (+RO)
 }
 RW_IRAM1 0x20001000 0x7000 { .ANY (+RW +ZI) }
}
"""


class SlibLayoutTests(unittest.TestCase):
    def layout(self, content=PRODUCER, symbols=None):
        return parse_layout(content, symbols or {"FIR_lowpass_filter": 0x0803e001},
                            (0x08000000, 0x40000), (0x20000000, 0x8000))

    def test_pair_reserves_same_private_ram_and_nonoverlapping_flash(self):
        producer, consumer = self.layout(), self.layout(CONSUMER)
        self.assertEqual(producer.reserved_ram, consumer.reserved_ram)
        self.assertEqual(producer.ram.origin, 0x20001000)
        self.assertEqual(consumer.flash.end, producer.support.origin)
        self.assertEqual(producer.support.end, producer.code.origin)
        self.assertEqual(producer.code.end, producer.constants.origin)
        self.assertEqual(producer.ram.end, 0x20008000)
        self.assertEqual(runtime_source(producer), runtime_source(consumer))

    def test_rejects_overlapping_or_out_of_device_regions(self):
        for text in (PRODUCER.replace("0x20001000", "0x20000000"),
                     PRODUCER.replace("0x0803e000", "0x0803d000"),
                     PRODUCER.replace("0x7000", "0x9000")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.layout(text)

    def test_rejects_unknown_scatter_semantics(self):
        for text in (PRODUCER.replace("(+XO)", "(+RO)"),
                     PRODUCER.replace("RW_IRAM1 0x20000000", "RW_IRAM1 +0"),
                     PRODUCER.replace("0x20000000 0x1000", "0x20000000 UNINIT 0x1000"),
                     PRODUCER.replace("*armlib*", "*custom*"),
                     PRODUCER.replace("RW_IRAM2 0x20001000", "RW_IRAM1 0x20001000")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.layout(text)

    def test_exports_require_unique_thumb_functions_inside_protected_code(self):
        for table in ("#<SYMDEFS>#\n0x0803e000 T entry\n",
                      "#<SYMDEFS>#\n0x0803e001 D entry\n",
                      "#<SYMDEFS>#\n0x0803e001 T entry\n0x0803e003 T entry\n"):
            with self.subTest(table=table), self.assertRaises(ValueError):
                read_symdefs(table)
        self.assertEqual(read_symdefs("#<SYMDEFS>#\n0x0803e001 T entry\n"),
                         {"entry": 0x0803e001})
        with self.assertRaises(ValueError):
            self.layout(symbols={"entry": 0x08001001})
        with self.assertRaises(ValueError):
            self.layout(CONSUMER, symbols={"entry": 0x08001001})

    def test_linker_pins_exports_and_reserves_private_state_before_general_sections(self):
        base = "MEMORY { FLASH (rx) : ORIGIN = 0, LENGTH = 0x40000 }\nSECTIONS { .text : { *(.text*) } > FLASH }"
        text = render_linker(self.layout(), base,
                             {"fir_filter": "fir_filter.c", "arm_fir": "arm_fir.c", "coefficients": "coefficients.c"})
        self.assertIn(".slib_api_0 0x0803E000", text)
        self.assertIn("ASSERT((FIR_lowpass_filter | 1) == 0x0803E001", text)
        self.assertIn("*fir_filter.c.obj(.text.FIR_lowpass_filter)", text)
        self.assertIn("*fir_filter.c.o(.text.FIR_lowpass_filter)", text)
        self.assertLess(text.index(".slib_bss"), text.index(".text :"))
        self.assertIn("ASSERT(SIZEOF(.slib_initialized_data) == 0", text)

    def test_ordinary_symdefs_do_not_enable_slib_conversion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scatter = root / "layout.sct"
            scatter.write_text(CONSUMER)
            symbols = root / "symbols.txt"
            symbols.write_text("#<SYMDEFS>#\n0x0803e001 T entry\n")
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.device_name = "AT32F415RCT7"
            parser.linker_script = str(scatter)
            parser.linker_inputs = [str(symbols)]
            parser.flash_origin, parser.flash_length = 0x08000000, 0x40000
            parser.ram_origin, parser.ram_length = 0x20000000, 0x8000
            self.assertIsNone(parser._read_slib_layout())
            scatter.write_text(CONSUMER + "; RAM reserved for SLIB code\n")
            self.assertIsNotNone(parser._read_slib_layout())

    def test_converter_preserves_originals_and_keeps_slib_rules_out_of_armclang(self):
        with TemporaryDirectory() as directory, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(directory)
            scatter = root / "layout.sct"
            scatter.write_text(PRODUCER, encoding="utf-8")
            symdefs = root / "exports.txt"
            symdefs.write_text("#<SYMDEFS>#\n0x0803e001 T FIR_lowpass_filter\n")
            for name in ("fir_filter", "arm_fir", "coefficients"):
                (root / f"{name}.c").write_text(f"int {name}(void) {{ return 0; }}\n")
            project = root / "sample.uvprojx"
            files = "".join(f"<File><FileName>{name}.c</FileName><FileType>1</FileType><FilePath>{name}.c</FilePath></File>"
                            for name in ("fir_filter", "arm_fir", "coefficients"))
            project.write_text(f'''<Project><Targets><Target><TargetName>SLIB</TargetName>
<TargetOption><TargetCommonOption><Device>AT32F415RCT7</Device><Vendor>ArteryTek</Vendor>
<Cpu>IRAM(0x20000000,0x8000) IROM(0x08000000,0x40000) CPUTYPE("Cortex-M4") ELITTLE</Cpu>
<OutputName>slib</OutputName></TargetCommonOption>
<TargetArmAds><LDads><ScatterFile>layout.sct</ScatterFile><Misc>--symdefs=exports.txt</Misc></LDads></TargetArmAds>
</TargetOption><Groups><Group><Files>{files}</Files></Group></Groups>
</Target></Targets></Project>''', encoding="utf-8")
            originals = {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
            parser = KeilProjectToCMake(str(project))
            self.assertTrue(parser.parse(), parser.last_error)
            for name in ("first", "second"):
                output = root / name
                self.assertTrue(parser.generate_cmake(str(output), False, GenerationOptions(compiler="gcc")),
                                parser.last_error)
                cmake = (output / "CMakeLists.txt").read_text(encoding="utf-8")
                self.assertIn("-mpure-code -fno-jump-tables -fno-lto", cmake)
                self.assertIn("slib_exports.symdefs", cmake)
                self.assertIn("slib_ram_init.c", cmake)
                self.assertIn("ORIGIN = 0x20001000", (output / "cmake" / "generated_linker.ld").read_text())
                self.assertEqual(len(parser.source_files), 3)
            stream = io.StringIO()
            parser._write_armclang_cmake_content(stream, str(root / "arm"), GenerationOptions(compiler="armclang"))
            self.assertNotIn("-mpure-code", stream.getvalue())
            self.assertNotIn("slib_ram_init", stream.getvalue())
            self.assertIn("layout.sct", stream.getvalue())
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)

    @unittest.skipUnless(shutil.which("arm-none-eabi-gcc") and shutil.which("arm-none-eabi-ld"),
                         "GNU Arm tools required")
    def test_linker_rejects_initialized_private_data_and_protected_code_overflow(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = "MEMORY { FLASH (rx) : ORIGIN = 0, LENGTH = 1 }\nSECTIONS { .text : { *(.text*) } > FLASH }"
            script = root / "layout.ld"
            script.write_text(render_linker(self.layout(), base,
                {"fir_filter": "fir_filter.c", "arm_fir": "arm_fir.c", "coefficients": "coefficients.c"}))
            cases = [
                ("int state = 7; int FIR_lowpass_filter(void) { return state; }",
                 "SLIB initialized private data"),
                ('void FIR_lowpass_filter(void) { __asm volatile (".space 5000"); }',
                 "SLIB_CODE"),
            ]
            for index, (content, expected) in enumerate(cases):
                with self.subTest(expected=expected):
                    source = root / "fir_filter.c"
                    source.write_text(content)
                    compiled = subprocess.run([
                        "arm-none-eabi-gcc", "-c", "-mcpu=cortex-m4", "-mthumb",
                        "-ffunction-sections", "-fdata-sections", str(source),
                        "-o", str(root / "fir_filter.c.obj"),
                    ], cwd=root, capture_output=True, text=True, timeout=30)
                    self.assertEqual(compiled.returncode, 0, compiled.stderr)
                    linked = subprocess.run([
                        "arm-none-eabi-ld", "-T", str(script), str(root / "fir_filter.c.obj"),
                        "-o", str(root / f"rejected-{index}.elf"),
                    ], cwd=root, capture_output=True, text=True, timeout=30)
                    self.assertNotEqual(linked.returncode, 0)
                    self.assertIn(expected, linked.stderr)


if __name__ == "__main__":
    unittest.main()
