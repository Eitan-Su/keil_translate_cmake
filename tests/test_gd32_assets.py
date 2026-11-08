from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gd32_assets import select_assets
from keil_to_cmake import GenerationOptions, KeilProjectToCMake


class Gd32AssetsTests(unittest.TestCase):
    def fixture(self, root, device="GD32F307VC"):
        arm = root / "Firmware" / "Source" / "ARM" / "startup_gd32f30x_cl.s"
        gcc = arm.parent.parent / "GCC" / arm.with_suffix(".S").name
        linker = gcc.parent / "Ld" / f"{device[:8].lower()}x{device[9]}_flash.ld"
        source = root / "Example" / "src" / "main.c"
        project = root / "Example" / "MDK" / "Project.uvprojx"
        for file in (arm, gcc, linker, source, project):
            file.parent.mkdir(parents=True, exist_ok=True)
        arm.write_text("""Stack_Size EQU 0x400
Heap_Size EQU 0x200
 AREA RESET, DATA, READONLY
__Vectors DCD __initial_sp
 DCD Reset_Handler
 DCD NMI_Handler
__Vectors_End
 AREA |.text|, CODE, READONLY
Reset_Handler PROC
 LDR R0, =SystemInit
 BLX R0
 ENDP
""", encoding="utf-8")
        gcc.write_text(""".syntax unified
.section .vectors,"a",%progbits
.global __gVectors
__gVectors:
.word _sp
.word Reset_Handler
.word NMI_Handler
.size __gVectors, . - __gVectors
""", encoding="utf-8")
        linker.write_text("""MEMORY
{
 FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 256K
 RAM (xrw) : ORIGIN = 0x20000000, LENGTH = 96K
}
ENTRY(Reset_Handler)
SECTIONS
{
 __stack_size = DEFINED(__stack_size) ? __stack_size : 2K;
 __heap_size = DEFINED(__heap_size) ? __heap_size : 1K;
 .vectors : { KEEP(*(.vectors)) __Vectors_End = .; __Vectors_Size = . - __gVectors; } > FLASH
 .text : { *(.text*) } > FLASH
 _sidata = LOADADDR(.data);
 .data : { _sdata = .; *(.data*) _edata = .; } > RAM AT> FLASH
 .bss : { _sbss = .; *(.bss*) _ebss = .; } > RAM
 .heap_stack : { end = .; . += __heap_size; _heap_end = .; . += __stack_size; _sp = .; } > RAM
}
""", encoding="utf-8")
        source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        project.write_text(f"""<Project><Targets><Target><TargetName>Demo</TargetName>
<TargetOption><TargetCommonOption><Device>{device}</Device><Vendor>GigaDevice</Vendor>
<Cpu>IRAM(0x20000000,0x18000) IROM(0x08000000,0x40000) CPUTYPE("Cortex-M4")</Cpu>
<OutputName>demo</OutputName></TargetCommonOption><TargetArmAds>
<ArmAdsMisc><OnChipMemories>
<IROM><StartAddress>0x08000000</StartAddress><Size>0x40000</Size></IROM>
<IRAM><StartAddress>0x20000000</StartAddress><Size>0x18000</Size></IRAM>
</OnChipMemories></ArmAdsMisc><LDads><useFile>0</useFile></LDads>
</TargetArmAds></TargetOption><Groups><Group><GroupName>Sources</GroupName><Files>
<File><FileName>{arm.name}</FileName><FileType>2</FileType><FilePath>{arm.as_posix()}</FilePath></File>
<File><FileName>main.c</FileName><FileType>1</FileType><FilePath>{source.as_posix()}</FilePath></File>
</Files></Group></Groups></Target></Targets></Project>""", encoding="utf-8")
        return project, source, arm, gcc, linker

    def select(self, files, device="GD32F307VC"):
        project, source, arm, _, _ = files
        return select_assets(device, project, "Demo", [arm, source], arm,
                             ["__initial_sp", "Reset_Handler", "NMI_Handler"], (0x400, 0x200))

    def test_matches_device_instead_of_sibling_example_or_wrong_variant(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            files = self.fixture(root)
            wrong = files[4].with_name("gd32f303xE_flash.ld")
            wrong.write_text(files[4].read_text(), encoding="utf-8")
            sibling = root / "OtherExample" / files[4].name
            sibling.parent.mkdir()
            sibling.write_text("invalid sibling linker", encoding="utf-8")
            selected = self.select(files)
            self.assertEqual(selected.linker, files[4])
            self.assertEqual(selected.startup, files[3])

    def test_missing_pair_and_different_interrupt_table_are_rejected(self):
        with TemporaryDirectory() as directory:
            files = self.fixture(Path(directory))
            content = files[3].read_text()
            files[3].write_text(content.replace("NMI_Handler", "Other_Handler"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "interrupt vectors differ"):
                self.select(files)
            files[3].unlink()
            with self.assertRaisesRegex(ValueError, "pair missing"):
                self.select(files)

    def test_preserves_target_memory_stack_and_heap_with_different_sdk_defaults(self):
        with TemporaryDirectory() as directory:
            files = self.fixture(Path(directory))
            project = files[0]
            project.write_text(project.read_text().replace("<Size>0x18000</Size>", "<Size>0x30000</Size>"), encoding="utf-8")
            selected = self.select(files)
            self.assertIn("LENGTH = 0x30000", selected.content)
            self.assertIn("__stack_size = 0x400;", selected.content)
            self.assertIn("__heap_size = 0x200;", selected.content)
            self.assertTrue(selected.notes)
            self.assertIn("LENGTH = 96K", files[4].read_text())

    def test_fixed_data_uses_own_project_linker_and_retains_address(self):
        with TemporaryDirectory() as directory:
            files = self.fixture(Path(directory))
            files[1].write_text('''#if defined(__CC_ARM)
const char Disk[] __attribute__((at(0x08004000))) = {1};
#else
const char Disk[] __attribute__((section(".flash_data"))) = {1};
#endif
''', encoding="utf-8")
            local = files[1].parent / files[4].name
            local.write_text(files[4].read_text(), encoding="utf-8")
            selected = self.select(files)
            self.assertEqual(selected.linker, local.resolve())
            self.assertIn('.flash_data 0x08004000 : { KEEP(*(.flash_data)) }', selected.content)
            self.assertIn('ASSERT(ADDR(.flash_data)', selected.content)
            files[1].write_text(files[1].read_text().replace("0x08004000", "0x09000000"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside target FLASH"):
                self.select(files)

    def test_fixed_section_conflict_and_missing_pair_are_rejected(self):
        with TemporaryDirectory() as directory:
            files = self.fixture(Path(directory))
            files[1].write_text('const char Disk[] __attribute__((at(0x08004000))) = {1};', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fixed-address declaration"):
                self.select(files)
            with files[1].open("a", encoding="utf-8") as stream:
                stream.write('\nconst char Disk[] __attribute__((section(".flash_data"))) = {1};')
            with files[4].open("a", encoding="utf-8") as stream:
                stream.write('\n.flash_data 0x08008000 : { KEEP(*(.flash_data)) } > FLASH\n')
            with self.assertRaisesRegex(ValueError, "address mismatch"):
                self.select(files)

    def test_custom_scatter_and_multiple_regions_fail_explicitly(self):
        with TemporaryDirectory() as directory:
            files = self.fixture(Path(directory))
            original = files[0].read_text()
            for replacement in (
                original.replace("<useFile>0", "<useFile>1"),
                original.replace("<ArmAdsMisc>", "<ArmAdsMisc><hadIRAM2>1</hadIRAM2>"),
            ):
                files[0].write_text(replacement, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "explicit conversion"):
                    self.select(files)

    def test_gcc_generation_uses_the_pair_and_keeps_original_sources(self):
        with TemporaryDirectory() as directory, patch("keil_to_cmake.find_keil_mdk_root", return_value=None), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(directory)
            files = self.fixture(root)
            before = {p: p.read_bytes() for p in files}
            parser = KeilProjectToCMake(str(files[0]), uv4_path=str(root / "no-mdk"))
            self.assertTrue(parser.parse(), parser.last_error)
            self.assertTrue(parser.generate_cmake(str(root / "gcc"), generation_options=GenerationOptions(compiler="gcc")), parser.last_error)
            cmake = (root / "gcc" / "CMakeLists.txt").read_text()
            self.assertIn("startup_gd32f30x_cl.S", cmake)
            self.assertNotIn("startup_gd32f30x_cl_gcc.S", cmake)
            for file, data in before.items():
                self.assertEqual(file.read_bytes(), data)
            with patch("gd32_assets.select_assets", side_effect=AssertionError("GCC-only resolver")):
                self.assertTrue(parser.generate_cmake(str(root / "arm"), generation_options=GenerationOptions(compiler="armclang")), parser.last_error)

    def test_existing_gcc_startup_and_missing_linker_definitions(self):
        with TemporaryDirectory() as directory:
            files = self.fixture(Path(directory))
            result = select_assets("GD32F307VC", files[0], "Demo", [files[1], files[3]],
                                   files[3], [], (0x400, 0x200))
            self.assertEqual(result.startup, files[3])
            files[4].write_text(files[4].read_text().replace("_sp = .;", "/* _sp = .; */"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing _sp"):
                self.select(files)


if __name__ == "__main__":
    unittest.main()
