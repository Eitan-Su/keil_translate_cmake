import io
import json
import textwrap
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from keil_to_cmake import BuildValidator, GenerationOptions, KeilProjectToCMake


class KeilToCMakeTests(unittest.TestCase):
    def _write_minimal_project(self, root: Path) -> Path:
        project_dir = root / "Project" / "MDK"
        project_dir.mkdir(parents=True)
        source_dir = root / "Src"
        include_dir = root / "Inc"
        source_dir.mkdir()
        include_dir.mkdir()
        (source_dir / "main.c").write_text(
            '#include "app.h"\nint main(void) { return APP_VALUE; }\n',
            encoding="utf-8",
        )
        (source_dir / "excluded.c").write_text(
            "int excluded(void) { return 1; }\n",
            encoding="utf-8",
        )
        (include_dir / "app.h").write_text(
            "#define APP_VALUE 0\n",
            encoding="utf-8",
        )

        uvprojx = project_dir / "sample.uvprojx"
        uvprojx.write_text(
            textwrap.dedent(
                """\
                <?xml version="1.0" encoding="UTF-8" standalone="no" ?>
                <Project>
                  <Targets>
                    <Target>
                      <TargetName>First</TargetName>
                      <TargetOption>
                        <TargetCommonOption>
                          <Device>STM32F103C8</Device>
                          <Vendor>STMicroelectronics</Vendor>
                          <Cpu>CPUTYPE("Cortex-M3") ELITTLE</Cpu>
                          <OutputDirectory>Objects</OutputDirectory>
                          <OutputName>first_app</OutputName>
                        </TargetCommonOption>
                      </TargetOption>
                    </Target>
                    <Target>
                      <TargetName>Second</TargetName>
                      <TargetOption>
                        <TargetCommonOption>
                          <Device>STM32F407ZG</Device>
                          <Vendor>STMicroelectronics</Vendor>
                          <Cpu>CPUTYPE("Cortex-M4") FPU2 ELITTLE</Cpu>
                          <OutputDirectory>Objects</OutputDirectory>
                          <OutputName>second_app</OutputName>
                        </TargetCommonOption>
                        <TargetArmAds>
                          <ArmAdsMisc>
                            <OnChipMemories>
                              <IRAM>
                                <StartAddress>0x20000000</StartAddress>
                                <Size>0x20000</Size>
                              </IRAM>
                              <IROM>
                                <StartAddress>0x08000000</StartAddress>
                                <Size>0x100000</Size>
                              </IROM>
                            </OnChipMemories>
                            <RvdsVP>2</RvdsVP>
                          </ArmAdsMisc>
                          <Cads>
                            <VariousControls>
                              <Define>USE_STDPERIPH_DRIVER,STM32F40_41xxx,</Define>
                              <IncludePath>..\\..\\Inc</IncludePath>
                            </VariousControls>
                          </Cads>
                          <LDads>
                            <ScatterFile></ScatterFile>
                          </LDads>
                        </TargetArmAds>
                      </TargetOption>
                      <Groups>
                        <Group>
                          <GroupName>User</GroupName>
                          <Files>
                            <File>
                              <FileName>main.c</FileName>
                              <FileType>1</FileType>
                              <FilePath>..\\..\\Src\\main.c</FilePath>
                            </File>
                            <File>
                              <FileName>excluded.c</FileName>
                              <FileType>1</FileType>
                              <FilePath>..\\..\\Src\\excluded.c</FilePath>
                              <FileOption>
                                <CommonProperty>
                                  <IncludeInBuild>0</IncludeInBuild>
                                </CommonProperty>
                              </FileOption>
                            </File>
                          </Files>
                        </Group>
                      </Groups>
                    </Target>
                  </Targets>
                  <RTE>
                    <apis/>
                    <components/>
                    <files/>
                  </RTE>
                </Project>
                """
            ),
            encoding="utf-8",
        )
        return uvprojx

    def test_generates_gcc_project_without_mdk_and_filters_disabled_files(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            output_dir = root / "out"

            with patch("keil_to_cmake.find_keil_mdk_root", return_value=None), patch(
                "keil_to_cmake.get_keil_mdk_info", return_value=None
            ):
                parser = KeilProjectToCMake(
                    str(uvprojx),
                    str(root / "missing_mdk"),
                    target_name="Second",
                )

            self.assertTrue(parser.parse(), parser.last_error)
            self.assertEqual(parser.target_name, "Second")
            self.assertEqual(parser.output_name, "second_app")
            self.assertEqual(parser.flash_origin, 0x08000000)
            self.assertEqual(parser.flash_length, 0x100000)
            self.assertTrue(any(path.endswith("main.c") for path in parser.source_files))
            self.assertFalse(
                any(path.endswith("excluded.c") for path in parser.source_files)
            )
            self.assertTrue(any("RTEPATH" in warning for warning in parser.warnings))

            options = GenerationOptions(compiler="gcc", generator="Ninja")
            self.assertTrue(parser.generate_cmake(str(output_dir), False, options))

            cmake_text = (output_dir / "CMakeLists.txt").read_text(encoding="utf-8")
            normalized_cmake_text = cmake_text.replace("\\", "/")
            self.assertIn("set(CMAKE_OBJECT_PATH_MAX 140)", cmake_text)
            self.assertIn("cmake/sources/main_", normalized_cmake_text)
            self.assertIn('"${CMAKE_CURRENT_LIST_DIR}/../Src"', normalized_cmake_text)
            self.assertIn("STM32F40_41xxx", cmake_text)
            self.assertIn("STM32F407ZG", cmake_text)
            self.assertNotIn("excluded.c", cmake_text)
            self.assertTrue((output_dir / "cmake" / "generated_linker.ld").is_file())
            self.assertTrue(any((output_dir / "cmake" / "sources").glob("main_*.c")))

            report = json.loads(
                (output_dir / "conversion-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["target_name"], "Second")
            self.assertEqual(report["compiler"], "gcc")
            linker_assets = [
                asset
                for asset in report["assets"]
                if asset["kind"] == "linker_script"
            ]
            self.assertEqual(len(linker_assets), 1)
            self.assertEqual(linker_assets[0]["source"], "generated")
            self.assertEqual(linker_assets[0]["support_level"], "review_required")

    def test_parse_fails_for_unknown_target_name(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)

            with patch("keil_to_cmake.find_keil_mdk_root", return_value=None), patch(
                "keil_to_cmake.get_keil_mdk_info", return_value=None
            ):
                parser = KeilProjectToCMake(
                    str(uvprojx),
                    str(root / "missing_mdk"),
                    target_name="Missing",
                )

            with redirect_stderr(io.StringIO()):
                self.assertFalse(parser.parse())
            self.assertIn("TargetName=Missing", parser.last_error)
            self.assertIn("First", parser.last_error)
            self.assertIn("Second", parser.last_error)

    def test_gcc_stages_sources_from_non_ascii_paths(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "中文工程"
            uvprojx = self._write_minimal_project(root)
            output_dir = Path(temp_dir) / "out"

            with patch("keil_to_cmake.find_keil_mdk_root", return_value=None), patch(
                "keil_to_cmake.get_keil_mdk_info", return_value=None
            ):
                parser = KeilProjectToCMake(
                    str(uvprojx),
                    str(root / "missing_mdk"),
                    target_name="Second",
                )

            self.assertTrue(parser.parse(), parser.last_error)
            self.assertTrue(
                parser.generate_cmake(
                    str(output_dir),
                    False,
                    GenerationOptions(compiler="gcc", generator="Ninja"),
                )
            )

            cmake_text = (output_dir / "CMakeLists.txt").read_text(encoding="utf-8")
            source_block = cmake_text.split("set(PROJECT_SOURCES", 1)[1].split(")", 1)[0]
            self.assertNotIn("中文工程", source_block)
            self.assertRegex(
                source_block.replace("\\", "/"),
                r'cmake/sources/main_[0-9a-f]{12}\.c',
            )

    def test_gcc_preserves_keil_optimization_policy(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            project_text = uvprojx.read_text(encoding="utf-8")
            project_text = project_text.replace(
                "<Cads>",
                "<Cads><Optim>4</Optim><oTime>0</oTime>",
                1,
            )
            uvprojx.write_text(project_text, encoding="utf-8")

            with patch("keil_to_cmake.find_keil_mdk_root", return_value=None), patch(
                "keil_to_cmake.get_keil_mdk_info", return_value=None
            ):
                parser = KeilProjectToCMake(
                    str(uvprojx),
                    str(root / "missing_mdk"),
                    target_name="Second",
                )

            self.assertTrue(parser.parse(), parser.last_error)
            output_dir = root / "out"
            self.assertTrue(
                parser.generate_cmake(
                    str(output_dir),
                    False,
                    GenerationOptions(compiler="gcc", generator="Ninja"),
                )
            )

            cmake_text = (output_dir / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn("    -Os\n", cmake_text)
            self.assertNotIn("$<$<CONFIG:Debug>:-Og>", cmake_text)
            self.assertIn("$<$<CONFIG:Debug>:-g3>", cmake_text)

            parser.keil_optimize_for_time = "1"
            self.assertEqual(parser._gcc_optimization_flag(), "-O3")

    def test_gcc_linker_candidate_rejects_generated_symbol_fragment(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            symbol_fragment = root / "cmake-generated" / "cmake" / "symbol_test.ld"
            symbol_fragment.parent.mkdir(parents=True)
            symbol_fragment.write_text("PROVIDE(test_symbol = 0x1234);\n", encoding="utf-8")
            vendor_script = root / "sdk" / "device_flash.ld"
            vendor_script.parent.mkdir()
            vendor_script.write_text(
                "MEMORY { FLASH (rx) : ORIGIN = 0, LENGTH = 64K }\n"
                "SECTIONS { .text : { *(.text*) } > FLASH }\n",
                encoding="utf-8",
            )

            self.assertFalse(
                KeilProjectToCMake._is_gcc_linker_script_candidate(symbol_fragment)
            )
            self.assertTrue(
                KeilProjectToCMake._is_gcc_linker_script_candidate(vendor_script)
            )

            ride_script = root / "RIDE" / "device_flash.ld"
            ride_script.parent.mkdir()
            ride_script.write_text(
                "SECTIONS { .text : { *(.text*) } }\n", encoding="utf-8"
            )
            self.assertFalse(
                KeilProjectToCMake._is_gcc_linker_script_candidate(ride_script)
            )

    def test_gcc_uses_nordic_linker_beside_gcc_startup_and_copies_includes(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            mdk_dir = root / "sdk" / "modules" / "nrfx" / "mdk"
            mdk_dir.mkdir(parents=True)
            startup = mdk_dir / "gcc_startup_nrf52.S"
            system = mdk_dir / "system_nrf52.c"
            device_linker = mdk_dir / "nrf52832_xxaa.ld"
            common_linker = mdk_dir / "nrf_common.ld"
            startup.write_text(".syntax unified\n", encoding="utf-8")
            system.write_text("void SystemInit(void) {}\n", encoding="utf-8")
            device_linker.write_text(
                "MEMORY { FLASH (rx) : ORIGIN = 0, LENGTH = 512K }\n"
                'INCLUDE "nrf_common.ld"\n',
                encoding="utf-8",
            )
            common_linker.write_text(
                "SECTIONS { .text : { *(.text*) } > FLASH }\n",
                encoding="utf-8",
            )

            with patch("keil_to_cmake.find_keil_mdk_root", return_value=None), patch(
                "keil_to_cmake.get_keil_mdk_info", return_value=None
            ):
                parser = KeilProjectToCMake(
                    str(uvprojx),
                    str(root / "missing_mdk"),
                    target_name="Second",
                )

            self.assertTrue(parser.parse(), parser.last_error)
            parser.device_name = "nRF52832_xxAA"
            parser.vendor = "Nordic Semiconductor"
            parser.source_files.extend([str(startup), str(system)])
            parser.linker_script = ""

            selected = parser._guess_gcc_linker_script()
            self.assertEqual(Path(selected), device_linker.resolve())

            output_dir = root / "out"
            self.assertTrue(
                parser.generate_cmake(
                    str(output_dir),
                    False,
                    GenerationOptions(compiler="gcc", generator="Ninja"),
                )
            )
            self.assertTrue(
                (output_dir / "cmake" / "linker" / "nrf52832_xxaa.ld").is_file()
            )
            self.assertTrue(
                (output_dir / "cmake" / "linker" / "nrf_common.ld").is_file()
            )
            staged_linker = (
                output_dir / "cmake" / "linker" / "nrf52832_xxaa.ld"
            ).read_text(encoding="utf-8")
            self.assertIn("__start_log_const_data", staged_linker)
            self.assertIn("__start_log_dynamic_data", staged_linker)
            self.assertIn("__start_nrf_balloc", staged_linker)
            cmake_text = (output_dir / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn("cmake/linker/nrf52832_xxaa.ld", cmake_text)
            self.assertIn('"-L${LINKER_SCRIPT_DIR}"', cmake_text)

    def test_patches_legacy_external_inline_definition(self):
        source = (
            "__INLINE void register_set(unsigned value) {}\n"
            "static __INLINE void already_static(void) {}\n"
        )
        patched, count = KeilProjectToCMake._patch_legacy_external_inline_header(
            source
        )

        self.assertEqual(count, 1)
        self.assertIn(
            "static __INLINE void register_set",
            patched,
        )
        self.assertIn("static __INLINE void already_static", patched)

    def test_patches_legacy_uint32_prototype_to_match_source(self):
        source = "void Delay(unsigned int count);\nvoid Other(unsigned int count);\n"
        patched, count = KeilProjectToCMake._patch_legacy_uint32_prototypes(
            source, {"Delay"}
        )

        self.assertEqual(count, 1)
        self.assertTrue(patched.startswith("#include <stdint.h>\n"))
        self.assertIn("void Delay(uint32_t count);", patched)
        self.assertIn("void Other(unsigned int count);", patched)

    def test_splits_space_and_comma_separated_defines(self):
        defines = KeilProjectToCMake._split_defines(
            "NRF52 NRF52832_XXAA,USE_SD_HW_RESOURCES;VALUE=3"
        )

        self.assertEqual(
            defines,
            ["NRF52", "NRF52832_XXAA", "USE_SD_HW_RESOURCES", "VALUE=3"],
        )

    def test_nordic_devices_without_fpu_force_soft_float(self):
        parser = object.__new__(KeilProjectToCMake)
        parser.device_name = "nRF52820_xxAA"
        parser.cpu = "Cortex-M4"
        parser.cpu_flags = [
            "-mfloat-abi=hard",
            "-mthumb",
            "-mfpu=fpv4-sp-d16",
            "-mcpu=cortex-m4",
        ]

        options = parser._gcc_cpu_options()

        self.assertIn("-mfloat-abi=soft", options)
        self.assertNotIn("-mfloat-abi=hard", options)
        self.assertFalse(any(flag.startswith("-mfpu=") for flag in options))

    def test_removes_prototype_conflicting_with_static_source_function(self):
        source = "void Delay(uint32_t count);\nvoid Keep(uint32_t count);\n"
        patched, count = KeilProjectToCMake._remove_conflicting_static_prototypes(
            source, {"Delay"}
        )

        self.assertEqual(count, 1)
        self.assertNotIn("void Delay", patched)
        self.assertIn("void Keep", patched)

    def test_static_function_detection_preserves_function_pointer_typedefs(self):
        source = (
            "static void worker(uint32_t value)\n"
            "{\n"
            "    (void)value;\n"
            "}\n"
            "static void visit(uint8_t (*task)(int value))\n"
            "{\n"
            "    (void)task;\n"
            "}\n"
            "static handler_t callback;\n"
        )
        names = KeilProjectToCMake._find_static_function_names(source)
        header = "typedef void (*handler_t)(int event);\nvoid worker(uint32_t value);\n"

        patched, count = KeilProjectToCMake._remove_conflicting_static_prototypes(
            header, names
        )

        self.assertEqual(names, {"worker", "visit"})
        self.assertEqual(count, 1)
        self.assertIn("typedef void (*handler_t)(int event);", patched)
        self.assertNotIn("void worker", patched)

    def test_patches_missing_uart_handle_when_single_handle_exists(self):
        source = (
            "UART_HandleTypeDef Uart1_Handle;\n"
            "void write_char(void) { HAL_UART_Transmit(&DebugUartHandle, 0, 0, 0); }\n"
        )
        patched, count = KeilProjectToCMake._patch_missing_uart_handle_alias(source)

        self.assertEqual(count, 1)
        self.assertIn("HAL_UART_Transmit(&Uart1_Handle", patched)

    def test_build_validator_classifies_configure_failure(self):
        validator = BuildValidator(timeout_seconds=10)
        completed = type(
            "Completed",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "CMake Error: arm-none-eabi-gcc was not found",
            },
        )()

        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.subprocess.run", return_value=completed
        ):
            report = validator.validate(
                Path(temp_dir),
                GenerationOptions(compiler="gcc", generator="Ninja").normalized(),
            )

        self.assertEqual(report.status, "configure_failed")
        self.assertIsNone(report.build)
        self.assertIn(
            "missing_tool", [diagnostic.category for diagnostic in report.diagnostics]
        )

    def test_build_validator_classifies_link_failure(self):
        validator = BuildValidator(timeout_seconds=10)
        configure = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "configured", "stderr": ""},
        )()
        build = type(
            "Completed",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "ld: undefined reference to `_sidata'",
            },
        )()

        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.subprocess.run", side_effect=[configure, build]
        ):
            report = validator.validate(
                Path(temp_dir),
                GenerationOptions(compiler="gcc", generator="Ninja").normalized(),
            )

        self.assertEqual(report.status, "build_failed")
        self.assertIn(
            "undefined_symbol",
            [diagnostic.category for diagnostic in report.diagnostics],
        )

    def test_generated_startup_uses_cortex_m0_compatible_copy_loop(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            startup = root / "startup_device.s"
            startup.write_text(
                textwrap.dedent(
                    """+                    AREA RESET, DATA, READONLY
                    EXPORT __Vectors
                    __Vectors
                        DCD __initial_sp
                        DCD Reset_Handler
                        DCD NMI_Handler
                    __Vectors_End
                    PRESERVE8
                    THUMB
                    """
                ),
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.cpu = "Cortex-M0"

            generated = parser._generate_gnu_startup_from_armasm(
                startup, str(root / "out")
            )

            self.assertIsNotNone(generated)
            content = Path(generated).read_text(encoding="utf-8")
            self.assertIn("adds r0, r0, #4", content)
            self.assertIn("adds r1, r1, #4", content)
            self.assertNotIn("[r0], #4", content)
            self.assertNotIn("[r1], #4", content)

    def test_patches_simple_armcc_delay_function(self):
        source = textwrap.dedent(
            """+            __ASM void system_delay_cycles(uint32_t count)
            {
                SUBS r0, #1
                BNE system_delay_cycles
                BX LR
            }
            """
        )

        patched, functions = KeilProjectToCMake._patch_armcc_embedded_assembly(
            source
        )

        self.assertEqual(functions, ["system_delay_cycles"])
        self.assertIn("__attribute__((naked))", patched)
        self.assertIn('"subs r0, r0, #1\\n"', patched)

    def test_patches_legacy_header_stdint_redefinitions(self):
        header = textwrap.dedent(
            """
            typedef unsigned char u8, uint8_t;
            typedef signed char int8_t;
            typedef unsigned int u32, uint32_t;
            typedef signed int s32, int32_t;
            """
        ).lstrip()

        patched, count = KeilProjectToCMake._patch_legacy_stdint_header(header)

        self.assertEqual(count, 4)
        self.assertTrue(patched.startswith("#include <stdint.h>"))
        self.assertIn("typedef uint8_t u8;", patched)
        self.assertIn("typedef uint32_t u32;", patched)
        self.assertNotIn("typedef signed int s32, int32_t;", patched)

    def test_adds_device_pack_cmsis_include_directory(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            device_include = (
                root
                / "PACK"
                / "Vendor"
                / "DevicePack"
                / "1.2.3"
                / "Drivers"
                / "CMSIS"
                / "Device"
                / "Include"
            )
            device_include.mkdir(parents=True)
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.mdk_info = {"RTEPATH": str(root / "PACK")}
            parser.pack_id = "Vendor.DevicePack.1.2.3"

            parser._add_selected_device_pack_includes()

            self.assertIn(
                str(device_include.resolve(strict=False)), parser.include_paths
            )

    def test_inline_compat_header_is_forced(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            include_dir = root / "include"
            include_dir.mkdir()
            (include_dir / "legacy.h").write_text(
                "__INLINE int legacy_value(void) { return 1; }\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser._add_include_path(str(include_dir))

            forced = parser._generate_legacy_stdint_compat_headers(str(root / "out"))

            self.assertIn("legacy.h", forced)

    def test_parses_project_local_rte_assets_for_selected_target(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            target_dir = root / "RTE" / "_Target"
            device_dir = root / "RTE" / "Device" / "STM32F401VE"
            target_dir.mkdir(parents=True)
            device_dir.mkdir(parents=True)
            (target_dir / "RTE_Components.h").write_text("\n", encoding="utf-8")
            (device_dir / "RTE_Device.h").write_text("\n", encoding="utf-8")
            rte = ET.fromstring(
                "<Project><RTE><files><file category='header'>"
                "<instance>RTE\\Device\\STM32F401VE\\RTE_Device.h</instance>"
                "<targetInfos><targetInfo name='Target'/></targetInfos>"
                "</file></files></RTE></Project>"
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.device_name = "STM32F401VE"

            parser._parse_project_rte_assets(rte, "Target")

            self.assertIn("_RTE_", parser.defines)
            self.assertIn(str(target_dir), parser.include_paths)
            self.assertIn(str(device_dir), parser.include_paths)
            self.assertIn(str(device_dir / "RTE_Device.h"), parser.header_files)


if __name__ == "__main__":
    unittest.main()
