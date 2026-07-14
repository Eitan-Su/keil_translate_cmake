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
import hashlib
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
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

SUPPORTED_DEBUG_PROBES = {
    "default": "Auto",
    "all": "All",
    "stlink": "ST-Link",
    "jlink": "J-Link",
    "daplink": "DAPLink",
    "nulink": "Nu-Link",
    "ulink": "ULINK",
}

SUPPORTED_DEBUG_BACKENDS = {
    "default": "Default",
    "all": "All",
    "openocd": "OpenOCD",
    "jlink": "J-Link GDB Server",
    "pyocd": "PyOCD",
    "keil": "Keil MDK",
    "none": "None",
}

# Backward-compatible alias for historical code and imports.
SUPPORTED_DEBUGGERS = SUPPORTED_DEBUG_BACKENDS

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
    debug_probe: str = "default"
    debug_backend: str = "default"
    build_dir: Optional[str] = None
    export_vsc_settings: bool = False

    def normalized(self) -> "GenerationOptions":
        compiler = (self.compiler or "armclang").strip().lower()
        if compiler not in SUPPORTED_COMPILERS:
            raise ValueError(f"Unsupported compiler: {self.compiler}")

        host_os = (self.host_os or "windows").strip().lower()
        if host_os not in SUPPORTED_HOST_SYSTEMS:
            raise ValueError(f"Unsupported host OS: {self.host_os}")

        debug_probe = (self.debug_probe or "default").strip().lower()
        if debug_probe not in SUPPORTED_DEBUG_PROBES:
            raise ValueError(f"Unsupported debug probe: {self.debug_probe}")

        raw_backend_field = (self.debug_backend or "").strip().lower()
        legacy_debugger = (self.debugger or "").strip().lower()
        if raw_backend_field and raw_backend_field != "default":
            backend_raw = raw_backend_field
        else:
            backend_raw = legacy_debugger or "default"

        if (
            raw_backend_field in {"", "default"}
            and backend_raw == "stlink"
            and debug_probe == "default"
        ):
            # Compatibility with historical value debugger=stlink.
            debug_probe = "stlink"
            backend_raw = "default"

        if backend_raw not in SUPPORTED_DEBUG_BACKENDS:
            raise ValueError(
                f"Unsupported debug backend: {self.debug_backend or self.debugger}"
            )
        if backend_raw == "jlink" and debug_probe not in {"default", "all", "jlink"}:
            raise ValueError(
                f"Probe '{debug_probe}' is incompatible with J-Link backend."
            )

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
            debugger=backend_raw,
            debug_probe=debug_probe,
            debug_backend=backend_raw,
            build_dir=build_dir,
            export_vsc_settings=bool(self.export_vsc_settings),
        )


@dataclass
class AssetRecord:
    kind: str
    path: str
    source: str
    support_level: str
    reason: str


@dataclass
class ConvertedTarget:
    project_file: str
    target_name: str
    output_name: str
    device: str
    vendor: str
    cpu: str
    compiler: str
    source_files: List[str]
    include_paths: List[str]
    defines: List[str]
    libraries: List[str]
    output_directory: str
    assets: List[AssetRecord] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class CommandResult:
    command: List[str]
    return_code: Optional[int]
    stdout: str
    stderr: str
    duration_seconds: float
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.return_code == 0 and not self.error


@dataclass
class BuildDiagnostic:
    category: str
    phase: str
    message: str


@dataclass
class BuildValidationReport:
    status: str
    configure: CommandResult
    build: Optional[CommandResult]
    diagnostics: List[BuildDiagnostic]


class BuildValidator:
    """Configure and build a generated project, then classify common failures."""

    DIAGNOSTIC_PATTERNS = [
        ("missing_tool", re.compile(r"is not recognized|command not found|executable not found|\bwas not found\b|\bnot set\b", re.I)),
        ("missing_header", re.compile(r"fatal error: .*: No such file or directory", re.I)),
        ("assembly_syntax", re.compile(r"assembler messages|unknown pseudo-op|bad instruction", re.I)),
        ("duplicate_symbol", re.compile(r"multiple definition of|duplicate symbol", re.I)),
        ("undefined_symbol", re.compile(r"undefined reference to|undefined symbol", re.I)),
        ("linker_script", re.compile(r"linker script|cannot open linker script|syntax error.*\.ld", re.I)),
        ("memory_overflow", re.compile(r"region .* overflowed|will not fit in region", re.I)),
        ("abi_mismatch", re.compile(r"uses VFP register arguments|conflicting CPU architectures|file format not recognized", re.I)),
        ("compile_error", re.compile(r"\berror:", re.I)),
    ]

    def __init__(
        self,
        timeout_seconds: int = 300,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.progress_callback = progress_callback
        self._last_progress = -1

    def _emit_progress(self, value: int, message: str) -> None:
        value = max(0, min(100, int(value)))
        if value < self._last_progress:
            return
        self._last_progress = value
        if self.progress_callback is not None:
            self.progress_callback(value, message)

    @staticmethod
    def _build_progress_from_line(line: str) -> Optional[tuple[int, int]]:
        ninja = re.search(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]", line)
        if ninja:
            current = int(ninja.group(1))
            total = int(ninja.group(2))
            if total > 0:
                return current, total

        make = re.search(r"\[\s*(\d{1,3})%\s*\]", line)
        if make:
            percent = max(0, min(100, int(make.group(1))))
            return percent, 100
        return None

    def _handle_stream_line(self, phase: str, line: str) -> None:
        if phase == "configure":
            if "Configuring done" in line:
                self._emit_progress(6, "CMake 配置完成")
            elif "Generating done" in line:
                self._emit_progress(9, "CMake 生成完成")
            return

        progress = self._build_progress_from_line(line)
        if progress is None:
            return
        current, total = progress
        percent = 10 + round(88 * current / total)
        self._emit_progress(percent, f"编译 {current}/{total}")

    def _run_streaming(
        self, command: List[str], cwd: Path, phase: str
    ) -> CommandResult:
        started = time.monotonic()
        stdout_parts: List[str] = []
        stderr_parts: List[str] = []
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            return CommandResult(
                command=command,
                return_code=None,
                stdout="",
                stderr="",
                duration_seconds=round(time.monotonic() - started, 3),
                error=str(exc),
            )

        output_queue: queue.Queue[tuple[str, Optional[str]]] = queue.Queue()

        def read_stream(name: str, stream: Any) -> None:
            try:
                for line in iter(stream.readline, ""):
                    output_queue.put((name, line))
            finally:
                stream.close()
                output_queue.put((name, None))

        readers = [
            threading.Thread(
                target=read_stream,
                args=("stdout", process.stdout),
                daemon=True,
            ),
            threading.Thread(
                target=read_stream,
                args=("stderr", process.stderr),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        completed_streams = 0
        error = ""
        while completed_streams < len(readers):
            if time.monotonic() - started >= self.timeout_seconds:
                process.kill()
                error = f"Command timed out after {self.timeout_seconds} seconds"
                break
            try:
                stream_name, line = output_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                completed_streams += 1
                continue
            if stream_name == "stdout":
                stdout_parts.append(line)
            else:
                stderr_parts.append(line)
            self._handle_stream_line(phase, line.rstrip())

        for reader in readers:
            reader.join(timeout=1)
        while True:
            try:
                stream_name, line = output_queue.get_nowait()
            except queue.Empty:
                break
            if line is None:
                continue
            if stream_name == "stdout":
                stdout_parts.append(line)
            else:
                stderr_parts.append(line)

        return_code = process.wait()
        return CommandResult(
            command=command,
            return_code=return_code,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            duration_seconds=round(time.monotonic() - started, 3),
            error=error,
        )

    def _run(
        self, command: List[str], cwd: Path, phase: str = ""
    ) -> CommandResult:
        if self.progress_callback is not None:
            return self._run_streaming(command, cwd, phase)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
            return CommandResult(
                command=command,
                return_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_seconds=round(time.monotonic() - started, 3),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(
                command=command,
                return_code=None,
                stdout=getattr(exc, "stdout", "") or "",
                stderr=getattr(exc, "stderr", "") or "",
                duration_seconds=round(time.monotonic() - started, 3),
                error=str(exc),
            )

    def _classify(self, phase: str, result: CommandResult) -> List[BuildDiagnostic]:
        if result.succeeded:
            return []
        text = "\n".join((result.error, result.stdout, result.stderr))
        diagnostics: List[BuildDiagnostic] = []
        for category, pattern in self.DIAGNOSTIC_PATTERNS:
            match = pattern.search(text)
            if match:
                diagnostics.append(
                    BuildDiagnostic(category=category, phase=phase, message=match.group(0))
                )
        if not diagnostics:
            diagnostics.append(
                BuildDiagnostic(
                    category=f"{phase}_failed",
                    phase=phase,
                    message=result.error or f"Command exited with code {result.return_code}",
                )
            )
        return diagnostics

    def validate(
        self, source_dir: Path, options: GenerationOptions
    ) -> BuildValidationReport:
        self._last_progress = -1
        self._emit_progress(0, "准备 CMake 配置")
        build_dir = source_dir / str(options.build_dir)
        configure_command = [
            "cmake",
            "-S",
            str(source_dir),
            "-B",
            str(build_dir),
            "-G",
            options.generator,
        ]
        configure = self._run(configure_command, source_dir, "configure")
        diagnostics = self._classify("configure", configure)
        if not configure.succeeded:
            return BuildValidationReport("configure_failed", configure, None, diagnostics)

        self._emit_progress(10, "开始编译")
        build = self._run(
            ["cmake", "--build", str(build_dir)], source_dir, "build"
        )
        diagnostics.extend(self._classify("build", build))
        status = "success" if build.succeeded else "build_failed"
        if build.succeeded:
            self._emit_progress(100, "构建验证完成")
        return BuildValidationReport(status, configure, build, diagnostics)


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
        self,
        uvprojx_path: str,
        uv4_path: Optional[str] = None,
        verbose: bool = False,
        target_name: Optional[str] = None,
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
        self.last_error: str = ""
        self.warnings: List[str] = []
        self.asset_records: List[AssetRecord] = []
        self._missing_source_cache: Dict[str, Optional[Path]] = {}
        self._project_rte_instance_names: Set[str] = set()
        self._copy_staged_source_mappings: List[tuple[Path, Path]] = []
        self._lvgl_lazy_new_allocator_cache: Optional[bool] = None
        self._generation_progress_callback: Optional[
            Callable[[int, str], None]
        ] = None
        self.requested_target_name = (target_name or "").strip()

        # 项目信息
        self.output_directory = ""
        self.output_name = ""
        self.target_name = ""
        self.device_name = ""
        self.vendor = ""
        self.pack_id = ""
        self.cpu = ""
        self.cpu_type = ""
        self.flash_origin: Optional[int] = None
        self.flash_length: Optional[int] = None
        self.ram_origin: Optional[int] = None
        self.ram_length: Optional[int] = None

        # 源文件和头文件
        self.source_files: List[str] = []
        self.header_files: List[str] = []
        self.include_paths: Set[str] = set()
        self.include_path_order: List[str] = []

        # 编译器设置
        self.defines: Set[str] = set()
        self.cpu_flags: List[str] = ["--target=arm-arm-none-eabi"]
        self.c_cpp_flags: List[str] = []  # C/Cpp 编译标记
        self.c_flags: List[str] = []  # C 编译标记
        self.cpp_flags: List[str] = []  # C++ 编译标记
        self.asm_flags: List[str] = []  # 汇编编译标记
        self.linker_flags: List[str] = []
        self.keil_optimization: Optional[str] = None
        self.keil_optimize_for_time: Optional[str] = None
        self.linker_script = ""
        self.linker_inputs: List[str] = []

        # 库文件
        self.libraries: List[str] = []
        self.library_paths: Set[str] = set()

    def _emit_generation_progress(self, value: int, message: str) -> None:
        if self._generation_progress_callback is not None:
            self._generation_progress_callback(
                max(0, min(100, int(value))), message
            )

    def parse(self) -> bool:
        """解析uVision项目文件"""
        self.last_error = ""
        self.warnings = []
        self.asset_records = []
        self._missing_source_cache = {}
        self._project_rte_instance_names = set()

        try:
            if not self.uvprojx_path.exists():
                self.last_error = f"项目文件不存在: {self.uvprojx_path}"
                print(f"错误: {self.last_error}", file=sys.stderr)
                return False

            # 解析XML文件
            tree = ET.parse(self.uvprojx_path)
            root = tree.getroot()

            targets = root.findall(".//Target")

            if len(targets) == 0:
                self.last_error = "项目文件中没有 Target 节点"
                return False

            target = self._select_target(targets)
            if target is None:
                return False

            # 解析项目基本信息
            self._parse_project_info(target)

            # 解析目标设备信息
            self._parse_target_info(target)

            # 解析源文件
            self._parse_source_files(target)

            # 解析编译器设置
            self._parse_compiler_settings(target)

            # 解析链接器设置
            self._parse_linker_settings(target)

            self._parse_project_rte_assets(root, self.target_name)
            self._parse_runtime_env(root, self.target_name)
            self._prefer_project_rte_instances()
            self._add_conventional_include_dirs()
            self._add_cmsis_core_include()
            self._add_selected_device_pack_includes()

            return True

        except ET.ParseError as e:
            self.last_error = f"XML解析错误: {e}"
            print(self.last_error, file=sys.stderr)
            return False
        except Exception as e:
            self.last_error = f"解析项目文件时发生错误: {e}"
            print(self.last_error, file=sys.stderr)
            return False

    def _parse_project_rte_assets(self, root: ET.Element, target_name: str) -> None:
        """Prefer generated RTE files stored beside the uVision project."""
        rte_root = self.project_dir / "RTE"
        if not rte_root.is_dir():
            return

        target_dir = rte_root / f"_{target_name}"
        if target_dir.is_dir():
            self._add_include_path(str(target_dir))
            self.defines.add("_RTE_")

        device_dir = rte_root / "Device" / self.device_name
        if device_dir.is_dir():
            self._add_include_path(str(device_dir))

        for file_node in root.findall(".//RTE/files/file"):
            target_names = {
                (node.get("name") or "").strip()
                for node in file_node.findall("./targetInfos/targetInfo")
            }
            if target_names and target_name not in target_names:
                continue
            for instance in file_node.findall("./instance"):
                if (instance.get("removed") or "").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                }:
                    continue
                instance_text = (instance.text or "").strip().replace("\\", "/")
                if not instance_text:
                    continue
                instance_path = Path(instance_text)
                if not instance_path.is_absolute():
                    instance_path = self.project_dir / instance_path
                instance_path = instance_path.resolve(strict=False)
                if not instance_path.is_file():
                    continue
                self._project_rte_instance_names.add(instance_path.name.lower())
                self._add_include_path(str(instance_path.parent))
                suffix = instance_path.suffix.lower()
                category = (file_node.get("category") or "").lower()
                if suffix in {".h", ".hpp", ".hxx"}:
                    self.header_files.append(str(instance_path))
                elif category == "source" and suffix in {
                    ".c", ".cpp", ".cc", ".cxx", ".s", ".asm"
                }:
                    self.source_files.append(str(instance_path))

    def _prefer_project_rte_instances(self) -> None:
        if not self._project_rte_instance_names:
            return
        rte_root = (self.project_dir / "RTE").resolve(strict=False)
        preferred: List[str] = []
        for source in self.source_files:
            source_path = Path(source).resolve(strict=False)
            if source_path.name.lower() not in self._project_rte_instance_names:
                preferred.append(source)
                continue
            try:
                source_path.relative_to(rte_root)
            except ValueError:
                continue
            preferred.append(source)
        self.source_files = self._dedupe_keep_order(preferred)

    def _select_target(self, targets: List[ET.Element]) -> Optional[ET.Element]:
        if not self.requested_target_name:
            return targets[0]

        available_names = [
            self._get_element_text(target, ".//TargetName") for target in targets
        ]
        for target, target_name in zip(targets, available_names):
            if target_name == self.requested_target_name:
                return target

        names = ", ".join(name for name in available_names if name) or "(unnamed)"
        self.last_error = (
            f"未找到目标 TargetName={self.requested_target_name}，可用目标: {names}"
        )
        print(self.last_error, file=sys.stderr)
        return None

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

        # Device Family Pack selected by the Keil target.
        self.pack_id = self._get_element_text(root, ".//PackID")

        # CPU类型
        self.cpu_type = self._get_element_text(root, ".//Cpu")
        self.flash_origin, self.flash_length = self._extract_memory_region(
            self.cpu_type, "IROM"
        )
        self.ram_origin, self.ram_length = self._extract_memory_region(
            self.cpu_type, "IRAM"
        )
        if self.flash_origin is None or self.flash_length is None:
            self.flash_origin, self.flash_length = self._extract_memory_node(
                root, "IROM"
            )
        if self.ram_origin is None or self.ram_length is None:
            self.ram_origin, self.ram_length = self._extract_memory_node(
                root, "IRAM"
            )

        if self.cpu_type == "":
            return

    @staticmethod
    def _extract_memory_region(cpu_desc: str, prefix: str) -> tuple[Optional[int], Optional[int]]:
        if not cpu_desc:
            return None, None

        pattern = re.compile(
            rf"{prefix}\d*\((0x[0-9a-fA-F]+),(0x[0-9a-fA-F]+)\)", re.IGNORECASE
        )
        match = pattern.search(cpu_desc)
        if match is None:
            return None, None
        try:
            return int(match.group(1), 16), int(match.group(2), 16)
        except ValueError:
            return None, None

    def _extract_memory_node(
        self, root: ET.Element, node_name: str
    ) -> tuple[Optional[int], Optional[int]]:
        node = root.find(f".//OnChipMemories/{node_name}")
        if node is None:
            node = root.find(f".//{node_name}")
        if node is None:
            return None, None

        origin_text = self._get_element_text(node, "StartAddress")
        length_text = self._get_element_text(node, "Size")
        try:
            return int(origin_text, 0), int(length_text, 0)
        except ValueError:
            return None, None

    @staticmethod
    def _normalize_gcc_cpu_name(cpu_name: str) -> str:
        name = (cpu_name or "").strip().lower()
        aliases = {
            "cortex-m0+": "cortex-m0plus",
        }
        return aliases.get(name, name)

    def _infer_cpu_from_device(self) -> str:
        clean_device = self._clean_device_name().upper()
        if clean_device.startswith(("STM32F0", "STM32G0", "STM32L0")):
            return "Cortex-M0+"
        if clean_device.startswith(("STM32F1", "STM32F2", "STM32L1")):
            return "Cortex-M3"
        if clean_device.startswith(
            (
                "STM32F3",
                "STM32F4",
                "STM32G4",
                "STM32L4",
                "STM32WB",
                "STM32WL",
                "AT32F4",
            )
        ):
            return "Cortex-M4"
        if clean_device.startswith(("STM32F7", "STM32H7")):
            return "Cortex-M7"
        if clean_device.startswith(("STM32H5", "STM32U5", "STM32WBA")):
            return "Cortex-M33"
        return ""

    def _parse_source_files(self, root: ET.Element):
        """解析源文件和头文件"""
        # 查找所有文件组
        groups = root.findall(".//Group")

        for group in groups:
            self._parse_local_controls(group, ".//GroupOption")
            files = group.findall(".//File")
            for file_node in files:
                file_name = self._get_element_text(file_node, "FileName")
                file_path = self._get_element_text(file_node, "FilePath")
                file_type = self._get_element_text(file_node, "FileType")

                if file_name and file_path:
                    if not self._is_file_included_in_build(file_node):
                        continue
                    self._parse_local_controls(file_node, ".//FileOption")

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
                            self._add_include_path(include_dir)
                    elif file_ext in [".lib", ".a"]:
                        self.libraries.append(file_path)

    def _parse_local_controls(self, node: ET.Element, option_path: str) -> None:
        option = node.find(option_path)
        if option is None:
            return

        define_text = self._get_element_text(
            option, ".//Cads/VariousControls/Define"
        )
        for define in define_text.replace(" ", "").split(","):
            if define.strip():
                self.defines.add(define.strip())

        include_text = self._get_element_text(
            option, ".//Cads/VariousControls/IncludePath"
        )
        for include in include_text.split(";"):
            include = include.strip().replace("\\", "/")
            if not include or include == ".":
                continue
            if not os.path.isabs(include):
                include = os.path.join(self.project_dir, include)
            self._add_include_path(os.path.normpath(include))

    def _is_file_included_in_build(self, file_node: ET.Element) -> bool:
        include_in_build = self._get_element_text(
            file_node, ".//FileOption/CommonProperty/IncludeInBuild", ""
        ).strip()
        return include_in_build != "0"

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

    def _add_include_path(self, path: str) -> None:
        normalized = os.path.normpath(path)
        if normalized in self.include_paths:
            return
        self.include_paths.add(normalized)
        self.include_path_order.append(normalized)

    def _ordered_include_paths(self) -> List[str]:
        ordered = list(self.include_path_order)
        ordered.extend(
            path for path in self.include_paths if path not in self.include_path_order
        )
        return ordered

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

    @staticmethod
    def _split_defines(defines_text: str) -> List[str]:
        return [
            value
            for value in re.split(r"[,;\s]+", defines_text.strip())
            if value
        ]

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

        if not self.cpu:
            ads_cpu = self._get_element_text(root, ".//AdsCpuType").strip('"')
            if ads_cpu:
                self.cpu = ads_cpu
        if not self.cpu:
            self.cpu = self._infer_cpu_from_device()

        cpu = self.cpu.lower()
        gcc_cpu = self._normalize_gcc_cpu_name(cpu)
        features = [f"-mcpu={gcc_cpu}"] if gcc_cpu else []

        # FPU
        fpu_txt = self._get_element_text(root, ".//RvdsVP")
        if not fpu_txt and (
            "FPU2" in cpu_feature or cpu_feature.get("FPU") not in {None, "NO_FPU"}
        ):
            fpu_txt = "2"

        if fpu_txt == "2" or fpu_txt == "3":
            self.cpu_flags += ["-mfloat-abi=hard"]
        else:
            self.cpu_flags += ["-mfloat-abi=soft"]

        match (gcc_cpu):
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

        if features:
            self.cpu_flags += ["".join(features)]

        branchprot_txt = self._get_element_text(root, ".//nBranchProt")
        if branchprot_txt == "2":
            self.cpu_flags += ["-mbranch-protection=bti+pac-ret"]
        elif branchprot_txt == "1":
            self.cpu_flags += ["-mbranch-protection=bti"]

        if "ENDIAN" in cpu_feature and cpu_feature["ENDIAN"] == "big":
            self.cpu_flags += ["-mbig-endian"]
        else:
            self.cpu_flags += ["-mlittle-endian"]

        # C编译器设置
        self.c_cpp_flags += ["-ffunction-sections"]

        c_compiler = root.find(".//Cads")
        if c_compiler is not None:
            self.keil_optimization = self._get_element_text(c_compiler, "Optim")
            self.keil_optimize_for_time = self._get_element_text(c_compiler, "oTime")

            # 预定义宏
            defines_text = self._get_element_text(
                c_compiler, ".//VariousControls/Define"
            )
            if defines_text:
                for define in self._split_defines(defines_text):
                    if define.strip():
                        self.defines.add(define.strip())

            # 包含路径
            include_text = self._get_element_text(
                c_compiler, ".//VariousControls/IncludePath"
            )
            if include_text:
                includes = include_text.split(";")
                explicit_includes: List[str] = []
                for include in includes:
                    include = include.strip().replace("\\", "/")
                    if include and include != ".":
                        if not os.path.isabs(include):
                            include = os.path.join(self.project_dir, include)
                        include = os.path.normpath(include)
                        self._add_include_path(include)
                        explicit_includes.append(include)
                self.include_path_order = explicit_includes + [
                    path
                    for path in self.include_path_order
                    if path not in explicit_includes
                ]

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
                for item in misc_text.split():
                    candidate = item.replace("\\", "/")
                    if not os.path.isabs(candidate):
                        candidate = os.path.join(self.project_dir, candidate)
                    candidate = os.path.normpath(candidate)
                    suffix = Path(candidate).suffix.lower()
                    if suffix in {".obj", ".o"} and Path(candidate).is_file():
                        self.linker_inputs.append(candidate)
                    elif suffix in {".lib", ".a"} and Path(candidate).is_file():
                        self.libraries.append(candidate)
                    else:
                        self.linker_flags.append(item)

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
        group: Optional[str],
        condition: Optional[str],
        sub: Optional[str] = None,
    ) -> None:
        """查找PDSC文件中指定类别和子类别的文件"""
        if not os.path.exists(filepath):
            return

        base = os.path.dirname(filepath)
        try:
            tree = ET.parse(filepath)
        except ET.ParseError as e:
            self.warnings.append(f"PDSC 解析失败: {filepath}: {e}")
            return
        root = tree.getroot()

        for c_node in root.findall(".//component"):
            if (
                c_node.get("Cclass") == cls
                and c_node.get("Cgroup") == group
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
                        source_parent = Path(p).parent
                        if source_parent.name.lower() == "src":
                            sibling_include = source_parent.parent / "inc"
                            if sibling_include.is_dir():
                                self._add_include_path(str(sibling_include))
                    else:
                        if os.path.isdir(p):
                            self._add_include_path(p)
                        #
                    #
                #
            #
        # end-for

    def _add_cmsis_core_include(self) -> None:
        if "cortex-m" not in (self.cpu or "").lower():
            return

        candidates: List[Path] = []
        mdk_roots: List[Path] = []
        if self.mdk_info:
            for key in ("UV4_ROOT", "RTEPATH"):
                value = self.mdk_info.get(key)
                if value:
                    mdk_roots.append(Path(value).resolve(strict=False))
        mdk_roots.append(Path(self.uv4_path).resolve(strict=False))

        checked_roots: Set[Path] = set()
        for root in mdk_roots:
            for base in (root, root.parent, root.parent.parent):
                if base in checked_roots or not base.is_dir():
                    continue
                checked_roots.add(base)
                direct_candidates = [
                    base / "ARM" / "CMSIS" / "Include",
                    base / "CMSIS" / "Include",
                    base / "CMSIS" / "Core" / "Include",
                ]
                candidates.extend(path for path in direct_candidates if path.is_dir())

                pack_root = base / "ARM" / "PACK" / "ARM" / "CMSIS"
                if pack_root.is_dir():
                    candidates.extend(pack_root.glob("*/CMSIS/Core/Include"))
                    candidates.extend(pack_root.glob("*/CMSIS/Include"))

        valid = [
            path.resolve(strict=False)
            for path in candidates
            if path.is_dir() and any(path.glob("core_cm*.h"))
        ]
        if valid:
            selected = sorted(set(valid), key=lambda path: path.as_posix())[-1]
            self._add_include_path(str(selected))

    def _add_selected_device_pack_includes(self) -> None:
        if not self.pack_id or not self.mdk_info:
            return

        rte_path = self.mdk_info.get("RTEPATH")
        parts = self.pack_id.split(".", 2)
        if not rte_path or len(parts) != 3:
            return

        vendor, package, version = parts
        package_dir = Path(rte_path) / vendor / package / version
        if not package_dir.is_dir():
            return

        include_dirs: List[Path] = []
        for candidate in package_dir.rglob("*"):
            if not candidate.is_dir() or candidate.name.lower() not in {
                "include",
                "inc",
            }:
                continue
            normalized = candidate.as_posix().lower()
            if (
                "/device/" in normalized
                or "/cmsis/core/" in normalized
                or "/cmsis/include" in normalized
            ):
                include_dirs.append(candidate.resolve(strict=False))

        for include_dir in sorted(set(include_dirs), key=lambda path: path.as_posix()):
            self._add_include_path(str(include_dir))

        if include_dirs:
            self.warnings.append(
                f"使用 Device Pack 头文件目录: {self.pack_id}"
            )

    def _add_conventional_include_dirs(self) -> None:
        for include_path in list(self.include_paths):
            path = Path(include_path)
            if path.name.lower() == "src":
                sibling_include = path.parent / "inc"
                if sibling_include.is_dir():
                    self._add_include_path(str(sibling_include.resolve(strict=False)))

    def _parse_runtime_env(self, root: ET.Element, target_name: str):
        """解析运行时环境设置"""
        runtime_env = root.find(".//RTE")
        if runtime_env is None:
            return

        if self.mdk_info is None or not self.mdk_info.get("RTEPATH"):
            self.warnings.append(
                "未找到 Keil MDK RTEPATH，跳过 RTE 组件自动补全"
            )
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

            self._search_pdsc_file(
                str(package_filepath), c_class, c_group, condition, c_sub
            )

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
                debug_probe=generation_options.debug_probe,
                debug_backend=generation_options.debug_backend,
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

    def _asset_source(self, path: Path, output_dir: Optional[Path] = None) -> str:
        resolved = path.resolve(strict=False)
        if output_dir is not None:
            try:
                resolved.relative_to(output_dir.resolve(strict=False))
                return "generated"
            except ValueError:
                pass
        try:
            resolved.relative_to(self.project_dir.resolve(strict=False))
            return "project"
        except ValueError:
            return "external"

    def _record_asset(
        self,
        kind: str,
        path: Path,
        source: str,
        support_level: str,
        reason: str,
    ) -> None:
        normalized = str(path.resolve(strict=False))
        for record in self.asset_records:
            if record.kind == kind and record.path == normalized:
                return
        self.asset_records.append(
            AssetRecord(kind, normalized, source, support_level, reason)
        )

    def converted_target(
        self, output_path: Path, options: GenerationOptions
    ) -> ConvertedTarget:
        return ConvertedTarget(
            project_file=str(self.uvprojx_path.resolve(strict=False)),
            target_name=self.target_name,
            output_name=self._artifact_name(),
            device=self.device_name,
            vendor=self.vendor,
            cpu=self.cpu,
            compiler=options.compiler,
            source_files=sorted(self.source_files),
            include_paths=self._ordered_include_paths(),
            defines=sorted(self.defines),
            libraries=list(self.libraries),
            output_directory=str(output_path.resolve(strict=False)),
            assets=list(self.asset_records),
            warnings=list(self.warnings),
        )

    def _write_conversion_report(
        self, output_path: Path, options: GenerationOptions
    ) -> None:
        report = asdict(self.converted_target(output_path, options))
        with open(output_path / "conversion-report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

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

    def _cmake_target_name(self) -> str:
        raw = self.target_name or self.project_name or "target"
        safe = re.sub(r"[^A-Za-z0-9_]", "_", raw)
        safe = re.sub(r"_+", "_", safe).strip("_")
        if not safe:
            safe = "target"
        if safe[0].isdigit():
            safe = f"T_{safe}"
        return safe

    def _cmake_artifacts_target_name(self) -> str:
        return f"{self._cmake_target_name()}_artifacts"

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
        mapped: List[str] = []
        for source in self.source_files:
            source_path = Path(source)
            if source_path.name.lower() in {"rt_sys.c", "rt_sys.cpp"}:
                try:
                    head = source_path.read_text(
                        encoding="utf-8", errors="ignore"
                    )[:4096]
                except OSError:
                    head = ""
                if "rt_sys.h" in head or "#pragma import" in head:
                    self.warnings.append(
                        f"GCC 模式排除 Keil C Library 重定向文件: {source_path}"
                    )
                    continue
            mapped.append(self._map_source_for_gcc(source))
        return sorted(self._dedupe_keep_order(mapped))

    @staticmethod
    def _format_stm32_startup_define(token: str) -> Optional[str]:
        value = (token or "").strip().lower()
        if not value.startswith("stm32"):
            return None

        if value.startswith("stm32f10x_"):
            return value.upper()

        formatted = ["STM32"]
        for char in value[5:]:
            if char.isalpha():
                formatted.append("x" if char == "x" else char.upper())
            else:
                formatted.append(char)
        return "".join(formatted)

    def _startup_device_define(self) -> Optional[str]:
        startup_sources = self._gcc_source_files() + sorted(self.source_files)
        seen: Set[str] = set()
        for source in startup_sources:
            stem = Path(source).stem.lower()
            if not stem.startswith("startup_") or stem in seen:
                continue
            seen.add(stem)
            macro = self._format_stm32_startup_define(stem.replace("startup_", "", 1))
            if macro:
                return macro
        return None

    def _device_flash_kb(self) -> Optional[int]:
        if self.flash_length:
            return self.flash_length // 1024

        memory_code = self._device_memory_code().upper()
        memory_by_code = {
            "4": 16,
            "6": 32,
            "8": 64,
            "B": 128,
            "C": 256,
            "D": 384,
            "E": 512,
            "F": 768,
            "G": 1024,
        }
        return memory_by_code.get(memory_code)

    def _infer_stm32f1_stdperiph_define(self) -> Optional[str]:
        startup_define = self._startup_device_define()
        if startup_define and re.fullmatch(r"STM32F10X_[A-Z0-9_]+", startup_define):
            return startup_define

        clean_device = self._clean_device_name().upper()
        if not clean_device.startswith("STM32F10"):
            return None

        if clean_device.startswith(("STM32F105", "STM32F107")):
            return "STM32F10X_CL"

        flash_kb = self._device_flash_kb()
        if flash_kb is None:
            return None

        if flash_kb <= 32:
            density = "LD"
        elif flash_kb <= 128:
            density = "MD"
        elif flash_kb <= 512:
            density = "HD"
        else:
            density = "XL"

        if clean_device.startswith("STM32F100"):
            return f"STM32F10X_{density}_VL" if density != "XL" else "STM32F10X_HD_VL"

        if clean_device.startswith(("STM32F101", "STM32F102", "STM32F103")):
            return f"STM32F10X_{density}"

        return None

    def _inferred_device_defines(self) -> List[str]:
        inferred: List[str] = []

        startup_define = self._startup_device_define()
        if startup_define:
            inferred.append(startup_define)

        stm32f1_define = self._infer_stm32f1_stdperiph_define()
        if stm32f1_define:
            inferred.append(stm32f1_define)

        device_name = self._clean_device_name()
        if device_name and re.fullmatch(r"[A-Za-z_]\w*", device_name):
            inferred.append(device_name)

        return self._dedupe_keep_order(inferred)

    @staticmethod
    def _is_armasm_startup_source(path: Path) -> bool:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False

        head = content[:4096].upper()
        normalized = " " + re.sub(r"\s+", " ", head) + " "
        required_tokens = (" AREA ", " EXPORT ", " DCD ")
        supporting_tokens = (" PROC", " ENDP", " PRESERVE8", " THUMB")
        return all(token in normalized for token in required_tokens) and any(
            token in normalized for token in supporting_tokens
        )

    @staticmethod
    def _parse_armasm_vector_entries(path: Path) -> List[str]:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return []

        entries: List[str] = []
        in_vectors = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("__Vectors_End"):
                break
            if stripped.startswith("__Vectors"):
                in_vectors = True
            if not in_vectors:
                continue

            match = re.search(r"\bDCD\b\s+([A-Za-z_][A-Za-z0-9_]*|0)\b", stripped)
            if match:
                entries.append(match.group(1))

        return entries

    @staticmethod
    def _parse_armasm_integer(value: str) -> Optional[int]:
        normalized = value.strip().strip("()")
        try:
            if normalized.lower().startswith("0x"):
                return int(normalized, 16)
            if normalized.lower().endswith("h"):
                return int(normalized[:-1], 16)
            return int(normalized, 10)
        except ValueError:
            return None

    @classmethod
    def _parse_armasm_stack_heap_sizes(
        cls, path: Path
    ) -> Tuple[Optional[int], Optional[int]]:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None, None

        def reservation(symbol: str) -> Optional[int]:
            match = re.search(
                rf"(?mi)^\s*{re.escape(symbol)}\s+EQU\s+([^\s;]+)",
                content,
            )
            return cls._parse_armasm_integer(match.group(1)) if match else None

        return reservation("Stack_Size"), reservation("Heap_Size")

    def _gcc_stack_heap_sizes(self) -> Tuple[int, int]:
        default_stack_size = 0x400
        default_heap_size = 0x400
        for source in self.source_files:
            path = Path(source)
            if path.suffix.lower() not in {".s", ".asm"} or not path.is_file():
                continue
            if "startup" not in path.stem.lower() and not self._is_armasm_startup_source(path):
                continue
            stack_size, heap_size = self._parse_armasm_stack_heap_sizes(path)
            if stack_size is None and heap_size is None:
                continue
            return (
                stack_size if stack_size is not None else default_stack_size,
                heap_size if heap_size is not None else default_heap_size,
            )
        return default_stack_size, default_heap_size

    def _parse_armasm_pre_system_init_calls(self, path: Path) -> List[str]:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []

        reset_match = re.search(
            r"(?mis)^\s*Reset_Handler\s+PROC\b(?P<body>.*?)^\s*ENDP\b",
            content,
        )
        if reset_match is None:
            return []

        register_targets: Dict[str, str] = {}
        calls: List[str] = []
        for raw_line in reset_match.group("body").splitlines():
            line = raw_line.split(";", 1)[0].strip()
            if not line:
                continue

            load_match = re.match(
                r"LDR\s+(R\d+)\s*,\s*=([A-Za-z_]\w*)\b",
                line,
                re.IGNORECASE,
            )
            if load_match:
                register = load_match.group(1).upper()
                target = load_match.group(2)
                if target == "SystemInit":
                    break
                register_targets[register] = target
                continue

            call_match = re.match(
                r"BLX?\s+([A-Za-z_]\w*)\b",
                line,
                re.IGNORECASE,
            )
            if call_match:
                operand = call_match.group(1)
                target = register_targets.get(operand.upper(), operand)
                if target == "SystemInit":
                    break
                if target not in {"__main", "Reset_Handler"}:
                    calls.append(target)

        return self._dedupe_keep_order(calls)

    def _generate_gnu_startup_from_armasm(self, source_path: Path, dest: str) -> Optional[str]:
        vector_entries = self._parse_armasm_vector_entries(source_path)
        if len(vector_entries) < 2:
            return None

        pre_system_init_calls = self._parse_armasm_pre_system_init_calls(source_path)

        cpu_name = self._normalize_gcc_cpu_name(self.cpu or "cortex-m3")
        generated_path = Path(dest) / "cmake" / f"{source_path.stem}_gcc.S"
        handlers: List[str] = []
        for entry in vector_entries[1:]:
            if entry in {"0", "Reset_Handler"}:
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", entry):
                handlers.append(entry)
        handlers = self._dedupe_keep_order(handlers)

        vector_lines = ["    .word _estack"]
        for entry in vector_entries[1:]:
            vector_lines.append(f"    .word {entry}" if entry != "0" else "    .word 0")

        alias_lines: List[str] = []
        for handler in handlers:
            alias_lines.append(f"    .weak {handler}")
            alias_lines.append(f"    .thumb_set {handler}, Default_Handler")

        content = "\n".join(
            [
                "/* Auto-generated GNU startup file converted from a Keil ARMASM startup file. */",
                ".syntax unified",
                f".cpu {cpu_name}",
                ".thumb",
                "",
                ".global __Vectors",
                ".global g_pfnVectors",
                ".global __Vectors_End",
                ".global __Vectors_Size",
                ".global Reset_Handler",
                ".extern SystemInit",
                ".extern __libc_init_array",
                ".extern main",
                *[f".extern {symbol}" for symbol in pre_system_init_calls],
                "",
                '.section .isr_vector,"a",%progbits',
                ".type __Vectors, %object",
                "__Vectors:",
                "g_pfnVectors:",
                *vector_lines,
                "__Vectors_End:",
                ".equ __Vectors_Size, __Vectors_End - __Vectors",
                "",
                '.section .text.Reset_Handler,"ax",%progbits',
                ".type Reset_Handler, %function",
                "Reset_Handler:",
                "    ldr r0, =_estack",
                "    mov sp, r0",
                *[f"    bl {symbol}" for symbol in pre_system_init_calls],
                "    bl SystemInit",
                "    ldr r0, =_sidata",
                "    ldr r1, =_sdata",
                "    ldr r2, =_edata",
                ".Lcopy_data:",
                "    cmp r1, r2",
                "    bcs .Lzero_bss",
                "    ldr r3, [r0]",
                "    str r3, [r1]",
                "    adds r0, r0, #4",
                "    adds r1, r1, #4",
                "    b .Lcopy_data",
                ".Lzero_bss:",
                "    ldr r1, =_sbss",
                "    ldr r2, =_ebss",
                "    movs r3, #0",
                ".Lfill_bss:",
                "    cmp r1, r2",
                "    bcs .Linit_runtime",
                "    str r3, [r1]",
                "    adds r1, r1, #4",
                "    b .Lfill_bss",
                ".Linit_runtime:",
                "    bl __libc_init_array",
                "    bl main",
                ".Lhang:",
                "    b .Lhang",
                ".size Reset_Handler, .-Reset_Handler",
                "",
                '.section .text.Default_Handler,"ax",%progbits',
                ".type Default_Handler, %function",
                "Default_Handler:",
                "    b .",
                ".size Default_Handler, .-Default_Handler",
                "",
                *alias_lines,
                "",
            ]
        )
        self._write_text_file(generated_path, content)
        self._record_asset(
            "startup",
            generated_path,
            "generated",
            "review_required",
            f"Generated from ARMASM vector table: {source_path}",
        )
        return str(generated_path.resolve(strict=False))

    def _generate_patched_core_cm3_source(self, source_path: Path, dest: str) -> Optional[str]:
        try:
            content = source_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

        replacements = {
            '__ASM volatile ("strexb %0, %2, [%1]" : "=r" (result) : "r" (addr), "r" (value) );':
            '__ASM volatile ("strexb %0, %2, [%1]" : "=&r" (result) : "r" (addr), "r" (value) );',
            '__ASM volatile ("strexh %0, %2, [%1]" : "=r" (result) : "r" (addr), "r" (value) );':
            '__ASM volatile ("strexh %0, %2, [%1]" : "=&r" (result) : "r" (addr), "r" (value) );',
            '__ASM volatile ("strex %0, %2, [%1]" : "=r" (result) : "r" (addr), "r" (value) );':
            '__ASM volatile ("strex %0, %2, [%1]" : "=&r" (result) : "r" (addr), "r" (value) );',
        }

        patched = content
        replaced = False
        for original, updated in replacements.items():
            if original in patched:
                patched = patched.replace(original, updated)
                replaced = True

        if not replaced:
            return None

        generated_path = Path(dest) / "cmake" / f"{source_path.stem}_gcc.c"
        self._write_text_file(generated_path, patched)
        return str(generated_path.resolve(strict=False))

    @staticmethod
    def _safe_ascii_file_stem(stem: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_")
        return safe or "source"

    @staticmethod
    def _patch_armcc_embedded_assembly(content: str) -> tuple[str, List[str]]:
        patched_functions: List[str] = []
        delay_pattern = re.compile(
            r"__ASM(?:\s+volatile)?\s+void\s+(?P<name>[A-Za-z_]\w*)\s*"
            r"\(\s*uint32_t\s+(?P<arg>[A-Za-z_]\w*)\s*\)\s*"
            r"\{\s*SUBS\s+r0\s*,\s*#1\s*"
            r"BNE\s+(?P=name)\s*BX\s+LR\s*\}",
            re.IGNORECASE,
        )

        def replace_delay(match: re.Match[str]) -> str:
            name = match.group("name")
            arg = match.group("arg")
            patched_functions.append(name)
            return (
                f"__attribute__((naked)) void {name}(uint32_t {arg})\n"
                "{\n"
                '    __asm(".syntax unified\\n"\n'
                '          "1:\\n"\n'
                '          "subs r0, r0, #1\\n"\n'
                '          "bne 1b\\n"\n'
                '          "bx lr");\n'
                "}"
            )

        return delay_pattern.sub(replace_delay, content), patched_functions

    @staticmethod
    def _patch_freertos_armcc_cm0_port(content: str) -> tuple[str, bool]:
        replacements = {
            r"__asm\s+void\s+prvPortStartFirstTask\s*\(\s*void\s*\)"
            r"\s*\{.*?\bALIGN\s*\n\s*\}": r'''static __attribute__((naked)) void prvPortStartFirstTask(void)
{
    __asm volatile(
        ".syntax unified\n"
        "ldr r3, =pxCurrentTCB\n"
        "ldr r1, [r3]\n"
        "ldr r0, [r1]\n"
        "adds r0, r0, #32\n"
        "msr psp, r0\n"
        "movs r0, #2\n"
        "msr control, r0\n"
        "isb\n"
        "pop {r0-r5}\n"
        "mov lr, r5\n"
        "pop {r3}\n"
        "pop {r2}\n"
        "cpsie i\n"
        "bx r3\n"
    );
}''',
            r"__asm\s+uint32_t\s+ulSetInterruptMaskFromISR\s*"
            r"\(\s*void\s*\)\s*\{.*?\}": r'''__attribute__((naked)) uint32_t ulSetInterruptMaskFromISR(void)
{
    __asm volatile(
        ".syntax unified\n"
        "mrs r0, primask\n"
        "cpsid i\n"
        "bx lr\n"
    );
}''',
            r"__asm\s+void\s+vClearInterruptMaskFromISR\s*"
            r"\(\s*uint32_t\s+ulMask\s*\)\s*\{.*?\}": r'''__attribute__((naked)) void vClearInterruptMaskFromISR(uint32_t ulMask)
{
    __asm volatile(
        ".syntax unified\n"
        "msr primask, r0\n"
        "bx lr\n"
    );
}''',
            r"__asm\s+void\s+xPortPendSVHandler\s*\(\s*void\s*\)"
            r"\s*\{.*?\bALIGN\s*\n\s*\}": r'''__attribute__((naked)) void xPortPendSVHandler(void)
{
    __asm volatile(
        ".syntax unified\n"
        "mrs r0, psp\n"
        "ldr r3, =pxCurrentTCB\n"
        "ldr r2, [r3]\n"
        "subs r0, r0, #32\n"
        "str r0, [r2]\n"
        "stmia r0!, {r4-r7}\n"
        "mov r4, r8\n"
        "mov r5, r9\n"
        "mov r6, r10\n"
        "mov r7, r11\n"
        "stmia r0!, {r4-r7}\n"
        "push {r3, lr}\n"
        "cpsid i\n"
        "bl vTaskSwitchContext\n"
        "cpsie i\n"
        "pop {r2, r3}\n"
        "ldr r1, [r2]\n"
        "ldr r0, [r1]\n"
        "adds r0, r0, #16\n"
        "ldmia r0!, {r4-r7}\n"
        "mov r8, r4\n"
        "mov r9, r5\n"
        "mov r10, r6\n"
        "mov r11, r7\n"
        "msr psp, r0\n"
        "subs r0, r0, #32\n"
        "ldmia r0!, {r4-r7}\n"
        "bx r3\n"
    );
}''',
        }

        patched = content
        replaced = 0
        for pattern, replacement in replacements.items():
            patched, count = re.subn(
                pattern,
                lambda _match, value=replacement: value,
                patched,
                flags=re.DOTALL | re.IGNORECASE,
            )
            replaced += count
        if replaced == len(replacements):
            patched = patched.replace(
                '#include "task.h"',
                '#include "task.h"\n'
                '#define __disable_irq() __asm volatile ("cpsid i" ::: "memory")\n'
                '#define __enable_irq() __asm volatile ("cpsie i" ::: "memory")\n'
                '#define __dsb(value) __asm volatile ("dsb 0xF" ::: "memory")\n'
                '#define __isb(value) __asm volatile ("isb 0xF" ::: "memory")',
                1,
            )
        return patched, replaced == len(replacements)

    def _stage_gcc_source_file(self, source_path: Path, dest: str) -> str:
        try:
            resolved_source = source_path.resolve(strict=False)
            output_path = Path(dest).resolve(strict=False)
            resolved_source.relative_to(output_path)
            return str(resolved_source)
        except ValueError:
            pass

        if not source_path.is_file():
            return str(source_path)

        resolved_source = source_path.resolve(strict=False)
        copy_only_staging = self._path_contains_non_ascii(resolved_source)
        digest = hashlib.sha1(
            str(resolved_source).encode("utf-8")
        ).hexdigest()[:12]
        safe_stem = self._safe_ascii_file_stem(source_path.stem)
        staged_dir = Path(dest) / "cmake" / "sources"
        staged_dir.mkdir(parents=True, exist_ok=True)
        staged_path = staged_dir / f"{safe_stem}_{digest}{source_path.suffix}"
        if staged_path.exists():
            staged_path.chmod(staged_path.stat().st_mode | 0o200)
        shutil.copy2(source_path, staged_path)
        staged_path.chmod(staged_path.stat().st_mode | 0o200)
        content_changed = False
        if source_path.suffix.lower() == ".c":
            try:
                original_content = staged_path.read_text(
                    encoding="utf-8", errors="ignore"
                )
                content = original_content
                normalized_source = source_path.as_posix().lower()
                if (
                    source_path.name.lower() == "port.c"
                    and "/portable/rvds/arm_cm0/" in normalized_source
                ):
                    patched_port, converted = self._patch_freertos_armcc_cm0_port(
                        content
                    )
                    if converted:
                        content = patched_port
                        self.warnings.append(
                            "FreeRTOS RVDS ARM_CM0 端口已转换为 GCC 汇编"
                        )
                patched, patched_functions = self._patch_armcc_embedded_assembly(
                    content
                )
                patched, uart_alias_count = self._patch_missing_uart_handle_alias(
                    patched
                )
                if patched != original_content:
                    self._write_text_file(staged_path, patched)
                    content_changed = True
                if patched_functions:
                    self.warnings.append(
                        "GCC ARMCC 内嵌汇编转换: "
                        f"{source_path.name}: {', '.join(patched_functions)}"
                    )
                if uart_alias_count:
                    self.warnings.append(
                        f"GCC UART 重定向句柄修正: {source_path.name}"
                    )
            except OSError:
                pass
        if source_path.suffix.lower() in {".cpp", ".cc", ".cxx"}:
            try:
                content = staged_path.read_text(encoding="utf-8", errors="ignore")
                patched = content.replace(
                    'extern "C" uint32_t system_core_clock;',
                    'extern "C" unsigned int system_core_clock;',
                )
                if self._has_lvgl_lazy_new_allocator():
                    init_pattern = re.compile(
                        r"(?P<indent>^[ \t]*)HAL::HAL_Init\(\);\s*\n"
                        r"(?P=indent)lv_init\(\);",
                        re.MULTILINE,
                    )
                    patched, init_order_count = init_pattern.subn(
                        lambda match: (
                            f"{match.group('indent')}lv_init();\n"
                            f"{match.group('indent')}HAL::HAL_Init();"
                        ),
                        patched,
                        count=1,
                    )
                    if init_order_count:
                        self.warnings.append(
                            "GCC LVGL 初始化顺序修正: lv_init() 先于 HAL::HAL_Init()"
                        )
                hardfault_pattern = re.compile(
                    r"\s*__asm\s+void\s+HardFault_Handler\s*\(\s*\)\s*\{.*?fault_loop\s*\n\s*b\s+fault_loop\s*\n\s*\}",
                    re.DOTALL,
                )
                hardfault_replacement = r'''
    __attribute__((naked)) void HardFault_Handler(void)
    {
        __asm volatile(
            "mov r4, lr\n"
            "tst r4, #4\n"
            "beq 1f\n"
            "mrs r5, psp\n"
            "b 2f\n"
            "1:\n"
            "mrs r5, msp\n"
            "2:\n"
            "mov r0, r4\n"
            "mov r1, r5\n"
            "bl vApplicationHardFaultDump\n"
            "mov r0, r4\n"
            "mov r1, r5\n"
            "bl vApplicationHardFaultTrace\n"
            "mov r0, r4\n"
            "mov r1, r5\n"
            "bl vApplicationHardFaultHook\n"
            "3: b 3b\n"
        );
    }'''
                patched = hardfault_pattern.sub(
                    lambda _match: hardfault_replacement, patched
                )
                if patched != content:
                    self._write_text_file(staged_path, patched)
                    content_changed = True
                    self.warnings.append(
                        f"GCC 类型兼容修正: {source_path.name}: system_core_clock"
                    )
            except OSError:
                pass

        if not content_changed and not copy_only_staging:
            staged_path.unlink(missing_ok=True)
            return str(resolved_source)

        # A staged source loses GCC's implicit search of its original directory.
        # Add that directory only for staged files instead of every source file.
        source_dir = str(source_path.parent.resolve(strict=False))
        if source_dir:
            self._add_include_path(source_dir)
        if source_path.parent.name.lower() == "src":
            sibling_include = source_path.parent.parent / "inc"
            if sibling_include.is_dir():
                self._add_include_path(str(sibling_include.resolve(strict=False)))

        if not content_changed:
            self._copy_staged_source_mappings.append(
                (resolved_source, staged_path.resolve(strict=False))
            )
        return str(staged_path.resolve(strict=False))

    def _has_lvgl_lazy_new_allocator(self) -> bool:
        if self._lvgl_lazy_new_allocator_cache is not None:
            return self._lvgl_lazy_new_allocator_cache

        for source in self._gcc_source_files():
            source_path = Path(source)
            if source_path.suffix.lower() not in {".cpp", ".cc", ".cxx"}:
                continue
            try:
                content = source_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if (
                re.search(r"\boperator\s+new(?:\s*\[\s*\])?\s*\(", content)
                and re.search(r"\blv_is_initialized\s*\(", content)
                and re.search(r"\blv_init\s*\(", content)
                and re.search(r"\blv_mem_alloc\s*\(", content)
            ):
                self._lvgl_lazy_new_allocator_cache = True
                return True
        self._lvgl_lazy_new_allocator_cache = False
        return False

    def _resolve_missing_source(self, source_path: Path) -> Path:
        if source_path.is_file():
            return source_path

        cache_key = source_path.name.lower()
        if cache_key in self._missing_source_cache:
            return self._missing_source_cache[cache_key] or source_path

        candidates: List[Path] = []
        search_root = self.project_dir.parent
        if search_root.is_dir():
            candidates = [
                candidate
                for candidate in search_root.rglob(source_path.name)
                if candidate.is_file()
                and not any(
                    part.lower().startswith(("build", "_keil_to_cmake_test"))
                    for part in candidate.parts
                )
            ]

        if candidates:
            device_tokens = [token.lower() for token in self._device_tokens()]

            def score(candidate: Path) -> tuple[int, int]:
                text = candidate.as_posix().lower()
                device_score = sum(token in text for token in device_tokens)
                rte_score = 1 if "/rte/" in text else 0
                return device_score, rte_score

            resolved = max(candidates, key=score).resolve(strict=False)
            self._missing_source_cache[cache_key] = resolved
            self.warnings.append(
                f"源文件原位置不存在，使用工程内同名文件: {source_path} -> {resolved}"
            )
            return resolved

        self._missing_source_cache[cache_key] = None
        return source_path

    def _prepared_gcc_source_files(self, dest: str) -> List[str]:
        prepared: List[str] = []
        sources = self._gcc_source_files()
        total = len(sources)
        for index, source in enumerate(sources, start=1):
            self._emit_generation_progress(
                5 + round(55 * index / max(total, 1)),
                f"处理源文件 {index}/{total}",
            )
            source_path = self._resolve_missing_source(Path(source))
            if source_path.name.lower().endswith("_keil.c"):
                gcc_name = source_path.name[:-7] + "_gcc.c"
                gcc_alternative = source_path.with_name(gcc_name)
                if gcc_alternative.is_file():
                    self.warnings.append(
                        f"GCC 编译器专用源文件替换: {source_path.name} -> {gcc_name}"
                    )
                    source_path = gcc_alternative.resolve(strict=False)
            if source_path.name.lower() == "core_cm3.c":
                generated = self._generate_patched_core_cm3_source(source_path, dest)
                if generated is not None:
                    prepared.append(generated)
                    continue
            if (
                source_path.name.lower().startswith("startup_")
                and source_path.suffix.lower() in {".s", ".asm"}
                and self._is_armasm_startup_source(source_path)
            ):
                generated = self._generate_gnu_startup_from_armasm(source_path, dest)
                if generated is not None:
                    prepared.append(generated)
                    continue
            staged = self._stage_gcc_source_file(source_path, dest)
            if "startup_" in source_path.name.lower():
                staged_path = Path(staged)
                self._record_asset(
                    "startup",
                    staged_path,
                    self._asset_source(source_path),
                    "exact",
                    f"Reused GCC-compatible startup source: {source_path}",
                )
            prepared.append(staged)
        return self._dedupe_keep_order(prepared)

    def _gcc_device_asset_keys(self) -> List[str]:
        device = re.sub(r"[^a-z0-9_]+", "", self._clean_device_name().lower())
        primary = device.split("_", 1)[0]
        keys = [primary] if primary else []
        family_match = re.match(r"([a-z]+)(\d{2})\d+", primary)
        if family_match:
            keys.append(f"{family_match.group(1)}{family_match.group(2)}")
        return self._dedupe_keep_order(keys)

    def _add_gcc_device_assets(self) -> None:
        source_names = {Path(source).name.lower() for source in self.source_files}
        has_startup = any("startup_" in name for name in source_names)
        has_system = any(name.startswith("system_") for name in source_names)
        if has_startup and has_system:
            return

        roots: List[Path] = []
        for include_path in self._ordered_include_paths():
            include_dir = Path(include_path).resolve(strict=False)
            roots.extend(
                [
                    include_dir,
                    include_dir / "mdk",
                    include_dir / "gcc",
                    include_dir.parent,
                    include_dir.parent / "gcc",
                    include_dir.parent / "Source",
                    include_dir.parent / "Source" / "gcc",
                    include_dir.parent.parent / "Source",
                    include_dir.parent.parent / "Source" / "gcc",
                ]
            )

        roots = [root for root in self._dedupe_keep_order(roots) if root.is_dir()]
        keys = self._gcc_device_asset_keys()

        if not has_startup:
            for key in keys:
                candidates = [
                    root / file_name
                    for root in roots
                    for file_name in (
                        f"gcc_startup_{key}.S",
                        f"startup_{key}.S",
                        f"startup_{key}.s",
                    )
                ]
                selected = next((path for path in candidates if path.is_file()), None)
                if selected is not None:
                    self.source_files.append(str(selected.resolve(strict=False)))
                    self._add_include_path(str(selected.parent.resolve(strict=False)))
                    self.warnings.append(f"使用 GCC 设备启动文件: {selected}")
                    has_startup = True
                    break

        if not has_system:
            for key in keys:
                candidates = [
                    root / f"system_{key}.c"
                    for root in roots
                ]
                selected = next((path for path in candidates if path.is_file()), None)
                if selected is not None:
                    self.source_files.append(str(selected.resolve(strict=False)))
                    self._add_include_path(str(selected.parent.resolve(strict=False)))
                    self.warnings.append(f"使用 GCC 设备系统文件: {selected}")
                    break

    @staticmethod
    def _find_arm_gcc_binary(name: str) -> Optional[str]:
        executable = f"{name}.exe" if os.name == "nt" else name
        found = shutil.which(executable) or shutil.which(name)
        if found:
            return found

        toolchain = os.environ.get("ARM_GCC_TOOLCHAIN", "").strip()
        if toolchain:
            candidate = Path(toolchain) / "bin" / executable
            if candidate.is_file():
                return str(candidate)
        return None

    def _convert_armcc_library_for_gcc(
        self, library_path: Path, dest: str
    ) -> Optional[str]:
        ar = self._find_arm_gcc_binary("arm-none-eabi-ar")
        objcopy = self._find_arm_gcc_binary("arm-none-eabi-objcopy")
        if not ar or not objcopy:
            self.warnings.append(
                f"缺少 arm-none-eabi-ar 或 objcopy，无法转换 Keil 库: {library_path}"
            )
            return None

        digest = hashlib.sha1(
            str(library_path.resolve(strict=False)).encode("utf-8")
        ).hexdigest()[:12]
        work_dir = Path(dest) / "cmake" / "libraries" / (
            f"{self._safe_ascii_file_stem(library_path.stem)}_{digest}"
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        output_library = work_dir / f"{library_path.stem}_gcc.a"

        try:
            listing = subprocess.run(
                [ar, "t", str(library_path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            members = [
                line.strip() for line in listing.stdout.splitlines() if line.strip()
            ]
            if listing.returncode != 0 or not members:
                return None

            extraction = subprocess.run(
                [ar, "x", str(library_path.resolve(strict=False))],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if extraction.returncode != 0:
                return None

            converted_members: List[Path] = []
            for index, member in enumerate(members):
                extracted = work_dir / Path(member).name
                if not extracted.is_file():
                    return None
                converted = work_dir / (
                    f"{self._safe_ascii_file_stem(extracted.stem)}_{index}.o"
                )
                result = subprocess.run(
                    [objcopy, "-O", "elf32-littlearm", str(extracted), str(converted)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if result.returncode != 0:
                    return None
                converted_members.append(converted)

            archive = subprocess.run(
                [ar, "rcs", str(output_library), *map(str, converted_members)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if archive.returncode != 0 or not output_library.is_file():
                return None
        except (OSError, subprocess.SubprocessError):
            return None

        self._record_asset(
            "library",
            output_library,
            "generated",
            "review_required",
            f"Repacked ARMCC archive members for GNU ld: {library_path}",
        )
        self.warnings.append(
            f"Keil .lib 已重新封装为 GCC .a: {library_path.name}"
        )
        return str(output_library.resolve(strict=False))

    def _prepared_gcc_libraries(self, dest: str) -> List[str]:
        prepared: List[str] = []
        for library in self.libraries:
            library_path = self._resolve_missing_source(Path(library))
            if library_path.suffix.lower() == ".lib" and library_path.is_file():
                converted = self._convert_armcc_library_for_gcc(library_path, dest)
                if converted:
                    prepared.append(converted)
                    continue
                self.warnings.append(
                    f"Keil .lib 与 GNU ld 兼容性未知: {library_path}"
                )
            prepared.append(str(library_path.resolve(strict=False)))
        return self._dedupe_keep_order(prepared)

    def _generate_gcc_symbol_fragments(self, dest: str) -> List[str]:
        fragments: List[str] = []
        symbol_pattern = re.compile(
            r"^(?P<address>0x[0-9A-Fa-f]+)\s+\S+\s+"
            r"(?P<name>[A-Za-z_.$][A-Za-z0-9_.$]*)\s*$"
        )
        for linker_input in self.linker_inputs:
            input_path = Path(linker_input)
            try:
                content = input_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not content.startswith("#<SYMDEFS>#"):
                continue

            definitions: List[str] = []
            for line in content.splitlines():
                match = symbol_pattern.match(line)
                if match:
                    definitions.append(
                        f"PROVIDE({match.group('name')} = "
                        f"{match.group('address')});"
                    )
            if not definitions:
                continue

            digest = hashlib.sha1(
                str(input_path.resolve(strict=False)).encode("utf-8")
            ).hexdigest()[:12]
            fragment_path = (
                Path(dest)
                / "cmake"
                / f"{self._safe_ascii_file_stem(input_path.stem)}_{digest}.ld"
            )
            self._write_text_file(
                fragment_path,
                "/* Converted from ARM Linker SYMDEFS. */\n"
                + "\n".join(definitions)
                + "\n",
            )
            fragments.append(str(fragment_path.resolve(strict=False)))
            self._record_asset(
                "linker_symbols",
                fragment_path,
                "generated",
                "exact",
                f"Converted ARM Linker SYMDEFS: {input_path}",
            )
        return fragments

    def _generate_gcc_runtime_compat_source(self, dest: str) -> Optional[str]:
        needs_irq_functions = False
        for source in self._gcc_source_files():
            source_path = Path(source)
            if source_path.suffix.lower() not in {".c", ".cpp", ".cc", ".cxx"}:
                continue
            try:
                content = source_path.read_text(
                    encoding="utf-8", errors="ignore"
                )
            except OSError:
                continue
            if "__disable_irq()" in content or "__enable_irq()" in content:
                needs_irq_functions = True
        if not needs_irq_functions:
            return None

        runtime_path = Path(dest) / "cmake" / "gcc_runtime_compat.c"
        fragments: List[str] = []
        if needs_irq_functions:
            fragments.append(
            "__asm__(\n"
            '    ".syntax unified\\n"\n'
            '    ".thumb\\n"\n'
            '    ".weak __disable_irq\\n"\n'
            '    ".type __disable_irq, %function\\n"\n'
            '    ".thumb_func\\n"\n'
            '    "__disable_irq:\\n"\n'
            '    "cpsid i\\n"\n'
            '    "bx lr\\n"\n'
            '    ".size __disable_irq, .-__disable_irq\\n"\n'
            '    ".weak __enable_irq\\n"\n'
            '    ".type __enable_irq, %function\\n"\n'
            '    ".thumb_func\\n"\n'
            '    "__enable_irq:\\n"\n'
            '    "cpsie i\\n"\n'
            '    "bx lr\\n"\n'
            '    ".size __enable_irq, .-__enable_irq\\n"\n'
                ");\n"
            )
        self._write_text_file(runtime_path, "\n".join(fragments))
        return str(runtime_path.resolve(strict=False))

    @staticmethod
    def _patch_legacy_stdint_header(content: str) -> tuple[str, int]:
        typedef_pattern = re.compile(
            r"^(?P<indent>[ \t]*)typedef[ \t]+"
            r"(?P<base>(?:signed|unsigned)\s+(?:char|short|int|long))\s+"
            r"(?P<aliases>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*;\s*$",
            re.MULTILINE,
        )
        standard_types = {
            "int8_t",
            "uint8_t",
            "int16_t",
            "uint16_t",
            "int32_t",
            "uint32_t",
            "int64_t",
            "uint64_t",
        }
        replacement_count = 0

        def replace_typedef(match: re.Match[str]) -> str:
            nonlocal replacement_count
            aliases = [
                alias.strip() for alias in match.group("aliases").split(",")
            ]
            canonical = next(
                (alias for alias in aliases if alias in standard_types), None
            )
            if canonical is None:
                return match.group(0)

            replacement_count += 1
            remaining = [alias for alias in aliases if alias != canonical]
            if not remaining:
                return ""
            return (
                f"{match.group('indent')}typedef {canonical} "
                f"{', '.join(remaining)};"
            )

        patched = typedef_pattern.sub(replace_typedef, content)
        if replacement_count:
            patched = "#include <stdint.h>\n" + patched
        return patched, replacement_count

    @staticmethod
    def _patch_legacy_external_inline_header(content: str) -> tuple[str, int]:
        pattern = re.compile(r"^(?P<indent>[ \t]*)__INLINE(?=[ \t])", re.MULTILINE)
        return pattern.subn(r"\g<indent>static __INLINE", content)

    @staticmethod
    def _patch_legacy_uint32_prototypes(
        content: str, function_names: Set[str]
    ) -> tuple[str, int]:
        identifiers = set(re.findall(r"\b[A-Za-z_]\w*\b", content))
        present_names = function_names.intersection(identifiers)
        if not present_names:
            return content, 0

        names = "|".join(re.escape(name) for name in sorted(present_names))
        pattern = re.compile(
            rf"(?P<prefix>\b(?:{names})\s*\(\s*(?:__IO\s+)?)"
            rf"unsigned\s+int\b"
        )
        content, replacement_count = pattern.subn(r"\g<prefix>uint32_t", content)
        if replacement_count and not re.search(
            r"^\s*#\s*include\s*[<\"]stdint\.h[>\"]", content, re.MULTILINE
        ):
            content = "#include <stdint.h>\n" + content
        return content, replacement_count

    @staticmethod
    def _remove_conflicting_static_prototypes(
        content: str, function_names: Set[str]
    ) -> tuple[str, int]:
        if not function_names:
            return content, 0

        identifiers = set(re.findall(r"\b[A-Za-z_]\w*\b", content))
        present_names = function_names.intersection(identifiers)
        if not present_names:
            return content, 0

        type_ranges = KeilProjectToCMake._cxx_type_body_ranges(content)
        names = "|".join(re.escape(name) for name in sorted(present_names))
        pattern = re.compile(
            rf"(?m)^\s*(?:extern\s+)?[A-Za-z_]\w*"
            rf"(?:\s+[A-Za-z_]\w*)*\s+(?P<name>{names})"
            rf"\s*\([^;{{}}]*\)\s*;\s*$\n?"
        )

        replacement_count = 0

        def remove_global_prototype(match: re.Match[str]) -> str:
            nonlocal replacement_count
            if any(start <= match.start() < end for start, end in type_ranges):
                return match.group(0)
            replacement_count += 1
            return ""

        return pattern.sub(remove_global_prototype, content), replacement_count

    @staticmethod
    def _cxx_type_body_ranges(content: str) -> List[tuple[int, int]]:
        masked = list(content)
        index = 0
        state = "code"
        quote = ""
        while index < len(masked):
            char = content[index]
            next_char = content[index + 1] if index + 1 < len(content) else ""
            if state == "code":
                if char == "/" and next_char == "/":
                    masked[index] = masked[index + 1] = " "
                    index += 2
                    state = "line_comment"
                    continue
                if char == "/" and next_char == "*":
                    masked[index] = masked[index + 1] = " "
                    index += 2
                    state = "block_comment"
                    continue
                if char in {'"', "'"}:
                    quote = char
                    masked[index] = " "
                    index += 1
                    state = "string"
                    continue
            elif state == "line_comment":
                if char in "\r\n":
                    state = "code"
                else:
                    masked[index] = " "
                index += 1
                continue
            elif state == "block_comment":
                if char == "*" and next_char == "/":
                    masked[index] = masked[index + 1] = " "
                    index += 2
                    state = "code"
                    continue
                if char not in "\r\n":
                    masked[index] = " "
                index += 1
                continue
            else:
                if char == "\\" and next_char:
                    masked[index] = " "
                    if next_char not in "\r\n":
                        masked[index + 1] = " "
                    index += 2
                    continue
                if char == quote:
                    state = "code"
                if char not in "\r\n":
                    masked[index] = " "
                index += 1
                continue
            index += 1

        sanitized = "".join(masked)
        type_pattern = re.compile(r"\b(?:class|struct|union)\b[^;{}]*\{")
        ranges: List[tuple[int, int]] = []
        for match in type_pattern.finditer(sanitized):
            opening = sanitized.find("{", match.start(), match.end())
            depth = 1
            cursor = opening + 1
            while cursor < len(sanitized) and depth:
                if sanitized[cursor] == "{":
                    depth += 1
                elif sanitized[cursor] == "}":
                    depth -= 1
                cursor += 1
            if depth == 0:
                ranges.append((opening + 1, cursor - 1))
        return ranges

    @staticmethod
    def _find_static_function_names(content: str) -> Set[str]:
        pattern = re.compile(
            r"(?m)^\s*static\b[^\n;{}=]*?\b([A-Za-z_]\w*)\s*"
            r"\([^;\n{}]*\)[ \t]*(?:\r?\n[ \t]*)?\{"
        )
        return {match.group(1) for match in pattern.finditer(content)}

    @staticmethod
    def _patch_missing_uart_handle_alias(content: str) -> tuple[str, int]:
        declared = set(
            re.findall(
                r"\bUART_HandleTypeDef\s+([A-Za-z_]\w*)\s*;",
                content,
            )
        )
        if len(declared) != 1:
            return content, 0
        selected = next(iter(declared))
        referenced = set(re.findall(r"&([A-Za-z_]\w*Handle)\b", content))
        missing = referenced - declared
        replacement_count = 0
        for name in sorted(missing):
            content, count = re.subn(rf"&{re.escape(name)}\b", f"&{selected}", content)
            replacement_count += count
        return content, replacement_count

    @staticmethod
    def _path_contains_non_ascii(path: Path) -> bool:
        return any(ord(character) > 127 for character in str(path))

    def _stage_gcc_include_directory(self, include_path: Path, dest: str) -> str:
        resolved = include_path.resolve(strict=False)
        if not resolved.is_dir() or not self._path_contains_non_ascii(resolved):
            return str(resolved)

        digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:12]
        safe_name = self._safe_ascii_file_stem(resolved.name)
        staged_root = Path(dest) / "cmake" / "includes" / f"{safe_name}_{digest}"
        header_suffixes = {
            ".h",
            ".hh",
            ".hpp",
            ".hxx",
            ".inc",
            ".inl",
            ".def",
            ".tcc",
        }
        copied = 0
        for source in resolved.rglob("*"):
            if not source.is_file():
                continue
            if source.suffix.lower() not in header_suffixes and source.suffix:
                continue
            target = staged_root / source.relative_to(resolved)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1

        if copied:
            self.warnings.append(f"GCC 非 ASCII 头文件目录镜像: {resolved}")
            return str(staged_root.resolve(strict=False))
        return str(resolved)

    def _prepared_gcc_include_paths(self, dest: str) -> List[str]:
        return self._dedupe_keep_order(
            [
                self._stage_gcc_include_directory(Path(include_path), dest)
                for include_path in self._ordered_include_paths()
            ]
        )

    def _generate_legacy_stdint_compat_headers(self, dest: str) -> List[str]:
        candidates: List[Path] = [
            Path(header) for header in self.header_files if Path(header).is_file()
        ]
        for include_path in self._ordered_include_paths():
            include_dir = Path(include_path)
            if include_dir.is_dir():
                candidates.extend(include_dir.glob("*.h"))

        compat_dir = Path(dest) / "cmake" / "compat-includes"
        generated_names: Set[str] = set()
        forced_names: Set[str] = set()
        inline_forced_names: Set[str] = set()
        uint32_function_names: Set[str] = set()
        static_function_names: Set[str] = set()
        function_pattern = re.compile(
            r"(?m)^\s*(?:static\s+)?(?:[A-Za-z_]\w*\s+)*?"
            r"(?P<name>[A-Za-z_]\w*)\s*\(\s*(?:__IO\s+)?uint32_t\b"
        )
        for source in self.source_files:
            try:
                source_content = Path(source).read_text(
                    encoding="utf-8", errors="ignore"
                )
            except OSError:
                continue
            uint32_function_names.update(
                match.group("name") for match in function_pattern.finditer(source_content)
            )
            static_function_names.update(
                self._find_static_function_names(source_content)
            )
        for header in self._dedupe_keep_order(
            [str(path.resolve(strict=False)) for path in candidates]
        ):
            header_path = Path(header)
            if header_path.name.lower() in generated_names:
                continue
            try:
                content = header_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            patched, replacement_count = self._patch_legacy_stdint_header(content)
            patched, inline_count = self._patch_legacy_external_inline_header(patched)
            patched, prototype_count = self._patch_legacy_uint32_prototypes(
                patched, uint32_function_names
            )
            patched, static_prototype_count = self._remove_conflicting_static_prototypes(
                patched, static_function_names
            )
            if (
                replacement_count < 2
                and inline_count == 0
                and prototype_count == 0
                and static_prototype_count == 0
            ):
                continue

            compat_header = compat_dir / header_path.name
            self._write_text_file(compat_header, patched)
            generated_names.add(header_path.name.lower())
            if replacement_count >= 2 or inline_count:
                forced_names.add(header_path.name.lower())
            if inline_count:
                inline_forced_names.add(header_path.name.lower())
            self.warnings.append(
                f"GCC 兼容头: {header_path}"
            )

        if generated_names:
            compat_dir_text = str(compat_dir.resolve(strict=False))
            self._add_include_path(compat_dir_text)
            self.include_path_order.remove(compat_dir_text)
            self.include_path_order.insert(0, compat_dir_text)
        reference_counts = {name: 0 for name in forced_names}
        include_pattern = re.compile(
            r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE
        )
        for header in candidates:
            try:
                header_content = header.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for included in include_pattern.findall(header_content):
                included_name = Path(included.replace("\\", "/")).name.lower()
                if included_name in reference_counts:
                    reference_counts[included_name] += 1

        return sorted(
            forced_names,
            key=lambda name: (
                name not in inline_forced_names,
                -reference_counts[name],
                name,
            ),
        )

    def _generate_gcc_cxx_compat_header(
        self, dest: str, forced_headers: Optional[List[str]] = None
    ) -> str:
        needs_cmath = False
        for include_path in self._ordered_include_paths():
            arduino_header = Path(include_path) / "Arduino.h"
            if not arduino_header.is_file():
                continue
            try:
                content = arduino_header.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if re.search(r"^\s*#\s*define\s+round\s*\(", content, re.MULTILINE):
                needs_cmath = True
                compat_include_dir = Path(dest) / "cmake" / "compat-includes"
                compat_arduino = compat_include_dir / "Arduino.h"
                min_max_pattern = re.compile(
                    r"^#define\s+min\(a,b\).*\n#define\s+max\(a,b\).*?$",
                    re.MULTILINE,
                )
                replacement = (
                    "#ifdef __cplusplus\n"
                    "using std::min;\n"
                    "using std::max;\n"
                    "#else\n"
                    "#define min(a,b) ((a)<(b)?(a):(b))\n"
                    "#define max(a,b) ((a)>(b)?(a):(b))\n"
                    "#endif"
                )
                patched_arduino = min_max_pattern.sub(replacement, content)
                self._write_text_file(compat_arduino, patched_arduino)
                compat_dir_text = str(compat_include_dir.resolve(strict=False))
                self._add_include_path(compat_dir_text)
                self.include_path_order.remove(compat_dir_text)
                self.include_path_order.insert(0, compat_dir_text)
                self.warnings.append(
                    "检测到 Arduino min/max/round 宏，启用 GCC 兼容头"
                )
                break

        lines = ["#pragma once"]
        for header_name in forced_headers or []:
            lines.append(f'#include "compat-includes/{header_name}"')
        if needs_cmath:
            lines.extend(
                [
                    "#ifdef __cplusplus",
                    "#include <algorithm>",
                    "#include <cmath>",
                    "#endif",
                ]
            )
        lines.extend(
            [
                "#ifndef __forceinline",
                "#define __forceinline inline __attribute__((always_inline))",
                "#endif",
                "#ifndef __align",
                "#define __align(n) __attribute__((aligned(n)))",
                "#endif",
                "#ifndef __weak",
                "#define __weak __attribute__((weak))",
                "#endif",
                "#ifndef __wfi",
                '#define __wfi() __asm volatile ("wfi")',
                "#endif",
                "",
            ]
        )
        compat_path = Path(dest) / "cmake" / "gcc_compat.h"
        self._write_text_file(compat_path, "\n".join(lines))
        return str(compat_path.resolve(strict=False))

    def _score_linker_script_candidate(self, path: Path) -> int:
        score = 0
        path_text = path.as_posix().lower()
        file_name = path.name.lower()
        normalized_file_name = re.sub(r"[^a-z0-9]", "", file_name)

        if "/startup/gcc/linker/" in path_text:
            score += 10
        if "flash" in file_name:
            score += 3

        for token in self._device_tokens():
            token_lower = token.lower()
            normalized_token = re.sub(r"[^a-z0-9]", "", token_lower)
            if normalized_token and normalized_token in normalized_file_name:
                score += 12
            elif token_lower in file_name:
                score += 5
            elif token_lower in path_text:
                score += 2

        memory_code = self._device_memory_code().lower()
        if memory_code and f"x{memory_code}" in file_name:
            score += 8

        return score

    @staticmethod
    def _is_gcc_linker_script_candidate(path: Path) -> bool:
        file_name = path.name.lower()
        if file_name == "generated_linker.ld" or file_name.startswith("symbol_"):
            return False
        if any(
            part.lower().startswith((
                "build",
                "_keil_to_cmake_test",
                "cmake-generated",
            ))
            for part in path.parts
        ):
            return False
        if any(part.lower() in {"iar", "ewarm", "ride"} for part in path.parts):
            return False
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return re.search(r"(?m)^\s*(?:SECTIONS|INCLUDE)\b", content) is not None

    def _guess_gcc_linker_script(self, dest: Optional[str] = None) -> Optional[str]:
        if self.linker_script and self.linker_script.lower().endswith(".ld"):
            return str(Path(self.linker_script).resolve(strict=False))

        adjacent_candidates: List[Path] = []
        for source in self._gcc_source_files():
            source_path = Path(source)
            source_name = source_path.name.lower()
            if "startup" not in source_name or source_path.suffix.lower() not in {
                ".s",
                ".asm",
            }:
                continue
            for linker_dir in (source_path.parent, source_path.parent / "linker"):
                if linker_dir.is_dir():
                    adjacent_candidates.extend(sorted(linker_dir.glob("*.ld")))

        candidates = adjacent_candidates
        if not candidates:
            for search_root in [self.project_dir, self.project_dir.parent]:
                if search_root.is_dir():
                    candidates.extend(sorted(search_root.rglob("*.ld")))

        candidates = [candidate.resolve(strict=False) for candidate in candidates]
        candidates = [
            candidate
            for candidate in candidates
            if candidate.is_file() and self._is_gcc_linker_script_candidate(candidate)
        ]
        if dest:
            output_path = Path(dest).resolve(strict=False)
            filtered: List[Path] = []
            for candidate in candidates:
                try:
                    candidate.relative_to(output_path)
                    continue
                except ValueError:
                    pass
                filtered.append(candidate)
            candidates = filtered
        if not candidates:
            return None

        candidates = list(dict.fromkeys(candidates))
        best = max(candidates, key=self._score_linker_script_candidate)
        return str(best)

    def _prepare_gcc_linker_script(self, linker_script: str, dest: str) -> str:
        source_path = Path(linker_script).resolve(strict=False)
        output_path = Path(dest).resolve(strict=False)
        try:
            source_path.relative_to(output_path)
            return str(source_path)
        except ValueError:
            pass

        linker_dir = output_path / "cmake" / "linker"
        linker_dir.mkdir(parents=True, exist_ok=True)
        copied: Set[Path] = set()

        def copy_with_includes(current: Path) -> Path:
            current = current.resolve(strict=False)
            target = linker_dir / current.name
            if current in copied:
                return target
            copied.add(current)
            shutil.copy2(current, target)

            try:
                content = current.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return target
            for match in re.finditer(
                r'(?mi)^\s*INCLUDE\s+["\']?([^"\'\s]+)["\']?', content
            ):
                include_path = (current.parent / match.group(1)).resolve(strict=False)
                if include_path.is_file():
                    copy_with_includes(include_path)
                else:
                    self.warnings.append(
                        f"GCC 链接脚本依赖未找到: {match.group(1)} ({current})"
                    )
            return target

        prepared_path = copy_with_includes(source_path)
        self._augment_nordic_linker_script(prepared_path)
        return str(prepared_path)

    def _augment_nordic_linker_script(self, linker_path: Path) -> None:
        device = self._clean_device_name().lower()
        vendor = (self.vendor or "").lower()
        if not (device.startswith("nrf") or "nordic" in vendor):
            return
        try:
            content = linker_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        if "__start_log_const_data" in content:
            return

        section_block = r"""
/* Nordic SDK section variables required by nrf_log and nrf_balloc. */
SECTIONS
{
  . = ALIGN(4);
  .mem_section_dummy_ram :
  {
  }
  .log_dynamic_data :
  {
    PROVIDE(__start_log_dynamic_data = .);
    KEEP(*(SORT(.log_dynamic_data*)))
    PROVIDE(__stop_log_dynamic_data = .);
  } > RAM
  .log_filter_data :
  {
    PROVIDE(__start_log_filter_data = .);
    KEEP(*(SORT(.log_filter_data*)))
    PROVIDE(__stop_log_filter_data = .);
  } > RAM
} INSERT AFTER .data;

SECTIONS
{
  .mem_section_dummy_rom :
  {
  }
  .log_const_data :
  {
    PROVIDE(__start_log_const_data = .);
    KEEP(*(SORT(.log_const_data*)))
    PROVIDE(__stop_log_const_data = .);
  } > FLASH
  .log_backends :
  {
    PROVIDE(__start_log_backends = .);
    KEEP(*(SORT(.log_backends*)))
    PROVIDE(__stop_log_backends = .);
  } > FLASH
  .nrf_balloc :
  {
    PROVIDE(__start_nrf_balloc = .);
    KEEP(*(.nrf_balloc))
    PROVIDE(__stop_nrf_balloc = .);
  } > FLASH
} INSERT AFTER .text;
""".strip()

        include_match = re.search(r"(?mi)^\s*INCLUDE\b", content)
        if include_match:
            content = (
                content[: include_match.start()].rstrip()
                + "\n\n"
                + section_block
                + "\n\n"
                + content[include_match.start() :]
            )
        else:
            content = content.rstrip() + "\n\n" + section_block + "\n"
        self._write_text_file(linker_path, content)
        self._record_asset(
            "linker_sections",
            linker_path,
            "generated",
            "exact",
            "Added Nordic SDK GCC section-variable definitions",
        )

    def _generate_fallback_gcc_linker_script(self, dest: str) -> Optional[str]:
        flash_origin = self.flash_origin
        flash_length = self.flash_length
        ram_origin = self.ram_origin
        ram_length = self.ram_length
        if (
            flash_origin is None
            or flash_length is None
            or ram_origin is None
            or ram_length is None
        ):
            return None

        cmake_dir = Path(dest) / "cmake"
        cmake_dir.mkdir(parents=True, exist_ok=True)
        fallback_path = cmake_dir / "generated_linker.ld"

        extra_memory_lines: List[str] = []
        extra_section_lines: List[str] = []
        scatter_path = Path(self.linker_script) if self.linker_script else None
        if scatter_path and scatter_path.is_file() and scatter_path.suffix.lower() == ".sct":
            try:
                scatter_text = scatter_path.read_text(
                    encoding="utf-8", errors="ignore"
                )
            except OSError:
                scatter_text = ""
            region_pattern = re.compile(
                r"(?ms)^\s{2}(RW_[A-Za-z0-9_]+)\s+(0x[0-9A-Fa-f]+)\s+(?:UNINIT\s+)?(0x[0-9A-Fa-f]+)\s*\{(.*?)^\s{2}\}"
            )
            for match in region_pattern.finditer(scatter_text):
                region_name, origin_text, length_text, body = match.groups()
                origin = int(origin_text, 16)
                length = int(length_text, 16)
                if origin == ram_origin:
                    if ram_length != length:
                        ram_length = length
                        self.warnings.append(
                            f"使用 SCT 主 RAM 区域 {region_name}: "
                            f"0x{origin:08X}+0x{length:X}"
                        )
                    continue
                section_names = re.findall(r"\*\(\.([A-Za-z0-9_.$-]+)\)", body)
                if not section_names:
                    continue
                extra_memory_lines.append(
                    f"  {region_name} (xrw) : ORIGIN = 0x{origin:08X}, LENGTH = 0x{length:X}"
                )
                for section_name in section_names:
                    extra_section_lines.extend(
                        [
                            f"  .{section_name} (NOLOAD) :",
                            "  {",
                            "    . = ALIGN(4);",
                            f"    *(.{section_name})",
                            f"    *(.{section_name}.*)",
                            "    . = ALIGN(4);",
                            f"  }} > {region_name}",
                            "",
                        ]
                    )
            if extra_memory_lines:
                self.warnings.append(
                    f"从 SCT 转换额外 RAM 区域: {scatter_path}"
                )

        extra_memory = "\n".join(extra_memory_lines)
        extra_sections = "\n".join(extra_section_lines)
        stack_size, heap_size = self._gcc_stack_heap_sizes()

        content = f"""/* Auto-generated fallback linker script from uvprojx memory info */
ENTRY(Reset_Handler)

_estack = ORIGIN(RAM) + LENGTH(RAM);
_sstack = ORIGIN(RAM);
_Min_Heap_Size = 0x{heap_size:X};
_Min_Stack_Size = 0x{stack_size:X};

MEMORY
{{
  FLASH (rx)  : ORIGIN = 0x{flash_origin:08X}, LENGTH = 0x{flash_length:X}
  RAM   (xrw) : ORIGIN = 0x{ram_origin:08X}, LENGTH = 0x{ram_length:X}
{extra_memory}
}}

SECTIONS
{{
  .isr_vector :
  {{
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  }} > FLASH

  .text :
  {{
    . = ALIGN(4);
    *(.text*)
    *(.rodata*)
    *(.glue_7)
    *(.glue_7t)
    *(.eh_frame)
    KEEP (*(.init))
    KEEP (*(.fini))
    . = ALIGN(4);
    _etext = .;
    _stext = ADDR(.text);
  }} > FLASH

  .log_const_data :
  {{
    PROVIDE(__start_log_const_data = .);
    KEEP(*(SORT(.log_const_data*)))
    PROVIDE(__stop_log_const_data = .);
  }} > FLASH

  .log_backends :
  {{
    PROVIDE(__start_log_backends = .);
    KEEP(*(SORT(.log_backends*)))
    PROVIDE(__stop_log_backends = .);
  }} > FLASH

  .nrf_balloc :
  {{
    PROVIDE(__start_nrf_balloc = .);
    KEEP(*(.nrf_balloc))
    PROVIDE(__stop_nrf_balloc = .);
  }} > FLASH

  .ARM.extab : {{ *(.ARM.extab* .gnu.linkonce.armextab.*) }} > FLASH
  .ARM.exidx :
  {{
    __exidx_start = .;
    *(.ARM.exidx*)
    __exidx_end = .;
  }} > FLASH

  .preinit_array :
  {{
    PROVIDE_HIDDEN (__preinit_array_start = .);
    KEEP (*(.preinit_array*))
    PROVIDE_HIDDEN (__preinit_array_end = .);
  }} > FLASH

  .init_array :
  {{
    PROVIDE_HIDDEN (__init_array_start = .);
    KEEP (*(SORT(.init_array.*)))
    KEEP (*(.init_array*))
    PROVIDE_HIDDEN (__init_array_end = .);
  }} > FLASH

  .fini_array :
  {{
    PROVIDE_HIDDEN (__fini_array_start = .);
    KEEP (*(SORT(.fini_array.*)))
    KEEP (*(.fini_array*))
    PROVIDE_HIDDEN (__fini_array_end = .);
  }} > FLASH

  _sidata = LOADADDR(.data);
  .data :
  {{
    . = ALIGN(4);
    _sdata = .;
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  }} > RAM AT > FLASH

  .log_dynamic_data :
  {{
    PROVIDE(__start_log_dynamic_data = .);
    KEEP(*(SORT(.log_dynamic_data*)))
    PROVIDE(__stop_log_dynamic_data = .);
  }} > RAM AT > FLASH

  .log_filter_data :
  {{
    PROVIDE(__start_log_filter_data = .);
    KEEP(*(SORT(.log_filter_data*)))
    PROVIDE(__stop_log_filter_data = .);
  }} > RAM AT > FLASH

  .bss :
  {{
    . = ALIGN(4);
    _sbss = .;
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  }} > RAM

  .stack (NOLOAD) :
  {{
    . = ALIGN(8);
    KEEP(*(.stack))
    . = ALIGN(8);
  }} > RAM

  .heap (NOLOAD) :
  {{
    . = ALIGN(8);
    KEEP(*(.heap))
    . = ALIGN(8);
  }} > RAM

{extra_sections}

  ._user_heap_stack :
  {{
    . = ALIGN(8);
    PROVIDE(end = .);
    . = . + _Min_Heap_Size;
    . = . + _Min_Stack_Size;
    . = ALIGN(8);
  }} > RAM

  /DISCARD/ : {{ *(.note.GNU-stack) }}
}}
"""
        self._write_text_file(fallback_path, content)
        return str(fallback_path.resolve(strict=False))

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

        normalized_device = re.sub(
            r"[^a-z0-9]", "", self._clean_device_name().lower()
        )
        no_fpu_devices = ("nrf52805", "nrf52810", "nrf52811", "nrf52820")
        if normalized_device.startswith(no_fpu_devices):
            cpu_options = [
                flag
                for flag in cpu_options
                if not flag.startswith(("-mfpu=", "-mfloat-abi="))
            ]
            cpu_options.append("-mfloat-abi=soft")

        if self.cpu and not any(flag.startswith("-mcpu=") for flag in cpu_options):
            cpu_options.append(f"-mcpu={self.cpu.lower()}")

        if "-mthumb" not in cpu_options:
            cpu_options.insert(1 if cpu_options else 0, "-mthumb")

        return self._dedupe_keep_order(cpu_options)

    def _gcc_cxx_multilib_include_fallbacks(
        self, cpu_options: List[str]
    ) -> List[str]:
        compiler = shutil.which("arm-none-eabi-g++")
        if not compiler:
            return []

        try:
            version_result = subprocess.run(
                [compiler, "-dumpfullversion"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            multilib_result = subprocess.run(
                [compiler, *cpu_options, "-print-multi-directory"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []

        version = version_result.stdout.strip()
        multilib = multilib_result.stdout.strip().replace("\\", "/")
        if not version or not multilib or multilib == ".":
            return []

        toolchain_root = Path(compiler).resolve(strict=False).parent.parent
        target_root = (
            toolchain_root
            / "arm-none-eabi"
            / "include"
            / "c++"
            / version
            / "arm-none-eabi"
        )
        if (target_root / multilib / "bits" / "c++config.h").is_file():
            return []

        alternatives = []
        if "v7e-m+fp/" in multilib:
            alternatives.append(multilib.replace("v7e-m+fp/", "v7+fp/"))
        if "v7e-m+dp/" in multilib:
            alternatives.append(multilib.replace("v7e-m+dp/", "v7+fp/"))

        for alternative in alternatives:
            alternative_dir = target_root / alternative
            alternative_config = alternative_dir / "bits" / "c++config.h"
            common_config = target_root / "bits" / "os_defines.h"
            if alternative_config.is_file() and common_config.is_file():
                self.warnings.append(
                    "GCC C++ multilib 配置头缺失，使用兼容配置目录: "
                    f"{multilib} -> {alternative}"
                )
                return [str(alternative_dir), str(target_root)]

        return []

    def _gcc_c_standard(self) -> str:
        standard = self._extract_first_flag(self.c_flags, "-std=")
        if standard in {None, "-std=c90", "-std=gnu90"}:
            return "-std=gnu99"
        return standard

    def _gcc_cpp_standard(self) -> str:
        standard = self._extract_first_flag(self.cpp_flags, "-std=")
        return standard or "-std=gnu++14"

    def _gcc_optimization_flag(self) -> str:
        """Translate the selected Keil target optimization policy to GCC."""
        level = (self.keil_optimization or "0").strip()
        optimize_for_time = (self.keil_optimize_for_time or "").strip()

        # In uVision, oTime=0 selects space optimization. GCC 12 and newer
        # provide -Oz; generated CMake falls back to -Os for older releases.
        if optimize_for_time == "0" and level not in {"0", "1"}:
            return "-Oz"

        return {
            "0": "-O0",
            "1": "-O0",
            "2": "-O1",
            "3": "-O2",
            "4": "-O3",
            "5": "-Ofast",
            "6": "-Os",
            # -Oz is absent from older Arm GNU Toolchain releases.
            "7": "-Os",
            "8": "-O3",
        }.get(level, "-O0")

    def _gcc_compatible_compile_options(self) -> List[str]:
        supported = {
            "-fsigned-char",
            "-funsigned-char",
            "-fshort-enums",
        }
        return self._dedupe_keep_order(
            flag for flag in self.c_cpp_flags if flag in supported
        )

    def _gcc_lto_enabled(self) -> bool:
        return "-flto" in self.c_cpp_flags

    def _gcc_size_optimized(self) -> bool:
        return self._gcc_optimization_flag() in {"-Os", "-Oz"}

    def _gcc_inline_assembly_symbols(self) -> List[str]:
        symbols: List[str] = []
        pattern = re.compile(r"(?i)\bblx?\s+([A-Za-z_][A-Za-z0-9_]*)\b")
        for source in self._gcc_source_files():
            source_path = Path(source)
            if source_path.suffix.lower() not in {".c", ".cpp", ".cc", ".cxx"}:
                continue
            try:
                content = source_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            symbols.extend(match.group(1) for match in pattern.finditer(content))
        return sorted(self._dedupe_keep_order(symbols))

    def _gcc_uses_printf_float(self) -> bool:
        float_format = re.compile(
            r"%(?:[-+ #0']*)(?:\d+|\*)?(?:\.(?:\d+|\*))?"
            r"(?:hh|h|ll|l|j|z|t|L)?[aAeEfFgG]"
        )
        for source in self._gcc_source_files():
            source_path = Path(source)
            if source_path.suffix.lower() not in {".c", ".cpp", ".cc", ".cxx"}:
                continue
            try:
                content = source_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if float_format.search(content):
                return True
        return False

    def _gcc_include_reference(self, include_path: str, dest: str) -> str:
        absolute = Path(include_path).resolve(strict=False).as_posix()
        relative = self._normalize_path(include_path, dest)
        cmake_relative = f"${{CMAKE_CURRENT_LIST_DIR}}/{relative}"
        return absolute if len(absolute) < len(cmake_relative) else cmake_relative

    def _effective_defines(self, compiler: str) -> List[str]:
        defines: List[str] = []
        for define in sorted(self.defines):
            value = define.strip()
            if value.startswith("-D"):
                value = value[2:]
            if value:
                defines.append(value)

        defines = self._dedupe_keep_order(defines)
        return self._dedupe_keep_order(defines + self._inferred_device_defines())

    def _find_openocd_dir(self) -> Optional[Path]:
        for candidate in [self.project_dir / "openocd", self.project_dir.parent / "openocd"]:
            if candidate.is_dir():
                return candidate
        return None

    @staticmethod
    def _name_has_token(text: str, token: str) -> bool:
        value = text.lower()
        needle = token.lower()
        if needle in value:
            return True
        compact_value = re.sub(r"[^a-z0-9]+", "", value)
        compact_needle = re.sub(r"[^a-z0-9]+", "", needle)
        return bool(compact_needle and compact_needle in compact_value)

    def _openocd_interface_tokens(self, probe_name: str) -> List[str]:
        probe = (probe_name or "").strip().lower()
        if not probe or probe == "default":
            return ["stlink", "st-link"]
        if probe == "all":
            return [
                "stlink",
                "st-link",
                "jlink",
                "j-link",
                "segger",
                "daplink",
                "cmsis-dap",
                "nulink",
                "nu-link",
                "ulink",
            ]
        if probe == "stlink":
            return ["stlink", "st-link"]
        if probe == "jlink":
            return ["jlink", "j-link", "segger"]
        if probe == "daplink":
            return ["daplink", "cmsis-dap", "cmsis_dap", "cmsisdap"]
        if probe == "nulink":
            return ["nulink", "nu-link", "nu_link"]
        if probe == "ulink":
            return ["ulink", "cmsis-dap", "cmsis_dap"]
        return [probe]

    def _guess_openocd_config(self, interface_name: str) -> Optional[Path]:
        openocd_dir = self._find_openocd_dir()
        if openocd_dir is None:
            return None

        candidates = sorted(openocd_dir.rglob("*.cfg"))
        if not candidates:
            return None

        interface_tokens = self._openocd_interface_tokens(interface_name)
        interface_matched = [
            candidate
            for candidate in candidates
            if any(
                self._name_has_token(candidate.as_posix(), token)
                for token in interface_tokens
            )
        ]
        search_pool = interface_matched or candidates

        cpu_token = self.cpu.lower().replace("-", "") if self.cpu else ""
        best_score = -1
        best_path: Optional[Path] = None

        for candidate in search_pool:
            score = 0
            candidate_text = candidate.as_posix().lower()
            for token in self._device_tokens():
                if self._name_has_token(candidate_text, token):
                    score += 4
            if cpu_token and self._name_has_token(candidate_text, cpu_token):
                score += 2

            if score > best_score:
                best_score = score
                best_path = candidate

        if best_path is None:
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

    def _resolved_probe(self, options: GenerationOptions) -> str:
        probe = (options.debug_probe or "default").strip().lower()
        if probe != "default":
            return probe
        backend = (options.debug_backend or options.debugger or "default").strip().lower()
        if backend == "jlink":
            return "jlink"
        return "stlink"

    def _probe_display_name(self, probe: str) -> str:
        return SUPPORTED_DEBUG_PROBES.get(probe, probe.upper())

    def _selected_probes(self, options: GenerationOptions) -> List[str]:
        probe = (options.debug_probe or "default").strip().lower()
        if probe == "all":
            return ["stlink", "jlink", "daplink", "nulink", "ulink"]
        if probe == "default":
            return [self._resolved_probe(options)]
        return [probe]

    @staticmethod
    def _probe_supports_backend(probe: str, backend: str) -> bool:
        if backend == "jlink":
            return probe == "jlink"
        if backend == "openocd":
            return probe in {"stlink", "jlink", "daplink", "nulink", "ulink"}
        if backend == "pyocd":
            return probe in {"daplink", "ulink"}
        return True

    def _resolve_openocd_probe_and_config(
        self, probe: str
    ) -> tuple[str, Optional[Path]]:
        for token in self._openocd_interface_tokens(probe):
            openocd_cfg = self._guess_openocd_config(token)
            if openocd_cfg is not None:
                return probe, openocd_cfg
        return probe, self._guess_openocd_config(probe)

    def _default_openocd_interface_script(self, probe: str) -> str:
        if probe == "jlink":
            return "interface/jlink.cfg"
        if probe in {"daplink", "ulink"}:
            return "interface/cmsis-dap.cfg"
        if probe == "nulink":
            return "interface/nulink.cfg"
        return "interface/stlink.cfg"

    def _default_openocd_target_script(self) -> str:
        clean_device = self._clean_device_name().upper()
        vendor = (self.vendor or "").upper()

        if "ARTERY" in vendor or clean_device.startswith("AT32"):
            return "target/artery/at32f4x.cfg"
        if clean_device.startswith("STM32F0"):
            return "target/stm32f0x.cfg"
        if clean_device.startswith("STM32F1"):
            return "target/stm32f1x.cfg"
        if clean_device.startswith("STM32F3"):
            return "target/stm32f3x.cfg"
        if clean_device.startswith("STM32F4"):
            return "target/stm32f4x.cfg"
        if clean_device.startswith("STM32G0"):
            return "target/stm32g0x.cfg"
        return "target/stm32f4x.cfg"

    def _resolve_openocd_config_arguments(
        self, output_path: Path, probe: str
    ) -> tuple[str, List[str]]:
        probe, openocd_cfg = self._resolve_openocd_probe_and_config(probe)
        probe_label = self._probe_display_name(probe)
        if openocd_cfg is not None:
            return probe_label, [self._relative_workspace_path(openocd_cfg, output_path)]
        return probe_label, [
            self._default_openocd_interface_script(probe),
            self._default_openocd_target_script(),
        ]

    @staticmethod
    def _openocd_file_args(config_files: List[str]) -> List[str]:
        args: List[str] = []
        for config_file in config_files:
            args.extend(["-f", config_file])
        return args

    def _selected_debug_backends(self, options: GenerationOptions) -> List[str]:
        debug_backend = (options.debug_backend or options.debugger or "default").strip().lower()
        if debug_backend in {"default", "all"}:
            if options.compiler == "gcc":
                debuggers = ["openocd", "jlink"]
            else:
                debuggers = ["keil", "pyocd", "jlink"]
        elif debug_backend == "none":
            debuggers = []
        else:
            debuggers = [debug_backend]

        if options.host_os != "windows":
            debuggers = [debugger for debugger in debuggers if debugger != "keil"]

        selected_probe = (options.debug_probe or "default").strip().lower()
        if selected_probe not in {"default", "all", "jlink"}:
            debuggers = [debugger for debugger in debuggers if debugger != "jlink"]

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
                    'set "BUILD_SWITCH="',
                    'if /I "%~1"=="rebuild" set "BUILD_SWITCH=--clean-first"',
                    f'cmake --build "{build_dir}" %BUILD_SWITCH% --parallel',
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
                'BUILD_SWITCH=""',
                'if [ "${1:-}" = "rebuild" ]; then BUILD_SWITCH="--clean-first"; fi',
                f'cmake --build "{build_dir}" $BUILD_SWITCH --parallel',
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
            "set(CMAKE_CXX_ABI_COMPILED TRUE CACHE INTERNAL \"\" FORCE)",
            "set(CMAKE_CXX_COMPILER_WORKS TRUE CACHE INTERNAL \"\" FORCE)",
            "set(CMAKE_CXX_COMPILER_ABI ELF CACHE STRING \"\" FORCE)",
            "set(CMAKE_CXX_SIZEOF_DATA_PTR 4 CACHE STRING \"\" FORCE)",
            "set(CMAKE_CXX_BYTE_ORDER LITTLE_ENDIAN CACHE STRING \"\" FORCE)",
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
            "cmake.copyCompileCommands": "${workspaceFolder}/compile_commands.json",
            "C_Cpp.default.compileCommands": (
                f"${{workspaceFolder}}/{options.build_dir}/compile_commands.json"
            ),
            "C_Cpp.default.browse.path": ["${workspaceFolder}"],
            "C_Cpp.default.browse.limitSymbolsToIncludedHeaders": False,
        }

        env = self._vscode_env(options)
        if env:
            settings["cmake.configureEnvironment"] = env

        return settings

    def _build_vscode_c_cpp_properties(
        self, output_path: Path, options: GenerationOptions
    ) -> Dict[str, Any]:
        include_path: List[str] = ["${workspaceFolder}"]
        for include_dir in self._ordered_include_paths():
            include_path.append(
                f"${{workspaceFolder}}/{self._normalize_path(include_dir, str(output_path))}"
            )

        return {
            "configurations": [
                {
                    "name": SUPPORTED_HOST_SYSTEMS[options.host_os],
                    "compilerPath": (
                        shutil.which("arm-none-eabi-gcc") or "arm-none-eabi-gcc"
                    )
                    if options.compiler == "gcc"
                    else (shutil.which("armclang") or "armclang"),
                    "compileCommands": (
                        f"${{workspaceFolder}}/{options.build_dir}/compile_commands.json"
                    ),
                    "includePath": include_path,
                    "browse": {
                        "path": include_path,
                        "limitSymbolsToIncludedHeaders": False,
                    },
                }
            ],
            "version": 4,
        }

    def _vscode_workspace_root(self, output_path: Path) -> Path:
        project_dir = self.project_dir.resolve(strict=False)
        candidate = project_dir.parent
        try:
            output_path.resolve(strict=False).relative_to(candidate)
        except ValueError:
            return output_path

        for source in self.source_files:
            source_path = Path(source).resolve(strict=False)
            try:
                source_path.relative_to(candidate)
            except ValueError:
                continue
            try:
                source_path.relative_to(project_dir)
            except ValueError:
                return candidate
        return output_path

    @staticmethod
    def _replace_workspace_folder(value: Any, replacement: str) -> Any:
        if isinstance(value, str):
            return value.replace("${workspaceFolder}", replacement)
        if isinstance(value, list):
            return [
                KeilProjectToCMake._replace_workspace_folder(item, replacement)
                for item in value
            ]
        if isinstance(value, dict):
            return {
                key: KeilProjectToCMake._replace_workspace_folder(item, replacement)
                for key, item in value.items()
            }
        return value

    def _write_root_vscode_configuration(
        self,
        output_path: Path,
        options: GenerationOptions,
        contents: Dict[str, Any],
    ) -> None:
        workspace_root = self._vscode_workspace_root(output_path)
        if workspace_root.resolve(strict=False) == output_path.resolve(strict=False):
            return

        root_vscode = workspace_root / ".vscode"
        ownership_file = root_vscode / "keilforge-workspace.json"
        managed_names = set(contents)
        existing = [name for name in managed_names if (root_vscode / name).exists()]
        if existing and not ownership_file.exists():
            self.warnings.append(
                "工程根目录已有 VS Code 配置，保留现有文件: "
                + ", ".join(sorted(existing))
            )
            return

        root_vscode.mkdir(parents=True, exist_ok=True)
        relative_output = os.path.relpath(output_path, workspace_root).replace("\\", "/")
        generated_root = f"${{workspaceFolder}}/{relative_output}"

        root_contents = {
            name: self._replace_workspace_folder(content, generated_root)
            for name, content in contents.items()
        }
        settings = root_contents["settings.json"]
        settings["cmake.sourceDirectory"] = generated_root
        settings["cmake.copyCompileCommands"] = "${workspaceFolder}/compile_commands.json"
        settings["C_Cpp.default.browse.path"] = ["${workspaceFolder}"]
        settings["clangd.arguments"] = [
            "--compile-commands-dir=${workspaceFolder}",
        ]

        cpp_config = root_contents["c_cpp_properties.json"]
        for configuration in cpp_config.get("configurations", []):
            include_path = configuration.setdefault("includePath", [])
            if "${workspaceFolder}" not in include_path:
                include_path.insert(0, "${workspaceFolder}")
            browse_path = configuration.setdefault("browse", {}).setdefault("path", [])
            if "${workspaceFolder}" not in browse_path:
                browse_path.insert(0, "${workspaceFolder}")

        root_tasks = root_contents["tasks.json"].get("tasks", [])
        compiler_label = "GCC" if options.compiler == "gcc" else "ArmClang"
        configure_label = f"Configure {compiler_label} Debug"
        refresh_label = "Refresh Compile Commands"
        refresh_task = {
            "label": refresh_label,
            "type": "shell",
            "command": "cmake",
            "args": [
                "-E",
                "copy_if_different",
                f"{generated_root}/{options.build_dir}/compile_commands.json",
                "${workspaceFolder}/compile_commands.json",
            ],
            "dependsOn": configure_label,
            "problemMatcher": [],
        }
        root_tasks.insert(1, refresh_task)
        for task in root_tasks:
            if task.get("dependsOn") == configure_label and task is not refresh_task:
                task["dependsOn"] = refresh_label
            task_options = task.setdefault("options", {})
            task_options["cwd"] = generated_root

        build_compile_commands = output_path / options.build_dir / "compile_commands.json"
        if build_compile_commands.is_file():
            shutil.copy2(build_compile_commands, workspace_root / "compile_commands.json")

        for file_name, content in root_contents.items():
            with open(root_vscode / file_name, "w", encoding="utf-8") as file:
                json.dump(content, file, indent=4, ensure_ascii=False)
        with open(ownership_file, "w", encoding="utf-8") as file:
            json.dump(
                {"generatedDirectory": relative_output},
                file,
                indent=4,
                ensure_ascii=False,
            )
        self.warnings.append(f"VS Code 根工作区配置: {workspace_root}")

    def _relative_workspace_path(self, path: Path, output_path: Path) -> str:
        return self._normalize_path(str(path.resolve(strict=False)), str(output_path))

    def _artifact_path(self, options: GenerationOptions, suffix: str) -> str:
        return f"${{workspaceFolder}}/{options.build_dir}/{self._artifact_name()}{suffix}"

    def _jlink_flash_device(self) -> str:
        return self._clean_device_name() or self.cpu or "Cortex-M4"

    def _write_jlink_scripts(self, output_path: Path, options: GenerationOptions) -> None:
        if options.compiler != "gcc":
            return
        if "jlink" not in self._selected_debug_backends(options):
            return
        if not any(
            self._probe_supports_backend(probe, "jlink")
            for probe in self._selected_probes(options)
        ):
            return

        vsc_dir = output_path / ".vscode"
        vsc_dir.mkdir(parents=True, exist_ok=True)

        scripts = {
            "jlink_flash_elf.jlink": f"r\nloadfile {options.build_dir}/{self._artifact_name()}.elf\nr\ng\nq\n",
            "jlink_flash_hex.jlink": f"r\nloadfile {options.build_dir}/{self._artifact_name()}.hex\nr\ng\nq\n",
            "jlink_flash_bin.jlink": f"r\nloadfile {options.build_dir}/{self._artifact_name()}.bin 0x08000000\nr\ng\nq\n",
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

        build_args: List[str] = [
            "--build",
            f"${{workspaceFolder}}/{options.build_dir}",
        ]
        if options.compiler == "gcc":
            build_args.extend(["--target", self._cmake_artifacts_target_name()])
        build_args.append("--parallel")

        build_task: Dict[str, Any] = {
            "label": build_label,
            "type": "shell",
            "command": commands["cmake"],
            "args": build_args,
            "dependsOn": configure_label,
            "group": {"kind": "build", "isDefault": True},
            "problemMatcher": [],
        }
        if task_options:
            build_task["options"] = task_options

        rebuild_args = list(build_args)
        rebuild_args.insert(-1, "--clean-first")
        rebuild_task: Dict[str, Any] = {
            "label": f"Rebuild {compiler_label} Debug",
            "type": "shell",
            "command": commands["cmake"],
            "args": rebuild_args,
            "dependsOn": configure_label,
            "problemMatcher": [],
        }
        if task_options:
            rebuild_task["options"] = task_options

        clean_task: Dict[str, Any] = {
            "label": f"Clean {compiler_label} Debug",
            "type": "shell",
            "command": commands["cmake"],
            "args": [
                "--build",
                f"${{workspaceFolder}}/{options.build_dir}",
                "--target",
                "clean",
            ],
            "dependsOn": configure_label,
            "problemMatcher": [],
        }
        if task_options:
            clean_task["options"] = task_options

        tasks: List[Dict[str, Any]] = [
            configure_task,
            build_task,
            rebuild_task,
            clean_task,
        ]

        debuggers = self._selected_debug_backends(options)
        selected_probes = self._selected_probes(options)
        if options.compiler == "gcc":
            if "openocd" in debuggers:
                for probe in selected_probes:
                    if not self._probe_supports_backend(probe, "openocd"):
                        continue
                    if probe == "jlink" and "jlink" in debuggers:
                        continue
                    openocd_probe_label, openocd_configs = (
                        self._resolve_openocd_config_arguments(output_path, probe)
                    )
                    flash_probe_label = (
                        f"{openocd_probe_label} OpenOCD"
                        if probe == "jlink"
                        else openocd_probe_label
                    )
                    openocd_file_args = self._openocd_file_args(openocd_configs)
                    tasks.extend(
                        [
                            {
                                "label": f"Flash {flash_probe_label} ELF",
                                "type": "shell",
                                "command": commands["openocd"],
                                "args": openocd_file_args
                                + [
                                    "-c",
                                    f"program {{{self._artifact_path(options, '.elf')}}} verify reset exit",
                                ],
                                "dependsOn": build_label,
                                "problemMatcher": [],
                            },
                            {
                                "label": f"Flash {flash_probe_label} HEX",
                                "type": "shell",
                                "command": commands["openocd"],
                                "args": openocd_file_args
                                + [
                                    "-c",
                                    f"program {{{self._artifact_path(options, '.hex')}}} verify reset exit",
                                ],
                                "dependsOn": build_label,
                                "problemMatcher": [],
                            },
                            {
                                "label": f"Flash {flash_probe_label} BIN",
                                "type": "shell",
                                "command": commands["openocd"],
                                "args": openocd_file_args
                                + [
                                    "-c",
                                    f"program {{{self._artifact_path(options, '.bin')}}} 0x08000000 verify reset exit",
                                ],
                                "dependsOn": build_label,
                                "problemMatcher": [],
                            },
                            {
                                "label": f"Start {openocd_probe_label} OpenOCD GDB Server",
                                "type": "shell",
                                "command": commands["openocd"],
                                "args": openocd_file_args,
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

            if "jlink" in debuggers and any(
                self._probe_supports_backend(probe, "jlink")
                for probe in selected_probes
            ):
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
                tasks.append(
                    {
                        "label": "Start J-Link GDB Server",
                        "type": "shell",
                        "command": commands["jlink_gdb_server"],
                        "args": [
                            "-device", self._jlink_flash_device(),
                            "-if", "SWD",
                            "-speed", "1000",
                            "-port", "2331",
                            "-singlerun",
                        ],
                        "dependsOn": build_label,
                        "isBackground": True,
                        "presentation": presentation,
                        "problemMatcher": {
                            "owner": "jlink",
                            "pattern": [{"regexp": ".", "file": 0, "location": 0, "message": 0}],
                            "background": {
                                "activeOnStart": True,
                                "beginsPattern": ".*J-Link GDB Server.*",
                                "endsPattern": ".*Listening on TCP/IP port 2331.*",
                            },
                        },
                    }
                )
        else:
            if "openocd" in debuggers:
                for probe in selected_probes:
                    if not self._probe_supports_backend(probe, "openocd"):
                        continue
                    openocd_probe_label, openocd_configs = (
                        self._resolve_openocd_config_arguments(output_path, probe)
                    )
                    openocd_file_args = self._openocd_file_args(openocd_configs)
                    tasks.append(
                        {
                            "label": f"Start {openocd_probe_label} OpenOCD GDB Server",
                            "type": "shell",
                            "command": commands["openocd"],
                            "args": openocd_file_args,
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
                        }
                    )
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
        debuggers = self._selected_debug_backends(options)
        selected_probes = self._selected_probes(options)

        if "openocd" in debuggers:
            for probe in selected_probes:
                if not self._probe_supports_backend(probe, "openocd"):
                    continue
                openocd_probe_label, openocd_configs = (
                    self._resolve_openocd_config_arguments(output_path, probe)
                )
                launch_configs.append(
                    {
                        "name": f"{jlink_device or self.target_name} / {openocd_probe_label} / OpenOCD",
                        "cwd": "${workspaceFolder}",
                        "type": "cortex-debug",
                        "request": "launch",
                        "servertype": "openocd",
                        "serverpath": commands["openocd"],
                        "gdbPath": "arm-none-eabi-gdb",
                        "executable": executable_path,
                        "configFiles": openocd_configs,
                        "runToEntryPoint": "main",
                        "preLaunchTask": build_label,
                    }
                )

        if "jlink" in debuggers and any(
            self._probe_supports_backend(probe, "jlink")
            for probe in selected_probes
        ):
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

        if "pyocd" in debuggers:
            for probe in selected_probes:
                if not self._probe_supports_backend(probe, "pyocd"):
                    continue
                probe_label = self._probe_display_name(probe)
                launch_configs.append(
                    {
                        "name": f"Debug with PyOCD ({probe_label})",
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

        return {"version": "0.2.0", "configurations": launch_configs}

    def _write_cmake_header(
        self, f: TextIOWrapper, toolchain_include: str, languages: List[str]
    ) -> None:
        project_name = (self.target_name or self.project_name or "project").replace('"', "_")
        f.write("# CMake file generated from Keil uVision project\n")
        f.write("# Generated by keil_uvprojx2cmake.py\n\n")
        f.write("cmake_minimum_required(VERSION 3.20)\n\n")
        f.write(f"include({toolchain_include})\n\n")
        f.write(
            'set(CMAKE_NINJA_FORCE_RESPONSE_FILE ON CACHE BOOL '
            '"Use response files with Ninja" FORCE)\n'
        )
        f.write(f'project("{project_name}" LANGUAGES {" ".join(languages)})\n')
        f.write("set(CMAKE_EXPORT_COMPILE_COMMANDS ON)\n")
        f.write("set(CMAKE_OBJECT_PATH_MAX 140)\n")
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
        cmake_target = self._cmake_target_name()
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
            self._record_asset(
                "scatter_file",
                Path(self.linker_script),
                self._asset_source(Path(self.linker_script)),
                "exact",
                "Reused the Keil scatter file with ArmClang",
            )
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
            for include_path in self._ordered_include_paths():
                f.write(f'    "{self._normalize_path(include_path, dest)}"\n')
            f.write(")\n\n")

        f.write("# Source files\n")
        f.write("set(SOURCES\n")
        for source_file in sorted(self.source_files):
            f.write(f'    "{self._normalize_path(source_file, dest)}"\n')
        f.write(")\n\n")

        f.write(f"add_executable({cmake_target} ${{SOURCES}})\n")
        f.write(
            f'set_target_properties({cmake_target} PROPERTIES OUTPUT_NAME "{self._artifact_name()}" SUFFIX ".axf")\n\n'
        )

        if self.libraries:
            f.write("# Link libraries\n")
            f.write(f"target_link_libraries({cmake_target} PRIVATE\n")
            for lib in self.libraries:
                f.write(f"    {lib}\n")
            f.write(")\n\n")

        output_directory = os.path.join(self.project_dir, self.output_directory)
        output_directory = os.path.normpath(output_directory).replace("\\", "/")
        f.write(f'set(DEST_DIR "{output_directory}")\n')
        f.write(
            f"""add_custom_target(always_copy ALL
    COMMAND ${{CMAKE_COMMAND}} -E make_directory "${{DEST_DIR}}"
    COMMAND ${{CMAKE_COMMAND}} -E copy_if_different "$<TARGET_FILE:{cmake_target}>" "${{DEST_DIR}}/{self._artifact_name()}.axf"
    DEPENDS {cmake_target}
)\n"""
        )
        f.write('set_property(TARGET always_copy PROPERTY FOLDER "postbuild")\n')

    def _write_gcc_cmake_content(
        self, f: TextIOWrapper, dest: str, options: GenerationOptions
    ) -> None:
        self._emit_generation_progress(2, "解析 GCC 设备与链接配置")
        cmake_target = self._cmake_target_name()
        artifacts_target = self._cmake_artifacts_target_name()
        self._add_gcc_device_assets()
        linker_script = self._guess_gcc_linker_script(dest)
        if linker_script is None:
            linker_script = self._generate_fallback_gcc_linker_script(dest)
        if linker_script is None:
            raise FileNotFoundError(
                "Unable to locate a GCC linker script (.ld), and failed to auto-generate one from uvprojx memory info."
            )

        linker_path = Path(linker_script)
        linker_source = self._asset_source(linker_path, Path(dest))
        if linker_source == "generated":
            linker_support = "review_required"
            linker_reason = "Generated from the first uvprojx FLASH and RAM regions"
        else:
            linker_support = "exact"
            linker_reason = "Reused an existing GNU linker script"
        self._record_asset(
            "linker_script",
            linker_path,
            linker_source,
            linker_support,
            linker_reason,
        )
        linker_script = self._prepare_gcc_linker_script(linker_script, dest)

        languages = self._project_languages()
        self._write_cmake_header(
            f,
            "cmake/arm-none-eabi-toolchain.cmake",
            languages,
        )

        rel_linker_script = self._normalize_path(linker_script, dest)
        artifact_name = self._artifact_name()
        source_files = self._prepared_gcc_source_files(dest)
        self._emit_generation_progress(62, "生成运行时兼容文件")
        runtime_compat_source = self._generate_gcc_runtime_compat_source(dest)
        if runtime_compat_source:
            source_files.append(runtime_compat_source)
        gcc_libraries = self._prepared_gcc_libraries(dest)
        symbol_fragments = self._generate_gcc_symbol_fragments(dest)
        self._emit_generation_progress(70, "检查头文件兼容性")
        forced_headers = self._generate_legacy_stdint_compat_headers(dest)
        cxx_compat_header = self._generate_gcc_cxx_compat_header(
            dest, forced_headers
        )
        self._emit_generation_progress(78, "整理包含目录")
        gcc_include_paths = self._prepared_gcc_include_paths(dest)
        cpu_options = self._gcc_cpu_options()
        optimization_flag = self._gcc_optimization_flag()
        compatible_compile_options = self._gcc_compatible_compile_options()
        lto_default = "ON" if self._gcc_lto_enabled() else "OFF"
        inline_assembly_symbols = self._gcc_inline_assembly_symbols()
        uses_printf_float = self._gcc_uses_printf_float()
        cxx_multilib_includes = self._gcc_cxx_multilib_include_fallbacks(
            cpu_options
        )
        self._emit_generation_progress(82, "写入 CMake 目标配置")

        f.write(
            f'set(LINKER_SCRIPT "${{CMAKE_CURRENT_LIST_DIR}}/{rel_linker_script}")\n'
        )
        f.write('get_filename_component(LINKER_SCRIPT_DIR "${LINKER_SCRIPT}" DIRECTORY)\n')
        f.write(f'set(MAP_FILE "${{CMAKE_CURRENT_BINARY_DIR}}/{artifact_name}.map")\n')
        f.write(f'set(ELF_FILE "{artifact_name}.elf")\n')
        f.write(f'set(HEX_FILE "{artifact_name}.hex")\n')
        f.write(f'set(BIN_FILE "{artifact_name}.bin")\n')
        f.write(f'set(ARTIFACT_STAMP "{artifact_name}.artifacts.stamp")\n')
        f.write('set(ARTIFACT_SCRIPT "run_artifacts.bat")\n')
        if optimization_flag == "-Oz":
            f.write('set(KEIL_OPTIMIZATION_FLAG "-Os")\n')
            f.write(
                'if(CMAKE_C_COMPILER_ID STREQUAL "GNU" AND '
                'CMAKE_C_COMPILER_VERSION VERSION_GREATER_EQUAL "12")\n'
            )
            f.write('    set(KEIL_OPTIMIZATION_FLAG "-Oz")\n')
            f.write("endif()\n")
        else:
            f.write(f'set(KEIL_OPTIMIZATION_FLAG "{optimization_flag}")\n')
        f.write(f'set(KEIL_GCC_PROJECT_LTO_DEFAULT "{lto_default}")\n')
        f.write(
            'if(NOT DEFINED KEIL_GCC_CACHED_PROJECT_LTO_DEFAULT OR '\
            'NOT "${KEIL_GCC_CACHED_PROJECT_LTO_DEFAULT}" STREQUAL '\
            '"${KEIL_GCC_PROJECT_LTO_DEFAULT}")\n'
        )
        f.write(
            '    set(KEIL_GCC_ENABLE_LTO "${KEIL_GCC_PROJECT_LTO_DEFAULT}" '
            'CACHE BOOL "Enable GCC link-time optimization" FORCE)\n'
        )
        f.write("else()\n")
        f.write(
            '    option(KEIL_GCC_ENABLE_LTO '
            '"Enable GCC link-time optimization" '
            '${KEIL_GCC_PROJECT_LTO_DEFAULT})\n'
        )
        f.write("endif()\n")
        f.write(
            'set(KEIL_GCC_CACHED_PROJECT_LTO_DEFAULT '
            '"${KEIL_GCC_PROJECT_LTO_DEFAULT}" CACHE INTERNAL '
            '"Last generated Keil LTO default" FORCE)\n'
        )
        f.write(
            'file(RELATIVE_PATH COMPILE_COMMANDS_COPY_TARGET "${CMAKE_CURRENT_BINARY_DIR}" "${CMAKE_CURRENT_SOURCE_DIR}/compile_commands.json")\n\n'
        )
        f.write('file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "@echo off\\r\\n")\n')
        f.write('file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "setlocal\\r\\n")\n')
        f.write(
            'file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "\\"${CMAKE_OBJCOPY}\\" -O ihex \\"${ELF_FILE}\\" \\"${HEX_FILE}\\"\\r\\n")\n'
        )
        f.write(
            'file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "if errorlevel 1 exit /b %errorlevel%\\r\\n")\n'
        )
        f.write(
            'file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "\\"${CMAKE_OBJCOPY}\\" -O binary \\"${ELF_FILE}\\" \\"${BIN_FILE}\\"\\r\\n")\n'
        )
        f.write(
            'file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "if errorlevel 1 exit /b %errorlevel%\\r\\n")\n'
        )
        f.write(
            'file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "\\"${CMAKE_SIZE}\\" --format=berkeley \\"${ELF_FILE}\\"\\r\\n")\n'
        )
        f.write(
            'file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "if errorlevel 1 exit /b %errorlevel%\\r\\n")\n'
        )
        f.write(
            'file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "\\"${CMAKE_COMMAND}\\" -E touch \\"${ARTIFACT_STAMP}\\"\\r\\n")\n'
        )
        f.write(
            'file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "if errorlevel 1 exit /b %errorlevel%\\r\\n")\n'
        )
        f.write(
            'file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "if exist \\"compile_commands.json\\" \\"${CMAKE_COMMAND}\\" -E copy_if_different \\"compile_commands.json\\" \\"${COMPILE_COMMANDS_COPY_TARGET}\\"\\r\\n")\n'
        )
        f.write(
            'file(APPEND "${CMAKE_CURRENT_BINARY_DIR}/${ARTIFACT_SCRIPT}" "if errorlevel 1 exit /b %errorlevel%\\r\\n\\r\\n")\n'
        )

        for original_source, staged_source in self._copy_staged_source_mappings:
            staged_reference = self._normalize_path(str(staged_source), dest)
            original_reference = original_source.as_posix()
            staged_parent = self._normalize_path(str(staged_source.parent), dest)
            f.write("add_custom_command(\n")
            f.write(
                f'    OUTPUT "${{CMAKE_CURRENT_LIST_DIR}}/{staged_reference}"\n'
            )
            f.write(
                f'    COMMAND ${{CMAKE_COMMAND}} -E make_directory '
                f'"${{CMAKE_CURRENT_LIST_DIR}}/{staged_parent}"\n'
            )
            f.write(
                f'    COMMAND ${{CMAKE_COMMAND}} -E copy_if_different '
                f'"{original_reference}" '
                f'"${{CMAKE_CURRENT_LIST_DIR}}/{staged_reference}"\n'
            )
            f.write(f'    DEPENDS "{original_reference}"\n')
            f.write("    VERBATIM\n")
            f.write(")\n")
            f.write(
                f'set_source_files_properties('
                f'"${{CMAKE_CURRENT_LIST_DIR}}/{staged_reference}" '
                f'PROPERTIES GENERATED TRUE)\n\n'
            )

        f.write("set(PROJECT_SOURCES\n")
        for source_file in source_files:
            f.write(f'    "{self._normalize_path(source_file, dest)}"\n')
        f.write(")\n\n")

        f.write(f"add_executable({cmake_target} ${{PROJECT_SOURCES}})\n")
        f.write(
            f'set_target_properties({cmake_target} PROPERTIES OUTPUT_NAME "{artifact_name}" SUFFIX ".elf")\n\n'
        )

        if gcc_include_paths:
            f.write(f"target_include_directories({cmake_target} PRIVATE\n")
            for include_path in gcc_include_paths:
                reference = self._gcc_include_reference(include_path, dest)
                f.write(f'    "{reference}"\n')
            f.write(")\n\n")

        defines = self._effective_defines("gcc")
        if defines:
            f.write(f"target_compile_definitions({cmake_target} PRIVATE\n")
            for define in defines:
                f.write(f"    {define}\n")
            f.write(")\n\n")

        f.write(f"target_compile_options({cmake_target} PRIVATE\n")
        for include_path in cxx_multilib_includes:
            normalized = include_path.replace("\\", "/")
            f.write(
                f'    "$<$<COMPILE_LANGUAGE:CXX>:SHELL:-isystem {normalized}>"\n'
            )
        if cxx_compat_header:
            normalized_compat = cxx_compat_header.replace("\\", "/")
            f.write(
                f'    "$<$<COMPILE_LANGUAGE:C,CXX>:SHELL:-include {normalized_compat}>"\n'
            )
        for flag in cpu_options:
            f.write(f"    {flag}\n")
        f.write("    -ffunction-sections\n")
        f.write("    -fdata-sections\n")
        f.write("    -fno-common\n")
        f.write("    -Wall\n")
        f.write("    -Wextra\n")
        for flag in compatible_compile_options:
            f.write(f"    {flag}\n")
        f.write('    "$<$<BOOL:${KEIL_GCC_ENABLE_LTO}>:-flto>"\n')
        f.write(
            '    "$<$<BOOL:${KEIL_GCC_ENABLE_LTO}>:-flto-partition=1to1>"\n'
        )
        f.write("    ${KEIL_OPTIMIZATION_FLAG}\n")
        f.write(f"    $<$<COMPILE_LANGUAGE:C>:{self._gcc_c_standard()}>\n")
        f.write("    $<$<CONFIG:Debug>:-g3>\n")
        if "CXX" in languages:
            f.write(f"    $<$<COMPILE_LANGUAGE:CXX>:{self._gcc_cpp_standard()}>\n")
            if "-fno-rtti" in self.c_cpp_flags:
                f.write("    $<$<COMPILE_LANGUAGE:CXX>:-fno-rtti>\n")
        f.write("    $<$<COMPILE_LANGUAGE:ASM>:-x>\n")
        f.write("    $<$<COMPILE_LANGUAGE:ASM>:assembler-with-cpp>\n")
        f.write(")\n\n")

        f.write(f"target_link_options({cmake_target} PRIVATE\n")
        for flag in cpu_options:
            f.write(f"    {flag}\n")
        f.write('    "$<$<BOOL:${KEIL_GCC_ENABLE_LTO}>:-flto>"\n')
        f.write(
            '    "$<$<BOOL:${KEIL_GCC_ENABLE_LTO}>:-flto-partition=1to1>"\n'
        )
        for symbol in inline_assembly_symbols:
            f.write(
                '    "$<$<BOOL:${KEIL_GCC_ENABLE_LTO}>:'
                f'-Wl,--undefined={symbol}>"\n'
            )
        f.write("    -T${LINKER_SCRIPT}\n")
        f.write('    "-L${LINKER_SCRIPT_DIR}"\n')
        for fragment in symbol_fragments:
            normalized_fragment = self._normalize_path(fragment, dest)
            f.write(
                f"    -T${{CMAKE_CURRENT_LIST_DIR}}/{normalized_fragment}\n"
            )
        f.write("    -Wl,--gc-sections\n")
        f.write("    -Wl,--relax\n")
        if linker_source == "generated":
            f.write("    -Wl,--require-defined=Reset_Handler\n")
        f.write("    -Wl,-Map=${MAP_FILE}\n")
        f.write("    -Wl,--print-memory-usage\n")
        f.write("    -specs=nano.specs\n")
        f.write("    -specs=nosys.specs\n")
        if uses_printf_float:
            f.write("    -u _printf_float\n")
        f.write(")\n\n")

        libraries = self._dedupe_keep_order(gcc_libraries + ["m"])
        f.write(f"target_link_libraries({cmake_target} PRIVATE\n")
        for library in libraries:
            if Path(library).is_absolute():
                f.write(
                    f'    "${{CMAKE_CURRENT_LIST_DIR}}/'
                    f'{self._normalize_path(library, dest)}"\n'
                )
            else:
                f.write(f"    {library}\n")
        f.write(")\n\n")

        f.write(
            f"""add_custom_target({artifacts_target} ALL
    COMMAND cmd /c "${{ARTIFACT_SCRIPT}}"
    DEPENDS {cmake_target}
    WORKING_DIRECTORY "${{CMAKE_CURRENT_BINARY_DIR}}"
    VERBATIM
)\n"""
        )

    def _write_cmake_content_with_options(
        self, f: TextIOWrapper, dest: str, options: GenerationOptions
    ) -> None:
        if options.compiler == "gcc":
            self._write_gcc_cmake_content(f, dest, options)
        else:
            self._write_armclang_cmake_content(f, dest, options)

    @staticmethod
    def _clean_generated_cmake_assets(output_path: Path) -> None:
        generated_cmake_dir = output_path / "cmake"
        if generated_cmake_dir.is_dir():
            shutil.rmtree(generated_cmake_dir)

    def _generate_configured_output(
        self,
        output_dir: Optional[str],
        options: GenerationOptions,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> bool:
        self.last_error = ""
        output_path = Path(output_dir) if output_dir else self._default_output_path(options)
        output_path.mkdir(parents=True, exist_ok=True)
        previous_callback = self._generation_progress_callback
        self._generation_progress_callback = progress_callback

        try:
            self._emit_generation_progress(0, "准备输出目录")
            self._copy_staged_source_mappings = []
            self._lvgl_lazy_new_allocator_cache = None
            self._clean_generated_cmake_assets(output_path)
            with open(output_path / "CMakeLists.txt", "w", encoding="utf-8") as f:
                self._write_cmake_content_with_options(f, str(output_path), options)

            self._emit_generation_progress(88, "写入工具链和构建脚本")
            self._write_toolchain_files(output_path, options)
            script_name = self._host_command_names(options.host_os)["script_name"]
            self._write_text_file(
                output_path / script_name, self._build_script_content(options)
            )

            if options.export_vsc_settings:
                self._emit_generation_progress(94, "写入 VS Code 配置")
                vsc_dir = output_path / ".vscode"
                vsc_dir.mkdir(parents=True, exist_ok=True)
                self._write_jlink_scripts(output_path, options)

                vscode_contents = {
                    "settings.json": self._build_vscode_settings(options),
                    "tasks.json": self._build_vscode_tasks(output_path, options),
                    "launch.json": self._build_vscode_launch(output_path, options),
                    "c_cpp_properties.json": self._build_vscode_c_cpp_properties(
                        output_path, options
                    ),
                }
                for file_name, content in vscode_contents.items():
                    with open(vsc_dir / file_name, "w", encoding="utf-8") as f:
                        json.dump(content, f, indent=4, ensure_ascii=False)
                self._write_root_vscode_configuration(
                    output_path, options, vscode_contents
                )

            self._emit_generation_progress(98, "写入转换报告")
            self._write_conversion_report(output_path, options)
            self._emit_generation_progress(100, "CMake 工程生成完成")

            return True
        except Exception as e:
            self.last_error = f"Error generating CMake files: {e}"
            print(self.last_error, file=sys.stderr)
            return False
        finally:
            self._generation_progress_callback = previous_callback

    def generate_cmake(
        self,
        output_dir: Optional[str] = None,
        export_vsc_settings: bool = False,
        generation_options: Optional[GenerationOptions] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> bool:
        """生成 CMake 文件"""
        self.last_error = ""
        try:
            options = self._prepare_generation_options(
                export_vsc_settings, generation_options
            )
        except Exception as e:
            self.last_error = f"Invalid generation options: {e}"
            print(self.last_error, file=sys.stderr)
            return False
        return self._generate_configured_output(
            output_dir, options, progress_callback
        )

    def validate_generated_build(
        self,
        output_dir: Optional[str],
        generation_options: GenerationOptions,
        timeout_seconds: int = 300,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> BuildValidationReport:
        options = generation_options.normalized()
        output_path = Path(output_dir) if output_dir else self._default_output_path(options)
        validator = BuildValidator(
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
        )
        report = validator.validate(output_path.resolve(strict=False), options)
        with open(output_path / "build-validation.json", "w", encoding="utf-8") as f:
            json.dump(asdict(report), f, indent=2, ensure_ascii=False)
        return report

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
    debug_probe: str = "default",
    debug_backend: str = "default",
    target_name: Optional[str] = None,
    validate_build: bool = False,
    validation_timeout: int = 300,
):
    if verbose:
        print(f"Keil 项目文件: {filepath}")

    parser = KeilProjectToCMake(filepath, uv4_path, verbose, target_name)
    if not parser.parse():
        print("解析失败!", file=sys.stderr)
        sys.exit(1)

    if verbose:
        parser.print_project_info()
        for warning in parser.warnings:
            print(f"警告: {warning}", file=sys.stderr)

    options = GenerationOptions(
        compiler=compiler,
        generator=generator or DEFAULT_GENERATOR_BY_HOST.get(host_os, "Ninja"),
        host_os=host_os,
        debugger=debugger,
        debug_probe=debug_probe,
        debug_backend=debug_backend,
        build_dir=build_dir,
        export_vsc_settings=export_vsc_settings,
    )

    if not parser.generate_cmake(output_dir, export_vsc_settings, options):
        print("生成 CMake 文件失败!", file=sys.stderr)
        sys.exit(1)

    if validate_build:
        report = parser.validate_generated_build(
            output_dir, options, timeout_seconds=validation_timeout
        )
        if report.status != "success":
            categories = ", ".join(
                diagnostic.category for diagnostic in report.diagnostics
            )
            print(f"构建验证失败: {categories}", file=sys.stderr)
            sys.exit(2)


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
        help="调试后端配置（兼容旧参数）",
    )
    parser.add_argument(
        "--debug-probe",
        choices=sorted(SUPPORTED_DEBUG_PROBES.keys()),
        default="default",
        help="硬件探针选项",
    )
    parser.add_argument(
        "--debug-backend",
        choices=sorted(SUPPORTED_DEBUG_BACKENDS.keys()),
        default=None,
        help="调试后端选项",
    )
    parser.add_argument(
        "--build-dir",
        default=None,
        help="构建目录名称，例如 build 或 build-gcc",
    )
    parser.add_argument(
        "--target-name",
        default=None,
        help="选择 uvprojx 中的 TargetName，多目标工程默认使用第一个目标",
    )
    parser.add_argument(
        "--validate-build",
        action="store_true",
        help="生成后执行 CMake 配置和编译，并输出 build-validation.json",
    )
    parser.add_argument(
        "--validation-timeout",
        type=int,
        default=300,
        help="单个 CMake 命令的超时秒数",
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
    debug_backend = args.debug_backend or args.debugger

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
        args.debug_probe,
        debug_backend,
        args.target_name,
        args.validate_build,
        args.validation_timeout,
    )
