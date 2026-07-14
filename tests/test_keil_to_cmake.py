import io
import json
import sys
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
            self.assertIn("../Src/main.c", normalized_cmake_text)
            self.assertNotIn(
                '"${CMAKE_CURRENT_LIST_DIR}/../Src"', normalized_cmake_text
            )
            self.assertIn("STM32F40_41xxx", cmake_text)
            self.assertIn("STM32F407ZG", cmake_text)
            self.assertNotIn("excluded.c", cmake_text)
            self.assertTrue((output_dir / "cmake" / "generated_linker.ld").is_file())
            self.assertFalse(any((output_dir / "cmake" / "sources").glob("main_*.c")))

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
            self.assertIn("add_custom_command(", cmake_text)
            self.assertIn("copy_if_different", cmake_text)
            self.assertIn("DEPENDS", cmake_text)
            self.assertIn("中文工程", cmake_text)

    def test_unchanged_gcc_sources_do_not_add_global_include_directories(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source_dir = root / "library" / "source"
            source_dir.mkdir(parents=True)
            source = source_dir / "module.cpp"
            source.write_text('#include "module.h"\n', encoding="utf-8")
            (source_dir / "module.h").write_text("#pragma once\n", encoding="utf-8")
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))

            staged = parser._stage_gcc_source_file(source, str(root / "out"))

            self.assertEqual(Path(staged), source.resolve())
            self.assertNotIn(str(source_dir.resolve()), parser.include_paths)

    def test_vscode_tasks_distinguish_build_rebuild_clean_and_jlink(self):
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
            self.assertTrue(
                parser.generate_cmake(
                    str(output_dir),
                    True,
                    GenerationOptions(
                        compiler="gcc",
                        generator="Ninja",
                        debug_probe="all",
                        debug_backend="all",
                    ),
                )
            )

            tasks = json.loads(
                (output_dir / ".vscode" / "tasks.json").read_text(
                    encoding="utf-8"
                )
            )["tasks"]
            by_label = {task["label"]: task for task in tasks}

            self.assertIn("Build GCC Debug", by_label)
            self.assertIn("Rebuild GCC Debug", by_label)
            self.assertIn("Clean GCC Debug", by_label)
            self.assertIn("--clean-first", by_label["Rebuild GCC Debug"]["args"])
            self.assertEqual(
                by_label["Clean GCC Debug"]["args"][-2:], ["--target", "clean"]
            )
            self.assertIn("Flash J-Link BIN", by_label)
            self.assertIn("Start J-Link GDB Server", by_label)
            self.assertNotIn("Flash J-Link OpenOCD BIN", by_label)
            jlink_args = by_label["Flash J-Link BIN"]["args"]
            self.assertEqual(
                jlink_args[jlink_args.index("-device") + 1],
                "STM32F407ZG",
            )
            for artifact in ("elf", "hex", "bin"):
                script = (
                    output_dir / ".vscode" / f"jlink_flash_{artifact}.jlink"
                ).read_text(encoding="utf-8")
                self.assertIn("\nr\ng\nq\n", script)
            self.assertEqual(len(by_label), len(tasks))
            self.assertEqual(
                by_label["Start J-Link GDB Server"]["args"][-3:],
                ["-port", "2331", "-singlerun"],
            )

            openocd_output_dir = root / "out-openocd"
            self.assertTrue(
                parser.generate_cmake(
                    str(openocd_output_dir),
                    True,
                    GenerationOptions(
                        compiler="gcc",
                        generator="Ninja",
                        debug_probe="jlink",
                        debug_backend="openocd",
                    ),
                )
            )
            openocd_tasks = json.loads(
                (openocd_output_dir / ".vscode" / "tasks.json").read_text(
                    encoding="utf-8"
                )
            )["tasks"]
            openocd_by_label = {task["label"]: task for task in openocd_tasks}
            self.assertIn("Flash J-Link OpenOCD BIN", openocd_by_label)
            self.assertNotIn("Flash J-Link BIN", openocd_by_label)
            self.assertEqual(len(openocd_by_label), len(openocd_tasks))

            build_script = (output_dir / "do_build.bat").read_text(
                encoding="utf-8"
            )
            self.assertIn('if /I "%~1"=="rebuild"', build_script)
            self.assertIn("--clean-first", build_script)

    def test_vscode_configuration_is_mirrored_to_project_root(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir) / "firmware"
            project_dir = root / "MDK-ARM"
            source_dir = root / "USER"
            project_dir.mkdir(parents=True)
            source_dir.mkdir(parents=True)
            uvprojx = self._write_minimal_project(project_dir)
            external_source = source_dir / "app.c"
            external_source.write_text("void app(void) {}\n", encoding="utf-8")
            output_dir = project_dir / "cmake-generated"
            build_dir = output_dir / "build-gcc"
            build_dir.mkdir(parents=True)
            (build_dir / "compile_commands.json").write_text(
                '[{"file":"app.c","command":"arm-none-eabi-gcc app.c"}]',
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)
            parser.project_dir = project_dir
            parser.source_files.append(str(external_source))

            self.assertTrue(
                parser.generate_cmake(
                    str(output_dir),
                    True,
                    GenerationOptions(compiler="gcc", generator="Ninja"),
                )
            )

            root_vscode = root / ".vscode"
            settings = json.loads(
                (root_vscode / "settings.json").read_text(encoding="utf-8")
            )
            tasks = json.loads(
                (root_vscode / "tasks.json").read_text(encoding="utf-8")
            )["tasks"]
            self.assertEqual(
                settings["cmake.sourceDirectory"],
                "${workspaceFolder}/MDK-ARM/cmake-generated",
            )
            self.assertEqual(
                settings["C_Cpp.default.compileCommands"],
                "${workspaceFolder}/MDK-ARM/cmake-generated/build-gcc/compile_commands.json",
            )
            self.assertTrue(
                all(
                    task["options"]["cwd"]
                    == "${workspaceFolder}/MDK-ARM/cmake-generated"
                    for task in tasks
                )
            )
            self.assertEqual(
                settings["clangd.arguments"],
                ["--compile-commands-dir=${workspaceFolder}"],
            )
            self.assertIn(
                "Refresh Compile Commands",
                [task["label"] for task in tasks],
            )
            self.assertEqual(
                (root / "compile_commands.json").read_text(encoding="utf-8"),
                (build_dir / "compile_commands.json").read_text(encoding="utf-8"),
            )

    def test_jlink_flash_prefers_keil_device_over_cpu_core(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            parser = KeilProjectToCMake(str(Path(temp_dir) / "sample.uvprojx"))
            parser.device_name = "-AT32F435RGT7"
            parser.cpu = "Cortex-M4"

            self.assertEqual(parser._jlink_flash_device(), "AT32F435RGT7")

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
            self.assertIn('set(KEIL_OPTIMIZATION_FLAG "-Os")', cmake_text)
            self.assertIn('set(KEIL_OPTIMIZATION_FLAG "-Oz")', cmake_text)
            self.assertIn(
                'set(KEIL_GCC_PROJECT_LTO_DEFAULT "OFF")',
                cmake_text,
            )
            self.assertIn("    ${KEIL_OPTIMIZATION_FLAG}\n", cmake_text)
            self.assertNotIn("$<$<CONFIG:Debug>:-Og>", cmake_text)
            self.assertIn("$<$<CONFIG:Debug>:-g3>", cmake_text)

            parser.keil_optimize_for_time = "1"
            self.assertEqual(parser._gcc_optimization_flag(), "-O3")

    def test_gcc_preserves_keil_abi_and_lto_options(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            project_text = uvprojx.read_text(encoding="utf-8")
            project_text = project_text.replace(
                "<Cads>",
                "<Cads><PlainCh>0</PlainCh><vShortEn>1</vShortEn>"
                "<vShortWch>1</vShortWch><v6Lto>1</v6Lto>",
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
            self.assertIn("    -funsigned-char\n", cmake_text)
            self.assertIn("    -fshort-enums\n", cmake_text)
            self.assertNotIn("    -fshort-wchar\n", cmake_text)
            self.assertIn(
                'set(KEIL_GCC_PROJECT_LTO_DEFAULT "ON")',
                cmake_text,
            )
            self.assertEqual(cmake_text.count("KEIL_GCC_ENABLE_LTO}>:-flto>"), 2)

    def test_fallback_linker_uses_armasm_stack_and_heap_sizes(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            startup = root / "startup_device.s"
            startup.write_text(
                "Stack_Size EQU 0x00000800\n"
                "Heap_Size EQU 00000200H\n"
                "AREA RESET, DATA, READONLY\n"
                "EXPORT __Vectors\n"
                "__Vectors DCD Stack_Mem\n"
                "DCD Reset_Handler\n"
                "THUMB\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(startup)]
            parser.flash_origin = 0x08000000
            parser.flash_length = 0x10000
            parser.ram_origin = 0x20000000
            parser.ram_length = 0x4000

            linker_script = parser._generate_fallback_gcc_linker_script(
                str(root / "out")
            )

            self.assertIsNotNone(linker_script)
            linker_text = Path(linker_script).read_text(encoding="utf-8")
            self.assertIn("_Min_Heap_Size = 0x200;", linker_text)
            self.assertIn("_Min_Stack_Size = 0x800;", linker_text)

    def test_fallback_linker_prefers_primary_ram_region_from_scatter(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            scatter = root / "project.sct"
            scatter.write_text(
                "LR_IROM1 0x08000000 0x100000 {\n"
                "  RW_IRAM1 0x20000000 0x58000 {\n"
                "    .ANY (+RW +ZI)\n"
                "  }\n"
                "  RW_IRAM2 0x20058000 UNINIT 0x28000 {\n"
                "    *(.sram_ext)\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.flash_origin = 0x08000000
            parser.flash_length = 0x100000
            parser.ram_origin = 0x20000000
            parser.ram_length = 0x60000
            parser.linker_script = str(scatter)

            linker_script = parser._generate_fallback_gcc_linker_script(
                str(root / "out")
            )

            self.assertIsNotNone(linker_script)
            linker_text = Path(linker_script).read_text(encoding="utf-8")
            self.assertIn(
                "RAM   (xrw) : ORIGIN = 0x20000000, LENGTH = 0x58000",
                linker_text,
            )
            self.assertIn(
                "RW_IRAM2 (xrw) : ORIGIN = 0x20058000, LENGTH = 0x28000",
                linker_text,
            )

    def test_generated_startup_preserves_pre_system_init_calls(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            startup = root / "startup_device.s"
            startup.write_text(
                "AREA RESET, DATA, READONLY\n"
                "EXPORT __Vectors\n"
                "__Vectors DCD __initial_sp\n"
                "DCD Reset_Handler\n"
                "DCD NMI_Handler\n"
                "__Vectors_End\n"
                "Reset_Handler PROC\n"
                "IMPORT extend_sram_512k\n"
                "LDR R0, =extend_sram_512k\n"
                "BLX R0\n"
                "LDR R0, =SystemInit\n"
                "BLX R0\n"
                "LDR R0, =__main\n"
                "BX R0\n"
                "ENDP\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.cpu = "Cortex-M4"

            generated = parser._generate_gnu_startup_from_armasm(
                startup, str(root / "out")
            )

            self.assertIsNotNone(generated)
            startup_text = Path(generated).read_text(encoding="utf-8")
            self.assertIn(".extern extend_sram_512k", startup_text)
            self.assertLess(
                startup_text.index("    bl extend_sram_512k"),
                startup_text.index("    bl SystemInit"),
            )

    def test_runtime_irq_compat_uses_assembly_symbols(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source = root / "main.c"
            source.write_text(
                "void run(void) { __disable_irq(); __enable_irq(); }\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]

            runtime = parser._generate_gcc_runtime_compat_source(str(root / "out"))

            self.assertIsNotNone(runtime)
            runtime_text = Path(runtime).read_text(encoding="utf-8")
            self.assertIn('"__disable_irq:\\n"', runtime_text)
            self.assertIn('"__enable_irq:\\n"', runtime_text)
            self.assertNotIn("void __disable_irq", runtime_text)

    def test_runtime_compat_does_not_preinitialize_lvgl(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source = root / "new.cpp"
            source.write_text(
                textwrap.dedent(
                    """
                    #include "lvgl.h"
                    static void *first_alloc(unsigned size) {
                        if(!lv_is_initialized()) lv_init();
                        return lv_mem_alloc(size);
                    }
                    void *operator new(unsigned size) { return first_alloc(size); }
                    """
                ),
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]

            runtime = parser._generate_gcc_runtime_compat_source(str(root / "out"))

            self.assertIsNone(runtime)

    def test_runtime_compat_generates_sbrk_from_linker_bounds(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source = root / "heap.c"
            linker = root / "device.ld"
            source.write_text(
                "void *allocate(unsigned size) { return malloc(size); }\n",
                encoding="utf-8",
            )
            linker.write_text(
                "_estack = ORIGIN(RAM) + LENGTH(RAM);\n"
                "_Min_Stack_Size = 0x400;\n"
                "SECTIONS { .bss : { end = .; } > RAM }\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]

            runtime = parser._generate_gcc_runtime_compat_source(
                str(root / "out"), str(linker)
            )

            self.assertIsNotNone(runtime)
            runtime_text = Path(runtime).read_text(encoding="utf-8")
            self.assertIn("void *_sbrk(ptrdiff_t increment)", runtime_text)
            self.assertIn("(uintptr_t)&_estack - (uintptr_t)&_Min_Stack_Size", runtime_text)
            self.assertTrue(any("newlib heap" in item for item in parser.warnings))

    def test_runtime_compat_preserves_existing_sbrk(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source = root / "syscalls.c"
            source.write_text(
                "void *malloc(unsigned);\n"
                "void *_sbrk(int size) { return 0; }\n"
                "void *allocate(unsigned size) { return malloc(size); }\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]

            runtime = parser._generate_gcc_runtime_compat_source(
                str(root / "out"), str(root / "missing.ld")
            )

            self.assertIsNone(runtime)

    def test_gcc_reorders_explicit_lvgl_init_before_hal_init(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            main_source = root / "main.cpp"
            allocator_source = root / "new.cpp"
            main_source.write_text(
                "void setup() {\n    HAL::HAL_Init();\n\n    lv_init();\n}\n",
                encoding="utf-8",
            )
            allocator_source.write_text(
                "void *operator new(unsigned size) {\n"
                "    if (!lv_is_initialized()) lv_init();\n"
                "    return lv_mem_alloc(size);\n}\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(main_source), str(allocator_source)]

            staged = parser._stage_gcc_source_file(main_source, str(root / "out"))
            staged_text = Path(staged).read_text(encoding="utf-8")

            self.assertLess(
                staged_text.index("lv_init();"),
                staged_text.index("HAL::HAL_Init();"),
            )
            self.assertTrue(any("LVGL 初始化顺序" in item for item in parser.warnings))

    def test_lvgl_lazy_allocator_detection_is_cached(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source = root / "new.cpp"
            source.write_text(
                "void *operator new(unsigned size) {\n"
                "    if (!lv_is_initialized()) lv_init();\n"
                "    return lv_mem_alloc(size);\n}\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]

            self.assertTrue(parser._has_lvgl_lazy_new_allocator())
            source.write_text("", encoding="utf-8")
            self.assertTrue(parser._has_lvgl_lazy_new_allocator())

    def test_gcc_detects_printf_float_formats(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source = root / "view.cpp"
            source.write_text(
                'void update(char *buf, float speed) { snprintf(buf, 16, "%0.1f", speed); }\n',
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]

            self.assertTrue(parser._gcc_uses_printf_float())

            source.write_text(
                'void update(char *buf, int speed) { snprintf(buf, 16, "%d", speed); }\n',
                encoding="utf-8",
            )
            self.assertFalse(parser._gcc_uses_printf_float())

    def test_gcc_distinguishes_printf_and_scanf_float_support(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source = root / "console.c"
            source.write_text(
                'void read_value(const char *text, float *value) {'
                ' sscanf(text, "%f", value); }\n',
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]

            self.assertFalse(parser._gcc_uses_printf_float())
            self.assertTrue(parser._gcc_uses_scanf_float())

            source.write_text(
                'void print_value(char *text, float value) {'
                ' snprintf(text, 16, "%.2f", value); }\n',
                encoding="utf-8",
            )
            self.assertTrue(parser._gcc_uses_printf_float())
            self.assertFalse(parser._gcc_uses_scanf_float())

    def test_generated_cmake_enables_only_required_float_io_helpers(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            source = root / "Src" / "main.c"
            source.write_text(
                '#include <stdio.h>\nint main(void) {'
                ' float value; return sscanf("1.0", "%f", &value); }\n',
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
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
            self.assertIn("    -u _scanf_float\n", cmake_text)
            self.assertNotIn("    -u _printf_float\n", cmake_text)

    def test_gcc_migration_analyzer_reports_runtime_and_linker_gaps(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            startup = root / "startup_device.s"
            source = root / "app.cpp"
            linker = root / "device.ld"
            startup.write_text(
                "Reset_Handler:\n    bl SystemInit\n    bl main\n",
                encoding="utf-8",
            )
            source.write_text(
                "void $Sub$$driver(void);\n"
                "void *allocate(unsigned size) { return malloc(size); }\n"
                "__asm void legacy(void) { bx lr }\n"
                "void boot(void) { __rt_lib_init(); }\n",
                encoding="utf-8",
            )
            linker.write_text(
                "SECTIONS { .text : { *(.text*) } }\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source), str(startup)]

            parser._analyze_gcc_migration_issues(
                [str(source), str(startup)], str(linker)
            )

            warnings = "\n".join(parser.warnings)
            for expected in (
                "__libc_init_array",
                ".init_array",
                ".ARM.exidx",
                "$Sub$$/$Super$$",
                "Arm C Library",
                "ARMASM",
                "_sbrk",
            ):
                self.assertIn(expected, warnings)

    def test_gcc_lto_keeps_inline_assembly_call_targets(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source = root / "port.c"
            source.write_text(
                '__asm void handler(void) { "bl vTaskSwitchContext\\n" }\n',
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]

            self.assertEqual(
                parser._gcc_inline_assembly_symbols(), ["vTaskSwitchContext"]
            )

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

    def test_preserves_class_members_matching_static_source_functions(self):
        header = (
            "class StdioStream {\n"
            " public:\n"
            "  int printHex(uint32_t value);\n"
            "};\n"
            "void printHex(uint32_t value);\n"
        )

        patched, count = KeilProjectToCMake._remove_conflicting_static_prototypes(
            header, {"printHex"}
        )

        self.assertEqual(count, 1)
        self.assertIn("int printHex(uint32_t value);", patched)
        self.assertNotIn("void printHex(uint32_t value);", patched)

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

    def test_build_validator_parses_ninja_and_make_progress(self):
        self.assertEqual(
            BuildValidator._build_progress_from_line("[12/48] Building C object"),
            (12, 48),
        )
        self.assertEqual(
            BuildValidator._build_progress_from_line("[ 42%] Linking target"),
            (42, 100),
        )
        self.assertIsNone(
            BuildValidator._build_progress_from_line("Building target")
        )

    def test_build_validator_streams_progress_output(self):
        events = []
        validator = BuildValidator(
            timeout_seconds=10,
            progress_callback=lambda value, message: events.append((value, message)),
        )

        with TemporaryDirectory() as temp_dir:
            result = validator._run(
                [sys.executable, "-c", "print('[2/4] Building C object')"],
                Path(temp_dir),
                "build",
            )

        self.assertTrue(result.succeeded)
        self.assertIn("[2/4] Building C object", result.stdout)
        self.assertIn((54, "编译 2/4"), events)

    def test_generation_cleans_stale_cmake_assets_only(self):
        with TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            stale_header = output / "cmake" / "compat-includes" / "StdioStream.h"
            build_marker = output / "build-gcc" / "keep.txt"
            stale_header.parent.mkdir(parents=True)
            build_marker.parent.mkdir(parents=True)
            stale_header.write_text("stale", encoding="utf-8")
            build_marker.write_text("keep", encoding="utf-8")

            KeilProjectToCMake._clean_generated_cmake_assets(output)

            self.assertFalse((output / "cmake").exists())
            self.assertEqual(build_marker.read_text(encoding="utf-8"), "keep")

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
            old_device_dir = root / "RTE" / "Device" / "STM32F401VC"
            target_dir.mkdir(parents=True)
            device_dir.mkdir(parents=True)
            old_device_dir.mkdir(parents=True)
            (target_dir / "RTE_Components.h").write_text("\n", encoding="utf-8")
            (device_dir / "RTE_Device.h").write_text("\n", encoding="utf-8")
            (device_dir / "system_stm32f4xx.c").write_text("\n", encoding="utf-8")
            (old_device_dir / "system_stm32f4xx.c").write_text(
                "\n", encoding="utf-8"
            )
            rte = ET.fromstring(
                "<Project><RTE><files>"
                "<file category='header'>"
                "<instance>RTE\\Device\\STM32F401VE\\RTE_Device.h</instance>"
                "<targetInfos><targetInfo name='Target'/></targetInfos>"
                "</file>"
                "<file category='source'>"
                "<instance>RTE\\Device\\STM32F401VE\\system_stm32f4xx.c</instance>"
                "<targetInfos><targetInfo name='Target'/></targetInfos>"
                "</file>"
                "<file category='source'>"
                "<instance removed='1'>"
                "RTE\\Device\\STM32F401VC\\system_stm32f4xx.c"
                "</instance><targetInfos/>"
                "</file>"
                "</files></RTE></Project>"
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.device_name = "STM32F401VE"

            parser._parse_project_rte_assets(rte, "Target")

            self.assertIn(
                str((device_dir / "system_stm32f4xx.c").resolve()),
                parser.source_files,
            )
            self.assertNotIn(
                str((old_device_dir / "system_stm32f4xx.c").resolve()),
                parser.source_files,
            )

            self.assertIn("_RTE_", parser.defines)
            self.assertIn(str(target_dir), parser.include_paths)
            self.assertIn(str(device_dir), parser.include_paths)
            self.assertIn(str(device_dir / "RTE_Device.h"), parser.header_files)


if __name__ == "__main__":
    unittest.main()
