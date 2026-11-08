import io
import json
import shutil
import subprocess
import sys
import threading
import time
import textwrap
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from keil_to_cmake import (
    BuildValidator,
    CommandResult,
    GenerationOptions,
    KeilProjectToCMake,
    OperationCancelled,
    SourceCompileSettings,
    SUPPORTED_DEBUG_BACKENDS,
    SUPPORTED_DEBUG_PROBES,
)


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
            self.assertIn(
                '"${CMAKE_CURRENT_LIST_DIR}/../Src/main.c"',
                normalized_cmake_text,
            )
            self.assertIn(
                '"${CMAKE_CURRENT_LIST_DIR}/../Inc"',
                normalized_cmake_text,
            )
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

    def test_parse_is_idempotent(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)

            with patch("keil_to_cmake.find_keil_mdk_root", return_value=None), patch(
                "keil_to_cmake.get_keil_mdk_info", return_value=None
            ):
                parser = KeilProjectToCMake(
                    str(uvprojx),
                    str(root / "missing_mdk"),
                    target_name="Second",
                )

            self.assertTrue(parser.parse(), parser.last_error)
            first_state = (
                list(parser.source_files),
                list(parser.header_files),
                list(parser.include_path_order),
                set(parser.defines),
                list(parser.cpu_flags),
                list(parser.c_cpp_flags),
                list(parser.c_flags),
                list(parser.cpp_flags),
                list(parser.asm_flags),
                list(parser.linker_flags),
                list(parser.linker_inputs),
                list(parser.libraries),
            )

            self.assertTrue(parser.parse(), parser.last_error)
            second_state = (
                list(parser.source_files),
                list(parser.header_files),
                list(parser.include_path_order),
                set(parser.defines),
                list(parser.cpu_flags),
                list(parser.c_cpp_flags),
                list(parser.c_flags),
                list(parser.cpp_flags),
                list(parser.asm_flags),
                list(parser.linker_flags),
                list(parser.linker_inputs),
                list(parser.libraries),
            )
            self.assertEqual(second_state, first_state)

    def test_file_type_object_is_included_in_generated_sources(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            object_file = root / "Src" / "algorithm.o"
            object_file.write_bytes(b"prebuilt object fixture")
            tree = ET.parse(uvprojx)
            files = tree.getroot().find(".//Target[TargetName='Second']//Files")
            self.assertIsNotNone(files)
            file_node = ET.SubElement(files, "File")
            ET.SubElement(file_node, "FileName").text = object_file.name
            ET.SubElement(file_node, "FileType").text = "3"
            ET.SubElement(file_node, "FilePath").text = "..\\..\\Src\\algorithm.o"
            tree.write(uvprojx, encoding="utf-8", xml_declaration=True)

            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)
            self.assertIn(str(object_file), parser.source_files)
            output = root / "out"
            self.assertTrue(
                parser.generate_cmake(
                    str(output),
                    False,
                    GenerationOptions(compiler="gcc", generator="Ninja"),
                ),
                parser.last_error,
            )

            cmake_text = (output / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn("../Src/algorithm.o", cmake_text.replace("\\", "/"))

    def test_group_and_file_controls_remain_source_scoped(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            (root / "Src" / "other.c").write_text(
                "int other(void) { return 0; }\n", encoding="utf-8"
            )
            group_include = root / "Group Include"
            file_include = root / "File Include"
            group_include.mkdir()
            file_include.mkdir()

            tree = ET.parse(uvprojx)
            target = tree.getroot().find(".//Target[TargetName='Second']")
            self.assertIsNotNone(target)
            controls = target.find(".//TargetOption//Cads/VariousControls")
            self.assertIsNotNone(controls)
            controls.find("Define").text = (
                "GLOBAL=1,TARGET_REMOVE=1,TARGET_GLOBAL_UNDEF=1"
            )
            ET.SubElement(controls, "Undefine").text = "TARGET_GLOBAL_UNDEF"

            group = target.find(".//Groups/Group")
            self.assertIsNotNone(group)
            group_option = ET.SubElement(group, "GroupOption")
            group_ads = ET.SubElement(group_option, "GroupArmAds")
            group_cads = ET.SubElement(group_ads, "Cads")
            group_controls = ET.SubElement(group_cads, "VariousControls")
            ET.SubElement(group_controls, "Define").text = "GROUP_ONLY=1"
            ET.SubElement(group_controls, "IncludePath").text = (
                "..\\..\\Group Include"
            )

            main_file = group.find(".//File[FileName='main.c']")
            self.assertIsNotNone(main_file)
            file_option = ET.SubElement(main_file, "FileOption")
            file_ads = ET.SubElement(file_option, "FileArmAds")
            file_cads = ET.SubElement(file_ads, "Cads")
            file_controls = ET.SubElement(file_cads, "VariousControls")
            ET.SubElement(file_controls, "Define").text = (
                "FILE_ONLY=1,FUNC(x)=((x)+1)"
            )
            ET.SubElement(file_controls, "Undefine").text = "TARGET_REMOVE"
            ET.SubElement(file_controls, "IncludePath").text = (
                "..\\..\\File Include"
            )

            groups = target.find(".//Groups")
            other_group = ET.SubElement(groups, "Group")
            ET.SubElement(other_group, "GroupName").text = "Other"
            other_files = ET.SubElement(other_group, "Files")
            other_file = ET.SubElement(other_files, "File")
            ET.SubElement(other_file, "FileName").text = "other.c"
            ET.SubElement(other_file, "FileType").text = "1"
            ET.SubElement(other_file, "FilePath").text = "..\\..\\Src\\other.c"
            tree.write(uvprojx, encoding="utf-8", xml_declaration=True)

            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)

            self.assertIn("GLOBAL=1", parser.defines)
            self.assertIn("TARGET_REMOVE=1", parser.defines)
            self.assertNotIn("GROUP_ONLY=1", parser.defines)
            self.assertNotIn("FILE_ONLY=1", parser.defines)
            self.assertNotIn(
                "TARGET_GLOBAL_UNDEF=1", parser._effective_defines("gcc")
            )
            main_path = str((root / "Src" / "main.c").resolve(strict=False))
            other_path = str((root / "Src" / "other.c").resolve(strict=False))
            main_settings = parser._source_compile_settings_for(main_path)
            other_settings = parser._source_compile_settings_for(other_path)
            self.assertEqual(
                main_settings.macro_actions,
                {
                    "GROUP_ONLY": "GROUP_ONLY=1",
                    "FILE_ONLY": "FILE_ONLY=1",
                    "FUNC": "FUNC(x)=((x)+1)",
                    "TARGET_REMOVE": None,
                },
            )
            self.assertEqual(
                main_settings.include_paths,
                [str(group_include), str(file_include)],
            )
            self.assertFalse(other_settings.macro_actions)
            self.assertFalse(other_settings.include_paths)

            for compiler in ("gcc", "armclang"):
                with self.subTest(compiler=compiler):
                    output = root / f"source-settings-{compiler}"
                    self.assertTrue(
                        parser.generate_cmake(
                            str(output),
                            False,
                            GenerationOptions(
                                compiler=compiler, generator="Ninja"
                            ),
                        ),
                        parser.last_error,
                    )
                    cmake_text = (output / "CMakeLists.txt").read_text(
                        encoding="utf-8"
                    )
                    if compiler == "gcc":
                        source_marker = (
                            'set_property(SOURCE '
                            '"${CMAKE_CURRENT_LIST_DIR}/../Src/main.c" '
                            'APPEND PROPERTY\n'
                        )
                        group_include_option = (
                            '    "-I${CMAKE_CURRENT_LIST_DIR}/../Group Include"\n'
                        )
                        file_include_option = (
                            '    "-I${CMAKE_CURRENT_LIST_DIR}/../File Include"\n'
                        )
                    else:
                        source_marker = (
                            'set_property(SOURCE "../Src/main.c" '
                            'APPEND PROPERTY\n'
                        )
                        group_include_option = (
                            f'    "-I{group_include.resolve().as_posix()}"\n'
                        )
                        file_include_option = (
                            f'    "-I{file_include.resolve().as_posix()}"\n'
                        )
                    self.assertEqual(cmake_text.count(source_marker), 1)
                    self.assertIn(
                        source_marker
                        + '    COMPILE_OPTIONS\n'
                        + '    "-UGROUP_ONLY"\n'
                        + '    "-DGROUP_ONLY=1"\n'
                        + '    "-UTARGET_REMOVE"\n'
                        + '    "-UFILE_ONLY"\n'
                        + '    "-DFILE_ONLY=1"\n'
                        + '    "-UFUNC"\n'
                        + '    "-DFUNC(x)=((x)+1)"\n'
                        + group_include_option
                        + file_include_option
                        + ')\n',
                        cmake_text,
                    )
                    self.assertIn('"-UTARGET_GLOBAL_UNDEF"', cmake_text)
                    self.assertNotIn('"TARGET_GLOBAL_UNDEF=1"', cmake_text)
                    self.assertNotIn(
                        'set_property(SOURCE "../Src/other.c"', cmake_text
                    )

    def test_dot_include_paths_resolve_to_project_directory(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            tree = ET.parse(uvprojx)
            include = tree.getroot().find(
                ".//Target[TargetName='Second']//Cads/VariousControls/IncludePath"
            )
            self.assertIsNotNone(include)
            include.text = ".;..\\..\\Inc"
            tree.write(uvprojx, encoding="utf-8", xml_declaration=True)

            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)
            project_dir = str(uvprojx.parent.resolve(strict=False))
            self.assertEqual(parser.include_path_order[0], project_dir)

            group = ET.fromstring(
                "<Group><GroupOption><GroupArmAds><Cads><VariousControls>"
                "<IncludePath>.</IncludePath>"
                "</VariousControls></Cads></GroupArmAds></GroupOption></Group>"
            )
            local_settings = parser._parse_local_controls(
                group, ".//GroupOption"
            )
            self.assertEqual(local_settings.include_paths, [project_dir])

    def test_linker_misc_objects_are_emitted_for_both_compilers(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            linker_object = root / "Object Inputs" / "algorithm.o"
            linker_object.parent.mkdir()
            linker_object.write_bytes(b"ordinary object fixture")
            symbol_file = root / "Object Inputs" / "symbols.o"
            symbol_file.write_text(
                "#<SYMDEFS>#\n0x08000100 T exported_symbol\n",
                encoding="utf-8",
            )
            tree = ET.parse(uvprojx)
            linker = tree.getroot().find(
                ".//Target[TargetName='Second']//LDads"
            )
            self.assertIsNotNone(linker)
            misc = ET.SubElement(linker, "Misc")
            misc.text = (
                '"..\\..\\Object Inputs\\algorithm.o" '
                '"..\\..\\Object Inputs\\symbols.o"'
            )
            tree.write(uvprojx, encoding="utf-8", xml_declaration=True)

            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)
            expected = str(linker_object.resolve(strict=False))
            expected_symbols = str(symbol_file.resolve(strict=False))
            self.assertEqual(parser.linker_inputs, [expected, expected_symbols])

            for compiler in ("gcc", "armclang"):
                with self.subTest(compiler=compiler):
                    output = root / f"generated-{compiler}"
                    self.assertTrue(
                        parser.generate_cmake(
                            str(output),
                            False,
                            GenerationOptions(
                                compiler=compiler, generator="Ninja"
                            ),
                        ),
                        parser.last_error,
                    )
                    cmake_text = (output / "CMakeLists.txt").read_text(
                        encoding="utf-8"
                    )
                    self.assertIn(
                        "../Object Inputs/algorithm.o",
                        cmake_text.replace("\\", "/"),
                    )
                    normalized_cmake = cmake_text.replace("\\", "/")
                    if compiler == "gcc":
                        self.assertNotIn(
                            "../Object Inputs/symbols.o", normalized_cmake
                        )
                        self.assertIn(
                            "cmake/linker/symbols/symbols_", normalized_cmake
                        )
                    else:
                        self.assertIn(
                            "../Object Inputs/symbols.o", normalized_cmake
                        )

    def test_missing_source_search_stays_inside_project_content_root(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            project_dir = root / "project-a"
            foreign_dir = root / "project-b"
            project_dir.mkdir()
            foreign_dir.mkdir()
            foreign_source = foreign_dir / "main.c"
            foreign_source.write_text("int foreign(void) { return 1; }\n")
            missing_source = project_dir / "missing" / "main.c"
            parser = KeilProjectToCMake(str(project_dir / "sample.uvprojx"))

            resolved = parser._resolve_missing_source(missing_source)

            self.assertEqual(resolved, missing_source)
            self.assertNotEqual(resolved.resolve(), foreign_source.resolve())

    def test_linker_search_stays_inside_project_content_root(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            project_dir = root / "project-a"
            foreign_dir = root / "project-b"
            project_dir.mkdir()
            foreign_dir.mkdir()
            startup = project_dir / "startup.s"
            startup.write_text(".syntax unified\n", encoding="utf-8")
            (foreign_dir / "foreign_FLASH.ld").write_text(
                "SECTIONS { .text : { *(.text*) } }\n", encoding="utf-8"
            )
            parser = KeilProjectToCMake(str(project_dir / "sample.uvprojx"))
            parser.device_name = "STM32F103C8"
            parser.source_files = [str(startup)]

            self.assertIsNone(parser._guess_gcc_linker_script())
            self.assertEqual(parser._mcu_family_token(), "STM32F103")

    def test_stm32f0_fallback_uses_cortex_m0(self):
        parser = KeilProjectToCMake("sample.uvprojx")
        parser.device_name = "STM32F030C8"

        self.assertEqual(parser._infer_cpu_from_device(), "Cortex-M0")
        self.assertEqual(parser._mcu_family_token(), "STM32F030")
        self.assertEqual(parser._device_memory_code(), "8")

    def test_armclang_toolchain_normalizes_and_quotes_environment_path(self):
        parser = KeilProjectToCMake("sample.uvprojx")
        parser.cpu = "Cortex-M0+"

        toolchain = parser._armclang_toolchain_content(
            GenerationOptions(compiler="armclang", host_os="windows").normalized()
        )

        self.assertIn("set(CMAKE_SYSTEM_PROCESSOR cortex-m0plus)", toolchain)
        self.assertIn(
            'set(CMAKE_EXE_LINKER_FLAGS_INIT "--cpu=${CMAKE_SYSTEM_PROCESSOR}")',
            toolchain,
        )
        self.assertIn(
            'file(TO_CMAKE_PATH "$ENV{ARMCLANG_PATH}" ARMCLANG_PATH)',
            toolchain,
        )
        for variable, executable in (
            ("CMAKE_C_COMPILER", "armclang"),
            ("CMAKE_CXX_COMPILER", "armclang"),
            ("CMAKE_ASM_COMPILER", "armclang"),
            ("CMAKE_LINKER", "armlink"),
            ("CMAKE_AR", "armar"),
            ("CMAKE_OBJCOPY", "fromelf"),
            ("FROMELF_EXECUTABLE", "fromelf"),
        ):
            self.assertIn(
                f'set({variable} "${{ARMCLANG_PATH}}/{executable}'
                '${_ARM_EXECUTABLE_SUFFIX}")',
                toolchain,
            )

        cmake_output = io.StringIO()
        parser._write_cmake_header(
            cmake_output,
            "cmake/armclang.cmake",
            ["C"],
            policies=["CMP0123"],
        )
        cmake_text = cmake_output.getvalue()
        self.assertLess(
            cmake_text.index("cmake_policy(SET CMP0123 NEW)"),
            cmake_text.index('project("sample" LANGUAGES C)'),
        )

    def test_armclang_include_options_use_forward_slashes(self):
        parser = KeilProjectToCMake("sample.uvprojx")
        include = Path(r"D:\SDK Folder\Include")
        parser._add_include_path(str(include))
        parser.output_name = "sample"
        cmake_output = io.StringIO()

        parser._write_armclang_cmake_content(
            cmake_output,
            r"D:\generated",
            GenerationOptions(
                compiler="armclang", generator="Ninja", host_os="windows"
            ).normalized(),
        )

        cmake = cmake_output.getvalue()
        self.assertIn("add_compile_options(", cmake)
        self.assertIn('"-ID:/SDK Folder/Include"', cmake)
        self.assertNotIn("include_directories(", cmake)

    def test_generation_removes_stale_build_validation_report(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            output = root / "out"
            output.mkdir()
            validation_report = output / "build-validation.json"
            validation_report.write_text(
                '{"status": "success"}\n', encoding="utf-8"
            )
            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")

            self.assertTrue(parser.parse(), parser.last_error)
            self.assertTrue(
                parser.generate_cmake(
                    str(output),
                    False,
                    GenerationOptions(compiler="gcc", generator="Ninja"),
                ),
                parser.last_error,
            )

            self.assertFalse(validation_report.exists())

    def test_multiconfig_generators_keep_firmware_in_build_root(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")

            self.assertTrue(parser.parse(), parser.last_error)
            for compiler in ("gcc", "armclang"):
                output = root / f"{compiler}-output"
                options = GenerationOptions(
                    compiler=compiler,
                    generator="Xcode",
                    host_os="macos",
                )
                self.assertTrue(
                    parser.generate_cmake(str(output), True, options),
                    parser.last_error,
                )
                cmake = (output / "CMakeLists.txt").read_text(encoding="utf-8")
                self.assertIn(
                    'RUNTIME_OUTPUT_DIRECTORY "$<1:${CMAKE_CURRENT_BINARY_DIR}>"',
                    cmake,
                )
                response_setting = "ON" if compiler == "gcc" else "OFF"
                self.assertIn(
                    f"set(CMAKE_NINJA_FORCE_RESPONSE_FILE {response_setting}",
                    cmake,
                )
                script = (output / "do_build.sh").read_text(encoding="utf-8")
                tasks = json.loads(
                    (output / ".vscode" / "tasks.json").read_text(encoding="utf-8")
                )["tasks"]
                tasks_by_label = {task["label"]: task for task in tasks}
                compiler_label = "GCC" if compiler == "gcc" else "ArmClang"
                debug_args = tasks_by_label[
                    f"Build {compiler_label} Debug"
                ]["args"]
                self.assertEqual(
                    debug_args[debug_args.index("--config") + 1], "Debug"
                )
                if compiler == "gcc":
                    self.assertIn(
                        'cmake --build "$ACTIVE_BUILD_DIR" --config "$BUILD_TYPE"',
                        script,
                    )
                    release_args = tasks_by_label["Build GCC Release"]["args"]
                    self.assertEqual(
                        release_args[release_args.index("--config") + 1],
                        "Release",
                    )
                else:
                    self.assertIn(
                        'cmake --build "build" --config Debug', script
                    )

    def test_generation_does_not_leak_gcc_assets_into_armclang(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            device_dir = root / "device"
            device_dir.mkdir()
            (device_dir / "gcc_startup_nrf52811.S").write_text(
                ".syntax unified\n.thumb\n", encoding="utf-8"
            )
            (device_dir / "system_nrf52811.c").write_text(
                "void SystemInit(void) {}\n", encoding="utf-8"
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
            parser.device_name = "nRF52811_xxAA"
            parser.vendor = "Nordic Semiconductor"
            parser.cpu = "Cortex-M4"
            parser._add_include_path(str(device_dir))
            parsed_sources = list(parser.source_files)

            gcc_output = root / "gcc-output"
            gcc_options = GenerationOptions(compiler="gcc", generator="Ninja")
            self.assertTrue(
                parser.generate_cmake(str(gcc_output), False, gcc_options),
                parser.last_error,
            )
            first_warnings = list(parser.warnings)
            self.assertIn(
                "gcc_startup_nrf52811.S",
                (gcc_output / "CMakeLists.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(parser.source_files, parsed_sources)

            self.assertTrue(
                parser.generate_cmake(str(gcc_output), False, gcc_options),
                parser.last_error,
            )
            self.assertEqual(parser.warnings, first_warnings)

            armclang_output = root / "armclang-output"
            self.assertTrue(
                parser.generate_cmake(
                    str(armclang_output),
                    False,
                    GenerationOptions(compiler="armclang", generator="Ninja"),
                ),
                parser.last_error,
            )
            armclang_cmake = (armclang_output / "CMakeLists.txt").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("gcc_startup_nrf52811.S", armclang_cmake)
            self.assertNotIn("system_nrf52811.c", armclang_cmake)

    def test_repeated_gcc_source_queries_do_not_duplicate_warnings(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            redirect_source = root / "rt_sys.c"
            redirect_source.write_text(
                '#include "rt_sys.h"\n', encoding="utf-8"
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(redirect_source)]

            self.assertEqual(parser._gcc_source_files(), [])
            self.assertEqual(parser._gcc_source_files(), [])

            matching_warnings = [
                warning
                for warning in parser.warnings
                if "Keil C Library" in warning
            ]
            self.assertEqual(len(matching_warnings), 1)

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
                    GenerationOptions(
                        compiler="gcc",
                        generator="Ninja",
                        debug_probe="all",
                        debug_backend="all",
                    ),
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

            with patch("keil_to_cmake.shutil.copy2") as copy:
                staged = parser._stage_gcc_source_file(source, str(root / "out"))

            self.assertEqual(Path(staged), source.resolve())
            self.assertNotIn(str(source_dir.resolve()), parser.include_paths)
            copy.assert_not_called()

    def test_generation_reuses_source_analysis_and_refreshes_after_edits(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            parser = KeilProjectToCMake(
                str(self._write_minimal_project(root)), target_name="Second"
            )
            self.assertTrue(parser.parse(), parser.last_error)
            source = root / "Src" / "main.c"
            output = root / "out"
            options = GenerationOptions(compiler="gcc", generator="Ninja")
            read_text = Path.read_text
            source_reads = []

            def tracked_read(path, *args, **kwargs):
                if path == source:
                    source_reads.append(path)
                return read_text(path, *args, **kwargs)

            for helper, content in (
                ("printf", 'int main(void) { printf("%f", 1.0); }\n'),
                ("scanf", 'int main(void) { float x; scanf("%f", &x); }\n'),
            ):
                with self.subTest(helper=helper):
                    source.write_text(content, encoding="utf-8")
                    source_reads.clear()
                    with patch.object(Path, "read_text", tracked_read):
                        self.assertTrue(
                            parser.generate_cmake(str(output), False, options),
                            parser.last_error,
                        )
                    self.assertEqual(len(source_reads), 1)
                    cmake = (output / "CMakeLists.txt").read_text(encoding="utf-8")
                    self.assertIn(f"    -u _{helper}_float\n", cmake)
                    other = "scanf" if helper == "printf" else "printf"
                    self.assertNotIn(f"    -u _{other}_float\n", cmake)
                    self.assertIsNone(parser._generation_source_analysis)
                    self.assertIsNone(parser._generation_resolved_paths)

    def test_failed_generation_releases_analysis_caches(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            parser = KeilProjectToCMake(
                str(self._write_minimal_project(root)), target_name="Second"
            )
            self.assertTrue(parser.parse(), parser.last_error)
            options = GenerationOptions(compiler="gcc", generator="Ninja")

            def fail_after_analysis(*args):
                self.assertTrue(parser._generation_source_analysis)
                self.assertTrue(parser._generation_resolved_paths)
                raise OSError("test output failure")

            with patch.object(
                parser, "_write_toolchain_files", side_effect=fail_after_analysis
            ), redirect_stderr(io.StringIO()):
                self.assertFalse(
                    parser.generate_cmake(str(root / "out"), False, options)
                )
            self.assertIn("test output failure", parser.last_error)
            self.assertIsNone(parser._generation_source_analysis)
            self.assertIsNone(parser._generation_resolved_paths)
            (root / "Src" / "main.c").write_text(
                'int main(void) { printf("%f", 1.0); }\n', encoding="utf-8"
            )
            self.assertTrue(
                parser.generate_cmake(str(root / "out"), False, options),
                parser.last_error,
            )
            self.assertIn(
                "    -u _printf_float\n",
                (root / "out" / "CMakeLists.txt").read_text(encoding="utf-8"),
            )

    def test_staged_analysis_is_distinct_and_invalidated_when_rewritten(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source = root / "delay.c"
            source.write_text(
                "__asm void delay(uint32_t count)\n"
                "{ SUBS r0, #1 BNE delay BX LR }\n", encoding="utf-8"
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser._generation_source_analysis = {}
            parser._generation_resolved_paths = {}
            staged = Path(parser._stage_gcc_source_file(source, str(root / "out")))
            self.assertNotEqual(source, staged)
            original = parser._analyze_source_file(source)
            prepared = parser._analyze_source_file(staged)
            self.assertTrue(original.migration_checks[2])
            self.assertFalse(prepared.migration_checks[2])
            self.assertFalse(prepared.printf_float)

            parser._write_text_file(staged, 'void log(void) { printf("%f", 1.0); }\n')
            self.assertTrue(parser._analyze_source_file(staged).printf_float)
            self.assertIs(parser._analyze_source_file(source), original)
            self.assertFalse(original.printf_float)

    def test_gcc_source_matching_has_linear_filesystem_query_budget(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            for index in range(32):
                source = root / f"module_{index:02d}.c"
                source.write_text("void module(void) {}\n", encoding="utf-8")
                parser.source_files.append(str(source))

            with patch.object(
                parser, "_source_control_key", wraps=parser._source_control_key
            ) as source_key:
                prepared = parser._prepared_gcc_source_files(str(root / "out"))

            self.assertEqual(prepared, sorted(parser.source_files))
            self.assertLessEqual(source_key.call_count, 3 * len(parser.source_files))

    def test_gcc_startup_mapping_preserves_local_settings_precedence(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            mdk_source = root / "startup" / "mdk" / "startup_device.s"
            gcc_source = root / "startup" / "gcc" / "startup_device.s"
            for source in (mdk_source, gcc_source):
                source.parent.mkdir(parents=True)
                source.write_text(".syntax unified\n", encoding="utf-8")
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(mdk_source), str(gcc_source)]
            inherited = SourceCompileSettings(macro_actions={"LOCAL": "LOCAL=1"})
            explicit = SourceCompileSettings(macro_actions={"LOCAL": "LOCAL=2"})
            parser.source_compile_settings[
                parser._source_control_key(mdk_source)
            ] = inherited
            gcc_key = parser._source_control_key(gcc_source)

            for direct_settings in (False, True, False):
                with self.subTest(direct_settings=direct_settings):
                    if direct_settings:
                        parser.source_compile_settings[gcc_key] = explicit
                    else:
                        parser.source_compile_settings.pop(gcc_key, None)
                    prepared = parser._prepared_gcc_source_files(str(root / "out"))
                    self.assertEqual(prepared, [str(gcc_source.resolve())])
                    self.assertEqual(
                        parser._source_compile_settings_for(prepared[0]),
                        explicit if direct_settings else inherited,
                    )

    def test_gcc_staging_preserves_original_when_assembly_needs_conversion(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "delay.c"
            original = (
                "__ASM void delay(uint32_t count)\n"
                "{ SUBS r0, #1 BNE delay BX LR }\n"
            )
            source.write_text(original, encoding="utf-8")
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))

            staged = Path(parser._stage_gcc_source_file(source, str(root / "out")))

            self.assertNotEqual(staged, source)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertIn("__attribute__((naked))", staged.read_text(encoding="utf-8"))
            self.assertFalse(parser._copy_staged_source_mappings)

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
            self.assertIn("Configure GCC Release", by_label)
            self.assertIn("Build GCC Release", by_label)
            self.assertIn("Rebuild GCC Release", by_label)
            self.assertIn("Clean GCC Release", by_label)
            self.assertIn("--clean-first", by_label["Rebuild GCC Debug"]["args"])
            self.assertIn(
                "--clean-first", by_label["Rebuild GCC Release"]["args"]
            )
            self.assertEqual(
                by_label["Clean GCC Debug"]["args"][-2:], ["--target", "clean"]
            )
            self.assertIn(
                "${workspaceFolder}/build-gcc-release",
                by_label["Configure GCC Release"]["args"],
            )
            self.assertIn(
                "-DCMAKE_BUILD_TYPE=Release",
                by_label["Configure GCC Release"]["args"],
            )
            self.assertIn("Flash J-Link BIN", by_label)
            self.assertIn("Flash J-Link BIN Release", by_label)
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
            self.assertEqual(
                by_label["Flash J-Link BIN Release"]["dependsOn"],
                "Build GCC Release",
            )
            release_script = (
                output_dir / ".vscode" / "jlink_flash_bin_release.jlink"
            ).read_text(encoding="utf-8")
            self.assertIn("build-gcc-release/", release_script)

            custom_debug_script = output_dir / ".vscode" / "custom.jlink"
            custom_debug_script.write_text("user content\n", encoding="utf-8")
            self.assertTrue(
                parser.generate_cmake(
                    str(output_dir),
                    True,
                    GenerationOptions(
                        compiler="gcc",
                        generator="Ninja",
                        debug_backend="none",
                    ),
                )
            )
            for artifact in ("elf", "hex", "bin"):
                self.assertFalse(
                    (
                        output_dir / ".vscode" / f"jlink_flash_{artifact}.jlink"
                    ).exists()
                )
                self.assertFalse(
                    (
                        output_dir
                        / ".vscode"
                        / f"jlink_flash_{artifact}_release.jlink"
                    ).exists()
                )
            self.assertFalse(
                (output_dir / ".vscode" / "openocd-jlink-swd.cfg").exists()
            )
            self.assertEqual(
                custom_debug_script.read_text(encoding="utf-8"), "user content\n"
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
            self.assertIn(
                "Flash J-Link OpenOCD BIN Release", openocd_by_label
            )
            self.assertNotIn("Flash J-Link BIN", openocd_by_label)
            self.assertEqual(
                openocd_by_label["Flash J-Link OpenOCD BIN Release"][
                    "dependsOn"
                ],
                "Build GCC Release",
            )
            self.assertEqual(len(openocd_by_label), len(openocd_tasks))

            build_script = (output_dir / "do_build.bat").read_text(
                encoding="utf-8"
            )
            self.assertIn('if /I "%~1"=="rebuild"', build_script)
            self.assertIn('if /I "%~1"=="release"', build_script)
            self.assertIn('set "ACTIVE_BUILD_DIR=build-gcc-release"', build_script)
            self.assertIn("--clean-first", build_script)

    def test_bin_flash_tasks_use_project_flash_origin(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            parser = KeilProjectToCMake(str(output_dir / "sample.uvprojx"))
            parser.target_name = "sample"
            parser.device_name = "nRF52840_xxAA"
            parser.cpu = "Cortex-M4"
            parser.flash_origin = 0
            parser._guess_openocd_config = lambda _token: None

            openocd_options = GenerationOptions(
                compiler="gcc",
                debug_probe="stlink",
                debug_backend="openocd",
            ).normalized()
            tasks = parser._build_vscode_tasks(output_dir, openocd_options)[
                "tasks"
            ]
            bin_task = next(
                task for task in tasks if task["label"] == "Flash ST-Link BIN"
            )
            self.assertIn("0x00000000", bin_task["args"][-1])

            jlink_options = GenerationOptions(
                compiler="gcc",
                debug_probe="jlink",
                debug_backend="jlink",
            ).normalized()
            parser._write_jlink_scripts(output_dir, jlink_options)
            debug_script = (
                output_dir / ".vscode" / "jlink_flash_bin.jlink"
            ).read_text(encoding="utf-8")
            release_script = (
                output_dir / ".vscode" / "jlink_flash_bin_release.jlink"
            ).read_text(encoding="utf-8")
            self.assertIn("0x00000000", debug_script)
            self.assertIn("0x00000000", release_script)

            parser.flash_origin = None
            self.assertEqual(parser._flash_load_address_text(), "0x08000000")

    def test_vscode_debug_option_matrix_has_valid_task_references(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            parser = KeilProjectToCMake(str(output_dir / "sample.uvprojx"))
            parser.target_name = "sample"
            parser.device_name = "STM32F407VGTx"
            parser.cpu = "Cortex-M4"
            parser.uv4_path = str(output_dir / "Keil")
            parser._guess_openocd_config = lambda _token: None

            checked = 0
            for compiler in ("gcc", "armclang"):
                for probe in SUPPORTED_DEBUG_PROBES:
                    for backend in SUPPORTED_DEBUG_BACKENDS:
                        try:
                            options = GenerationOptions(
                                compiler=compiler,
                                debug_probe=probe,
                                debug_backend=backend,
                            ).normalized()
                        except ValueError:
                            continue

                        task_data = parser._build_vscode_tasks(
                            output_dir, options
                        )
                        tasks = task_data["tasks"]
                        labels = [task["label"] for task in tasks]
                        self.assertEqual(len(labels), len(set(labels)))
                        label_set = set(labels)
                        for task in tasks:
                            depends_on = task.get("dependsOn")
                            if isinstance(depends_on, str):
                                self.assertIn(depends_on, label_set)

                        launch_data = parser._build_vscode_launch(
                            output_dir, options
                        )
                        for configuration in launch_data["configurations"]:
                            self.assertIn(
                                configuration["preLaunchTask"], label_set
                            )
                        checked += 1

            self.assertEqual(checked, 77)

    def test_debug_backend_probe_compatibility_is_validated(self):
        pyocd_options = GenerationOptions(
            compiler="gcc",
            debug_probe="default",
            debug_backend="pyocd",
        ).normalized()
        parser = KeilProjectToCMake("sample.uvprojx")
        self.assertEqual(parser._selected_probes(pyocd_options), ["daplink"])

        for probe in ("stlink", "jlink", "nulink"):
            with self.subTest(probe=probe):
                with self.assertRaises(ValueError):
                    GenerationOptions(
                        compiler="gcc",
                        debug_probe=probe,
                        debug_backend="pyocd",
                    ).normalized()

        with self.assertRaises(ValueError):
            GenerationOptions(
                compiler="gcc",
                host_os="linux",
                debug_backend="keil",
            ).normalized()

        with self.assertRaises(ValueError):
            GenerationOptions(
                compiler="gcc",
                host_os="windows",
                debug_backend="keil",
            ).normalized()

        self.assertEqual(
            GenerationOptions(
                compiler="armclang",
                host_os="windows",
                debug_backend="keil",
            ).normalized().debug_backend,
            "keil",
        )

    def test_openocd_target_is_specific_or_omitted(self):
        parser = KeilProjectToCMake("sample.uvprojx")
        parser.device_name = "nRF52840_xxAA"
        parser.vendor = "Nordic Semiconductor"
        self.assertEqual(
            parser._default_openocd_target_script(),
            "target/nordic/nrf52.cfg",
        )

        parser.device_name = "N32WB031"
        parser.vendor = "Nationstech"
        parser.target_name = "sample"
        parser.cpu = "Cortex-M0"
        parser._guess_openocd_config = lambda _token: None
        options = GenerationOptions(
            compiler="gcc",
            debug_probe="all",
            debug_backend="all",
        ).normalized()
        tasks = parser._build_vscode_tasks(Path.cwd(), options)["tasks"]
        labels = {task["label"] for task in tasks}
        self.assertFalse(any("OpenOCD" in label for label in labels))
        self.assertIn("Flash J-Link BIN", labels)
        self.assertTrue(
            any("OpenOCD 目标脚本无法确定" in warning for warning in parser.warnings)
        )

    def test_openocd_device_family_mapping_is_specific(self):
        parser = KeilProjectToCMake("sample.uvprojx")
        parser.vendor = "Nordic Semiconductor"
        parser.device_name = "nRF5340_xxAA"
        self.assertEqual(
            parser._default_openocd_target_script(),
            "target/nordic/nrf53.cfg",
        )

        parser.vendor = "Texas Instruments"
        parser.device_name = "CC1352R1F3"
        self.assertIsNone(parser._default_openocd_target_script())

    def test_openocd_maps_supported_stm32_families(self):
        parser = KeilProjectToCMake("sample.uvprojx")
        parser.vendor = "STMicroelectronics"
        mappings = {
            "STM32F205RB": "target/stm32f2x.cfg",
            "STM32G474RE": "target/stm32g4x.cfg",
            "STM32L072CZ": "target/stm32l0.cfg",
            "STM32L152RE": "target/stm32l1.cfg",
            "STM32U585AI": "target/stm32u5x.cfg",
            "STM32WB55RG": "target/stm32wbx.cfg",
            "STM32WL55JC": "target/stm32wlx.cfg",
            "STM32WBA52CG": "target/stm32wbax.cfg",
        }

        for device, expected in mappings.items():
            with self.subTest(device=device):
                parser.device_name = device
                self.assertEqual(
                    parser._default_openocd_target_script(), expected
                )

    def test_openocd_jlink_wrapper_selects_swd_before_target(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            parser = KeilProjectToCMake(str(output_dir / "sample.uvprojx"))
            parser.device_name = "nRF52840_xxAA"
            parser.vendor = "Nordic Semiconductor"
            parser._guess_openocd_config = lambda _token: None

            label, configs = parser._resolve_openocd_config_arguments(
                output_dir, "jlink"
            )

            self.assertEqual(label, "J-Link")
            self.assertEqual(
                configs,
                ["${workspaceFolder}/.vscode/openocd-jlink-swd.cfg"],
            )
            wrapper = (
                output_dir / ".vscode" / "openocd-jlink-swd.cfg"
            ).read_text(encoding="utf-8")
            self.assertEqual(
                wrapper.splitlines(),
                [
                    "source [find interface/jlink.cfg]",
                    "transport select swd",
                    "source [find target/nordic/nrf52.cfg]",
                ],
            )

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
                    GenerationOptions(
                        compiler="gcc",
                        generator="Ninja",
                        debug_probe="all",
                        debug_backend="all",
                    ),
                )
            )

            root_vscode = root / ".vscode"
            settings = json.loads(
                (root_vscode / "settings.json").read_text(encoding="utf-8")
            )
            tasks = json.loads(
                (root_vscode / "tasks.json").read_text(encoding="utf-8")
            )["tasks"]
            launch = json.loads(
                (root_vscode / "launch.json").read_text(encoding="utf-8")
            )["configurations"]
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
            jlink_openocd = next(
                configuration
                for configuration in launch
                if configuration.get("servertype") == "openocd"
                and "J-Link" in configuration["name"]
            )
            self.assertEqual(
                jlink_openocd["configFiles"],
                [
                    "${workspaceFolder}/MDK-ARM/cmake-generated/"
                    ".vscode/openocd-jlink-swd.cfg"
                ],
            )
            self.assertTrue(
                (output_dir / ".vscode" / "openocd-jlink-swd.cfg").is_file()
            )

    def test_legacy_generated_root_vscode_tasks_are_upgraded(self):
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

            root_vscode = root / ".vscode"
            root_vscode.mkdir()
            (root / "compile_commands.json").write_text(
                "stale compile commands\n", encoding="utf-8"
            )
            (root_vscode / "settings.json").write_text(
                json.dumps(
                    {
                        "cmake.buildDirectory": "${workspaceFolder}/build-gcc",
                        "cmake.copyCompileCommands": (
                            "${workspaceFolder}/compile_commands.json"
                        ),
                        "C_Cpp.default.compileCommands": (
                            "${workspaceFolder}/build-gcc/compile_commands.json"
                        ),
                    }
                ),
                encoding="utf-8",
            )
            (root_vscode / "tasks.json").write_text(
                json.dumps(
                    {
                        "version": "2.0.0",
                        "tasks": [
                            {
                                "label": "Configure GCC Debug",
                                "command": "cmake",
                                "args": [
                                    "-S",
                                    "${workspaceFolder}",
                                    "-B",
                                    "${workspaceFolder}/build-gcc",
                                    "-DCMAKE_BUILD_TYPE=Debug",
                                ],
                            },
                            {
                                "label": "Build GCC Debug",
                                "command": "cmake",
                                "args": ["--build", "build-gcc"],
                            },
                            {"label": "Flash ST-Link BIN"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)
            parser.project_dir = project_dir
            parser.source_files.append(str(external_source))
            output_dir = project_dir / "cmake-generated"

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
                (root_vscode / "tasks.json").read_text(encoding="utf-8")
            )["tasks"]
            labels = {task["label"] for task in tasks}
            self.assertIn("Flash ST-Link BIN", labels)
            self.assertIn("Flash DAPLink BIN", labels)
            self.assertIn("Flash Nu-Link BIN", labels)
            self.assertIn("Flash ULINK BIN", labels)
            self.assertIn("Flash J-Link BIN", labels)
            self.assertTrue(
                (root_vscode / "keilforge-workspace.json").is_file()
            )
            self.assertFalse((root / "compile_commands.json").exists())

    def test_legacy_armclang_vscode_without_flash_tasks_is_detected(self):
        with TemporaryDirectory() as temp_dir:
            vscode_dir = Path(temp_dir)
            (vscode_dir / "settings.json").write_text(
                json.dumps(
                    {
                        "cmake.buildDirectory": "${workspaceFolder}/build",
                        "cmake.copyCompileCommands": (
                            "${workspaceFolder}/compile_commands.json"
                        ),
                        "C_Cpp.default.compileCommands": (
                            "${workspaceFolder}/build/compile_commands.json"
                        ),
                    }
                ),
                encoding="utf-8",
            )
            (vscode_dir / "tasks.json").write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "label": "Configure ArmClang Debug",
                                "command": "cmake",
                                "args": [
                                    "-S",
                                    "${workspaceFolder}",
                                    "-B",
                                    "${workspaceFolder}/build",
                                    "-DCMAKE_BUILD_TYPE=Debug",
                                ],
                            },
                            {
                                "label": "Build ArmClang Debug",
                                "command": "cmake",
                                "args": ["--build", "build"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(
                KeilProjectToCMake._is_legacy_generated_vscode_configuration(
                    vscode_dir
                )
            )

    def test_user_vscode_tasks_are_not_treated_as_legacy_generated_files(self):
        with TemporaryDirectory() as temp_dir:
            vscode_dir = Path(temp_dir)
            (vscode_dir / "settings.json").write_text(
                json.dumps(
                    {
                        "cmake.buildDirectory": "${workspaceFolder}/build",
                        "cmake.copyCompileCommands": (
                            "${workspaceFolder}/compile_commands.json"
                        ),
                        "C_Cpp.default.compileCommands": (
                            "${workspaceFolder}/build/compile_commands.json"
                        ),
                    }
                ),
                encoding="utf-8",
            )
            (vscode_dir / "tasks.json").write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "label": "Configure ArmClang Debug",
                                "command": "cmake",
                                "args": [
                                    "-S",
                                    "${workspaceFolder}",
                                    "-B",
                                    "${workspaceFolder}/build",
                                    "-DCMAKE_BUILD_TYPE=Debug",
                                ],
                            },
                            {
                                "label": "Build ArmClang Debug",
                                "command": "cmake",
                                "args": ["--build", "build"],
                            },
                            {
                                "label": "User Packaging Task",
                                "command": "custom-packager",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertFalse(
                KeilProjectToCMake._is_legacy_generated_vscode_configuration(
                    vscode_dir
                )
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
            self.assertIn("find_program(KEIL_GCC_COMPILER_CACHE", cmake_text)
            self.assertIn('set(CMAKE_${KEIL_LANGUAGE}_FLAGS_RELEASE "")', cmake_text)
            self.assertIn("    -pipe\n", cmake_text)
            self.assertIn('$<$<CONFIG:Debug>:-Og>', cmake_text)
            self.assertIn(
                '$<$<NOT:$<CONFIG:Debug>>:${KEIL_OPTIMIZATION_FLAG}>',
                cmake_text,
            )
            self.assertIn('$<$<CONFIG:Debug>:-g2>', cmake_text)
            self.assertNotIn('$<$<CONFIG:Debug>:-g3>', cmake_text)

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
            self.assertIn("    -fshort-wchar\n", cmake_text)
            self.assertIn(
                'set(KEIL_GCC_PROJECT_LTO_DEFAULT "ON")',
                cmake_text,
            )
            self.assertEqual(
                cmake_text.count(
                    "$<AND:$<BOOL:${KEIL_GCC_ENABLE_LTO}>,$<NOT:$<CONFIG:Debug>>>"
                ),
                4,
            )

    def test_gcc_forced_include_supports_output_directory_with_spaces(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)
            output = root / "generated output"

            self.assertTrue(
                parser.generate_cmake(
                    str(output),
                    False,
                    GenerationOptions(compiler="gcc", generator="Ninja"),
                ),
                parser.last_error,
            )

            cmake_text = (output / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn(
                '"$<$<COMPILE_LANGUAGE:C,CXX>:-include>"', cmake_text
            )
            self.assertIn(
                '"$<$<COMPILE_LANGUAGE:C,CXX>:'
                '${CMAKE_CURRENT_LIST_DIR}/cmake/gcc_compat.h>"',
                cmake_text,
            )
            self.assertNotIn("SHELL:-include", cmake_text)

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
            self.assertEqual(
                Path(generated).parent,
                (root / "out" / "cmake" / "sources").resolve(),
            )
            startup_text = Path(generated).read_text(encoding="utf-8")
            self.assertIn(".extern extend_sram_512k", startup_text)
            self.assertLess(
                startup_text.index("    bl extend_sram_512k"),
                startup_text.index("    bl SystemInit"),
            )

    def test_patched_core_source_uses_owned_directory(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            source = root / "core_cm3.c"
            source.write_text(
                '__ASM volatile ("strexb %0, %2, [%1]" : "=r" (result) : '
                '"r" (addr), "r" (value) );\n',
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))

            generated = parser._generate_patched_core_cm3_source(
                source, str(root / "out")
            )

            self.assertIsNotNone(generated)
            generated_path = Path(generated)
            self.assertEqual(
                generated_path.parent,
                (root / "out" / "cmake" / "sources").resolve(),
            )
            KeilProjectToCMake._clean_generated_cmake_assets(root / "out")
            self.assertFalse(generated_path.exists())

    def test_armclang_patches_legacy_core_naked_functions(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "core_cm3.c"
            source.write_text(
                textwrap.dedent(
                    """\
                    #include <stdint.h>
                    #define __ASM __asm
                    uint32_t __get_PSP(void) __attribute__( ( naked ) );
                    uint32_t __get_PSP(void)
                    {
                      uint32_t result=0;
                      __ASM volatile ("MRS %0, psp\\n\\t"
                                      "MOV r0, %0 \\n\\t"
                                      "BX lr \\n\\t" : "=r" (result) );
                      return(result);
                    }
                    void __set_PSP(uint32_t topOfProcStack) __attribute__( ( naked ) );
                    void __set_PSP(uint32_t topOfProcStack)
                    {
                      __ASM volatile ("MSR psp, %0\\n\\t"
                                      "BX lr \\n\\t" : : "r" (topOfProcStack) );
                    }
                    uint32_t __get_MSP(void) __attribute__( ( naked ) );
                    uint32_t __get_MSP(void)
                    {
                      uint32_t result=0;
                      __ASM volatile ("MRS %0, msp\\n\\t"
                                      "MOV r0, %0 \\n\\t"
                                      "BX lr \\n\\t" : "=r" (result) );
                      return(result);
                    }
                    void __set_MSP(uint32_t topOfMainStack) __attribute__( ( naked ) );
                    void __set_MSP(uint32_t topOfMainStack)
                    {
                      __ASM volatile ("MSR msp, %0\\n\\t"
                                      "BX lr \\n\\t" : : "r" (topOfMainStack) );
                    }
                    """
                ),
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]

            prepared = parser._prepared_armclang_source_files(str(root / "out"))

            self.assertEqual(len(prepared), 1)
            generated = prepared[0]
            self.assertNotEqual(Path(generated).resolve(), source.resolve())
            patched = Path(generated).read_text(encoding="utf-8")
            self.assertNotIn("naked", patched)
            self.assertNotIn("BX lr", patched)
            self.assertEqual(patched.count("MRS %0"), 2)
            self.assertEqual(patched.count("MSR "), 2)
            self.assertTrue(Path(generated).name.endswith("_armclang.c"))
            self.assertEqual(
                parser._prepared_source_origins[
                    parser._source_control_key(generated)
                ],
                parser._source_control_key(source),
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

    def test_gcc_linker_subdirectory_includes_are_rewritten(self):
        gcc = shutil.which("arm-none-eabi-gcc")
        if not gcc:
            self.skipTest("GNU Arm compiler unavailable")

        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            linker_root = root / "vendor-linker"
            fragment = linker_root / "fragments" / "memory.ld"
            fragment.parent.mkdir(parents=True)
            fragment.write_text(
                "MEMORY { FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 64K }\n",
                encoding="utf-8",
            )
            source_linker = linker_root / "device.ld"
            source_linker.write_text(
                "INCLUDE fragments/memory.ld\n"
                "SECTIONS { .text : { *(.text*) } > FLASH }\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))

            prepared = Path(
                parser._prepare_gcc_linker_script(
                    str(source_linker), str(root / "output")
                )
            )
            prepared_text = prepared.read_text(encoding="utf-8")

            self.assertIn('INCLUDE "memory.ld"', prepared_text)
            self.assertTrue((prepared.parent / "memory.ld").is_file())
            self.assertFalse((prepared.parent / "fragments").exists())

            startup = root / "startup.S"
            startup.write_text(
                ".syntax unified\n"
                ".thumb\n"
                ".global _start\n"
                ".section .text\n"
                "_start:\n"
                "    b .\n",
                encoding="utf-8",
            )
            startup_object = root / "startup.o"
            subprocess.run(
                [
                    gcc,
                    "-mcpu=cortex-m3",
                    "-mthumb",
                    "-c",
                    str(startup),
                    "-o",
                    str(startup_object),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            firmware = root / "firmware.elf"
            subprocess.run(
                [
                    gcc,
                    "-mcpu=cortex-m3",
                    "-mthumb",
                    "-nostdlib",
                    f"-T{prepared}",
                    f"-L{prepared.parent}",
                    str(startup_object),
                    "-o",
                    str(firmware),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(firmware.is_file())

    def test_armcc_library_conversion_preserves_duplicate_member_names(self):
        tools = {
            name: shutil.which(name)
            for name in (
                "arm-none-eabi-gcc",
                "arm-none-eabi-ar",
                "arm-none-eabi-objcopy",
                "arm-none-eabi-nm",
            )
        }
        missing = [name for name, executable in tools.items() if not executable]
        if missing:
            self.skipTest("GNU Arm tools unavailable: " + ", ".join(missing))

        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            objects = []
            for directory_name, symbol in (
                ("first", "first_member_symbol"),
                ("second", "second_member_symbol"),
            ):
                source_dir = root / directory_name
                source_dir.mkdir()
                source = source_dir / "member.c"
                source.write_text(
                    f"int {symbol}(void) {{ return 0; }}\n", encoding="utf-8"
                )
                obj = source_dir / "member.o"
                subprocess.run(
                    [tools["arm-none-eabi-gcc"], "-c", str(source), "-o", str(obj)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                objects.append(obj)

            library = root / "duplicate.lib"
            subprocess.run(
                [tools["arm-none-eabi-ar"], "qc", str(library), *map(str, objects)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [tools["arm-none-eabi-ar"], "s", str(library)],
                check=True,
                capture_output=True,
                text=True,
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))

            converted = parser._convert_armcc_library_for_gcc(
                library, str(root / "output")
            )

            self.assertIsNotNone(converted)
            symbols = subprocess.run(
                [
                    tools["arm-none-eabi-nm"],
                    "-g",
                    "--defined-only",
                    str(converted),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(symbols.count("first_member_symbol"), 1)
            self.assertEqual(symbols.count("second_member_symbol"), 1)

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

    def test_preserves_quoted_define_and_armclang_library_path(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            library = root / "vendor libs" / "algorithm.lib"
            library.parent.mkdir()
            library.write_bytes(b"library fixture")
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.target_name = "sample"
            parser.output_name = "sample"
            parser.defines = set(
                parser._split_defines('MESSAGE="hello world",VALUE=1')
            )
            parser.libraries = [str(library)]
            output = io.StringIO()

            parser._write_armclang_cmake_content(
                output,
                str(root / "generated"),
                GenerationOptions(compiler="armclang").normalized(),
            )

            cmake_text = output.getvalue()
            self.assertIn('    "-DMESSAGE=\\"hello world\\""', cmake_text)
            self.assertIn(
                '    "${CMAKE_CURRENT_LIST_DIR}/../vendor libs/algorithm.lib"',
                cmake_text,
            )

    def test_linker_misc_preserves_quoted_file_and_flag_arguments(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            symbol_file = root / "objects with spaces" / "symbols.o"
            symbol_file.parent.mkdir()
            symbol_file.write_text(
                "#<SYMDEFS>#\n0x08000100 X exported_symbol\n",
                encoding="utf-8",
            )
            target = ET.fromstring(
                '<Target><LDads><Misc>"objects with spaces/symbols.o" '
                '--keep="symbol one"</Misc></LDads></Target>'
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))

            parser._parse_linker_settings(target)

            self.assertEqual(parser.linker_inputs, [str(symbol_file)])
            self.assertEqual(parser.linker_flags[0], '"--keep=symbol one"')

    def test_compiler_misc_preserves_quoted_arguments(self):
        target = ET.fromstring(
            "<Target><Cads><VariousControls><MiscControls>"
            '-include &quot;include files/forced header.h&quot; '
            "--diag_suppress=1234"
            "</MiscControls></VariousControls></Cads></Target>"
        )
        parser = KeilProjectToCMake("sample.uvprojx")

        parser._parse_compiler_settings(target)

        self.assertEqual(
            parser.c_cpp_flags[-3:],
            [
                "-include",
                '"include files/forced header.h"',
                "--diag_suppress=1234",
            ],
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
            "/*\n"
            "static void commented_out(uint32_t value)\n"
            "{\n"
            "}\n"
            "*/\n"
        )
        names = KeilProjectToCMake._find_static_function_names(source)
        header = (
            "typedef void (*handler_t)(int event);\n"
            "void worker(uint32_t value);\n"
            "void commented_out(uint32_t value);\n"
        )

        patched, count = KeilProjectToCMake._remove_conflicting_static_prototypes(
            header, names
        )

        self.assertEqual(names, {"worker", "visit"})
        self.assertEqual(count, 1)
        self.assertIn("typedef void (*handler_t)(int event);", patched)
        self.assertNotIn("void worker", patched)
        self.assertIn("void commented_out", patched)

    def test_source_scanners_ignore_raw_strings_and_continued_comments(self):
        raw_example = (
            'const char *example = R"doc("\n'
            "static void raw_fake(void) {}\n"
            "HAL_UART_Transmit(&RawUartHandle, 0, 0, 0);\n"
            ')doc";\n'
        )
        continued_comment = (
            "// documented example \\\n"
            "static void comment_fake(void) { "
            "HAL_UART_Transmit(&CommentUartHandle, 0, 0, 0); }\n"
        )
        source = (
            "UART_HandleTypeDef UartHandle;\n"
            + raw_example
            + continued_comment
            + "static void worker(void) {}\n"
            + "void send(void) { "
            + "HAL_UART_Transmit(&DebugUartHandle, 0, 0, 0); }\n"
        )

        names = KeilProjectToCMake._find_static_function_names(source)
        patched, count = KeilProjectToCMake._patch_missing_uart_handle_alias(source)

        self.assertEqual(names, {"worker"})
        self.assertEqual(count, 1)
        self.assertIn(raw_example, patched)
        self.assertIn(continued_comment, patched)
        self.assertIn("HAL_UART_Transmit(&UartHandle, 0, 0, 0);", patched)

    def test_source_mask_preserves_positions_and_line_endings(self):
        spans = [
            '"quoted \\\" value"',
            '"even backslashes \\\\"',
            "'\\''",
            "/* block\r\ncomment\n */",
            '// line \\\ncontinued \\\r\nagain',
            '// line \\\rcontinued',
            'R"(raw " // /* text)"',
            'u8R"tag(raw \\"\r\ntext)tag"',
            'uR"tag(raw)tag"',
            'UR"tag(raw)tag"',
            'LR"tag(raw)tag"',
        ]
        for span in spans:
            with self.subTest(span=span):
                prefix = "int before; "
                suffix = "\r\nint after;"
                expected = prefix + "".join(
                    char if char in "\r\n" else " " for char in span
                ) + suffix
                self.assertEqual(
                    KeilProjectToCMake._mask_c_comments_and_literals(
                        prefix + span + suffix
                    ),
                    expected,
                )

    def test_source_mask_handles_unterminated_spans_and_raw_prefix_boundaries(self):
        for span in ('"unterminated\\', "'unterminated", "/* open\r\n", 'R"tag(open'):
            with self.subTest(span=span):
                self.assertEqual(
                    KeilProjectToCMake._mask_c_comments_and_literals("keep; " + span),
                    "keep; " + "".join(
                        char if char in "\r\n" else " " for char in span
                    ),
                )
        for prefix in ("nameR", "_R", "\u00e9R", "nameu8R"):
            with self.subTest(prefix=prefix):
                first, last = '"tag(first"', '"last)tag"'
                source = prefix + first + " + visible + " + last
                self.assertEqual(
                    KeilProjectToCMake._mask_c_comments_and_literals(source),
                    prefix + " " * len(first) + " + visible + " + " " * len(last),
                )

    def test_patches_missing_uart_handle_when_single_handle_exists(self):
        source = (
            "UART_HandleTypeDef Uart1_Handle;\n"
            "void write_char(void) { HAL_UART_Transmit(&DebugUartHandle, 0, 0, 0); }\n"
        )
        patched, count = KeilProjectToCMake._patch_missing_uart_handle_alias(source)

        self.assertEqual(count, 1)
        self.assertIn("HAL_UART_Transmit(&Uart1_Handle", patched)

    def test_uart_handle_patch_preserves_non_uart_handles(self):
        source = (
            "UART_HandleTypeDef UartHandle;\n"
            "SPI_HandleTypeDef SpiHandle;\n"
            "I2C_HandleTypeDef I2cHandle;\n"
            "void send(void) {\n"
            "    HAL_SPI_Transmit(&SpiHandle, 0, 0, 0);\n"
            "    HAL_I2C_Master_Transmit(&I2cHandle, 0, 0, 0, 0);\n"
            "    send_packet(&PacketHandle);\n"
            "    HAL_UART_Transmit(&DebugUartHandle, 0, 0, 0);\n"
            "}\n"
            'const char *uart_log = "HAL_UART_Transmit(&LogUartHandle)";\n'
            "// HAL_UART_Transmit(&CommentUartHandle, 0, 0, 0);\n"
            "/* HAL_UART_Transmit(&BlockUartHandle, 0, 0, 0); */\n"
        )

        patched, count = KeilProjectToCMake._patch_missing_uart_handle_alias(source)

        self.assertEqual(count, 1)
        self.assertIn("HAL_SPI_Transmit(&SpiHandle", patched)
        self.assertIn("HAL_I2C_Master_Transmit(&I2cHandle", patched)
        self.assertIn("send_packet(&PacketHandle", patched)
        self.assertIn("HAL_UART_Transmit(&UartHandle", patched)
        self.assertIn('"HAL_UART_Transmit(&LogUartHandle)"', patched)
        self.assertIn("HAL_UART_Transmit(&CommentUartHandle", patched)
        self.assertIn("HAL_UART_Transmit(&BlockUartHandle", patched)

    def test_build_validator_classifies_configure_failure(self):
        validator = BuildValidator(timeout_seconds=10)
        completed = CommandResult(
            command=["cmake"],
            return_code=1,
            stdout="",
            stderr="CMake Error: arm-none-eabi-gcc was not found",
            duration_seconds=0.1,
        )

        with TemporaryDirectory() as temp_dir, patch.object(
            validator, "_run", return_value=completed
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
        configure = CommandResult(
            command=["cmake"],
            return_code=0,
            stdout="configured",
            stderr="",
            duration_seconds=0.1,
        )
        build = CommandResult(
            command=["cmake", "--build"],
            return_code=1,
            stdout="",
            stderr="ld: undefined reference to `_sidata'",
            duration_seconds=0.1,
        )

        with TemporaryDirectory() as temp_dir, patch.object(
            validator, "_run", side_effect=[configure, build]
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

    def test_build_validator_rejects_missing_artifacts(self):
        validator = BuildValidator(timeout_seconds=10)
        configure = CommandResult(
            command=["cmake"],
            return_code=0,
            stdout="configured",
            stderr="",
            duration_seconds=0.1,
        )
        build = CommandResult(
            command=["cmake", "--build"],
            return_code=0,
            stdout="built",
            stderr="",
            duration_seconds=0.1,
        )

        with TemporaryDirectory() as temp_dir, patch.object(
            validator, "_run", side_effect=[configure, build]
        ):
            missing = Path(temp_dir) / "build-gcc" / "firmware.elf"
            report = validator.validate(
                Path(temp_dir),
                GenerationOptions(compiler="gcc", generator="Ninja").normalized(),
                [missing],
            )

        self.assertEqual(report.status, "artifact_failed")
        self.assertIn(
            "missing_artifact",
            [diagnostic.category for diagnostic in report.diagnostics],
        )

    def test_build_validator_selects_debug_for_multiconfig_generator(self):
        validator = BuildValidator(timeout_seconds=10)
        configure = CommandResult(
            command=["cmake"],
            return_code=0,
            stdout="configured",
            stderr="",
            duration_seconds=0.1,
        )
        build = CommandResult(
            command=["cmake", "--build"],
            return_code=0,
            stdout="built",
            stderr="",
            duration_seconds=0.1,
        )

        with TemporaryDirectory() as temp_dir, patch.object(
            validator, "_run", side_effect=[configure, build]
        ) as run:
            report = validator.validate(
                Path(temp_dir),
                GenerationOptions(
                    compiler="gcc", generator="Ninja Multi-Config"
                ).normalized(),
            )

        self.assertEqual(report.status, "success")
        build_command = run.call_args_list[1].args[0]
        self.assertEqual(build_command[-2:], ["--config", "Debug"])

    def test_build_validator_timeout_terminates_process_group(self):
        validator = BuildValidator(timeout_seconds=0.1)

        with TemporaryDirectory() as temp_dir, patch.object(
            validator,
            "_terminate_process_tree",
            wraps=validator._terminate_process_tree,
        ) as terminate:
            result = validator._run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                Path(temp_dir),
                "build",
            )

        self.assertIn("timed out", result.error)
        terminate.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32", "Windows taskkill test")
    def test_windows_process_tree_termination_uses_recursive_taskkill(self):
        process = Mock()
        process.pid = 1234
        process.poll.return_value = None
        completed = Mock(returncode=0)

        with patch("keil_to_cmake.subprocess.run", return_value=completed) as run:
            BuildValidator._terminate_process_tree(process)

        self.assertEqual(
            run.call_args.args[0],
            ["taskkill", "/PID", "1234", "/T", "/F"],
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

    def test_validator_reports_stage_counts_and_live_plain_logs(self):
        events, logs = [], []
        validator = BuildValidator(activity_callback=events.append, log_callback=logs.append)
        with TemporaryDirectory() as temp_dir:
            result = validator._run(
                [sys.executable, '-c', "print('[2/4] Building C object main.c'); print('[3/4] Linking C executable app.elf')"],
                Path(temp_dir), 'build',
            )
        self.assertTrue(result.succeeded)
        self.assertEqual((events[0].phase, events[0].current, events[0].total), ('compile', 2, 4))
        self.assertEqual(events[-1].phase, 'link')
        self.assertIn('main.c', logs[0])
        validator._handle_stream_line('build', '[ 42%] Building C object')
        self.assertEqual(events[-1].unit, 'percent')

    def test_cancel_before_configure_never_spawns_a_process(self):
        validator = BuildValidator(cancel_check=lambda: True)
        with TemporaryDirectory() as temp_dir, patch('keil_to_cmake.subprocess.Popen') as spawn:
            report = validator.validate(Path(temp_dir), GenerationOptions().normalized())
        self.assertFalse(spawn.called)
        self.assertEqual(report.status, 'cancelled')
        self.assertTrue(report.configure.cancelled)
        self.assertFalse(report.configure.succeeded)
        self.assertIsNone(report.build)

    def test_cancel_running_build_retains_logs_and_stops_promptly(self):
        cancellation = threading.Event()
        logs = []
        def on_log(line):
            logs.append(line)
            if 'ready' in line:
                cancellation.set()
        validator = BuildValidator(cancel_check=cancellation.is_set, log_callback=on_log)
        with TemporaryDirectory() as temp_dir:
            started = time.monotonic()
            result = validator._run([sys.executable, '-c',
                "import time; print('ready', flush=True); time.sleep(30)"], Path(temp_dir), 'build')
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result.cancelled)
        self.assertIn('ready', result.stdout)
        self.assertTrue(logs)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process job test')
    def test_cancel_stops_compiler_descendants_when_taskkill_is_unavailable(self):
        import ctypes
        from ctypes import wintypes
        cancellation = threading.Event()
        child_pid = []
        def on_log(line):
            if line.startswith('child='):
                child_pid.append(int(line.split('=')[1]))
                cancellation.set()
        script = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "print('child='+str(child.pid),flush=True); time.sleep(30)"
        )
        validator = BuildValidator(cancel_check=cancellation.is_set, log_callback=on_log)
        with TemporaryDirectory() as temp_dir, patch('keil_to_cmake.subprocess.run', side_effect=OSError('blocked')):
            result = validator._run([sys.executable, '-c', script], Path(temp_dir), 'build')
        self.assertTrue(result.cancelled)
        self.assertTrue(child_pid)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, child_pid[0])
        if handle:
            try:
                exit_code = wintypes.DWORD()
                self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
                self.assertNotEqual(exit_code.value, 259)
            finally:
                kernel.CloseHandle(handle)

    def test_cancel_generation_restores_parser_and_allows_retry(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self._write_minimal_project(root)
            parser = KeilProjectToCMake(str(project), target_name='Second')
            self.assertTrue(parser.parse())
            sources = list(parser.source_files)
            def cancel(value, message):
                raise OperationCancelled()
            with self.assertRaises(OperationCancelled):
                parser.generate_cmake(str(root / 'cancel-output'), False, progress_callback=cancel)
            self.assertEqual(parser.source_files, sources)
            self.assertIsNone(parser._generation_progress_callback)
            self.assertTrue(parser.generate_cmake(str(root / 'cancel-output'), False))

    def test_log_callback_exception_terminates_running_process(self):
        validator = BuildValidator(log_callback=lambda line: (_ for _ in ()).throw(ValueError('consumer failed')))
        with TemporaryDirectory() as temp_dir, patch.object(
            validator, '_terminate_process_tree', wraps=validator._terminate_process_tree
        ) as terminate:
            with self.assertRaisesRegex(ValueError, 'consumer failed'):
                validator._run([sys.executable, '-c',
                    "import time; print('ready', flush=True); time.sleep(30)"], Path(temp_dir), 'build')
        terminate.assert_called_once()

    def test_generation_cleans_stale_cmake_assets_only(self):
        with TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            stale_header = output / "cmake" / "compat-includes" / "StdioStream.h"
            user_cmake = output / "cmake" / "user-owned.cmake"
            legacy_startup = output / "cmake" / "startup_device_gcc.S"
            legacy_core = output / "cmake" / "core_cm3_gcc.c"
            legacy_symbols = output / "cmake" / "symbols_0123456789ab.ld"
            user_startup = output / "cmake" / "startup_custom_gcc.S"
            user_linker = output / "cmake" / "custom.ld"
            build_marker = output / "build-gcc" / "keep.txt"
            stale_header.parent.mkdir(parents=True)
            build_marker.parent.mkdir(parents=True)
            stale_header.write_text("stale", encoding="utf-8")
            user_cmake.write_text("user content", encoding="utf-8")
            legacy_startup.write_text(
                "/* Auto-generated GNU startup file converted from a Keil "
                "ARMASM startup file. */\n",
                encoding="utf-8",
            )
            legacy_core.write_text(
                '__ASM volatile ("strexb %0" : "=&r" (result));\n',
                encoding="utf-8",
            )
            legacy_symbols.write_text(
                "/* Converted from ARM Linker SYMDEFS. */\n"
                "PROVIDE(old_symbol = 0x1234);\n",
                encoding="utf-8",
            )
            user_startup.write_text("custom startup\n", encoding="utf-8")
            user_linker.write_text(
                "SECTIONS { .text : { *(.text*) } }\n", encoding="utf-8"
            )
            build_marker.write_text("keep", encoding="utf-8")

            KeilProjectToCMake._clean_generated_cmake_assets(output)

            self.assertFalse(stale_header.exists())
            self.assertFalse(legacy_startup.exists())
            self.assertFalse(legacy_core.exists())
            self.assertFalse(legacy_symbols.exists())
            self.assertEqual(user_cmake.read_text(encoding="utf-8"), "user content")
            self.assertEqual(
                user_startup.read_text(encoding="utf-8"), "custom startup\n"
            )
            self.assertTrue(user_linker.is_file())
            self.assertEqual(build_marker.read_text(encoding="utf-8"), "keep")

    def test_generation_migrates_legacy_generated_cmake_files(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            output = root / "out"
            cmake_dir = output / "cmake"
            cmake_dir.mkdir(parents=True)
            (output / "CMakeLists.txt").write_text(
                "old incorrect configuration\n", encoding="utf-8"
            )
            (cmake_dir / "armclang.cmake").write_text(
                "old incorrect toolchain\n", encoding="utf-8"
            )
            (cmake_dir / "startup_device_gcc.S").write_text(
                "/* Auto-generated GNU startup file converted from a Keil "
                "ARMASM startup file. */\nold incorrect startup\n",
                encoding="utf-8",
            )
            (cmake_dir / "symbols_0123456789ab.ld").write_text(
                "/* Converted from ARM Linker SYMDEFS. */\n"
                "PROVIDE(old_symbol = 0x1234);\n",
                encoding="utf-8",
            )
            user_cmake = cmake_dir / "user-owned.cmake"
            user_cmake.write_text("user content\n", encoding="utf-8")

            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)
            self.assertTrue(
                parser.generate_cmake(
                    str(output),
                    False,
                    GenerationOptions(compiler="gcc", generator="Ninja"),
                ),
                parser.last_error,
            )

            self.assertIn(
                "# CMake file generated from Keil uVision project",
                (output / "CMakeLists.txt").read_text(encoding="utf-8"),
            )
            self.assertFalse((cmake_dir / "armclang.cmake").exists())
            self.assertFalse((cmake_dir / "startup_device_gcc.S").exists())
            self.assertFalse((cmake_dir / "symbols_0123456789ab.ld").exists())
            self.assertTrue((cmake_dir / "arm-none-eabi-toolchain.cmake").is_file())
            self.assertEqual(
                user_cmake.read_text(encoding="utf-8"), "user content\n"
            )

    @staticmethod
    def _write_freertos_ports(root: Path) -> tuple[Path, Path]:
        kernel = root / "FreeRTOS" / "Source"
        (kernel / "include").mkdir(parents=True)
        (kernel / "include" / "FreeRTOS.h").write_text("/* FreeRTOS */\n")
        ports = [kernel / "portable" / name / "ARM_CM3" for name in ("RVDS", "GCC")]
        for port in ports:
            port.mkdir(parents=True)
            (port / "port.c").write_text('#include "portmacro.h"\n')
            (port / "portmacro.h").write_text("#define portSTACK_TYPE unsigned long\n")
        return ports[0], ports[1]

    def test_freertos_wrapper_uses_gcc_port_without_changing_original(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            rvds, gcc = self._write_freertos_ports(root)
            wrapper = root / "Src" / "include_port.c"
            original = (
                "#if (__ARMCC_VERSION > 6000000)\n"
                '#include "../FreeRTOS/Source/portable/GCC/ARM_CM3/port.c"\n'
                "#else\n"
                '#include "..\\FreeRTOS\\Source\\portable\\RVDS\\ARM_CM3\\port.c"\n'
                "#endif\n"
                "int wrapper_value = 7;\n"
            )
            wrapper.write_text(original, encoding="utf-8")
            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)
            parser.source_files.append(str(wrapper))
            parser._add_include_path(str(rvds))
            for iteration in range(2):
                output = root / f"gcc-{iteration}"
                self.assertTrue(parser.generate_cmake(
                    str(output), False, GenerationOptions(compiler="gcc")
                ), parser.last_error)
                staged = next((output / "cmake" / "sources").glob("include_port_*.c"))
                content = staged.read_text(encoding="utf-8")
                included = content.split("#else\n", 1)[1].split('"')[1]
                self.assertEqual((staged.parent / included).resolve(), gcc / "port.c")
                self.assertIn("int wrapper_value = 7;", content)
                self.assertNotIn("#define __ARMCC_VERSION", content)
                cmake = (output / "CMakeLists.txt").read_text(encoding="utf-8")
                self.assertIn("portable/GCC/ARM_CM3", cmake)
                self.assertNotIn("portable/RVDS/ARM_CM3", cmake)
                self.assertNotIn("-D__ARMCC_VERSION", cmake)
                self.assertIn(str(rvds), parser.include_paths)
            self.assertEqual(wrapper.read_text(encoding="utf-8"), original)
            armclang = io.StringIO()
            parser._write_armclang_cmake_content(
                armclang, str(root / "armclang"), GenerationOptions(compiler="armclang")
            )
            self.assertIn("portable/RVDS/ARM_CM3", armclang.getvalue())
            self.assertIn("include_port.c", armclang.getvalue())

    def test_freertos_source_and_file_local_includes_use_matching_gcc_port(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rvds, gcc = self._write_freertos_ports(root)
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            source = str(rvds / "port.c")
            parser.source_files = [source]
            parser.source_compile_settings[parser._source_control_key(source)] = (
                SourceCompileSettings(
                    macro_actions={"PORT_CONFIG": "PORT_CONFIG=3"},
                    include_paths=[str(rvds)],
                )
            )
            output = root / "out"
            prepared = parser._prepared_gcc_source_files(str(output))
            self.assertEqual(prepared, [str(gcc / "port.c")])
            settings = io.StringIO()
            parser._write_source_compile_settings(settings, prepared, str(output), "gcc")
            self.assertIn("-DPORT_CONFIG=3", settings.getvalue())
            self.assertIn("portable/GCC/ARM_CM3", settings.getvalue())
            self.assertNotIn("portable/RVDS/ARM_CM3", settings.getvalue())
            self.assertEqual(parser.source_files, [source])

    def test_freertos_mapping_requires_complete_port_in_same_kernel(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for missing in ("port.c", "portmacro.h", "FreeRTOS.h"):
                with self.subTest(missing=missing):
                    rvds, gcc = self._write_freertos_ports(root / missing)
                    if missing == "FreeRTOS.h":
                        (gcc.parents[2] / "include" / missing).unlink()
                    else:
                        (gcc / missing).unlink()
                    parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
                    self.assertEqual(parser._map_source_for_gcc(str(rvds / "port.c")),
                                     str(rvds / "port.c"))
                    parser._add_include_path(str(rvds))
                    self.assertEqual(parser._prepared_gcc_include_paths(str(root / "out")),
                                     [str(rvds)])

    def test_freertos_port_includes_ignore_comments_and_unrelated_includes(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_freertos_ports(root)
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            content = (
                "/*\n"
                '#include "FreeRTOS/Source/portable/RVDS/ARM_CM3/port.c"\n'
                "*/\n"
                '#include "unrelated/port.c"\n'
            )
            self.assertEqual(parser._patch_freertos_port_includes(
                content, root / "wrapper.c", root / "out"
            ), content)

    def test_file_type_three_symdefs_are_linked_for_gcc_and_armclang(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            symbols = uvprojx.parent / "fir_filter_symbol.txt"
            original = (
                "#<SYMDEFS># ARM Linker\n\n"
                "0x0803e001 T FIR_lowpass_filter\n"
                "0x20000000 D shared_data\n"
            )
            symbols.write_text(original, encoding="utf-8")
            tree = ET.parse(uvprojx)
            files = tree.find(".//Target[TargetName='Second']/Groups/Group/Files")
            for name, included in ((symbols.name, True), ("disabled.txt", False)):
                node = ET.SubElement(files, "File")
                ET.SubElement(node, "FileName").text = name
                ET.SubElement(node, "FileType").text = "3"
                ET.SubElement(node, "FilePath").text = name
                if not included:
                    (uvprojx.parent / name).write_text(
                        "#<SYMDEFS>#\n0x08000201 T excluded_symbol\n"
                    )
                    option = ET.SubElement(node, "FileOption")
                    common = ET.SubElement(option, "CommonProperty")
                    ET.SubElement(common, "IncludeInBuild").text = "0"
            tree.write(uvprojx, encoding="utf-8", xml_declaration=True)
            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)
            self.assertEqual(parser.linker_inputs, [str(symbols)])
            output = root / "gcc"
            self.assertTrue(parser.generate_cmake(
                str(output), False, GenerationOptions(compiler="gcc")
            ), parser.last_error)
            fragments = list((output / "cmake" / "linker" / "symbols").glob("*.ld"))
            self.assertEqual(len(fragments), 1)
            self.assertIn("PROVIDE(FIR_lowpass_filter = 0x0803e001);",
                          fragments[0].read_text(encoding="utf-8"))
            self.assertIn("PROVIDE(shared_data = 0x20000000);",
                          fragments[0].read_text(encoding="utf-8"))
            cmake = (output / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn(f'/symbols/{fragments[0].name}', cmake)
            self.assertEqual(parser._gcc_linker_object_inputs(), [])
            self.assertNotIn("excluded_symbol", cmake)
            self.assertTrue(any("SYMDEFS" in warning for warning in parser.warnings))
            armclang = io.StringIO()
            parser._write_armclang_cmake_content(
                armclang, str(root / "armclang"), GenerationOptions(compiler="armclang")
            )
            self.assertIn("fir_filter_symbol.txt", armclang.getvalue())
            self.assertIn('PROPERTIES EXTERNAL_OBJECT TRUE)', armclang.getvalue())
            self.assertEqual(symbols.read_text(encoding="utf-8"), original)

    def test_symbol_fragments_use_managed_linker_directory(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            symbols = root / "legacy.symdefs"
            symbols.write_text(
                "#<SYMDEFS>#\n0x08000100 T exported_symbol\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.linker_inputs = [str(symbols)]

            fragments = parser._generate_gcc_symbol_fragments(str(root / "out"))

            self.assertEqual(len(fragments), 1)
            fragment = Path(fragments[0])
            self.assertEqual(fragment.parent.name, "symbols")
            self.assertEqual(fragment.parent.parent.name, "linker")
            self.assertIn(
                "PROVIDE(exported_symbol = 0x08000100);",
                fragment.read_text(encoding="utf-8"),
            )

    def test_gcc_artifact_target_is_host_independent(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            with patch("keil_to_cmake.find_keil_mdk_root", return_value=None), patch(
                "keil_to_cmake.get_keil_mdk_info", return_value=None
            ):
                parser = KeilProjectToCMake(
                    str(uvprojx),
                    str(root / "missing_mdk"),
                    target_name="Second",
                )
            self.assertTrue(parser.parse(), parser.last_error)
            for host_os in ("linux", "macos"):
                with self.subTest(host_os=host_os):
                    output = root / f"output-{host_os}"
                    self.assertTrue(
                        parser.generate_cmake(
                            str(output),
                            False,
                            GenerationOptions(
                                compiler="gcc",
                                host_os=host_os,
                                generator="Ninja",
                            ),
                        ),
                        parser.last_error,
                    )

                    cmake = (output / "CMakeLists.txt").read_text(
                        encoding="utf-8"
                    )
                    self.assertIn('COMMAND "${CMAKE_OBJCOPY}" -O ihex', cmake)
                    self.assertIn('COMMAND "${CMAKE_OBJCOPY}" -O binary', cmake)
                    self.assertIn("cmake/copy_if_exists.cmake", cmake)
                    self.assertTrue(
                        (output / "cmake" / "copy_if_exists.cmake").is_file()
                    )
                    self.assertNotIn("ARTIFACT_SCRIPT", cmake)
                    self.assertNotIn("COMMAND cmd /c", cmake)
                    self.assertNotIn("COMMAND sh ", cmake)

    def test_gcc_cxx_multilib_headers_are_resolved_during_cmake_configure(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            uvprojx = self._write_minimal_project(root)
            cpp_source = root / "Src" / "module.cpp"
            cpp_source.write_text("int module() { return 0; }\n", encoding="utf-8")
            parser = KeilProjectToCMake(str(uvprojx), target_name="Second")
            self.assertTrue(parser.parse(), parser.last_error)
            parser.source_files.append(str(cpp_source))
            output = root / "out"

            self.assertTrue(
                parser.generate_cmake(
                    str(output),
                    False,
                    GenerationOptions(compiler="gcc", generator="Ninja"),
                ),
                parser.last_error,
            )

            cmake = (output / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn("-print-sysroot", cmake)
            self.assertIn("-dumpfullversion", cmake)
            self.assertIn("-print-multi-directory", cmake)
            self.assertIn('string(REPLACE "v7e-m+fp/" "v7+fp/"', cmake)
            self.assertIn(
                "target_include_directories(Second SYSTEM PRIVATE", cmake
            )
            self.assertIn("${ARM_GXX_MULTILIB_STDINC}", cmake)
            self.assertIn("${ARM_GXX_TARGET_STDINC}", cmake)
            self.assertNotIn("arm-gnu-toolchain-13.3.rel1-ming", cmake)

            toolchain = (
                output / "cmake" / "arm-none-eabi-toolchain.cmake"
            ).read_text(encoding="utf-8")
            self.assertIn("ARM_TOOLCHAIN_ROOT", toolchain)
            self.assertNotIn("D:/", toolchain)

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
        commented_example = (
            "// __ASM void documented_delay(uint32_t count) "
            "{ SUBS r0, #1 BNE documented_delay BX LR }\n"
        )
        source += commented_example

        patched, functions = KeilProjectToCMake._patch_armcc_embedded_assembly(
            source
        )

        self.assertEqual(functions, ["system_delay_cycles"])
        self.assertIn("__attribute__((naked))", patched)
        self.assertIn('"subs r0, r0, #1\\n"', patched)
        self.assertIn(commented_example, patched)

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

    def test_pack_include_paths_map_to_repository_vendor_directories(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = root / "firmware"
            project_dir = repository / "MDK-ARM"
            output = project_dir / "cmake-generated"
            (repository / ".git").mkdir(parents=True)
            output.mkdir(parents=True)

            pack_root = root / "keil" / "ARM" / "PACK"
            cmsis_pack = pack_root / "ARM" / "CMSIS" / "5.6.0" / "CMSIS" / "Include"
            peripheral_pack = (
                pack_root
                / "ArteryTek"
                / "AT32F435_437_DFP"
                / "2.2.6"
                / "Device"
                / "Firmware"
                / "Peripherals"
                / "inc"
            )
            device_pack = (
                pack_root
                / "ArteryTek"
                / "AT32F435_437_DFP"
                / "2.2.6"
                / "Device"
                / "Include"
            )
            for path in (cmsis_pack, peripheral_pack, device_pack):
                path.mkdir(parents=True)

            vendor_paths = (
                repository / "vendor" / "CMSIS" / "Include",
                repository / "vendor" / "AT32F435_437_DFP" / "Peripherals" / "inc",
                repository / "vendor" / "AT32F435_437_DFP" / "Device" / "Include",
            )
            for path in vendor_paths:
                path.mkdir(parents=True)
            for source_path, vendor_path in zip(
                (cmsis_pack, peripheral_pack, device_pack),
                vendor_paths,
            ):
                (source_path / "fixture.h").write_text(
                    "#pragma once\n", encoding="utf-8"
                )
                (vendor_path / "fixture.h").write_text(
                    "#pragma once\n", encoding="utf-8"
                )

            parser = KeilProjectToCMake(str(project_dir / "sample.uvprojx"))
            references = [
                parser._gcc_include_reference(str(path), str(output))
                for path in (cmsis_pack, peripheral_pack, device_pack)
            ]

            self.assertEqual(
                references,
                [
                    "${CMAKE_CURRENT_LIST_DIR}/../../vendor/CMSIS/Include",
                    "${CMAKE_CURRENT_LIST_DIR}/../../vendor/AT32F435_437_DFP/Peripherals/inc",
                    "${CMAKE_CURRENT_LIST_DIR}/../../vendor/AT32F435_437_DFP/Device/Include",
                ],
            )

    def test_external_source_and_unmapped_pack_keep_absolute_paths_with_warnings(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = root / "firmware"
            project_dir = repository / "MDK-ARM"
            output = project_dir / "cmake-generated"
            (repository / ".git").mkdir(parents=True)
            output.mkdir(parents=True)
            external_source = root / "external" / "module.c"
            external_source.parent.mkdir()
            external_source.write_text("void module(void) {}\n", encoding="utf-8")
            pack_include = (
                root
                / "keil"
                / "ARM"
                / "PACK"
                / "Vendor"
                / "MissingPack"
                / "1.0.0"
                / "Include"
            )
            pack_include.mkdir(parents=True)

            parser = KeilProjectToCMake(str(project_dir / "sample.uvprojx"))
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                source_reference = parser._portable_cmake_reference(
                    external_source, str(output), "源文件"
                )
                include_reference = parser._gcc_include_reference(
                    str(pack_include), str(output)
                )

            self.assertEqual(source_reference, external_source.as_posix())
            self.assertEqual(include_reference, pack_include.as_posix())
            self.assertTrue(any("仓库树外" in warning for warning in parser.warnings))
            self.assertTrue(any("vendor 映射" in warning for warning in parser.warnings))
            self.assertIn("仓库树外", stderr.getvalue())
            self.assertIn("vendor 映射", stderr.getvalue())

    def test_cmsis_core_include_uses_numeric_version_order(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            for version in ("9.10.0", "10.0.0"):
                include = (
                    root
                    / "ARM"
                    / "PACK"
                    / "ARM"
                    / "CMSIS"
                    / version
                    / "CMSIS"
                    / "Core"
                    / "Include"
                )
                include.mkdir(parents=True)
                (include / "core_cm3.h").write_text(
                    "#pragma once\n", encoding="utf-8"
                )
            parser = KeilProjectToCMake(
                str(root / "sample.uvprojx"), str(root)
            )
            parser.cpu = "Cortex-M3"

            parser._add_cmsis_core_include()

            selected = Path(parser.include_path_order[-1])
            self.assertEqual(selected.parents[2].name, "10.0.0")

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

    def test_legacy_header_scan_reads_once_and_preserves_reference_order(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            includes = root / "include"
            includes.mkdir()
            for name in ("a", "z"):
                (includes / f"{name}.h").write_text(
                    f"__INLINE int {name}(void) {{ return 1; }}\n", encoding="utf-8"
                )
            (includes / "one.h").write_text('#include "a.h"\n', encoding="utf-8")
            repeated = includes / "repeated.h"
            repeated.write_text('#include "z.h"\n', encoding="utf-8")
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.header_files = [str(repeated)]
            parser._add_include_path(str(includes))
            reads = {}
            read_text = Path.read_text

            def tracked_read(path, *args, **kwargs):
                reads[path] = reads.get(path, 0) + 1
                return read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", tracked_read):
                forced = parser._generate_legacy_stdint_compat_headers(str(root / "out"))

            self.assertEqual(forced, ["z.h", "a.h"])
            self.assertEqual(set(reads), set(includes.glob("*.h")))
            self.assertTrue(all(count == 1 for count in reads.values()))

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

    @unittest.skipUnless(sys.platform == "win32", "Windows junction test")
    def test_generation_cleanup_detaches_cmake_directory_link(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            external = root / "external"
            marker = external / "compat-includes" / "keep.h"
            marker.parent.mkdir(parents=True)
            marker.write_text("keep\n", encoding="utf-8")
            output = root / "output"
            output.mkdir()
            cmake_link = output / "cmake"
            completed = subprocess.run(
                [
                    "cmd",
                    "/c",
                    "mklink",
                    "/J",
                    str(cmake_link),
                    str(external),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(completed.stderr or completed.stdout)

            self.assertTrue(KeilProjectToCMake._is_directory_link(cmake_link))
            KeilProjectToCMake._clean_generated_cmake_assets(output)

            self.assertFalse(cmake_link.exists())
            self.assertTrue(marker.is_file())
            cmake_link.mkdir()

            child_external = root / "child-external"
            child_marker = child_external / "keep.h"
            child_external.mkdir()
            child_marker.write_text("keep\n", encoding="utf-8")
            child_link = cmake_link / "includes"
            completed = subprocess.run(
                [
                    "cmd",
                    "/c",
                    "mklink",
                    "/J",
                    str(child_link),
                    str(child_external),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr or completed.stdout,
            )
            KeilProjectToCMake._clean_generated_cmake_assets(output)
            self.assertTrue(child_marker.is_file())

            vscode_external = root / "vscode-external"
            vscode_marker = vscode_external / "settings.json"
            vscode_external.mkdir()
            vscode_marker.write_text("{}\n", encoding="utf-8")
            vscode_link = output / ".vscode"
            completed = subprocess.run(
                [
                    "cmd",
                    "/c",
                    "mklink",
                    "/J",
                    str(vscode_link),
                    str(vscode_external),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr or completed.stdout,
            )
            KeilProjectToCMake._clean_generated_vscode_assets(output)
            self.assertFalse(vscode_link.exists())
            self.assertTrue(vscode_marker.is_file())

    def test_portable_reference_keeps_absolute_when_relpath_is_unavailable(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = root / "repository"
            project_dir = repository / "project"
            output = project_dir / "generated"
            source = repository / "src" / "main.c"
            (repository / ".git").mkdir(parents=True)
            project_dir.mkdir()
            output.mkdir()
            source.parent.mkdir()
            source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
            parser = KeilProjectToCMake(str(project_dir / "sample.uvprojx"))

            with patch("keil_to_cmake.os.path.relpath", side_effect=ValueError):
                reference = parser._portable_cmake_reference(
                    source, str(output), "源文件"
                )
                vscode_reference = parser._vscode_workspace_reference(
                    source.parent, output, "头文件目录"
                )

            self.assertEqual(reference, source.resolve(strict=False).as_posix())
            self.assertNotIn("CMAKE_CURRENT_LIST_DIR", reference)
            self.assertEqual(
                vscode_reference,
                source.parent.resolve(strict=False).as_posix(),
            )
            self.assertNotIn("workspaceFolder", vscode_reference)

    def test_armclang_assets_keep_absolute_when_relpath_is_unavailable(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = root / "repository"
            project_dir = repository / "project"
            output_dir = project_dir / "generated"
            library = repository / "vendor" / "algorithm.lib"
            scatter = repository / "linker" / "memory.sct"
            (repository / ".git").mkdir(parents=True)
            output_dir.mkdir(parents=True)
            library.parent.mkdir()
            scatter.parent.mkdir()
            library.write_bytes(b"library")
            scatter.write_text("LR_IROM1 0x08000000 0x1000 {}\n")
            parser = KeilProjectToCMake(str(project_dir / "sample.uvprojx"))
            parser.target_name = "sample"
            parser.output_name = "sample"
            parser.libraries = [str(library)]
            parser.linker_script = str(scatter)
            generated = io.StringIO()

            with patch("keil_to_cmake.os.path.relpath", side_effect=ValueError):
                parser._write_armclang_cmake_content(
                    generated,
                    str(output_dir),
                    GenerationOptions(compiler="armclang").normalized(),
                )

            cmake_text = generated.getvalue()
            self.assertIn(
                f'set(LINKER_SCRIPT "{scatter.resolve().as_posix()}")',
                cmake_text,
            )
            self.assertIn(
                f'    "{library.resolve().as_posix()}"',
                cmake_text,
            )

    def test_missing_source_search_rejects_ambiguous_same_name_candidates(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sdk = root / "sdk"
            project_dir = sdk / "examples" / "A" / "MDK"
            missing = sdk / "examples" / "A" / "Src" / "main.c"
            candidate_b = sdk / "examples" / "B" / "Src" / "main.c"
            candidate_c = sdk / "examples" / "C" / "Src" / "main.c"
            project_dir.mkdir(parents=True)
            candidate_b.parent.mkdir(parents=True)
            candidate_c.parent.mkdir(parents=True)
            candidate_b.write_text("int from_b(void) { return 1; }\n")
            candidate_c.write_text("int from_c(void) { return 2; }\n")
            parser = KeilProjectToCMake(str(project_dir / "sample.uvprojx"))

            with patch.object(
                parser, "_project_content_root", return_value=sdk
            ):
                resolved = parser._resolve_missing_source(missing)

            self.assertEqual(resolved, missing)
            self.assertTrue(
                any("候选存在歧义" in warning for warning in parser.warnings)
            )

    def test_uart_handle_patch_preserves_names_visible_through_headers(self):
        source = (
            '#include "board.h"\n'
            "UART_HandleTypeDef UartHandle;\n"
            "void send(void) {\n"
            "    HAL_UART_Transmit(&DebugUartHandle, 0, 0, 0);\n"
            "}\n"
        )

        patched, count = KeilProjectToCMake._patch_missing_uart_handle_alias(source)

        self.assertEqual(count, 0)
        self.assertEqual(patched, source)

    def test_pack_vendor_mapping_rejects_different_directory_contents(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = root / "firmware"
            project_dir = repository / "MDK-ARM"
            output = project_dir / "generated"
            pack_include = (
                root
                / "keil"
                / "ARM"
                / "PACK"
                / "Acme"
                / "SharedPack"
                / "1.0.0"
                / "Include"
            )
            legacy_include = repository / "vendor" / "SharedPack" / "Include"
            (repository / ".git").mkdir(parents=True)
            output.mkdir(parents=True)
            pack_include.mkdir(parents=True)
            legacy_include.mkdir(parents=True)
            (pack_include / "board.h").write_text("PACK\n", encoding="utf-8")
            (legacy_include / "board.h").write_text("repository\n", encoding="utf-8")
            parser = KeilProjectToCMake(str(project_dir / "sample.uvprojx"))

            reference = parser._gcc_include_reference(
                str(pack_include), str(output)
            )

            self.assertEqual(reference, pack_include.resolve(strict=False).as_posix())
            self.assertTrue(any("内容不同" in warning for warning in parser.warnings))

    def test_pack_vendor_mapping_preserves_vendor_and_version(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = root / "firmware"
            project_dir = repository / "MDK-ARM"
            output = project_dir / "generated"
            pack_include = (
                root
                / "keil"
                / "ARM"
                / "PACK"
                / "Acme"
                / "SharedPack"
                / "2.0.0"
                / "Include"
            )
            versioned_include = (
                repository
                / "vendor"
                / "Acme"
                / "SharedPack"
                / "2.0.0"
                / "Include"
            )
            legacy_include = repository / "vendor" / "SharedPack" / "Include"
            (repository / ".git").mkdir(parents=True)
            output.mkdir(parents=True)
            pack_include.mkdir(parents=True)
            versioned_include.mkdir(parents=True)
            legacy_include.mkdir(parents=True)
            for path in (pack_include, versioned_include, legacy_include):
                (path / "board.h").write_text(
                    "#pragma once\n", encoding="utf-8"
                )
            parser = KeilProjectToCMake(str(project_dir / "sample.uvprojx"))

            components = parser._pack_path_components(pack_include)
            reference = parser._gcc_include_reference(
                str(pack_include), str(output)
            )

            self.assertEqual(
                components,
                ("Acme", "SharedPack", "2.0.0", ("Include",)),
            )
            self.assertEqual(
                reference,
                "${CMAKE_CURRENT_LIST_DIR}/../../vendor/"
                "Acme/SharedPack/2.0.0/Include",
            )

    def test_jlink_scripts_quote_artifact_paths_with_spaces(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            parser = KeilProjectToCMake(str(output_dir / "sample.uvprojx"))
            parser.target_name = "artifact name"
            parser.device_name = "nRF52840_xxAA"
            parser.cpu = "Cortex-M4"
            options = GenerationOptions(
                compiler="gcc",
                build_dir="build output",
                debug_probe="jlink",
                debug_backend="jlink",
            ).normalized()

            parser._write_jlink_scripts(output_dir, options)

            for script in (
                "jlink_flash_elf.jlink",
                "jlink_flash_hex.jlink",
                "jlink_flash_bin.jlink",
                "jlink_flash_elf_release.jlink",
                "jlink_flash_hex_release.jlink",
                "jlink_flash_bin_release.jlink",
            ):
                content = (
                    output_dir / ".vscode" / script
                ).read_text(encoding="utf-8")
                self.assertRegex(content, r'loadfile "[^"]*artifact name\.')

    def test_gcc_compat_header_declares_missing_namespace_functions(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            include_dir = root / "include"
            include_dir.mkdir()
            header = include_dir / "HAL.h"
            header.write_text(
                "#include <stdint.h>\n"
                "namespace HAL {\n"
                "void Display_SendPixels(const uint16_t*, uint32_t);\n"
                "}\n",
                encoding="utf-8",
            )
            source = root / "display.cpp"
            source.write_text(
                '#include "HAL.h"\n'
                "void HAL::Display_SendPixels(const uint16_t*, uint32_t) {}\n"
                "void HAL::Display_WaitForSendFinish() {}\n"
                "void HAL::Display_SendPixelsBlocking("
                "const uint16_t* pixels, uint32_t len) {}\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]
            parser._add_include_path(str(include_dir))

            compat_path = parser._generate_gcc_cxx_compat_header(
                str(root / "out"), source_files=parser.source_files
            )
            compat = Path(compat_path).read_text(encoding="utf-8")

            self.assertIn(
                "void Display_WaitForSendFinish();",
                compat,
            )
            self.assertIn(
                "void Display_SendPixelsBlocking("
                "const uint16_t* pixels, uint32_t len);",
                compat,
            )
            self.assertNotIn("void Display_SendPixels(", compat)
            self.assertTrue(
                any(
                    "HAL::Display_WaitForSendFinish" in warning
                    and "HAL::Display_SendPixelsBlocking" in warning
                    for warning in parser.warnings
                )
            )

    def test_namespace_include_cache_preserves_owner_directory_precedence(self):
        with TemporaryDirectory() as temp_dir, patch(
            "keil_to_cmake.find_keil_mdk_root", return_value=None
        ), patch("keil_to_cmake.get_keil_mdk_info", return_value=None):
            root = Path(temp_dir)
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            for namespace in ("First", "Second"):
                directory = root / namespace
                directory.mkdir()
                (directory / "api.h").write_text(
                    f"namespace {namespace} {{ void Ready(); }}\n", encoding="utf-8"
                )
                source = directory / "module.cpp"
                source.write_text(
                    '#include "api.h"\n'
                    f"void {namespace}::Ready() {{}}\n"
                    f"void {namespace}::Missing() {{}}\n", encoding="utf-8"
                )
                parser.source_files.append(str(source))
            (root / "api.h").write_text(
                "namespace First { void Missing(); }\n"
                "namespace Second { void Missing(); }\n", encoding="utf-8"
            )
            parser._add_include_path(str(root))

            self.assertEqual(
                parser._gcc_missing_namespace_declarations(),
                [("First", "Missing", "void Missing();"),
                 ("Second", "Missing", "void Missing();")],
            )

    def test_gcc_compat_header_keeps_existing_namespace_declaration(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            header = root / "HAL.h"
            header.write_text(
                "namespace HAL { void Ready(); }\n",
                encoding="utf-8",
            )
            source = root / "hal.cpp"
            source.write_text(
                '#include "HAL.h"\nvoid HAL::Ready() {}\n',
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]
            parser._add_include_path(str(root))

            compat_path = parser._generate_gcc_cxx_compat_header(
                str(root / "out"), source_files=parser.source_files
            )
            compat = Path(compat_path).read_text(encoding="utf-8")

            self.assertNotIn("void Ready();", compat)
            self.assertFalse(
                any("HAL::Ready" in warning for warning in parser.warnings)
            )

    def test_gcc_compat_header_distinguishes_namespace_overloads(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            header = root / "HAL.h"
            header.write_text(
                "#include <stdint.h>\n"
                "namespace HAL { void Send(uint32_t value); "
                "void Send(const uint16_t*); }\n",
                encoding="utf-8",
            )
            source = root / "hal.cpp"
            source.write_text(
                '#include "HAL.h"\n'
                "void HAL::Send(uint32_t input) {}\n"
                "void HAL::Send(const uint16_t* pixels) {}\n"
                "void HAL::Send(uint16_t first, uint16_t second) {}\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]
            parser._add_include_path(str(root))

            compat_path = parser._generate_gcc_cxx_compat_header(
                str(root / "out"), source_files=parser.source_files
            )
            compat = Path(compat_path).read_text(encoding="utf-8")

            self.assertNotIn("void Send(uint32_t input);", compat)
            self.assertNotIn("void Send(const uint16_t* pixels);", compat)
            self.assertIn(
                "void Send(uint16_t first, uint16_t second);",
                compat,
            )

    def test_gcc_compat_header_distinguishes_same_name_across_namespaces(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            header = root / "First.h"
            header.write_text(
                "namespace First { void Ready(); }\n",
                encoding="utf-8",
            )
            source = root / "second.cpp"
            source.write_text(
                '#include "First.h"\nvoid Second::Ready() {}\n',
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]
            parser._add_include_path(str(root))

            compat_path = parser._generate_gcc_cxx_compat_header(
                str(root / "out"), source_files=parser.source_files
            )
            compat = Path(compat_path).read_text(encoding="utf-8")

            self.assertIn("namespace Second", compat)
            self.assertIn("void Ready();", compat)

    def test_gcc_compat_header_resolves_nested_namespace_using_directive(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            header = root / "TileSystem.h"
            header.write_text(
                "namespace Outer { namespace Inner { "
                "uint32_t Size(int level); } }\n",
                encoding="utf-8",
            )
            source = root / "tile.cpp"
            source.write_text(
                '#include "TileSystem.h"\n'
                "using namespace Outer;\n"
                "uint32_t Inner::Size(int level) { return level; }\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]
            parser._add_include_path(str(root))

            compat_path = parser._generate_gcc_cxx_compat_header(
                str(root / "out"), source_files=parser.source_files
            )
            compat = Path(compat_path).read_text(encoding="utf-8")

            self.assertNotIn("namespace Inner", compat)

    def test_gcc_compat_header_skips_custom_parameter_types(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "hal.cpp"
            source.write_text(
                "void HAL::SetCallback(Display_CallbackFunc_t callback) {}\n",
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]

            compat_path = parser._generate_gcc_cxx_compat_header(
                str(root / "out"), source_files=parser.source_files
            )
            compat = Path(compat_path).read_text(encoding="utf-8")

            self.assertNotIn("SetCallback", compat)

    def test_gcc_compat_header_skips_class_member_definitions(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            header = root / "Device.h"
            header.write_text(
                "class Device { public: void Reset(); };\n",
                encoding="utf-8",
            )
            source = root / "device.cpp"
            source.write_text(
                '#include "Device.h"\nvoid Device::Reset() {}\n',
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]
            parser._add_include_path(str(root))

            compat_path = parser._generate_gcc_cxx_compat_header(
                str(root / "out"), source_files=parser.source_files
            )
            compat = Path(compat_path).read_text(encoding="utf-8")

            self.assertNotIn("namespace Device", compat)

    def test_gcc_compat_header_skips_nested_class_member_definitions(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            header = root / "Device.h"
            header.write_text(
                "namespace Board { class Device { public: void Reset(); }; }\n",
                encoding="utf-8",
            )
            source = root / "device.cpp"
            source.write_text(
                '#include "Device.h"\nvoid Board::Device::Reset() {}\n',
                encoding="utf-8",
            )
            parser = KeilProjectToCMake(str(root / "sample.uvprojx"))
            parser.source_files = [str(source)]
            parser._add_include_path(str(root))

            compat_path = parser._generate_gcc_cxx_compat_header(
                str(root / "out"), source_files=parser.source_files
            )
            compat = Path(compat_path).read_text(encoding="utf-8")

            self.assertNotIn("namespace Device", compat)

    def test_build_validator_classifies_windows_missing_executable(self):
        validator = BuildValidator(timeout_seconds=10)
        result = CommandResult(
            command=["cmake"],
            return_code=None,
            stdout="",
            stderr="",
            duration_seconds=0.1,
            error="[WinError 2] The system cannot find the file specified",
        )

        diagnostics = validator._classify("configure", result)

        self.assertEqual([item.category for item in diagnostics], ["missing_tool"])


if __name__ == "__main__":
    unittest.main()
