#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Keil uVision Project (.uvprojx) to CMake Converter
This script parses a Keil uVision project file and generates CMake configuration files.
"""

from io import TextIOWrapper
import xml.etree.ElementTree as ET
import os
import sys
import re
import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from get_keil_mdk_info import find_keil_mdk_root, get_keil_mdk_info


SUPPORTED_COMPILERS = {
    "armclang": "Keil MDK ArmClang",
    "gcc": "GNU Arm Embedded GCC",
}

SUPPORTED_HOST_SYSTEMS = {
    "windows": "Windows",
    "linux": "Linux",
    "macos": "macOS",
}

SUPPORTED_DEBUGGERS = {
    "default": "Default",
    "all": "All",
    "openocd": "OpenOCD",
    "jlink": "J-Link",
    "pyocd": "PyOCD",
    "keil": "Keil MDK",
    "none": "None",
}

SUPPORTED_GENERATORS = {
    "windows": ["Ninja", "MinGW Makefiles", "NMake Makefiles", "Unix Makefiles"],
    "linux": ["Ninja", "Unix Makefiles"],
    "macos": ["Ninja", "Unix Makefiles", "Xcode"],
}

DEFAULT_GENERATOR_BY_HOST = {
    "windows": "Ninja",
    "linux": "Ninja",
    "macos": "Ninja",
}

DEFAULT_BUILD_DIR_BY_COMPILER = {
    "armclang": "build",
    "gcc": "build-gcc",
}


def get_supported_generators(host_os: str) -> List[str]:
    host = (host_os or "windows").strip().lower()
    return SUPPORTED_GENERATORS.get(host, SUPPORTED_GENERATORS["windows"])


@dataclass
class GenerationOptions:
    compiler: str = "armclang"
    generator: str = DEFAULT_GENERATOR_BY_HOST["windows"]
    host_os: str = "windows"
    debugger: str = "default"
    build_dir: Optional[str] = None
    export_vsc_settings: bool = False

    def normalized(self) -> "GenerationOptions":
        compiler = (self.compiler or "armclang").strip().lower()
        if compiler not in SUPPORTED_COMPILERS:
            raise ValueError(f"Unsupported compiler: {self.compiler}")

        host_os = (self.host_os or "windows").strip().lower()
        if host_os not in SUPPORTED_HOST_SYSTEMS:
            raise ValueError(f"Unsupported host OS: {self.host_os}")

        debugger = (self.debugger or "default").strip().lower()
        if debugger not in SUPPORTED_DEBUGGERS:
            raise ValueError(f"Unsupported debugger: {self.debugger}")

        generator = (self.generator or DEFAULT_GENERATOR_BY_HOST[host_os]).strip()
        if not generator:
            generator = DEFAULT_GENERATOR_BY_HOST[host_os]

        build_dir = (self.build_dir or DEFAULT_BUILD_DIR_BY_COMPILER[compiler]).strip()
        if not build_dir:
            build_dir = DEFAULT_BUILD_DIR_BY_COMPILER[compiler]

        return GenerationOptions(
            compiler=compiler,
            generator=generator,
            host_os=host_os,
            debugger=debugger,
            build_dir=build_dir,
            export_vsc_settings=bool(self.export_vsc_settings),
        )


class KeilProjectToCMake:
    """解析Keil uVision项目文件的类"""

    CPU_INFO = re.compile(r"^(\S+)\((\S+)\)$")  # 用于解析CPU信息

    MAP_WARING = {
        "2": "-Wall -Wextra -Wno-packed -Wno-reserved-id-macro -Wno-unused-macros -Wno-documentation-unknown-command -Wno-documentation -Wno-license-management -Wno-parentheses-equality -Wno-reserved-identifier",
        "3": "-Wno-packed -Wno-missing-variable-declarations -Wno-missing-prototypes -Wno-missing-noreturn -Wno-sign-conversion -Wno-nonportable-include-path -Wno-reserved-id-macro -Wno-unused-macros -Wno-documentation-unknown-command -Wno-documentation -Wno-license-management -Wno-parentheses-equality -Wno-reserved-identifier",
        "4": "-Wno-packed -Wno-missing-variable-declarations -Wno-missing-prototypes -Wno-missing-noreturn -Wno-sign-conversion -Wno-nonportable-include-path -Wno-reserved-id-macro -Wno-unused-macros -Wno-documentation-unknown-command -Wno-documentation -Wno-license-management -Wno-parentheses-equality -Wno-reserved-identifier -Wno-covered-switch-default -Wno-unreachable-code-break",
    }

    MAP_C_STD = {
        "0": "-xc",  # <default>
        "1": "-xc -std=c90",  # c90
        "2": "-xc -std=gnu90",  # gnu90
        "3": "-xc -std=c99",  # c99
        "4": "-xc -std=gnu99",  # gnu99
        "5": "-xc -std=c11",  # c11
        "6": "-xc -std=gnu11",  # gnu11
    }

    MAP_CPP_STD = {
        "0": "-xc++ -std=gnu++14",  # <default>
        "1": "-xc++ -std=c++98",  # c++98
        "2": "-xc++ -std=gnu++98",  # gnu++98
        "3": "-xc++ -std=c++11",  # c++11
        "4": "-xc++ -std=gnu++11",  # gnu++11
        "5": "-xc++ -std=c++03",  # c++03
        "6": "-xc++ -std=c++14",  # c++14
        "7": "-xc++ -std=gnu++14",  # gnu++14
        "8": "-xc++ -std=c++17",  # -std=c++17
        "9": "-xc++ -std=gnu++17",  # -std=gnu++17
    }

    MAP_ASM_STD = {
        "0": "-masm=auto -Wa,armasm,--diag_suppress=A1950W",  # armclang(Auto)
        "2": "-masm=gnu",  # armclang(GNU Syntax)
        "3": "-masm=armasm -Wa,armasm,--diag_suppress=A1950W",  # armclang(Arm Syntax)
    }

    MAP_OPTIM = {
        "0": "",  # <default>
        "1": "-O0",
        "2": "-O1",
        "3": "-O2",
        "4": "-O3",
        "5": "-Ofast",
        "6": "-Os",
        "7": "-Oz",
        "8": "-Omax",
    }

    def __init__(
        self, uvprojx_path: str, uv4_path: Optional[str] = None, verbose: bool = False
    ):
        resolved_uv4_path = find_keil_mdk_root(uv4_path)
        if resolved_uv4_path is not None:
            self.uv4_path = resolved_uv4_path
        elif uv4_path is None:
            self.uv4_path = str(Path.home() / "AppData" / "Local" / "Keil_v5")
        else:
            self.uv4_path = uv4_path

        self.mdk_info: Optional[Dict[str, str]] = get_keil_mdk_info(self.uv4_path)
        if self.mdk_info is not None:
            self.uv4_path = self.mdk_info.get("UV4_ROOT", self.uv4_path)

        self.uvprojx_path = Path(uvprojx_path)
        self.project_dir = self.uvprojx_path.parent
        self.project_name = self.uvprojx_path.stem
        self.verbose = verbose

        # 项目信息
        self.output_directory = ""
        self.output_name = ""
        self.target_name = ""
        self.device_name = ""
        self.vendor = ""
        self.cpu = ""

        # 源文件和头文件
        self.source_files: List[str] = []
        self.header_files: List[str] = []
        self.include_paths: Set[str] = set()

        # 编译器设置
        self.defines: Set[str] = set()
        self.cpu_flags: List[str] = ["--target=arm-arm-none-eabi"]
        self.c_cpp_flags: List[str] = []  # C/Cpp 编译标记
        self.c_flags: List[str] = []  # C 编译标记
        self.cpp_flags: List[str] = []  # C++ 编译标记
        self.asm_flags: List[str] = []  # 汇编编译标记
        self.linker_flags: List[str] = []
        self.linker_script = ""

        # 库文件
        self.libraries: List[str] = []
        self.library_paths: Set[str] = set()

    def parse(self) -> bool:
        """解析uVision项目文件"""

        if self.mdk_info is None:
            print(
                f"错误: 无法获取 Keil MDK 信息，请检查路径: {self.uv4_path}",
                file=sys.stderr,
            )
            return False

        try:
            if not self.uvprojx_path.exists():
                print(f"错误: 项目文件不存在 {self.uvprojx_path}", file=sys.stderr)
                return False

            # 解析XML文件
            tree = ET.parse(self.uvprojx_path)
            root = tree.getroot()

            targets = root.findall(".//Target")

            if len(targets) == 0:
                return False

            # 解析项目基本信息
            self._parse_project_info(targets[0])

            # 解析目标设备信息
            self._parse_target_info(targets[0])

            # 解析源文件
            self._parse_source_files(targets[0])

            # 解析编译器设置
            self._parse_compiler_settings(targets[0])

            # 解析链接器设置
            self._parse_linker_settings(targets[0])

            self._parse_runtime_env(root, self.target_name)

            return True

        except ET.ParseError as e:
            print(f"XML解析错误: {e}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"解析项目文件时发生错误: {e}", file=sys.stderr)
            return False

    def _parse_project_info(self, root: ET.Element):
        """解析项目基本信息"""
        # 获取项目名称
        target_name = self._get_element_text(root, ".//TargetName")
        self.target_name = target_name if target_name else self.project_name

        # 获取输出目录
        self.output_directory = self._get_element_text(
            root, ".//OutputDirectory", "Objects"
        )
        if not self.output_directory:
            self.output_directory = "Objects"

        # 获取输出名称
        self.output_name = self._get_element_text(root, ".//OutputName")
        if not self.output_name:
            self.output_name = self.target_name

    def _parse_target_info(self, root: ET.Element):
        """解析目标设备信息"""
        # 设备名称
        self.device_name = self._get_element_text(root, ".//Device")

        # 厂商信息
        self.vendor = self._get_element_text(root, ".//Vendor")

        # CPU类型
        self.cpu_type = self._get_element_text(root, ".//Cpu")

        if self.cpu_type == "":
            return

    def _parse_source_files(self, root: ET.Element):
        """解析源文件和头文件"""
        # 查找所有文件组
        groups = root.findall(".//Group")

        for group in groups:
            files = group.findall(".//File")
            for file_node in files:
                file_name = self._get_element_text(file_node, "FileName")
                file_path = self._get_element_text(file_node, "FilePath")
                file_type = self._get_element_text(file_node, "FileType")

                if file_name and file_path:
                    # 转换路径分隔符并解析相对路径
                    file_path = file_path.replace("\\", "/")
                    if not os.path.isabs(file_path):
                        file_path = os.path.join(self.project_dir, file_path)

                    file_path = os.path.normpath(file_path)

                    # 根据文件类型分类
                    file_ext = os.path.splitext(file_path)[1].lower()

                    if file_ext in [".c", ".cpp", ".cc", ".cxx", ".s", ".asm"]:
                        self.source_files.append(file_path)
                    elif file_ext in [".h", ".hpp", ".hxx"]:
                        self.header_files.append(file_path)
                        # 添加头文件目录到包含路径
                        include_dir = os.path.dirname(file_path)
                        if include_dir:
                            self.include_paths.add(include_dir)

    def _get_element_text(
        self, parent: ET.Element, xpath: str, default: str = ""
    ) -> str:
        """获取XML元素的文本内容

        Args:
            parent: 父元素
            xpath: XPath表达式
            default: 默认值

        Returns:
            str: 元素的文本内容，如果不存在则返回默认值
        """
        node = parent.find(xpath)
        if node is not None and node.text:
            return node.text
        return default

    def _parse_flags(
        self, parent: ET.Element, child_name: str, options: dict[str, str]
    ) -> str:
        """解析编译标志

        Args:
            parent: 父元素
            child_name: 子元素名称
            options: 选项映射字典

        Returns:
            str: 解析得到的编译标志，如果没有则返回空字符串
        """
        txt = self._get_element_text(parent, f".//{child_name}")

        if txt in options:
            return options[txt] if options[txt] else ""
        else:
            return options.get("default", "")

    def _parse_compiler_settings(self, root: ET.Element):
        """解析编译器设置"""

        # 解析CPU特性
        cpu_feature = {}
        for item in self.cpu_type.split(" "):
            m = self.CPU_INFO.match(item)
            if m is None:
                match (item):
                    case "DSP":
                        cpu_feature["DSP"] = 1
                    case "PACBTI":
                        cpu_feature["PACBTI"] = 1
                    case "ELITTLE":
                        cpu_feature["ENDIAN"] = "little"
                    case "FPU2":
                        cpu_feature["FPU2"] = 1
                    case _:
                        if self.verbose:
                            print(f"未处理的信息: {item}")
            else:
                key = m.group(1)
                value = m.group(2).replace('"', "")

                match key:
                    case "CPUTYPE":
                        self.cpu = value
                    case "MVE":
                        cpu_feature["MVE"] = value
                    case "CDECP":
                        cpu_feature["CDE"] = value
                    case "FPU3":
                        cpu_feature["FPU"] = value
                    case "FPU":
                        cpu_feature["FPU"] = value

                    case _:
                        if self.verbose:
                            print(f"未知特性: {key} = {value}")

        cpu = self.cpu.lower()
        features = [f"-mcpu={cpu}"]

        # FPU
        fpu_txt = self._get_element_text(root, ".//RvdsVP")

        if fpu_txt == "2" or fpu_txt == "3":
            self.cpu_flags += ["-mfloat-abi=hard"]
        else:
            self.cpu_flags += ["-mfloat-abi=soft"]

        match (cpu):
            case "cortex-m52" | "cortex-m55" | "cortex-m85":
                # MVE
                mve_txt = self._get_element_text(root, ".//RvdsMve")
                if mve_txt:
                    if self.verbose:
                        print(f"MVE类型: {mve_txt}")

                # FPU
                # 重新获取FPU类型用于后续处理
                fpu_txt = self._get_element_text(root, ".//RvdsVP")
                if fpu_txt:
                    if self.verbose:
                        print(f"FVP类型: {fpu_txt}")

                if (
                    fpu_txt == "1"
                    and mve_txt == "0"
                    or fpu_txt == "3"
                    and mve_txt == "1"
                    or fpu_txt == "2"
                    and mve_txt == "2"
                ):
                    raise ValueError(f"No support: FPU={fpu_txt}, MVE={mve_txt}.")

                if mve_txt == "2":
                    # MVE FP half-precision and single-precision
                    pass
                elif mve_txt == "1":
                    # MVE integer only
                    features += ["+nomve.fp"]
                else:
                    # No MVE
                    features += ["+nomve"]

                if fpu_txt == "3":
                    # Scalar FP double-precision and single-precision and half-precision
                    pass
                elif fpu_txt == "2":
                    # Scalar single-precision and half-precision
                    features += ["+nofp.dp"]
                else:
                    # No FPU
                    features += ["+nofp"]

                # PACBTI
                if "PACBTI" not in cpu_feature:
                    features += ["+nopacbti"]
                else:
                    features += ["+pacbti"]

                cde_txt = self._get_element_text(root, ".//RvdsCdeCp")
                if cde_txt:
                    number = int(cde_txt)
                    for i in range(8):
                        if number & (1 << i):
                            features += [f"+cdecp{i}"]
            case "cortex-m4":
                if fpu_txt == "2":
                    self.cpu_flags += ["-mfpu=fpv4-sp-d16"]
                else:
                    self.cpu_flags += ["-mfpu=none"]
            case "cortex-m33" | "cortex-m35p":
                if fpu_txt == "2":
                    self.cpu_flags += ["-mfpu=fpv5-sp-d16"]
                else:
                    self.cpu_flags += ["-mfpu=none"]
            case "cortex-m7":
                if fpu_txt == "2":
                    self.cpu_flags += ["-mfpu=fpv5-sp-d16"]
                elif fpu_txt == "3":
                    self.cpu_flags += ["-mfpu=fpv5-d16"]
                else:
                    self.cpu_flags += ["-mfpu=none"]
            case _:
                pass

        self.cpu_flags += ["".join(features)]

        branchprot_txt = self._get_element_text(root, ".//nBranchProt")
        if branchprot_txt == "2":
            self.cpu_flags += ["-mbranch-protection=bti+pac-ret"]
        elif branchprot_txt == "1":
            self.cpu_flags += ["-mbranch-protection=bti"]

        if "ENDIAN" in cpu_feature and cpu_feature["ENDIAN"] == "little":
            self.cpu_flags += ["-mlittle-endian"]
        else:
            self.cpu_flags += ["-mbig-endian"]

        # C编译器设置
        self.c_cpp_flags += ["-ffunction-sections"]

        c_compiler = root.find(".//Cads")
        if c_compiler is not None:
            # 预定义宏
            defines_text = self._get_element_text(
                c_compiler, ".//VariousControls/Define"
            )
            if defines_text:
                defines = defines_text.replace(" ", "").split(",")
                for define in defines:
                    if define.strip():
                        self.defines.add(define.strip())

            # 包含路径
            include_text = self._get_element_text(
                c_compiler, ".//VariousControls/IncludePath"
            )
            if include_text:
                includes = include_text.split(";")
                for include in includes:
                    include = include.strip().replace("\\", "/")
                    if include and include != ".":
                        if not os.path.isabs(include):
                            include = os.path.join(self.project_dir, include)
                        include = os.path.normpath(include)
                        self.include_paths.add(include)

            #
            flag = self._parse_flags(
                c_compiler, "Optim", self.MAP_OPTIM
            )  # <TODO> -flto
            if flag:
                self.c_cpp_flags.append(flag)

            flag = self._parse_flags(
                c_compiler, "v6Rtti", {"1": "", "default": "-fno-rtti"}
            )
            if flag:
                self.c_cpp_flags.append(flag)
            flag = self._parse_flags(
                c_compiler,
                "PlainCh",
                {"1": "-fsigned-char", "default": "-funsigned-char"},
            )
            if flag:
                self.c_cpp_flags.append(flag)
            flag = self._parse_flags(c_compiler, "vShortEn", {"1": "-fshort-enums"})
            if flag:
                self.c_cpp_flags.append(flag)
            flag = self._parse_flags(c_compiler, "vShortWch", {"1": "-fshort-wchar"})
            if flag:
                self.c_cpp_flags.append(flag)
            flag = self._parse_flags(c_compiler, "v6Lto", {"1": "-flto"})
            if flag:
                self.c_cpp_flags.append(flag)
            flag = self._parse_flags(c_compiler, "SplitLS", {"1": "-fno-ldm-stm"})
            if flag:
                self.c_cpp_flags.append(flag)
            flag = self._parse_flags(c_compiler, "Ropi", {"1": "-fropi"})
            if flag:
                self.c_cpp_flags.append(flag)
            flag = self._parse_flags(c_compiler, "Rwpi", {"1": "-frwpi"})
            if flag:
                self.c_cpp_flags.append(flag)
            flag = self._parse_flags(c_compiler, "wLevel", self.MAP_WARING)
            if flag:
                self.c_cpp_flags.append(flag)
            flag = self._parse_flags(c_compiler, "v6WtE", {"1": "-Werror"})
            if flag:
                self.c_cpp_flags.append(flag)

            # 其他编译器选项
            misc_text = self._get_element_text(
                c_compiler, ".//VariousControls/MiscControls"
            )
            if misc_text:
                self.c_cpp_flags.extend(misc_text.split())

            # C FLAGS
            flag = self._parse_flags(c_compiler, "v6Lang", self.MAP_C_STD)
            if flag:
                self.c_flags.append(flag)

            # CPP FLAGS
            flag = self._parse_flags(c_compiler, "v6LangP", self.MAP_CPP_STD)
            if flag:
                self.cpp_flags.append(flag)

        # ASM Flags
        flag = self._parse_flags(root, "ClangAsOpt", self.MAP_ASM_STD)
        if flag:
            self.asm_flags.append(flag)
        else:
            self.asm_flags.append(self.MAP_ASM_STD["0"])

        # C++编译器设置
        cpp_compiler = root.find(".//Cppads")
        if cpp_compiler is not None:
            # C++特定的设置可以在这里添加
            pass

    def _parse_linker_settings(self, root: ET.Element):
        """解析链接器设置"""
        linker = root.find(".//LDads")
        if linker is not None:
            # 链接器脚本
            scatter_text = self._get_element_text(linker, ".//ScatterFile")
            if scatter_text:
                linker_script = scatter_text.replace("\\", "/")
                if not os.path.isabs(linker_script):
                    linker_script = os.path.join(self.project_dir, linker_script)
                self.linker_script = os.path.normpath(linker_script)

            # 其他链接器选项
            misc_text = self._get_element_text(linker, ".//Misc")
            if misc_text:
                self.linker_flags.extend(misc_text.split())

        self.linker_flags.extend(
            [
                "--summary_stderr",
                "--info summarysizes",
                "--map",
                "--load_addr_map_info",
                "--xref",
                "--callgraph",
                "--symbols",
                "--info sizes",
                "--info totals",
                "--info unused",
                "--info veneers",
            ]
        )

    def _search_pdsc_file(
        self,
        filepath: str,
        cls: Optional[str],
        condition: Optional[str],
        sub: Optional[str] = None,
    ) -> None:
        """查找PDSC文件中指定类别和子类别的文件"""
        if not os.path.exists(filepath):
            return

        base = os.path.dirname(filepath)
        tree = ET.parse(filepath)
        root = tree.getroot()

        for c_node in root.findall(".//component"):
            if (
                c_node.get("Cclass") == cls
                and c_node.get("Csub") == sub
                and condition == c_node.get("condition")
            ):

                for file_node in c_node.findall(".//file"):
                    if file_node is None:
                        continue

                    if file_node.get("attr") is not None:
                        # 如果有attr属性，表示这是一个特殊的文件，不处理
                        continue

                    c = file_node.get("category")
                    p = file_node.get("name")

                    if c is None or p is None:
                        continue
                    p = os.path.join(base, p).replace("\\", "/")

                    if c == "source":
                        self.source_files.append(p)
                    else:
                        if os.path.isdir(p):
                            self.include_paths.add(p)
                        #
                    #
                #
            #
        # end-for

    def _parse_runtime_env(self, root: ET.Element, target_name: str):
        """解析运行时环境设置"""
        runtime_env = root.find(".//RTE")
        if runtime_env is None:
            return

        for c_node in runtime_env.findall(".//component"):
            targets = [n.get("name") for n in c_node.findall(".//targetInfo")]
            if target_name not in targets:
                continue

            p_node = c_node.find(".//package")
            package_name = p_node.get("name") if p_node is not None else ""
            package_vendor = p_node.get("vendor") if p_node is not None else ""
            package_version = p_node.get("version") if p_node is not None else ""

            package_direpath = Path(self.mdk_info["RTEPATH"]) / Path(package_vendor) / Path(package_name) / Path(package_version)  # type: ignore

            package_filepath = (
                package_direpath / f"{package_vendor}.{package_name}.pdsc"
            )

            c_class = c_node.get("Cclass")
            c_group = c_node.get("Cgroup")
            c_sub = c_node.get("Csub")
            c_vendor = c_node.get("Cvendor")
            condition = c_node.get("condition")

            self._search_pdsc_file(str(package_filepath), c_class, condition, c_sub)

        for f_node in runtime_env.findall(".//file"):

            targets = [n.get("name") for n in f_node.findall(".//targetInfo")]
            if target_name not in targets:
                continue

            name = f_node.get("name")
            if name is None:
                continue

            package = f_node.find(".//package")
            if package is not None:
                package_name = package.get("name")
                package_vendor = package.get("vendor")
                package_version = package.get("version")

                if package_name and package_vendor and package_version:
                    p = Path(self.mdk_info["RTEPATH"]) / Path(package_vendor) / Path(package_name) / Path(package_version) / Path(name)  # type: ignore

                    self.source_files.append(str(p))

    def _prepare_generation_options(
        self,
        export_vsc_settings: bool,
        generation_options: Optional[GenerationOptions],
    ) -> GenerationOptions:
        if generation_options is None:
            generation_options = GenerationOptions(
                export_vsc_settings=export_vsc_settings
            )
        elif export_vsc_settings and not generation_options.export_vsc_settings:
            generation_options = GenerationOptions(
                compiler=generation_options.compiler,
                generator=generation_options.generator,
                host_os=generation_options.host_os,
                debugger=generation_options.debugger,
                build_dir=generation_options.build_dir,
                export_vsc_settings=True,
            )

        return generation_options.normalized()

    def _default_output_path(self, options: GenerationOptions) -> Path:
        if (
            options.compiler == "gcc"
            and self.project_dir.name.lower() in {"mdk_v5", "mdk", "uvprojx"}
        ):
            return self.project_dir.parent
        return self.project_dir

    def _flatten_flags(self, flags: List[str]) -> List[str]:
        flattened: List[str] = []
        for chunk in flags:
            if chunk:
                flattened.extend(chunk.split())
        return flattened

    def _dedupe_keep_order(self, values: List[str]) -> List[str]:
        seen: Set[str] = set()
        result: List[str] = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _project_languages(self) -> List[str]:
        languages = ["C"]
        if any(
            Path(source).suffix.lower() in {".cpp", ".cxx", ".cc"}
            for source in self.source_files
        ):
            languages.append("CXX")
        languages.append("ASM")
        return languages

    def _clean_device_name(self) -> str:
        return self.device_name.lstrip("-").strip()

    def _mcu_family_token(self) -> str:
        for source in self._gcc_source_files():
            stem = Path(source).stem.lower()
            if stem.startswith("startup_"):
                return stem.replace("startup_", "").upper()

        clean_device = self._clean_device_name().upper()
        match = re.match(r"([A-Z0-9]+?)([A-Z][A-Z0-9]*)?$", clean_device)
        return match.group(1) if match else clean_device

    def _device_memory_code(self) -> str:
        clean_device = self._clean_device_name().upper()
        family_token = self._mcu_family_token().upper()
        if clean_device.startswith(family_token):
            suffix = clean_device[len(family_token) :]
            if len(suffix) >= 2 and suffix[1].isalnum():
                return suffix[1].upper()
        return ""

    def _device_tokens(self) -> List[str]:
        device = self._clean_device_name().upper()
        tokens = [self._mcu_family_token().upper()]
        if device:
            tokens.append(re.sub(r"[^A-Z0-9]", "", device))
        return sorted(set(tokens), key=len, reverse=True)

    def _artifact_name(self) -> str:
        return self.output_name or self.target_name

    def _map_source_for_gcc(self, source_path: str) -> str:
        normalized = Path(source_path).as_posix()
        if "/startup/mdk/" in normalized:
            candidate = Path(
                normalized.replace("/startup/mdk/", "/startup/gcc/")
            ).resolve(strict=False)
            if candidate.exists():
                return str(candidate)
        return source_path

    def _gcc_source_files(self) -> List[str]:
        mapped = [self._map_source_for_gcc(source) for source in self.source_files]
        return sorted(self._dedupe_keep_order(mapped))

    def _score_linker_script_candidate(self, path: Path) -> int:
        score = 0
        path_text = path.as_posix().lower()
        file_name = path.name.lower()

        if "/startup/gcc/linker/" in path_text:
            score += 10
        if "flash" in file_name:
            score += 3

        for token in self._device_tokens():
            token_lower = token.lower()
            if token_lower in file_name:
                score += 5
            elif token_lower in path_text:
                score += 2

        memory_code = self._device_memory_code().lower()
        if memory_code and f"x{memory_code}" in file_name:
            score += 8

        return score

    def _guess_gcc_linker_script(self) -> Optional[str]:
        if self.linker_script and self.linker_script.lower().endswith(".ld"):
            return str(Path(self.linker_script).resolve(strict=False))

        candidates: List[Path] = []
        for source in self._gcc_source_files():
            source_path = Path(source)
            if source_path.name.lower().startswith("startup_"):
                linker_dir = source_path.parent / "linker"
                if linker_dir.is_dir():
                    candidates.extend(sorted(linker_dir.glob("*.ld")))

        if not candidates:
            for search_root in [self.project_dir, self.project_dir.parent]:
                if search_root.is_dir():
                    candidates.extend(sorted(search_root.rglob("*.ld")))

        candidates = [candidate.resolve(strict=False) for candidate in candidates]
        candidates = [candidate for candidate in candidates if candidate.is_file()]
        if not candidates:
            return None

        candidates = list(dict.fromkeys(candidates))
        best = max(candidates, key=self._score_linker_script_candidate)
        return str(best)

    def _extract_first_flag(self, flags: List[str], prefix: str) -> Optional[str]:
        for flag in self._flatten_flags(flags):
            if flag.startswith(prefix):
                return flag
        return None

    def _gcc_cpu_options(self) -> List[str]:
        cpu_options: List[str] = []
        for flag in self._flatten_flags(self.cpu_flags):
            if flag.startswith("--target="):
                continue
            if flag == "-mfpu=none":
                continue
            if flag == "-gdwarf-4":
                continue
            cpu_options.append(flag)

        if self.cpu and not any(flag.startswith("-mcpu=") for flag in cpu_options):
            cpu_options.append(f"-mcpu={self.cpu.lower()}")

        if "-mthumb" not in cpu_options:
            cpu_options.insert(1 if cpu_options else 0, "-mthumb")

        return self._dedupe_keep_order(cpu_options)

    def _gcc_c_standard(self) -> str:
        standard = self._extract_first_flag(self.c_flags, "-std=")
        if standard in {None, "-std=c90", "-std=gnu90"}:
            return "-std=gnu99"
        return standard

    def _gcc_cpp_standard(self) -> str:
        standard = self._extract_first_flag(self.cpp_flags, "-std=")
        return standard or "-std=gnu++14"

    def _effective_defines(self, compiler: str) -> List[str]:
        defines = []
        for define in sorted(self.defines):
            value = define.strip()
            if value.startswith("-D"):
                value = value[2:]
            if value:
                defines.append(value)

        defines = self._dedupe_keep_order(defines)
        if compiler != "gcc":
            return defines

        device_name = self._clean_device_name()
        if not device_name:
            return defines

        tokens = self._device_tokens()
        filtered: List[str] = []
        for define in defines:
            if define == device_name:
                filtered.append(define)
                continue

            if re.fullmatch(r"[A-Za-z0-9_]+", define) and any(
                define.upper().startswith(token) for token in tokens
            ):
                continue

            filtered.append(define)

        filtered.append(device_name)
        return self._dedupe_keep_order(filtered)

    def _find_openocd_dir(self) -> Optional[Path]:
        for candidate in [self.project_dir / "openocd", self.project_dir.parent / "openocd"]:
            if candidate.is_dir():
                return candidate
        return None

    def _guess_openocd_config(self, interface_name: str) -> Optional[Path]:
        openocd_dir = self._find_openocd_dir()
        if openocd_dir is None:
            return None

        candidates = sorted(openocd_dir.glob("*.cfg"))
        if not candidates:
            return None

        cpu_token = self.cpu.lower().replace("-", "") if self.cpu else ""
        best_score = -1
        best_path: Optional[Path] = None

        for candidate in candidates:
            score = 0
            stem = candidate.stem.lower()
            if interface_name in stem:
                score += 6
            for token in self._device_tokens():
                if token.lower() in stem:
                    score += 4
            if cpu_token and cpu_token in stem:
                score += 2

            if score > best_score:
                best_score = score
                best_path = candidate

        if best_path is None:
            return None
        if best_score <= 0 and len(candidates) > 1:
            return None

        return best_path

    def _host_command_names(self, host_os: str) -> Dict[str, str]:
        if host_os == "windows":
            return {
                "cmake": "cmake",
                "openocd": "openocd",
                "jlink": "JLink.exe",
                "jlink_gdb_server": "JLinkGDBServerCL.exe",
                "script_name": "do_build.bat",
                "script_command": "${workspaceFolder}\\do_build.bat",
                "armclang_suffix": ".exe",
            }

        return {
            "cmake": "cmake",
            "openocd": "openocd",
            "jlink": "JLinkExe",
            "jlink_gdb_server": "JLinkGDBServerCLExe",
            "script_name": "do_build.sh",
            "script_command": "bash",
            "armclang_suffix": "",
        }

    def _selected_debuggers(self, options: GenerationOptions) -> List[str]:
        if options.debugger in {"default", "all"}:
            if options.compiler == "gcc":
                debuggers = ["openocd", "jlink"]
            else:
                debuggers = ["keil", "pyocd", "jlink"]
        elif options.debugger == "none":
            debuggers = []
        else:
            debuggers = [options.debugger]

        if options.host_os != "windows":
            debuggers = [debugger for debugger in debuggers if debugger != "keil"]

        return self._dedupe_keep_order(debuggers)

    def _write_text_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

        if path.suffix == ".sh":
            os.chmod(path, 0o755)

    def _build_script_content(self, options: GenerationOptions) -> str:
        build_dir = options.build_dir
        generator = options.generator

        if options.host_os == "windows":
            lines = ["@echo off", "setlocal", ""]
            if options.compiler == "armclang":
                lines.extend(
                    [
                        'if not defined ARMCLANG_PATH if defined KEIL_MDK_ROOT if exist "%KEIL_MDK_ROOT%\\ARM\\ARMCLANG\\bin\\armclang.exe" set "ARMCLANG_PATH=%KEIL_MDK_ROOT%\\ARM\\ARMCLANG\\bin"',
                        'if not defined ARMCLANG_PATH if exist "%USERPROFILE%\\AppData\\Local\\Keil_v5\\ARM\\ARMCLANG\\bin\\armclang.exe" set "ARMCLANG_PATH=%USERPROFILE%\\AppData\\Local\\Keil_v5\\ARM\\ARMCLANG\\bin"',
                        'if not defined ARMCLANG_PATH if exist "D:\\install\\keil5 mdk\\ARM\\ARMCLANG\\bin\\armclang.exe" set "ARMCLANG_PATH=D:\\install\\keil5 mdk\\ARM\\ARMCLANG\\bin"',
                        "",
                        "if not defined ARMCLANG_PATH (",
                        "echo Error: ARMCLANG_PATH was not found.",
                        "echo Set ARMCLANG_PATH or KEIL_MDK_ROOT before running this script.",
                        "exit /b 1",
                        ")",
                        "",
                        'set "ARMCLANG_PATH=%ARMCLANG_PATH:\\=/%"',
                        "",
                    ]
                )

            lines.extend(
                [
                    f'cmake -S . -B "{build_dir}" -G "{generator}" -DCMAKE_BUILD_TYPE=Debug',
                    "if errorlevel 1 exit /b %errorlevel%",
                    f'cmake --build "{build_dir}" --parallel',
                    "endlocal",
                    "",
                ]
            )
            return "\n".join(lines)

        lines = ["#!/usr/bin/env sh", "set -eu", ""]
        if options.compiler == "armclang":
            lines.extend(
                [
                    'if [ -z "${ARMCLANG_PATH:-}" ]; then',
                    '    if [ -n "${KEIL_MDK_ROOT:-}" ] && [ -x "${KEIL_MDK_ROOT}/ARM/ARMCLANG/bin/armclang" ]; then',
                    '        ARMCLANG_PATH="${KEIL_MDK_ROOT}/ARM/ARMCLANG/bin"',
                    "        export ARMCLANG_PATH",
                    "    else",
                    '        echo "Error: ARMCLANG_PATH was not found."',
                    '        echo "Set ARMCLANG_PATH or KEIL_MDK_ROOT before running this script."',
                    "        exit 1",
                    "    fi",
                    "fi",
                    "",
                ]
            )

        lines.extend(
            [
                f'cmake -S . -B "{build_dir}" -G "{generator}" -DCMAKE_BUILD_TYPE=Debug',
                f'cmake --build "{build_dir}" --parallel',
                "",
            ]
        )
        return "\n".join(lines)

    def _armclang_toolchain_content(self, options: GenerationOptions) -> str:
        executable_suffix = self._host_command_names(options.host_os)["armclang_suffix"]
        lines = [
            "set(CMAKE_SYSTEM_NAME Generic)",
            "set(CMAKE_SYSTEM_PROCESSOR arm)",
            "set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)",
            "",
            'if(NOT DEFINED ENV{ARMCLANG_PATH})',
            '    message(FATAL_ERROR "env \\"ARMCLANG_PATH\\" not set")',
            "endif()",
            "",
            "set(ARMCLANG_PATH $ENV{ARMCLANG_PATH})",
            f'set(_ARM_EXECUTABLE_SUFFIX "{executable_suffix}")',
            "",
            "set(CMAKE_C_COMPILER ${ARMCLANG_PATH}/armclang${_ARM_EXECUTABLE_SUFFIX})",
            "set(CMAKE_CXX_COMPILER ${ARMCLANG_PATH}/armclang${_ARM_EXECUTABLE_SUFFIX})",
            "set(CMAKE_ASM_COMPILER ${ARMCLANG_PATH}/armclang${_ARM_EXECUTABLE_SUFFIX})",
            "set(CMAKE_LINKER ${ARMCLANG_PATH}/armlink${_ARM_EXECUTABLE_SUFFIX})",
            "set(CMAKE_AR ${ARMCLANG_PATH}/armar${_ARM_EXECUTABLE_SUFFIX})",
            "set(CMAKE_OBJCOPY ${ARMCLANG_PATH}/fromelf${_ARM_EXECUTABLE_SUFFIX})",
            "set(FROMELF_EXECUTABLE ${ARMCLANG_PATH}/fromelf${_ARM_EXECUTABLE_SUFFIX})",
            "",
            "set(CMAKE_C_COMPILER_WORKS TRUE)",
            "set(CMAKE_CXX_COMPILER_WORKS TRUE)",
            "set(CMAKE_ASM_COMPILER_WORKS TRUE)",
            "set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)",
            "set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)",
            "set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)",
            "set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)",
            'set(CMAKE_EXECUTABLE_SUFFIX ".axf")',
            "",
            'if(NOT EXISTS "${CMAKE_C_COMPILER}")',
            '    message(FATAL_ERROR "Compiler ${CMAKE_C_COMPILER} does not exist")',
            "endif()",
            "",
        ]
        return "\n".join(lines)

    def _gcc_toolchain_content(self) -> str:
        lines = [
            "set(CMAKE_SYSTEM_NAME Generic)",
            "set(CMAKE_SYSTEM_PROCESSOR arm)",
            "set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)",
            "set(CMAKE_C_ABI_COMPILED TRUE CACHE INTERNAL \"\" FORCE)",
            "set(CMAKE_C_COMPILER_WORKS TRUE CACHE INTERNAL \"\" FORCE)",
            "set(CMAKE_C_COMPILER_ABI ELF CACHE STRING \"\" FORCE)",
            "set(CMAKE_C_SIZEOF_DATA_PTR 4 CACHE STRING \"\" FORCE)",
            "set(CMAKE_C_BYTE_ORDER LITTLE_ENDIAN CACHE STRING \"\" FORCE)",
            "set(CMAKE_SIZEOF_VOID_P 4 CACHE INTERNAL \"\" FORCE)",
            "",
            "find_program(CMAKE_C_COMPILER arm-none-eabi-gcc REQUIRED)",
            "find_program(CMAKE_CXX_COMPILER arm-none-eabi-g++ REQUIRED)",
            "find_program(CMAKE_ASM_COMPILER arm-none-eabi-gcc REQUIRED)",
            "",
            'get_filename_component(ARM_BIN_DIR "${CMAKE_C_COMPILER}" DIRECTORY)',
            "",
            'set(CMAKE_AR "${ARM_BIN_DIR}/arm-none-eabi-ar" CACHE FILEPATH "" FORCE)',
            'set(CMAKE_OBJCOPY "${ARM_BIN_DIR}/arm-none-eabi-objcopy" CACHE FILEPATH "" FORCE)',
            'set(CMAKE_OBJDUMP "${ARM_BIN_DIR}/arm-none-eabi-objdump" CACHE FILEPATH "" FORCE)',
            'set(CMAKE_SIZE "${ARM_BIN_DIR}/arm-none-eabi-size" CACHE FILEPATH "" FORCE)',
            'set(CMAKE_RANLIB "${ARM_BIN_DIR}/arm-none-eabi-ranlib" CACHE FILEPATH "" FORCE)',
            "",
            'set(CMAKE_EXECUTABLE_SUFFIX ".elf")',
            "set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)",
            "set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)",
            "set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)",
            "set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)",
            "",
        ]
        return "\n".join(lines)

    def _write_toolchain_files(
        self, output_path: Path, options: GenerationOptions
    ) -> None:
        cmake_dir = output_path / "cmake"
        cmake_dir.mkdir(parents=True, exist_ok=True)

        if options.compiler == "armclang":
            self._write_text_file(
                cmake_dir / "armclang.cmake",
                self._armclang_toolchain_content(options),
            )
        else:
            self._write_text_file(
                cmake_dir / "arm-none-eabi-toolchain.cmake",
                self._gcc_toolchain_content(),
            )

    def _vscode_env(self, options: GenerationOptions) -> Dict[str, str]:
        if options.compiler != "armclang":
            return {}

        mdk_info = self.mdk_info or {}
        armclang_path = mdk_info.get(
            "ARMCLANG_PATH",
            os.path.join(self.uv4_path, "ARM\\ARMCLANG\\bin"),
        )
        return {"ARMCLANG_PATH": armclang_path.replace("\\", "/")}

    def _build_vscode_settings(self, options: GenerationOptions) -> Dict[str, Any]:
        settings: Dict[str, Any] = {
            "cmake.generator": options.generator,
            "cmake.buildDirectory": f"${{workspaceFolder}}/{options.build_dir}",
        }

        env = self._vscode_env(options)
        if env:
            settings["cmake.configureEnvironment"] = env

        return settings

    def _build_vscode_c_cpp_properties(
        self, options: GenerationOptions
    ) -> Dict[str, Any]:
        return {
            "configurations": [
                {
                    "name": SUPPORTED_HOST_SYSTEMS[options.host_os],
                    "compileCommands": f"${{workspaceFolder}}/{options.build_dir}/compile_commands.json",
                    "configurationProvider": "ms-vscode.cmake-tools",
                }
            ],
            "version": 4,
        }

    def _relative_workspace_path(self, path: Path, output_path: Path) -> str:
        return self._normalize_path(str(path.resolve(strict=False)), str(output_path))

    def _artifact_path(self, options: GenerationOptions, suffix: str) -> str:
        return f"${{workspaceFolder}}/{options.build_dir}/{self._artifact_name()}{suffix}"

    def _jlink_flash_device(self) -> str:
        return self.cpu or self._clean_device_name() or "Cortex-M4"

    def _write_jlink_scripts(self, output_path: Path, options: GenerationOptions) -> None:
        if options.compiler != "gcc" or "jlink" not in self._selected_debuggers(options):
            return

        vsc_dir = output_path / ".vscode"
        vsc_dir.mkdir(parents=True, exist_ok=True)

        scripts = {
            "jlink_flash_elf.jlink": f"r\nloadfile {options.build_dir}/{self._artifact_name()}.elf\nr\nq\n",
            "jlink_flash_hex.jlink": f"r\nloadfile {options.build_dir}/{self._artifact_name()}.hex\nr\nq\n",
            "jlink_flash_bin.jlink": f"r\nloadfile {options.build_dir}/{self._artifact_name()}.bin 0x08000000\nr\nq\n",
        }

        for file_name, content in scripts.items():
            self._write_text_file(vsc_dir / file_name, content)

    def _build_vscode_tasks(
        self, output_path: Path, options: GenerationOptions
    ) -> Dict[str, Any]:
        commands = self._host_command_names(options.host_os)
        compiler_label = "GCC" if options.compiler == "gcc" else "ArmClang"
        configure_label = f"Configure {compiler_label} Debug"
        build_label = f"Build {compiler_label} Debug"
        task_env = self._vscode_env(options)
        task_options = {"env": task_env} if task_env else {}

        configure_task: Dict[str, Any] = {
            "label": configure_label,
            "type": "shell",
            "command": commands["cmake"],
            "args": [
                "-S",
                "${workspaceFolder}",
                "-B",
                f"${{workspaceFolder}}/{options.build_dir}",
                "-G",
                options.generator,
                "-DCMAKE_BUILD_TYPE=Debug",
            ],
            "problemMatcher": [],
        }
        if task_options:
            configure_task["options"] = task_options

        build_task: Dict[str, Any] = {
            "label": build_label,
            "type": "shell",
            "command": commands["cmake"],
            "args": [
                "--build",
                f"${{workspaceFolder}}/{options.build_dir}",
                "--parallel",
            ],
            "dependsOn": configure_label,
            "group": {"kind": "build", "isDefault": True},
            "problemMatcher": [],
        }
        if task_options:
            build_task["options"] = task_options

        tasks: List[Dict[str, Any]] = [configure_task, build_task]

        if options.host_os == "windows":
            rebuild_command = commands["script_command"]
            rebuild_args: List[str] = []
        else:
            rebuild_command = commands["script_command"]
            rebuild_args = [f"${{workspaceFolder}}/{commands['script_name']}"]

        rebuild_task: Dict[str, Any] = {
            "label": "Rebuild",
            "type": "shell",
            "command": rebuild_command,
            "args": rebuild_args,
            "problemMatcher": [],
        }
        if task_options:
            rebuild_task["options"] = task_options
        tasks.append(rebuild_task)

        debuggers = self._selected_debuggers(options)
        if options.compiler == "gcc":
            openocd_cfg = self._guess_openocd_config("stlink")
            if openocd_cfg is not None and "openocd" in debuggers:
                openocd_rel = self._relative_workspace_path(openocd_cfg, output_path)
                tasks.extend(
                    [
                        {
                            "label": "Flash ST-Link ELF",
                            "type": "shell",
                            "command": commands["openocd"],
                            "args": [
                                "-f",
                                openocd_rel,
                                "-c",
                                f"program {{{self._artifact_path(options, '.elf')}}} verify reset exit",
                            ],
                            "dependsOn": build_label,
                            "problemMatcher": [],
                        },
                        {
                            "label": "Flash ST-Link HEX",
                            "type": "shell",
                            "command": commands["openocd"],
                            "args": [
                                "-f",
                                openocd_rel,
                                "-c",
                                f"program {{{self._artifact_path(options, '.hex')}}} verify reset exit",
                            ],
                            "dependsOn": build_label,
                            "problemMatcher": [],
                        },
                        {
                            "label": "Flash ST-Link BIN",
                            "type": "shell",
                            "command": commands["openocd"],
                            "args": [
                                "-f",
                                openocd_rel,
                                "-c",
                                f"program {{{self._artifact_path(options, '.bin')}}} 0x08000000 verify reset exit",
                            ],
                            "dependsOn": build_label,
                            "problemMatcher": [],
                        },
                        {
                            "label": "Start ST-Link OpenOCD GDB Server",
                            "type": "shell",
                            "command": commands["openocd"],
                            "args": ["-f", openocd_rel],
                            "dependsOn": build_label,
                            "isBackground": True,
                            "presentation": {
                                "echo": True,
                                "reveal": "always",
                                "panel": "dedicated",
                                "focus": False,
                            },
                            "problemMatcher": {
                                "owner": "openocd",
                                "pattern": [
                                    {
                                        "regexp": ".",
                                        "file": 0,
                                        "location": 0,
                                        "message": 0,
                                    }
                                ],
                                "background": {
                                    "activeOnStart": True,
                                    "beginsPattern": ".*",
                                    "endsPattern": ".*Listening on port 3333 for gdb connections.*",
                                },
                            },
                        },
                    ]
                )

            if "jlink" in debuggers:
                presentation = {
                    "echo": True,
                    "reveal": "always",
                    "panel": "dedicated",
                    "focus": False,
                }
                for label, script_name in [
                    ("Flash J-Link ELF", "jlink_flash_elf.jlink"),
                    ("Flash J-Link HEX", "jlink_flash_hex.jlink"),
                    ("Flash J-Link BIN", "jlink_flash_bin.jlink"),
                ]:
                    tasks.append(
                        {
                            "label": label,
                            "type": "shell",
                            "command": commands["jlink"],
                            "args": [
                                "-device",
                                self._jlink_flash_device(),
                                "-if",
                                "SWD",
                                "-speed",
                                "1000",
                                "-CommanderScript",
                                f"${{workspaceFolder}}/.vscode/{script_name}",
                            ],
                            "dependsOn": build_label,
                            "presentation": presentation,
                            "problemMatcher": [],
                        }
                    )
        else:
            if "keil" in debuggers and options.host_os == "windows":
                mdk_info = self.mdk_info or {}
                uv4_exe = mdk_info.get(
                    "UV4_EXE",
                    os.path.join(self.uv4_path, "UV4\\UV4.exe"),
                )
                tasks.extend(
                    [
                        {
                            "label": "Flash (Keil MDK)",
                            "type": "shell",
                            "command": uv4_exe,
                            "args": ["-f", str(self.uvprojx_path)],
                            "problemMatcher": [],
                        },
                        {
                            "label": "Debug (Keil MDK)",
                            "type": "shell",
                            "command": uv4_exe,
                            "args": ["-d", str(self.uvprojx_path)],
                            "problemMatcher": [],
                        },
                    ]
                )

        return {"version": "2.0.0", "tasks": tasks}

    def _build_vscode_launch(
        self, output_path: Path, options: GenerationOptions
    ) -> Dict[str, Any]:
        commands = self._host_command_names(options.host_os)
        compiler_label = "GCC" if options.compiler == "gcc" else "ArmClang"
        build_label = f"Build {compiler_label} Debug"
        executable_path = self._artifact_path(
            options, ".elf" if options.compiler == "gcc" else ".axf"
        )
        clean_device = self._clean_device_name()
        jlink_device = clean_device or self._jlink_flash_device()
        launch_configs: List[Dict[str, Any]] = []
        debuggers = self._selected_debuggers(options)

        if options.compiler == "gcc":
            openocd_cfg = self._guess_openocd_config("stlink")
            if openocd_cfg is not None and "openocd" in debuggers:
                launch_configs.append(
                    {
                        "name": f"{jlink_device or self.target_name} / ST-Link / OpenOCD",
                        "cwd": "${workspaceFolder}",
                        "type": "cortex-debug",
                        "request": "launch",
                        "servertype": "openocd",
                        "serverpath": commands["openocd"],
                        "gdbPath": "arm-none-eabi-gdb",
                        "executable": executable_path,
                        "configFiles": [self._relative_workspace_path(openocd_cfg, output_path)],
                        "runToEntryPoint": "main",
                        "preLaunchTask": build_label,
                    }
                )

            if "jlink" in debuggers:
                launch_configs.append(
                    {
                        "name": f"{jlink_device or self.target_name} / J-Link / GDB Server",
                        "cwd": "${workspaceFolder}",
                        "type": "cortex-debug",
                        "request": "launch",
                        "servertype": "jlink",
                        "serverpath": commands["jlink_gdb_server"],
                        "device": jlink_device or self._jlink_flash_device(),
                        "interface": "swd",
                        "gdbPath": "arm-none-eabi-gdb",
                        "executable": executable_path,
                        "runToEntryPoint": "main",
                        "preLaunchTask": build_label,
                    }
                )

                if clean_device and self.cpu and clean_device != self.cpu:
                    launch_configs.append(
                        {
                            "name": f"{self.cpu} / J-Link / Fallback",
                            "cwd": "${workspaceFolder}",
                            "type": "cortex-debug",
                            "request": "launch",
                            "servertype": "jlink",
                            "serverpath": commands["jlink_gdb_server"],
                            "device": self.cpu,
                            "interface": "swd",
                            "gdbPath": "arm-none-eabi-gdb",
                            "executable": executable_path,
                            "runToEntryPoint": "main",
                            "preLaunchTask": build_label,
                        }
                    )
        else:
            if "pyocd" in debuggers:
                launch_configs.append(
                    {
                        "name": "Debug with PyOCD",
                        "cwd": "${workspaceFolder}",
                        "type": "cortex-debug",
                        "request": "launch",
                        "servertype": "pyocd",
                        "gdbPath": "arm-none-eabi-gdb",
                        "executable": executable_path,
                        "runToEntryPoint": "main",
                        "showDevDebugOutput": "none",
                        "preLaunchTask": build_label,
                    }
                )

            if "jlink" in debuggers:
                launch_configs.append(
                    {
                        "name": "Cortex Debug",
                        "cwd": "${workspaceFolder}",
                        "type": "cortex-debug",
                        "request": "launch",
                        "servertype": "jlink",
                        "serverpath": commands["jlink_gdb_server"],
                        "device": jlink_device or self._jlink_flash_device(),
                        "interface": "swd",
                        "gdbPath": "arm-none-eabi-gdb",
                        "executable": executable_path,
                        "runToEntryPoint": "main",
                        "preLaunchTask": build_label,
                    }
                )

        return {"version": "0.2.0", "configurations": launch_configs}

    def _write_cmake_header(
        self, f: TextIOWrapper, toolchain_include: str, languages: List[str]
    ) -> None:
        f.write("# CMake file generated from Keil uVision project\n")
        f.write("# Generated by keil_uvprojx2cmake.py\n\n")
        f.write("cmake_minimum_required(VERSION 3.20)\n\n")
        f.write(f"include({toolchain_include})\n\n")
        f.write(f"project({self.target_name} LANGUAGES {' '.join(languages)})\n")
        f.write("set(CMAKE_EXPORT_COMPILE_COMMANDS ON)\n")
        f.write("if(NOT CMAKE_BUILD_TYPE)\n")
        f.write('    set(CMAKE_BUILD_TYPE Debug CACHE STRING "Build type" FORCE)\n')
        f.write("endif()\n\n")

        if self.device_name:
            f.write(f"# Target Device: {self.device_name}\n")
        if self.vendor:
            f.write(f"# Vendor: {self.vendor}\n")
        if self.cpu_type:
            f.write(f"# CPU: {self.cpu_type}\n")
        f.write("\n")

    def _write_armclang_cmake_content(
        self, f: TextIOWrapper, dest: str, options: GenerationOptions
    ) -> None:
        self._write_cmake_header(f, "cmake/armclang.cmake", self._project_languages())

        cpu_flags = self._dedupe_keep_order(self.cpu_flags + ["-gdwarf-4"])
        c_flags = " ".join(self.c_flags)
        cxx_flags = " ".join(self.cpp_flags)
        common_flags = " ".join(self.c_cpp_flags)
        asm_flags = " ".join(self.asm_flags)

        f.write("# Compiler flags\n")
        f.write(f'set(CPU_FLAGS "{" ".join(cpu_flags)}")\n')
        f.write(f'set(CMAKE_C_FLAGS "${{CPU_FLAGS}} {c_flags} {common_flags}")\n')
        f.write(f'set(CMAKE_CXX_FLAGS "${{CPU_FLAGS}} {cxx_flags} {common_flags}")\n')
        f.write(f'set(CMAKE_ASM_FLAGS "${{CPU_FLAGS}} {asm_flags}")\n\n')

        if self.linker_flags:
            f.write("# Linker flags\n")
            f.write(
                f'set(CMAKE_EXE_LINKER_FLAGS "${{CMAKE_EXE_LINKER_FLAGS}} {" ".join(self.linker_flags)}")\n\n'
            )

        if self.linker_script:
            rel_linker_script = self._normalize_path(
                str(Path(self.linker_script).resolve(strict=False)), dest
            )
            f.write(
                f'set(LINKER_SCRIPT "${{CMAKE_CURRENT_SOURCE_DIR}}/{rel_linker_script}")\n'
            )
            f.write(
                'set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} --strict --scatter \\"${LINKER_SCRIPT}\\"")\n\n'
            )

        defines = self._effective_defines("armclang")
        if defines:
            f.write("# Preprocessor definitions\n")
            f.write("add_definitions(\n")
            for define in defines:
                f.write(f"    -D{define}\n")
            f.write(")\n\n")

        if self.include_paths:
            f.write("# Include directories\n")
            f.write("include_directories(\n")
            for include_path in sorted(self.include_paths):
                f.write(f'    "{self._normalize_path(include_path, dest)}"\n')
            f.write(")\n\n")

        f.write("# Source files\n")
        f.write("set(SOURCES\n")
        for source_file in sorted(self.source_files):
            f.write(f'    "{self._normalize_path(source_file, dest)}"\n')
        f.write(")\n\n")

        f.write(f"add_executable({self.target_name} ${{SOURCES}})\n")
        f.write(
            f'set_target_properties({self.target_name} PROPERTIES OUTPUT_NAME "{self._artifact_name()}" SUFFIX ".axf")\n\n'
        )

        if self.libraries:
            f.write("# Link libraries\n")
            f.write(f"target_link_libraries({self.target_name} PRIVATE\n")
            for lib in self.libraries:
                f.write(f"    {lib}\n")
            f.write(")\n\n")

        output_directory = os.path.join(self.project_dir, self.output_directory)
        output_directory = os.path.normpath(output_directory).replace("\\", "/")
        f.write(f'set(DEST_DIR "{output_directory}")\n')
        f.write(
            f"""add_custom_target(always_copy ALL
    COMMAND ${{CMAKE_COMMAND}} -E make_directory "${{DEST_DIR}}"
    COMMAND ${{CMAKE_COMMAND}} -E copy_if_different "$<TARGET_FILE:{self.target_name}>" "${{DEST_DIR}}/{self._artifact_name()}.axf"
    DEPENDS {self.target_name}
)\n"""
        )
        f.write('set_property(TARGET always_copy PROPERTY FOLDER "postbuild")\n')

    def _write_gcc_cmake_content(
        self, f: TextIOWrapper, dest: str, options: GenerationOptions
    ) -> None:
        linker_script = self._guess_gcc_linker_script()
        if linker_script is None:
            raise FileNotFoundError("Unable to locate a GCC linker script (.ld).")

        languages = self._project_languages()
        self._write_cmake_header(
            f,
            "cmake/arm-none-eabi-toolchain.cmake",
            languages,
        )

        rel_linker_script = self._normalize_path(linker_script, dest)
        artifact_name = self._artifact_name()
        source_files = self._gcc_source_files()
        cpu_options = self._gcc_cpu_options()

        f.write(
            f'set(LINKER_SCRIPT "${{CMAKE_CURRENT_LIST_DIR}}/{rel_linker_script}")\n'
        )
        f.write(f'set(MAP_FILE "${{CMAKE_CURRENT_BINARY_DIR}}/{artifact_name}.map")\n')
        f.write(f'set(HEX_FILE "${{CMAKE_CURRENT_BINARY_DIR}}/{artifact_name}.hex")\n')
        f.write(f'set(BIN_FILE "${{CMAKE_CURRENT_BINARY_DIR}}/{artifact_name}.bin")\n')
        f.write(
            f'set(ARTIFACT_STAMP "${{CMAKE_CURRENT_BINARY_DIR}}/{artifact_name}.artifacts.stamp")\n\n'
        )

        f.write("set(PROJECT_SOURCES\n")
        for source_file in source_files:
            f.write(f'    "{self._normalize_path(source_file, dest)}"\n')
        f.write(")\n\n")

        f.write(f"add_executable({self.target_name} ${{PROJECT_SOURCES}})\n")
        f.write(
            f'set_target_properties({self.target_name} PROPERTIES OUTPUT_NAME "{artifact_name}" SUFFIX ".elf")\n\n'
        )

        if self.include_paths:
            f.write(f"target_include_directories({self.target_name} PRIVATE\n")
            for include_path in sorted(self.include_paths):
                f.write(
                    f'    "${{CMAKE_CURRENT_LIST_DIR}}/{self._normalize_path(include_path, dest)}"\n'
                )
            f.write(")\n\n")

        defines = self._effective_defines("gcc")
        if defines:
            f.write(f"target_compile_definitions({self.target_name} PRIVATE\n")
            for define in defines:
                f.write(f"    {define}\n")
            f.write(")\n\n")

        f.write(f"target_compile_options({self.target_name} PRIVATE\n")
        for flag in cpu_options:
            f.write(f"    {flag}\n")
        f.write("    -ffunction-sections\n")
        f.write("    -fdata-sections\n")
        f.write("    -fno-common\n")
        f.write("    -Wall\n")
        f.write("    -Wextra\n")
        f.write(f"    $<$<COMPILE_LANGUAGE:C>:{self._gcc_c_standard()}>\n")
        f.write("    $<$<CONFIG:Debug>:-Og>\n")
        f.write("    $<$<CONFIG:Debug>:-g3>\n")
        if "CXX" in languages:
            f.write(f"    $<$<COMPILE_LANGUAGE:CXX>:{self._gcc_cpp_standard()}>\n")
        f.write("    $<$<COMPILE_LANGUAGE:ASM>:-x>\n")
        f.write("    $<$<COMPILE_LANGUAGE:ASM>:assembler-with-cpp>\n")
        f.write(")\n\n")

        f.write(f"target_link_options({self.target_name} PRIVATE\n")
        for flag in cpu_options:
            f.write(f"    {flag}\n")
        f.write("    -T${LINKER_SCRIPT}\n")
        f.write("    -Wl,--gc-sections\n")
        f.write("    -Wl,-Map=${MAP_FILE}\n")
        f.write("    -Wl,--print-memory-usage\n")
        f.write("    -specs=nano.specs\n")
        f.write("    -specs=nosys.specs\n")
        f.write("    -u\n")
        f.write("    _printf_float\n")
        f.write(")\n\n")

        libraries = self._dedupe_keep_order(self.libraries + ["m"])
        f.write(f"target_link_libraries({self.target_name} PRIVATE\n")
        for library in libraries:
            f.write(f"    {library}\n")
        f.write(")\n\n")

        f.write(
            f"""add_custom_command(
    OUTPUT ${{HEX_FILE}} ${{BIN_FILE}} ${{ARTIFACT_STAMP}}
    COMMAND ${{CMAKE_OBJCOPY}} -O ihex $<TARGET_FILE:{self.target_name}> ${{HEX_FILE}}
    COMMAND ${{CMAKE_OBJCOPY}} -O binary $<TARGET_FILE:{self.target_name}> ${{BIN_FILE}}
    COMMAND ${{CMAKE_SIZE}} --format=berkeley $<TARGET_FILE:{self.target_name}>
    COMMAND ${{CMAKE_COMMAND}} -E touch ${{ARTIFACT_STAMP}}
    DEPENDS {self.target_name}
    VERBATIM
)\n\n"""
        )
        f.write(
            f"""add_custom_target({self.target_name}_artifacts ALL
    DEPENDS ${{HEX_FILE}} ${{BIN_FILE}} ${{ARTIFACT_STAMP}}
)\n"""
        )

    def _write_cmake_content_with_options(
        self, f: TextIOWrapper, dest: str, options: GenerationOptions
    ) -> None:
        if options.compiler == "gcc":
            self._write_gcc_cmake_content(f, dest, options)
        else:
            self._write_armclang_cmake_content(f, dest, options)

    def _generate_configured_output(
        self, output_dir: Optional[str], options: GenerationOptions
    ) -> bool:
        output_path = Path(output_dir) if output_dir else self._default_output_path(options)
        output_path.mkdir(parents=True, exist_ok=True)

        try:
            with open(output_path / "CMakeLists.txt", "w", encoding="utf-8") as f:
                self._write_cmake_content_with_options(f, str(output_path), options)

            self._write_toolchain_files(output_path, options)
            script_name = self._host_command_names(options.host_os)["script_name"]
            self._write_text_file(
                output_path / script_name, self._build_script_content(options)
            )

            if options.export_vsc_settings:
                vsc_dir = output_path / ".vscode"
                vsc_dir.mkdir(parents=True, exist_ok=True)
                self._write_jlink_scripts(output_path, options)

                for file_name, content in {
                    "settings.json": self._build_vscode_settings(options),
                    "tasks.json": self._build_vscode_tasks(output_path, options),
                    "launch.json": self._build_vscode_launch(output_path, options),
                    "c_cpp_properties.json": self._build_vscode_c_cpp_properties(options),
                }.items():
                    with open(vsc_dir / file_name, "w", encoding="utf-8") as f:
                        json.dump(content, f, indent=4, ensure_ascii=False)

            return True
        except Exception as e:
            print(f"Error generating CMake files: {e}", file=sys.stderr)
            return False

    def generate_cmake(
        self,
        output_dir: Optional[str] = None,
        export_vsc_settings: bool = False,
        generation_options: Optional[GenerationOptions] = None,
    ) -> bool:
        """生成 CMake 文件"""
        options = self._prepare_generation_options(
            export_vsc_settings, generation_options
        )
        return self._generate_configured_output(output_dir, options)

    def _normalize_path(self, path: str, dest: str) -> str:

        try:
            rel_path = os.path.relpath(path, dest).replace("\\", "/")
        except ValueError:
            # 如果路径无法转换，直接使用绝对路径
            rel_path = path.replace("\\", "/")

        return rel_path

    def print_project_info(self):
        """打印项目信息"""
        print(f"项目名称: {self.target_name}")
        print(f"设备: {self.device_name}")
        print(f"厂商: {self.vendor}")
        print(f"CPU: {self.cpu}")
        print(f"编译器标志: {self.c_cpp_flags}, {self.linker_flags}")
        print(f"源文件数量: {len(self.source_files)}")
        print(f"头文件数量: {len(self.header_files)}")
        print(f"包含路径数量: {len(self.include_paths)}")
        print(f"预定义宏数量: {len(self.defines)}")

        if self.source_files:
            print("\n源文件:")
            for src in self.source_files[:10]:  # 只显示前10个
                print(f"  {src}")
            if len(self.source_files) > 10:
                print(f"  ... 还有 {len(self.source_files) - 10} 个文件")

        if self.defines:
            print(f"\n预定义宏: {', '.join(sorted(list(self.defines)[:10]))}")
            if len(self.defines) > 10:
                print(f"  ... 还有 {len(self.defines) - 10} 个宏")


def main(
    filepath: str,
    output_dir: Optional[str],
    uv4_path: Optional[str],
    verbose: bool,
    export_vsc_settings: bool,
    compiler: str = "armclang",
    generator: Optional[str] = None,
    host_os: str = "windows",
    debugger: str = "default",
    build_dir: Optional[str] = None,
):
    if verbose:
        print(f"Keil 项目文件: {filepath}")

    parser = KeilProjectToCMake(filepath, uv4_path, verbose)
    if not parser.parse():
        print("解析失败!", file=sys.stderr)
        sys.exit(1)

    if verbose:
        parser.print_project_info()

    options = GenerationOptions(
        compiler=compiler,
        generator=generator or DEFAULT_GENERATOR_BY_HOST.get(host_os, "Ninja"),
        host_os=host_os,
        debugger=debugger,
        build_dir=build_dir,
        export_vsc_settings=export_vsc_settings,
    )

    if not parser.generate_cmake(output_dir, export_vsc_settings, options):
        print("生成 CMake 文件失败!", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="解析 Keil 项目文件")
    parser.add_argument("path", help="Keil 项目文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="启用详细模式")
    parser.add_argument(
        "-e", "--export-vsc-settings", action="store_true", help="导出 VS Code 设置"
    )
    parser.add_argument(
        "-d",
        "--destination",
        dest="dest",
        help="输出目录（可选，默认为项目文件所在目录）",
    )
    parser.add_argument(
        "-a",
        "--ask-dest",
        dest="ask_dest",
        action="store_true",
        default=False,
        help="弹出对话框选择输出目录，-d 参数不再生效",
    )
    parser.add_argument(
        "--uv4-path",
        dest="uv4_path",
        help="Keil UV4 安装路径（可选）",
    )
    parser.add_argument(
        "--compiler",
        choices=sorted(SUPPORTED_COMPILERS.keys()),
        default="armclang",
        help="生成的编译器配置",
    )
    parser.add_argument(
        "--generator",
        default=None,
        help="生成的 CMake Generator，例如 Ninja、Unix Makefiles、MinGW Makefiles",
    )
    parser.add_argument(
        "--host-os",
        choices=sorted(SUPPORTED_HOST_SYSTEMS.keys()),
        default="windows",
        help="生成脚本和调试命令所针对的宿主系统",
    )
    parser.add_argument(
        "--debugger",
        choices=sorted(SUPPORTED_DEBUGGERS.keys()),
        default="default",
        help="调试/烧录后端配置",
    )
    parser.add_argument(
        "--build-dir",
        default=None,
        help="构建目录名称，例如 build 或 build-gcc",
    )
    args = parser.parse_args()

    if args.ask_dest:
        from tkinter import filedialog, messagebox
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        rc = messagebox.askquestion(
            "请确认", "输出目录是当前工程目录吗？\n选择【否】将打开文件夹选择对话框"
        )
        if rc == "no":
            rc = ""
            rc = filedialog.askdirectory(
                title="请选择文件夹", initialdir=os.getcwd()
            )
        else:
            rc = os.path.dirname(args.path)
        root.destroy()

        if rc == "":
            print("未选择输出目录，程序退出。", file=sys.stderr)
            sys.exit(1)
        dest = rc
    else:
        dest = args.dest

    generator = args.generator or DEFAULT_GENERATOR_BY_HOST[args.host_os]
    if generator not in get_supported_generators(args.host_os) and args.verbose:
        print(f'使用自定义 Generator: "{generator}"')

    main(
        args.path,
        dest,
        args.uv4_path,
        args.verbose,
        args.export_vsc_settings,
        args.compiler,
        generator,
        args.host_os,
        args.debugger,
        args.build_dir,
    )
