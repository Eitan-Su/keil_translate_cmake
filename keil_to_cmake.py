#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Keil uVision Project (.uvprojx) to CMake Converter
This script parses a Keil uVision project file and generates CMake configuration files.
"""

from io import TextIOWrapper
import filecmp
import xml.etree.ElementTree as ET
import os
import sys
import re
import argparse
import json
import hashlib
import queue
import shlex
import shutil
import signal
import stat
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

# PACK 安装目录可能比仓库内精简后的 vendor 目录多出若干层级。
# 优先保留厂商、包名和版本；旧式 vendor/<package> 仅在内容相同时使用。
PACK_VENDOR_LAYOUT_MAPPINGS: Tuple[
    Tuple[str, str, Tuple[str, ...], Tuple[str, ...]], ...
] = (
    ("ARM", "CMSIS", ("CMSIS", "Core", "Include"), ("Include",)),
    ("ARM", "CMSIS", ("CMSIS", "Include"), ("Include",)),
    ("*", "*", ("Device", "Firmware"), ()),
    ("*", "*", (), ()),
)


def get_supported_generators(host_os: str) -> List[str]:
    host = (host_os or "windows").strip().lower()
    return SUPPORTED_GENERATORS.get(host, SUPPORTED_GENERATORS["windows"])


def _is_multi_config_generator(generator: str) -> bool:
    name = (generator or "").strip().lower()
    return (
        name in {"xcode", "ninja multi-config", "green hills multi"}
        or name.startswith("visual studio ")
    )


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
        if backend_raw == "pyocd" and debug_probe not in {
            "default",
            "all",
            "daplink",
            "ulink",
        }:
            raise ValueError(
                f"Probe '{debug_probe}' is incompatible with PyOCD backend."
            )
        if backend_raw == "keil" and host_os != "windows":
            raise ValueError("Keil backend is only available on Windows.")
        if backend_raw == "keil" and compiler != "armclang":
            raise ValueError(
                "Keil backend requires the ArmClang compiler configuration."
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
class SourceCompileSettings:
    macro_actions: Dict[str, Optional[str]] = field(default_factory=dict)
    include_paths: List[str] = field(default_factory=list)


@dataclass
class SourceAnalysis:
    uses_heap: bool
    defines_sbrk: bool
    declares_sbrk: bool
    needs_irq: bool
    printf_float: bool
    scanf_float: bool
    has_text: bool
    has_libc_init: bool
    migration_checks: Tuple[bool, ...]
    static_functions: Set[str]
    uint32_functions: Set[str]
    assembly_symbols: List[str]
    quoted_includes: List[str] = field(default_factory=list)
    class_names: Set[str] = field(default_factory=set)
    using_namespaces: Set[str] = field(default_factory=set)
    namespace_functions: List[Tuple[str, str, str, str, str]] = field(default_factory=list)


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
    cancelled: bool = False

    @property
    def succeeded(self) -> bool:
        return self.return_code == 0 and not self.error and not self.cancelled


class OperationCancelled(Exception):
    """Stop conversion at a checkpoint without reporting a conversion failure."""


@dataclass(frozen=True)
class BuildActivity:
    phase: str
    message: str
    current: int = 0
    total: int = 0
    unit: str = "steps"


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


class _WindowsProcessJob:
    """Keep cancellable builds and their descendants in one Windows job."""

    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        class Limits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io_counters", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            error = ctypes.get_last_error()
            process.kill()
            process.wait()
            raise ctypes.WinError(error)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self.close()
            process.kill()
            process.wait()
            raise

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class BuildValidator:
    """Configure and build a generated project, then classify common failures."""

    MISSING_EXECUTABLE_PATTERN = re.compile(
        r"\[(?:WinError|Errno)\s*2\]|"
        r"The system cannot find the file specified|"
        r"No such file or directory|"
        r"系统找不到指定的文件",
        re.I,
    )
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
        cancel_check: Optional[Callable[[], bool]] = None,
        activity_callback: Optional[Callable[[BuildActivity], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.progress_callback = progress_callback
        self.cancel_check = cancel_check
        self.activity_callback = activity_callback
        self.log_callback = log_callback
        self._last_progress = -1

    def _cancel_requested(self) -> bool:
        return self.cancel_check is not None and self.cancel_check()

    def _activity(self, phase: str, message: str, current=0, total=0, unit="steps") -> None:
        if self.activity_callback is not None:
            self.activity_callback(BuildActivity(phase, message, current, total, unit))

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
        if self.log_callback is not None:
            self.log_callback(line)
        if phase == "configure":
            if "Configuring done" in line:
                self._emit_progress(6, "CMake 配置完成")
            elif "Generating done" in line:
                self._emit_progress(9, "CMake 生成完成")
            return

        progress = self._build_progress_from_line(line)
        if progress is not None:
            current, total = progress
            percent = 10 + round(88 * current / total)
            self._emit_progress(percent, f"编译 {current}/{total}")
        # A build counter also counts link and post-build commands, not just sources.
        if re.search(r"\bLinking\b|\barmlink(?:\.exe)?\b", line, re.I):
            self._activity("link", "正在链接固件")
        elif progress is not None:
            unit = "percent" if re.search(r"\[\s*\d+%\s*\]", line) else "steps"
            self._activity("compile", "正在编译", current, total, unit)

    @staticmethod
    def _process_group_options() -> Dict[str, Any]:
        if os.name == "nt":
            return {
                "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            }
        return {"start_new_session": True}

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen) -> None:
        job = process.__dict__.get("_keil_job")
        if job is not None:
            job.close()
            process.wait(timeout=10)
            return
        if process.poll() is not None:
            return

        terminated = False
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                terminated = completed.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                terminated = False
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                terminated = True
            except OSError:
                terminated = False

        if not terminated and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

        try:
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _run_streaming(
        self, command: List[str], cwd: Path, phase: str
    ) -> CommandResult:
        started = time.monotonic()
        stdout_parts: List[str] = []
        stderr_parts: List[str] = []
        if self._cancel_requested():
            return CommandResult(command, None, "", "", 0, "Cancelled by user", True)
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
                **self._process_group_options(),
            )
            if os.name == "nt" and self.cancel_check is not None:
                process._keil_job = _WindowsProcessJob(process)
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
        cancelled = False
        try:
            while completed_streams < len(readers) or process.poll() is None:
                if self._cancel_requested():
                    cancelled = True
                    error = "Cancelled by user"
                    break
                if time.monotonic() - started >= self.timeout_seconds:
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
        finally:
            # Cancellation and callback failures must both release child processes.
            if process.poll() is None:
                self._terminate_process_tree(process)
            job = process.__dict__.get("_keil_job")
            if job is not None:
                job.close()
            for reader in readers:
                reader.join(timeout=2)
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
            if self.log_callback is not None:
                self.log_callback(line.rstrip())

        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(process)
            return_code = process.poll()
            error = error or "Command did not exit after output streams closed"
        return CommandResult(
            command=command,
            return_code=return_code,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            duration_seconds=round(time.monotonic() - started, 3),
            error=error,
            cancelled=cancelled,
        )

    def _run(
        self, command: List[str], cwd: Path, phase: str = ""
    ) -> CommandResult:
        if any(callback is not None for callback in (
            self.progress_callback, self.cancel_check, self.activity_callback, self.log_callback
        )):
            return self._run_streaming(command, cwd, phase)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **self._process_group_options(),
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

        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            return CommandResult(
                command=command,
                return_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=round(time.monotonic() - started, 3),
            )
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(process)
            stdout, stderr = process.communicate()
            return CommandResult(
                command=command,
                return_code=process.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
                duration_seconds=round(time.monotonic() - started, 3),
                error=f"Command timed out after {self.timeout_seconds} seconds",
            )

    def _classify(self, phase: str, result: CommandResult) -> List[BuildDiagnostic]:
        if result.cancelled:
            return [BuildDiagnostic("cancelled", phase, "Cancelled by user")]
        if result.succeeded:
            return []
        text = "\n".join((result.error, result.stdout, result.stderr))
        if result.return_code is None:
            missing_executable = self.MISSING_EXECUTABLE_PATTERN.search(text)
            if missing_executable:
                return [
                    BuildDiagnostic(
                        category="missing_tool",
                        phase=phase,
                        message=missing_executable.group(0),
                    )
                ]
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
        self,
        source_dir: Path,
        options: GenerationOptions,
        expected_artifacts: Optional[List[Path]] = None,
    ) -> BuildValidationReport:
        self._last_progress = -1
        self._emit_progress(0, "准备 CMake 配置")
        self._activity("configure", "正在配置 CMake")
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
        if configure.cancelled:
            return BuildValidationReport("cancelled", configure, None, diagnostics)
        if not configure.succeeded:
            return BuildValidationReport("configure_failed", configure, None, diagnostics)

        self._emit_progress(10, "开始编译")
        self._activity("compile", "正在启动编译")
        build_command = ["cmake", "--build", str(build_dir)]
        if _is_multi_config_generator(options.generator):
            build_command.extend(["--config", "Debug"])
        build = self._run(build_command, source_dir, "build")
        diagnostics.extend(self._classify("build", build))
        if build.cancelled:
            return BuildValidationReport("cancelled", configure, build, diagnostics)
        status = "success" if build.succeeded else "build_failed"
        if build.succeeded:
            self._activity("artifact", "正在检查固件文件")
            missing_artifacts = [
                artifact
                for artifact in expected_artifacts or []
                if not artifact.is_file()
            ]
            if missing_artifacts:
                diagnostics.append(
                    BuildDiagnostic(
                        category="missing_artifact",
                        phase="artifact",
                        message="Expected build artifacts are missing: "
                        + ", ".join(str(path) for path in missing_artifacts),
                    )
                )
                return BuildValidationReport(
                    "artifact_failed", configure, build, diagnostics
                )
            self._emit_progress(100, "构建验证完成")
        return BuildValidationReport(status, configure, build, diagnostics)


class KeilProjectToCMake:
    """解析Keil uVision项目文件的类"""

    CPU_INFO = re.compile(r"^(\S+)\((\S+)\)$")  # 用于解析CPU信息

    GCC_MIGRATION_CHECKS = (
        (
            re.compile(r"\$(?:Sub|Super)\$\$[A-Za-z_]\w*"),
            "迁移检查: 检测到 Arm linker $Sub$$/$Super$$ 符号包装，"
            "GNU ld 需要 --wrap 或源码适配",
        ),
        (
            re.compile(r"\b(?:__rt_entry|__rt_lib_init|__user_initial_stackheap)\b"),
            "迁移检查: 检测到 Arm C Library 私有入口，newlib 不提供等价符号",
        ),
        (
            re.compile(
                r"\b__asm\s+(?:void|int|unsigned|long|short|char)\s+"
                r"[A-Za-z_]\w*\s*\("
            ),
            "迁移检查: 转换后仍存在 ARMASM 函数语法，"
            "需要人工转换为 GNU 扩展汇编",
        ),
        (
            re.compile(r"__attribute__\s*\(\(\s*at\s*\("),
            "迁移检查: 检测到绝对地址放置属性 at(...)，"
            "需要在 GNU 链接脚本中定义专用 section",
        ),
    )

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
        self._generation_progress_callback: Optional[
            Callable[[int, str], None]
        ] = None
        self.requested_target_name = (target_name or "").strip()
        self._reset_parsed_state()

    def _reset_parsed_state(self) -> None:
        self.last_error = ""
        self.warnings: List[str] = []
        self.asset_records: List[AssetRecord] = []
        self._parsed_warnings: List[str] = []
        self._parsed_asset_records: List[AssetRecord] = []
        self._missing_source_cache: Dict[str, Optional[Path]] = {}
        self._project_rte_instance_names: Set[str] = set()
        self._copy_staged_source_mappings: List[tuple[Path, Path]] = []
        self._lvgl_lazy_new_allocator_cache: Optional[bool] = None
        self._repository_root_cache: Optional[Path] = None
        self._generation_resolved_paths: Optional[Dict[str, Path]] = None
        self._generation_source_analysis: Optional[Dict[str, SourceAnalysis]] = None

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
        self.source_compile_settings: Dict[str, SourceCompileSettings] = {}
        self._prepared_source_origins: Dict[str, str] = {}

        # 编译器设置
        self.defines: Set[str] = set()
        self.undefines: Set[str] = set()
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
        self._reset_parsed_state()

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

            self._parsed_warnings = list(self.warnings)
            self._parsed_asset_records = list(self.asset_records)

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
        if clean_device.startswith("STM32F0"):
            return "Cortex-M0"
        if clean_device.startswith(("STM32G0", "STM32L0")):
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
            group_settings = self._parse_local_controls(
                group, ".//GroupOption"
            )
            files = group.findall(".//File")
            for file_node in files:
                file_name = self._get_element_text(file_node, "FileName")
                file_path = self._get_element_text(file_node, "FilePath")
                file_type = self._get_element_text(file_node, "FileType")

                if file_name and file_path:
                    if not self._is_file_included_in_build(file_node):
                        continue
                    file_settings = self._parse_local_controls(
                        file_node, ".//FileOption"
                    )

                    # 转换路径分隔符并解析相对路径
                    file_path = file_path.replace("\\", "/")
                    if not os.path.isabs(file_path):
                        file_path = os.path.join(self.project_dir, file_path)

                    file_path = os.path.normpath(file_path)

                    # 根据文件类型分类
                    file_ext = os.path.splitext(file_path)[1].lower()

                    if file_type == "3" and self._arm_linker_symdefs_content(
                        Path(file_path)
                    ) is not None:
                        self.linker_inputs.append(file_path)
                    elif file_ext in [
                        ".c",
                        ".cpp",
                        ".cc",
                        ".cxx",
                        ".s",
                        ".asm",
                        ".o",
                        ".obj",
                    ]:
                        self.source_files.append(file_path)
                        macro_actions = dict(group_settings.macro_actions)
                        macro_actions.update(file_settings.macro_actions)
                        include_paths = self._dedupe_keep_order(
                            [
                                *group_settings.include_paths,
                                *file_settings.include_paths,
                            ]
                        )
                        if macro_actions or include_paths:
                            self.source_compile_settings[
                                self._source_control_key(file_path)
                            ] = SourceCompileSettings(
                                macro_actions=macro_actions,
                                include_paths=include_paths,
                            )
                    elif file_ext in [".h", ".hpp", ".hxx"]:
                        self.header_files.append(file_path)
                        # 添加头文件目录到包含路径
                        include_dir = os.path.dirname(file_path)
                        if include_dir:
                            self._add_include_path(include_dir)
                    elif file_ext in [".lib", ".a"]:
                        self.libraries.append(file_path)

    def _parse_local_controls(
        self, node: ET.Element, option_path: str
    ) -> SourceCompileSettings:
        option = node.find(option_path)
        if option is None:
            return SourceCompileSettings()

        macro_actions: Dict[str, Optional[str]] = {}
        undefine_text = self._get_element_text(
            option, ".//Cads/VariousControls/Undefine"
        )
        for undefine in self._split_defines(undefine_text):
            macro_name = self._macro_name(undefine)
            if macro_name:
                macro_actions[macro_name] = None
        define_text = self._get_element_text(
            option, ".//Cads/VariousControls/Define"
        )
        for define in self._split_defines(define_text):
            macro_name = self._macro_name(define)
            if macro_name:
                macro_actions[macro_name] = define.strip()

        include_paths: List[str] = []
        include_text = self._get_element_text(
            option, ".//Cads/VariousControls/IncludePath"
        )
        for include in include_text.split(";"):
            include = include.strip().replace("\\", "/")
            if not include:
                continue
            if not os.path.isabs(include):
                include = os.path.join(self.project_dir, include)
            include_paths.append(os.path.normpath(include))
        return SourceCompileSettings(
            macro_actions=macro_actions,
            include_paths=self._dedupe_keep_order(include_paths),
        )

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

    def _resolved_path(self, path: str | Path) -> Path:
        value = Path(path)
        cache = self._generation_resolved_paths
        if cache is None:
            return value.resolve(strict=False)
        # 保留 .. 的符号链接语义；相对输入使用当前目录区分缓存项。
        key = str(value if value.is_absolute() else Path.cwd() / value)
        if key not in cache:
            resolved = value.resolve(strict=False)
            cache[key] = resolved
            cache[str(resolved)] = resolved
        return cache[key]

    def _source_control_key(self, path: str | Path) -> str:
        return os.path.normcase(str(self._resolved_path(path)))

    @staticmethod
    def _macro_name(value: str) -> str:
        normalized = value.strip()
        if normalized.startswith(("-D", "-U")):
            normalized = normalized[2:]
        return normalized.split("=", 1)[0].split("(", 1)[0].strip()

    def _source_compile_settings_for(self, path: str | Path) -> SourceCompileSettings:
        source_key = self._source_control_key(path)
        origin_key = self._prepared_source_origins.get(source_key, source_key)
        return self.source_compile_settings.get(origin_key, SourceCompileSettings())

    def _source_local_include_paths(self) -> List[str]:
        return self._dedupe_keep_order(
            [
                include_path
                for settings in self.source_compile_settings.values()
                for include_path in settings.include_paths
            ]
        )

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
        values: List[str] = []
        current: List[str] = []
        quote = ""
        escaped = False
        nesting = 0

        def flush() -> None:
            value = "".join(current).strip()
            if value:
                values.append(value)
            current.clear()

        for character in defines_text.strip():
            if quote:
                current.append(character)
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = ""
                continue

            if character in {'"', "'"}:
                quote = character
                current.append(character)
            elif character in "([{":
                nesting += 1
                current.append(character)
            elif character in ")]}" and nesting:
                nesting -= 1
                current.append(character)
            elif nesting == 0 and (character in ",;" or character.isspace()):
                flush()
            else:
                current.append(character)

        flush()
        return values

    @staticmethod
    def _split_command_line(value: str) -> List[str]:
        lexer = shlex.shlex(value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.escape = ""
        try:
            return list(lexer)
        except ValueError:
            return value.split()

    @staticmethod
    def _quote_command_argument(value: str) -> str:
        if not any(character.isspace() for character in value):
            return value
        return '"' + value.replace('"', '\\"') + '"'

    @staticmethod
    def _cmake_escape_argument(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace(";", "\\;")
            .replace("\r", "\\r")
            .replace("\n", "\\n")
        )

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

            undefines_text = self._get_element_text(
                c_compiler, ".//VariousControls/Undefine"
            )
            for undefine in self._split_defines(undefines_text):
                macro_name = self._macro_name(undefine)
                if macro_name:
                    self.undefines.add(macro_name)

            # 包含路径
            include_text = self._get_element_text(
                c_compiler, ".//VariousControls/IncludePath"
            )
            if include_text:
                includes = include_text.split(";")
                explicit_includes: List[str] = []
                for include in includes:
                    include = include.strip().replace("\\", "/")
                    if include:
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
                self.c_cpp_flags.extend(
                    self._quote_command_argument(item)
                    for item in self._split_command_line(misc_text)
                )

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
                for item in self._split_command_line(misc_text):
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
                        self.linker_flags.append(
                            self._quote_command_argument(item)
                        )

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

        direct_candidates: List[Path] = []
        pack_candidates: List[tuple[tuple[int, ...], Path]] = []
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
                legacy_candidates = [
                    base / "ARM" / "CMSIS" / "Include",
                    base / "CMSIS" / "Include",
                    base / "CMSIS" / "Core" / "Include",
                ]
                direct_candidates.extend(
                    path for path in legacy_candidates if path.is_dir()
                )

                pack_root = base / "ARM" / "PACK" / "ARM" / "CMSIS"
                if pack_root.is_dir():
                    for pattern in ("*/CMSIS/Core/Include", "*/CMSIS/Include"):
                        for path in pack_root.glob(pattern):
                            version = path.relative_to(pack_root).parts[0]
                            version_key = tuple(
                                int(part) for part in re.findall(r"\d+", version)
                            )
                            pack_candidates.append((version_key, path))

        valid_pack = [
            (version, path.resolve(strict=False))
            for version, path in pack_candidates
            if path.is_dir() and any(path.glob("core_cm*.h"))
        ]
        valid_direct = [
            path.resolve(strict=False)
            for path in direct_candidates
            if path.is_dir() and any(path.glob("core_cm*.h"))
        ]
        if valid_pack:
            _, selected = max(
                set(valid_pack),
                key=lambda item: (item[0], item[1].as_posix().lower()),
            )
            self._add_include_path(str(selected))
        elif valid_direct:
            selected = list(dict.fromkeys(valid_direct))[0]
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
        resolved = self._resolved_path(path)
        if output_dir is not None:
            try:
                resolved.relative_to(self._resolved_path(output_dir))
                return "generated"
            except ValueError:
                pass
        try:
            resolved.relative_to(self._resolved_path(self.project_dir))
            return "project"
        except ValueError:
            return "external"

    def _project_content_root(self) -> Path:
        project_dir = self._resolved_path(self.project_dir)
        paths = [project_dir]

        def add_project_path(path: Path) -> None:
            try:
                common = Path(os.path.commonpath((str(project_dir), str(path))))
            except ValueError:
                return
            if common == Path(project_dir.anchor):
                return
            paths.append(path)

        for value in [*self.source_files, *self.header_files]:
            path = self._resolved_path(value)
            if path.is_file():
                add_project_path(path.parent)
        for value in [
            *self._ordered_include_paths(),
            *self._source_local_include_paths(),
        ]:
            path = self._resolved_path(value)
            if path.is_dir():
                add_project_path(path)
        if self.linker_script:
            linker_path = self._resolved_path(self.linker_script)
            if linker_path.is_file():
                add_project_path(linker_path.parent)

        try:
            return Path(os.path.commonpath([str(path) for path in paths]))
        except ValueError:
            return self._resolved_path(self.project_dir)

    def _project_repository_root(self) -> Path:
        if self._repository_root_cache is not None:
            return self._repository_root_cache

        project_dir = self._resolved_path(self.project_dir)
        for candidate in (project_dir, *project_dir.parents):
            if (candidate / ".git").exists():
                self._repository_root_cache = candidate
                return candidate
        self._repository_root_cache = self._project_content_root()
        return self._repository_root_cache

    def _pack_path_components(
        self, path: Path,
    ) -> Optional[Tuple[str, str, str, Tuple[str, ...]]]:
        parts = self._resolved_path(path).parts
        for index in range(len(parts) - 1, -1, -1):
            if parts[index].lower() != "pack" or len(parts) < index + 4:
                continue
            vendor = parts[index + 1]
            package = parts[index + 2]
            version = parts[index + 3]
            tail = tuple(parts[index + 4 :])
            return vendor, package, version, tail
        return None

    def _pack_directories_match(self, source: Path, mapped: Path) -> bool:
        source = self._resolved_path(source)
        mapped = self._resolved_path(mapped)
        try:
            source_files = {
                candidate.relative_to(source).as_posix(): candidate
                for candidate in source.rglob("*")
                if candidate.is_file()
            }
            mapped_files = {
                candidate.relative_to(mapped).as_posix(): candidate
                for candidate in mapped.rglob("*")
                if candidate.is_file()
            }
            matches = source_files.keys() == mapped_files.keys() and all(
                filecmp.cmp(
                    source_files[relative],
                    mapped_files[relative],
                    shallow=False,
                )
                for relative in source_files
            )
        except OSError:
            matches = False

        return matches

    def _mapped_pack_vendor_path(self, path: Path) -> Optional[Path]:
        components = self._pack_path_components(path)
        if components is None:
            return None

        vendor, package, version, tail = components
        repository_root = self._project_repository_root()
        for (
            expected_vendor,
            expected_package,
            source_prefix,
            destination_prefix,
        ) in PACK_VENDOR_LAYOUT_MAPPINGS:
            if expected_vendor != "*" and vendor.lower() != expected_vendor.lower():
                continue
            if (
                expected_package != "*"
                and package.lower() != expected_package.lower()
            ):
                continue
            if len(tail) < len(source_prefix) or tuple(
                part.lower() for part in tail[: len(source_prefix)]
            ) != tuple(part.lower() for part in source_prefix):
                continue

            mapped_tail = (*destination_prefix, *tail[len(source_prefix) :])
            versioned = repository_root / "vendor" / vendor / package / version
            versioned = versioned.joinpath(*mapped_tail)
            if versioned.is_dir():
                return self._resolved_path(versioned)

            legacy = repository_root / "vendor" / package
            legacy = legacy.joinpath(*mapped_tail)
            if not legacy.is_dir():
                continue
            if self._pack_directories_match(path, legacy):
                return self._resolved_path(legacy)
            self._append_portability_warning(
                "PACK 目录与仓库旧式 vendor 映射内容不同，跳过映射: "
                f"{self._resolved_path(path)} -> {self._resolved_path(legacy)}"
            )
        return None

    def _portable_cmake_reference(
        self,
        path: str | Path,
        dest: str,
        role: str,
        map_pack_include: bool = False,
    ) -> str:
        resolved = self._resolved_path(path)
        pack_components = (
            self._pack_path_components(resolved) if map_pack_include else None
        )
        if pack_components is not None:
            mapped = self._mapped_pack_vendor_path(resolved)
            if mapped is not None:
                resolved = mapped
            else:
                self._append_portability_warning(
                    f"PACK {role}未找到仓库 vendor 映射，保留绝对位置: {resolved}"
                )
                return self._cmake_escape_argument(resolved.as_posix())

        repository_root = self._project_repository_root()
        output_root = self._resolved_path(dest)
        is_portable = False
        for root in (repository_root, output_root):
            try:
                resolved.relative_to(root)
                is_portable = True
                break
            except ValueError:
                continue
        if not is_portable:
            self._append_portability_warning(
                f"{role}位于仓库树外，保留绝对位置: {resolved}"
            )
            return self._cmake_escape_argument(resolved.as_posix())

        try:
            relative = os.path.relpath(resolved, output_root).replace("\\", "/")
        except ValueError:
            self._append_portability_warning(
                f"{role}与输出目录无法建立相对位置，保留绝对位置: {resolved}"
            )
            return self._cmake_escape_argument(resolved.as_posix())
        escaped = self._cmake_escape_argument(relative)
        if escaped == ".":
            return "${CMAKE_CURRENT_LIST_DIR}"
        return f"${{CMAKE_CURRENT_LIST_DIR}}/{escaped}"

    def _append_portability_warning(self, message: str) -> None:
        if message in self.warnings:
            return
        self.warnings.append(message)
        print(f"警告: {message}", file=sys.stderr)

    def _record_asset(
        self,
        kind: str,
        path: Path,
        source: str,
        support_level: str,
        reason: str,
    ) -> None:
        normalized = str(self._resolved_path(path))
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
            project_file=str(self._resolved_path(self.uvprojx_path)),
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
            output_directory=str(self._resolved_path(output_path)),
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

        clean_device = re.sub(
            r"[^A-Z0-9]", "", self._clean_device_name().upper()
        )
        family_patterns = (
            r"^(STM32(?:[FHLUC]\d{3}|G(?:\d{3}|0[BC]\d)|"
            r"WB\d{2}|WL\d{2}|WBA\d{2}))",
            r"^((?:AT32|GD32|N32)[A-Z]\d{3})",
            r"^(NRF\d{5})",
            r"^(MSP432[A-Z]\d{3})",
        )
        for pattern in family_patterns:
            match = re.match(pattern, clean_device)
            if match:
                return match.group(1)
        return clean_device

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
        gcc_port = self._gcc_freertos_port(Path(source_path))
        if gcc_port != Path(source_path):
            return str(gcc_port)
        normalized = Path(source_path).as_posix()
        if "/startup/mdk/" in normalized:
            candidate = Path(
                normalized.replace("/startup/mdk/", "/startup/gcc/")
            ).resolve(strict=False)
            if candidate.exists():
                return str(candidate)
        return source_path

    def _gcc_freertos_port(self, path: Path) -> Path:
        port_dir = (
            path.parent if path.name.lower() in {"port.c", "portmacro.h"} else path
        )
        if (
            port_dir.parent.name.lower() != "rvds"
            or port_dir.parent.parent.name.lower() != "portable"
        ):
            return path
        portable = port_dir.parent.parent
        if not (portable.parent / "include" / "FreeRTOS.h").is_file():
            return path
        for compiler_dir in ("GCC", "gcc"):
            candidate = portable / compiler_dir / port_dir.name
            # 仅采用同一内核中源文件和头文件完整的 GCC 移植层。
            if all((candidate / name).is_file() for name in ("port.c", "portmacro.h")):
                self._append_warning_once(
                    f"FreeRTOS GCC 移植层替换: {port_dir} -> {candidate}"
                )
                return candidate / path.name if port_dir != path else candidate
        self._append_warning_once(
            f"FreeRTOS GCC 移植层缺失或文件不完整，保留原输入: {port_dir}"
        )
        return path

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
                    self._append_warning_once(
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
        generated_path = (
            Path(dest) / "cmake" / "sources" / f"{source_path.stem}_gcc.S"
        )
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

    @staticmethod
    def _patch_armclang_legacy_core_intrinsics(content: str) -> tuple[str, int]:
        getter_pattern = re.compile(
            r"uint32_t\s+(?P<name>__get_(?P<register>[PM]SP))\s*"
            r"\(\s*void\s*\)\s*__attribute__\s*"
            r"\(\s*\(\s*naked\s*\)\s*\)\s*;\s*"
            r"uint32_t\s+(?P=name)\s*\(\s*void\s*\)\s*"
            r"\{.*?\n\s*\}",
            re.DOTALL,
        )
        setter_pattern = re.compile(
            r"void\s+(?P<name>__set_(?P<register>[PM]SP))\s*"
            r"\(\s*uint32_t\s+[A-Za-z_]\w*\s*\)\s*"
            r"__attribute__\s*\(\s*\(\s*naked\s*\)\s*\)\s*;\s*"
            r"void\s+(?P=name)\s*\(\s*uint32_t\s+"
            r"(?P<argument>[A-Za-z_]\w*)\s*\)\s*\{.*?\n\s*\}",
            re.DOTALL,
        )

        def replace_getter(match: re.Match[str]) -> str:
            register = match.group("register").lower()
            return (
                f'uint32_t {match.group("name")}(void)\n'
                "{\n"
                "  uint32_t result;\n"
                f'  __ASM volatile ("MRS %0, {register}" : "=r" (result) );\n'
                "  return result;\n"
                "}"
            )

        def replace_setter(match: re.Match[str]) -> str:
            register = match.group("register").lower()
            argument = match.group("argument")
            return (
                f'void {match.group("name")}(uint32_t {argument})\n'
                "{\n"
                f'  __ASM volatile ("MSR {register}, %0" : : "r" ({argument}) );\n'
                "}"
            )

        patched, getter_count = getter_pattern.subn(replace_getter, content)
        patched, setter_count = setter_pattern.subn(replace_setter, patched)
        return patched, getter_count + setter_count

    def _generate_patched_core_cm3_source(
        self, source_path: Path, dest: str, compiler: str = "gcc"
    ) -> Optional[str]:
        try:
            content = source_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

        if compiler == "armclang":
            patched, replacement_count = self._patch_armclang_legacy_core_intrinsics(
                content
            )
            if replacement_count == 0:
                return None
            generated_path = (
                Path(dest)
                / "cmake"
                / "sources"
                / f"{source_path.stem}_armclang.c"
            )
            self._write_text_file(generated_path, patched)
            return str(generated_path.resolve(strict=False))

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

        generated_path = (
            Path(dest) / "cmake" / "sources" / f"{source_path.stem}_gcc.c"
        )
        self._write_text_file(generated_path, patched)
        return str(generated_path.resolve(strict=False))

    def _prepared_armclang_source_files(self, dest: str) -> List[str]:
        prepared: List[str] = []
        for source in sorted(self.source_files):
            prepared_source = source
            source_path = Path(source).resolve(strict=False)
            if source_path.name.lower() == "core_cm3.c":
                generated = self._generate_patched_core_cm3_source(
                    source_path, dest, "armclang"
                )
                if generated is not None:
                    prepared_source = generated
                    self._prepared_source_origins[
                        self._source_control_key(generated)
                    ] = self._source_control_key(source)
            prepared.append(prepared_source)
        return self._dedupe_keep_order(prepared)

    @staticmethod
    def _safe_ascii_file_stem(stem: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_")
        return safe or "source"

    @staticmethod
    def _patch_armcc_embedded_assembly(content: str) -> tuple[str, List[str]]:
        if re.search(r"__ASM", content, re.IGNORECASE) is None:
            return content, []
        delay_pattern = re.compile(
            r"__ASM(?:\s+volatile)?\s+void\s+(?P<name>[A-Za-z_]\w*)\s*"
            r"\(\s*uint32_t\s+(?P<arg>[A-Za-z_]\w*)\s*\)\s*"
            r"\{\s*SUBS\s+r0\s*,\s*#1\s*"
            r"BNE\s+(?P=name)\s*BX\s+LR\s*\}",
            re.IGNORECASE,
        )

        def delay_replacement(match: re.Match[str]) -> str:
            name = match.group("name")
            arg = match.group("arg")
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

        sanitized = KeilProjectToCMake._mask_c_comments_and_literals(content)
        matches = list(delay_pattern.finditer(sanitized))
        patched = content
        for match in reversed(matches):
            patched = (
                patched[: match.start()]
                + delay_replacement(match)
                + patched[match.end() :]
            )
        return patched, [match.group("name") for match in matches]

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

    def _patch_freertos_port_includes(
        self, content: str, source_path: Path, staged_dir: Path
    ) -> str:
        if "portable" not in content.lower() or "port.c" not in content.lower():
            return content
        include_pattern = re.compile(
            r'(?m)^[ \t]*#[ \t]*include[ \t]+"(?P<path>[^"\r\n]+)"'
        )
        sanitized = self._mask_c_comments_and_literals(content)
        patched = content
        for match in reversed(list(include_pattern.finditer(content))):
            if not sanitized[match.start() : match.end()].lstrip().startswith("#"):
                continue
            included = match.group("path").replace("\\", "/")
            original = self._resolved_path(source_path.parent / included)
            if original.name.lower() != "port.c":
                continue
            gcc_port = self._gcc_freertos_port(original)
            if gcc_port == original:
                continue
            # 生成副本中的相对包含目录以副本所在目录为基准。
            replacement = self._normalize_path(str(gcc_port), str(staged_dir))
            patched = (
                patched[: match.start("path")]
                + replacement
                + patched[match.end("path") :]
            )
        return patched

    def _stage_gcc_source_file(self, source_path: Path, dest: str) -> str:
        try:
            resolved_source = self._resolved_path(source_path)
            output_path = self._resolved_path(dest)
            resolved_source.relative_to(output_path)
            return str(resolved_source)
        except ValueError:
            pass

        if not source_path.is_file():
            return str(source_path)

        copy_only_staging = self._path_contains_non_ascii(resolved_source)
        digest = hashlib.sha1(
            str(resolved_source).encode("utf-8")
        ).hexdigest()[:12]
        safe_stem = self._safe_ascii_file_stem(source_path.stem)
        staged_dir = Path(dest) / "cmake" / "sources"
        staged_path = staged_dir / f"{safe_stem}_{digest}{source_path.suffix}"
        prepared_content: Optional[str] = None
        if source_path.suffix.lower() == ".c":
            try:
                original_content = source_path.read_text(
                    encoding="utf-8", errors="ignore"
                )
                if self._generation_source_analysis is not None:
                    self._analyze_source_file(source_path, original_content)
                content = self._patch_freertos_port_includes(
                    original_content, source_path, staged_dir
                )
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
                    prepared_content = patched
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
                content = source_path.read_text(encoding="utf-8", errors="ignore")
                if self._generation_source_analysis is not None:
                    self._analyze_source_file(source_path, content)
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
                    prepared_content = patched
                    self.warnings.append(
                        f"GCC 类型兼容修正: {source_path.name}: system_core_clock"
                    )
            except OSError:
                pass

        if prepared_content is None and not copy_only_staging:
            staged_path.unlink(missing_ok=True)
            return str(resolved_source)

        staged_dir.mkdir(parents=True, exist_ok=True)
        if staged_path.exists():
            staged_path.chmod(staged_path.stat().st_mode | 0o200)
        if prepared_content is None:
            shutil.copy2(source_path, staged_path)
            staged_path.chmod(staged_path.stat().st_mode | 0o200)
        else:
            self._write_text_file(staged_path, prepared_content)

        # A staged source loses GCC's implicit search of its original directory.
        # Add that directory only for staged files instead of every source file.
        source_dir = str(self._resolved_path(source_path.parent))
        if source_dir:
            self._add_include_path(source_dir)
        if source_path.parent.name.lower() == "src":
            sibling_include = source_path.parent.parent / "inc"
            if sibling_include.is_dir():
                self._add_include_path(str(self._resolved_path(sibling_include)))

        if prepared_content is None:
            self._copy_staged_source_mappings.append(
                (resolved_source, self._resolved_path(staged_path))
            )
        return str(self._resolved_path(staged_path))

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

        cache_key = os.path.normcase(
            str(source_path.resolve(strict=False))
        )
        if cache_key in self._missing_source_cache:
            return self._missing_source_cache[cache_key] or source_path

        candidates: List[Path] = []
        search_root = self._project_content_root()
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
            expected_parent_parts = tuple(
                os.path.normcase(part).casefold()
                for part in source_path.parent.resolve(strict=False).parts
            )

            def score(candidate: Path) -> tuple[int, int, int]:
                candidate_parent_parts = tuple(
                    os.path.normcase(part).casefold()
                    for part in candidate.parent.resolve(strict=False).parts
                )
                parent_suffix_score = 0
                for expected, actual in zip(
                    reversed(expected_parent_parts),
                    reversed(candidate_parent_parts),
                ):
                    if expected != actual:
                        break
                    parent_suffix_score += 1
                text = candidate.as_posix().lower()
                device_score = sum(token in text for token in device_tokens)
                rte_score = 1 if "/rte/" in text else 0
                return parent_suffix_score, device_score, rte_score

            best_score = max(score(candidate) for candidate in candidates)
            best_candidates = sorted(
                (
                    candidate.resolve(strict=False)
                    for candidate in candidates
                    if score(candidate) == best_score
                ),
                key=lambda candidate: candidate.as_posix().lower(),
            )
            if len(best_candidates) != 1:
                self._missing_source_cache[cache_key] = None
                self.warnings.append(
                    "源文件原位置不存在，工程搜索范围内候选存在歧义，"
                    f"保留原位置: {source_path}; 候选: "
                    + ", ".join(str(candidate) for candidate in best_candidates)
                )
                return source_path

            resolved = best_candidates[0]
            self._missing_source_cache[cache_key] = resolved
            self.warnings.append(
                f"源文件原位置不存在，使用工程搜索范围内同名文件: "
                f"{source_path} -> {resolved}"
            )
            return resolved

        self._missing_source_cache[cache_key] = None
        return source_path

    def _prepared_gcc_source_files(
        self, dest: str, startup_replacements: Optional[Dict[str, Path]] = None
    ) -> List[str]:
        prepared: List[str] = []
        self._prepared_source_origins = {}
        sources = self._gcc_source_files()
        source_keys: Dict[str, str] = {}

        def source_key(path: str | Path) -> str:
            text = str(path)
            if text not in source_keys:
                source_keys[text] = self._source_control_key(path)
            return source_keys[text]

        # 保留首个原始文件的设置，避免对每个 GCC 文件重复遍历全部输入。
        origins: Dict[str, str] = {}
        for original in self.source_files:
            mapped_key = source_key(self._map_source_for_gcc(original))
            origins.setdefault(mapped_key, source_key(original))

        total = len(sources)
        for index, source in enumerate(sources, start=1):
            self._emit_generation_progress(
                5 + round(55 * index / max(total, 1)),
                f"处理源文件 {index}/{total}",
            )
            source_control_key = source_key(source)
            if source_control_key not in self.source_compile_settings:
                source_control_key = origins.get(source_control_key, source_control_key)

            def append_prepared(path: str) -> None:
                prepared.append(path)
                self._prepared_source_origins[
                    source_key(path)
                ] = source_control_key

            source_path = self._resolve_missing_source(Path(source))
            if startup_replacements and str(source_path) in startup_replacements:
                source_path = startup_replacements[str(source_path)]
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
                    append_prepared(generated)
                    continue
            if (
                source_path.name.lower().startswith("startup_")
                and source_path.suffix.lower() in {".s", ".asm"}
                and self._is_armasm_startup_source(source_path)
            ):
                generated = self._generate_gnu_startup_from_armasm(source_path, dest)
                if generated is not None:
                    append_prepared(generated)
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
            append_prepared(staged)
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
        for include_path in [
            *self._ordered_include_paths(),
            *self._source_local_include_paths(),
        ]:
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
        if work_dir.is_dir():
            shutil.rmtree(work_dir)
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

            converted_members: List[Path] = []
            member_occurrences: Dict[str, int] = {}
            for index, member in enumerate(members):
                occurrence = member_occurrences.get(member, 0) + 1
                member_occurrences[member] = occurrence
                extraction_dir = work_dir / "members" / f"{index:04d}"
                extraction_dir.mkdir(parents=True, exist_ok=True)
                extraction = subprocess.run(
                    [
                        ar,
                        "xN",
                        str(occurrence),
                        str(library_path.resolve(strict=False)),
                        member,
                    ],
                    cwd=extraction_dir,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if extraction.returncode != 0:
                    return None
                extracted = extraction_dir / Path(
                    member.replace("\\", "/")
                ).name
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

    @staticmethod
    def _arm_linker_symdefs_content(path: Path) -> Optional[str]:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        return content if content.startswith("#<SYMDEFS>#") else None

    def _gcc_linker_object_inputs(self) -> List[str]:
        return self._dedupe_keep_order(
            [
                str(Path(linker_input).resolve(strict=False))
                for linker_input in self.linker_inputs
                if self._arm_linker_symdefs_content(Path(linker_input)) is None
            ]
        )

    def _generate_gcc_symbol_fragments(self, dest: str) -> List[str]:
        fragments: List[str] = []
        symbol_pattern = re.compile(
            r"^(?P<address>0x[0-9A-Fa-f]+)\s+\S+\s+"
            r"(?P<name>[A-Za-z_.$][A-Za-z0-9_.$]*)\s*$"
        )
        for linker_input in self.linker_inputs:
            input_path = Path(linker_input)
            content = self._arm_linker_symdefs_content(input_path)
            if content is None:
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
                / "linker"
                / "symbols"
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
            self._append_warning_once(
                f"SYMDEFS 固定地址符号来自 {input_path}；"
                "需与提供这些符号的实际固件地址及 ABI 一致"
            )
        return fragments

    def _analyze_source_file(
        self, path: Path, content: Optional[str] = None
    ) -> SourceAnalysis:
        cache = self._generation_source_analysis
        key = self._source_control_key(path) if cache is not None else ""
        if cache is not None and key in cache:
            return cache[key]
        if content is None:
            content = path.read_text(encoding="utf-8", errors="ignore")
        is_cxx = path.suffix.lower() in {".cpp", ".cc", ".cxx"}
        sanitized = (
            self._mask_c_comments_and_literals(content)
            if is_cxx or "static" in content
            else ""
        )
        float_format = re.compile(
            r"%(?:[-+ #0']*)(?:\d+|\*)?(?:\.(?:\d+|\*))?"
            r"(?:hh|h|ll|l|j|z|t|L)?[aAeEfFgG]"
        )

        def uses_float_io(names: str, token: str) -> bool:
            if token not in content or "%" not in content:
                return False
            calls = re.compile(
                rf"\b(?:{names})\s*\((?P<args>.{{0,2048}}?)\)\s*;",
                re.DOTALL,
            )
            return any(
                float_format.search(match.group("args"))
                for match in calls.finditer(content)
            )

        summary = SourceAnalysis(
            uses_heap=bool(re.search(r"\b(?:malloc|calloc|realloc)\s*\(", content)),
            defines_sbrk=bool(re.search(
                r"\b_?sbrk\s*\([^;{}]*\)\s*\{", content, re.MULTILINE
            )),
            declares_sbrk=bool(re.search(
                r"\b_?sbrk\s*\([^;{}]*\)\s*(?:\{|$)", content, re.MULTILINE
            )),
            needs_irq="__disable_irq()" in content or "__enable_irq()" in content,
            printf_float=uses_float_io(
                "printf|fprintf|sprintf|snprintf|vprintf|vfprintf|vsprintf|vsnprintf",
                "printf",
            ),
            scanf_float=uses_float_io("scanf|fscanf|sscanf|vscanf|vfscanf|vsscanf", "scanf"),
            has_text=bool(content),
            has_libc_init="__libc_init_array" in content,
            migration_checks=tuple(
                bool(pattern.search(content))
                for pattern, _ in self.GCC_MIGRATION_CHECKS
            ),
            static_functions=self._find_static_function_names(
                content, sanitized=sanitized
            ),
            uint32_functions=set(re.findall(
                r"(?m)^\s*(?:static\s+)?(?:[A-Za-z_]\w*\s+)*?"
                r"([A-Za-z_]\w*)\s*\(\s*(?:__IO\s+)?uint32_t\b", content
            )) if "uint32_t" in content else set(),
            assembly_symbols=re.findall(
                r"(?i)\bblx?\s+([A-Za-z_][A-Za-z0-9_]*)\b", content
            ),
        )
        if is_cxx:
            summary.quoted_includes = re.findall(
                r'^\s*#\s*include\s*"([^"]+)"', content, re.MULTILINE
            )
            summary.class_names = set(re.findall(
                r"\b(?:class|struct|union)\s+([A-Za-z_]\w*)", sanitized
            ))
            summary.using_namespaces = set(re.findall(
                r"\busing\s+namespace\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*;", content
            ))
            summary.namespace_functions = self._cxx_namespace_function_names(
                content, sanitized=sanitized
            )
        if cache is not None:
            cache[key] = summary
        return summary

    def _gcc_newlib_heap_usage(self) -> tuple[bool, bool]:
        uses_heap = False
        defines_sbrk = False
        for source in self._gcc_source_files():
            source_path = Path(source)
            if source_path.suffix.lower() not in {".c", ".cpp", ".cc", ".cxx"}:
                continue
            try:
                summary = self._analyze_source_file(source_path)
            except OSError:
                continue
            uses_heap = uses_heap or summary.uses_heap
            defines_sbrk = defines_sbrk or summary.defines_sbrk
            if uses_heap and defines_sbrk:
                break
        return uses_heap, defines_sbrk

    @staticmethod
    def _gcc_sbrk_linker_bounds(
        linker_script: Optional[str],
    ) -> Optional[tuple[str, str, Optional[str]]]:
        if not linker_script:
            return None
        try:
            content = Path(linker_script).read_text(
                encoding="utf-8", errors="ignore"
            )
        except OSError:
            return None

        for start, limit in (
            ("__HeapBase", "__HeapLimit"),
            ("__heap_start__", "__heap_end__"),
            ("_heap_start", "_heap_end"),
        ):
            if re.search(rf"\b{re.escape(start)}\b", content) and re.search(
                rf"\b{re.escape(limit)}\b", content
            ):
                return start, limit, None

        start = next(
            (
                symbol
                for symbol in ("end", "_end", "__end__")
                if re.search(rf"\b{re.escape(symbol)}\b", content)
            ),
            None,
        )
        if (
            start
            and re.search(r"\b_estack\b", content)
            and re.search(r"\b_Min_Stack_Size\b", content)
        ):
            return start, "_estack", "_Min_Stack_Size"
        return None

    def _generate_gcc_runtime_compat_source(
        self, dest: str, linker_script: Optional[str] = None
    ) -> Optional[str]:
        needs_irq_functions = False
        for source in self._gcc_source_files():
            source_path = Path(source)
            if source_path.suffix.lower() not in {".c", ".cpp", ".cc", ".cxx"}:
                continue
            try:
                summary = self._analyze_source_file(source_path)
            except OSError:
                continue
            if summary.needs_irq:
                needs_irq_functions = True
        uses_heap, defines_sbrk = self._gcc_newlib_heap_usage()
        sbrk_bounds = (
            self._gcc_sbrk_linker_bounds(linker_script)
            if uses_heap and not defines_sbrk
            else None
        )
        if not needs_irq_functions and sbrk_bounds is None:
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
        if sbrk_bounds is not None:
            heap_start, heap_limit, stack_size = sbrk_bounds
            limit_expression = f"(uintptr_t)&{heap_limit}"
            declarations = [
                f"extern char {heap_start};",
                f"extern char {heap_limit};",
            ]
            if stack_size is not None:
                declarations.append(f"extern char {stack_size};")
                limit_expression += f" - (uintptr_t)&{stack_size}"
            fragments.append(
                "#include <errno.h>\n"
                "#include <stddef.h>\n"
                "#include <stdint.h>\n\n"
                + "\n".join(declarations)
                + "\n\nvoid *_sbrk(ptrdiff_t increment)\n"
                "{\n"
                f"    static uintptr_t current = (uintptr_t)&{heap_start};\n"
                f"    const uintptr_t base = (uintptr_t)&{heap_start};\n"
                f"    const uintptr_t limit = {limit_expression};\n"
                "    uintptr_t next;\n\n"
                "    if (increment >= 0)\n"
                "    {\n"
                "        if (current > limit || (uintptr_t)increment > limit - current)\n"
                "        {\n"
                "            errno = ENOMEM;\n"
                "            return (void *)-1;\n"
                "        }\n"
                "        next = current + (uintptr_t)increment;\n"
                "    }\n"
                "    else\n"
                "    {\n"
                "        uintptr_t decrease = (uintptr_t)(-(increment + 1)) + 1U;\n"
                "        if (current < base || decrease > current - base)\n"
                "        {\n"
                "            errno = ENOMEM;\n"
                "            return (void *)-1;\n"
                "        }\n"
                "        next = current - decrease;\n"
                "    }\n\n"
                "    void *previous = (void *)current;\n"
                "    current = next;\n"
                "    return previous;\n"
                "}\n"
            )
            self._append_warning_once(
                "GCC newlib heap 兼容: 根据链接脚本边界生成 _sbrk"
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
        sanitized = KeilProjectToCMake._mask_c_comments_and_literals(content)
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
    def _mask_c_comments_and_literals(content: str) -> str:
        marker = re.compile(
            r'(?<!\w)(?:u8|u|U|L)?R"(?P<delimiter>[^ ()\\\t\v\f\r\n]{0,16})\('
            r'|//|/\*|["\']'
        )
        line_end = re.compile(r"(?<!\\)\r|(?<![\\\r])\n")
        non_newline = re.compile(r"[^\r\n]+")
        fragments: List[str] = []
        index = 0
        # 按片段跳过普通代码，仅定位注释和字面量边界；保留字符位置与换行。
        while match := marker.search(content, index):
            delimiter = match.group("delimiter")
            token = match.group(0)
            if delimiter is not None:
                terminator = f'){delimiter}"'
                closing = content.find(terminator, match.end())
                end = len(content) if closing < 0 else closing + len(terminator)
            elif token == "//":
                closing_line = line_end.search(content, match.end())
                end = closing_line.start() if closing_line else len(content)
            elif token == "/*":
                closing = content.find("*/", match.end())
                end = len(content) if closing < 0 else closing + 2
            else:
                closing = content.find(token, match.end())
                while closing >= 0:
                    escape_start = closing
                    while (
                        escape_start > match.end()
                        and content[escape_start - 1] == "\\"
                    ):
                        escape_start -= 1
                    if (closing - escape_start) % 2 == 0:
                        break
                    closing = content.find(token, closing + 1)
                end = len(content) if closing < 0 else closing + 1
            fragments.append(content[index : match.start()])
            fragments.append(
                non_newline.sub(
                    lambda span: " " * (span.end() - span.start()),
                    content[match.start() : end],
                )
            )
            index = end
        if not fragments:
            return content
        fragments.append(content[index:])
        return "".join(fragments)

    @staticmethod
    def _find_static_function_names(
        content: str, *, sanitized: Optional[str] = None
    ) -> Set[str]:
        if "static" not in content:
            return set()
        pattern = re.compile(
            r"(?m)^\s*static\b[^\n;{}=]*?\b([A-Za-z_]\w*)\s*"
            r"\([^;\n{}]*\)[ \t]*(?:\r?\n[ \t]*)?\{"
        )
        if sanitized is None:
            sanitized = KeilProjectToCMake._mask_c_comments_and_literals(content)
        return {match.group(1) for match in pattern.finditer(sanitized)}

    @staticmethod
    def _patch_missing_uart_handle_alias(content: str) -> tuple[str, int]:
        if "UART_HandleTypeDef" not in content:
            return content, 0
        sanitized = KeilProjectToCMake._mask_c_comments_and_literals(content)
        if re.search(r"(?m)^[ \t]*#[ \t]*include\b", sanitized):
            return content, 0
        uart_handles = set(
            re.findall(
                r"\bUART_HandleTypeDef\s+([A-Za-z_]\w*)\s*;",
                sanitized,
            )
        )
        if len(uart_handles) != 1:
            return content, 0
        declared_handles = set(
            re.findall(
                r"\b[A-Za-z_]\w*_HandleTypeDef\s+"
                r"([A-Za-z_]\w*)\s*(?:=|;)",
                sanitized,
            )
        )
        selected = next(iter(uart_handles))
        uart_call = re.compile(
            r"(?P<prefix>\b(?:__HAL_UART|HAL_UART(?:Ex)?)_[A-Za-z0-9_]+"
            r"\s*\(\s*)&(?P<handle>[A-Za-z_]\w*Handle)\b"
        )
        replacements = [
            match.span("handle")
            for match in uart_call.finditer(sanitized)
            if match.group("handle") not in declared_handles
        ]
        for start, end in reversed(replacements):
            content = content[:start] + selected + content[end:]
        return content, len(replacements)

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
                self._stage_gcc_include_directory(
                    self._gcc_freertos_port(Path(include_path)), dest
                )
                for include_path in self._ordered_include_paths()
            ]
        )

    def _generate_legacy_stdint_compat_headers(self, dest: str) -> List[str]:
        candidates: List[Path] = [
            Path(header) for header in self.header_files if Path(header).is_file()
        ]
        for include_path in [
            *self._ordered_include_paths(),
            *self._source_local_include_paths(),
        ]:
            include_dir = Path(include_path)
            if include_dir.is_dir():
                candidates.extend(include_dir.glob("*.h"))

        compat_dir = Path(dest) / "cmake" / "compat-includes"
        generated_names: Set[str] = set()
        forced_names: Set[str] = set()
        inline_forced_names: Set[str] = set()
        uint32_function_names: Set[str] = set()
        static_function_names: Set[str] = set()
        for source in self.source_files:
            try:
                summary = self._analyze_source_file(Path(source))
            except OSError:
                continue
            uint32_function_names.update(summary.uint32_functions)
            static_function_names.update(summary.static_functions)
        include_pattern = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)
        header_references: Dict[str, List[str]] = {}
        canonical_candidates = [str(self._resolved_path(path)) for path in candidates]
        for header in self._dedupe_keep_order(canonical_candidates):
            header_path = Path(header)
            try:
                content = header_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            header_references[header] = [
                Path(included.replace("\\", "/")).name.lower()
                for included in include_pattern.findall(content)
            ]
            if header_path.name.lower() in generated_names:
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
        # 保留原候选列表的重复计数，避免改变强制包含头文件的排序。
        for header in canonical_candidates:
            for included_name in header_references.get(header, []):
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

    @staticmethod
    def _cxx_namespace_function_names(
        content: str, *, sanitized: Optional[str] = None,
    ) -> List[Tuple[str, str, str, str, str]]:
        """Find namespace-qualified function definitions in C++ source text."""
        if sanitized is None:
            sanitized = KeilProjectToCMake._mask_c_comments_and_literals(content)
        pattern = re.compile(
            r"(?ms)^[ \t]*(?P<prefix>[^{}\n;]*?)"
            r"\b(?P<qualified>(?:[A-Za-z_]\w*::)+"
            r"(?P<name>[A-Za-z_]\w*))\s*\("
            r"(?P<parameters>[^(){};]*)\)\s*"
            r"(?P<suffix>(?:(?:const|volatile|&{1,2}|"
            r"noexcept(?:\s*\([^)]*\))?)[ \t\r\n]*)*)\{"
        )
        functions: List[Tuple[str, str, str, str, str]] = []
        for match in pattern.finditer(sanitized):
            prefix = re.sub(
                r"\b(?:inline|static|virtual|constexpr|consteval|"
                r"extern|friend|typename)\b",
                " ",
                match.group("prefix"),
            )
            prefix = re.sub(r"\s+", " ", prefix).strip()
            if not prefix or any(
                token in prefix.split()
                for token in ("if", "for", "while", "switch", "return")
            ):
                continue
            namespace, name = match.group("qualified").rsplit("::", 1)
            functions.append(
                (
                    namespace,
                    name,
                    prefix,
                    match.group("parameters").strip(),
                    match.group("suffix").strip(),
                )
            )
        return functions

    @staticmethod
    def _standard_cxx_type_tokens(expression: str) -> Optional[str]:
        allowed = {
            "bool",
            "char",
            "char8_t",
            "char16_t",
            "char32_t",
            "const",
            "double",
            "float",
            "int",
            "int8_t",
            "int16_t",
            "int32_t",
            "int64_t",
            "intptr_t",
            "long",
            "ptrdiff_t",
            "restrict",
            "short",
            "signed",
            "size_t",
            "unsigned",
            "volatile",
            "void",
            "wchar_t",
            "uint8_t",
            "uint16_t",
            "uint32_t",
            "uint64_t",
            "uintptr_t",
            "__I",
            "__IO",
            "__O",
            "__restrict",
            "__restrict__",
            "_Bool",
        }
        normalized = re.sub(r"\b(?:__I|__IO|__O)\b", " ", expression)
        identifiers = re.findall(r"\b[A-Za-z_]\w*\b", normalized)
        if not identifiers or any(
            identifier not in allowed for identifier in identifiers
        ):
            return None
        return re.sub(r"\s+", " ", normalized).strip()

    @classmethod
    def _standard_cxx_parameter_type(cls, parameter: str) -> Optional[str]:
        parameter = re.sub(r"\s*=.*$", "", parameter, flags=re.DOTALL).strip()
        parameter = re.sub(r"\b(?:__I|__IO|__O)\b", " ", parameter)
        parameter = re.sub(r"\s+", " ", parameter).strip()
        if parameter in {"", "void"}:
            return parameter or "void"
        direct_type = cls._standard_cxx_type_tokens(parameter)
        if direct_type is not None:
            return direct_type

        candidate = parameter
        type_words = {
            "bool",
            "char",
            "double",
            "float",
            "int",
            "long",
            "short",
            "signed",
            "unsigned",
            "void",
            "wchar_t",
        }
        array_match = re.search(r"(?P<array>(?:\s*\[[^\]]*\])+)$", parameter)
        array_suffix = array_match.group("array") if array_match else ""
        parameter_core = (
            parameter[: array_match.start()].rstrip()
            if array_match
            else parameter
        )
        name_match = re.search(r"(?P<name>[A-Za-z_]\w*)\s*$", parameter_core)
        if name_match and name_match.group("name") not in type_words:
            type_prefix = parameter_core[: name_match.start()].rstrip()
            if type_prefix and not type_prefix.endswith("::"):
                candidate = type_prefix + array_suffix
        return cls._standard_cxx_type_tokens(candidate)

    @classmethod
    def _standard_namespace_function_key(
        cls, namespace: str, name: str, parameters: str
    ) -> Optional[Tuple[str, str, Tuple[str, ...]]]:
        if not re.fullmatch(
            r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*", namespace
        ) or not re.fullmatch(r"[A-Za-z_]\w*", name):
            return None
        values = cls._split_cxx_parameters(parameters)
        normalized: List[str] = []
        if values and values != ["void"]:
            for value in values:
                parameter_type = cls._standard_cxx_parameter_type(value)
                if parameter_type is None:
                    return None
                normalized.append(parameter_type)
        return namespace, name, tuple(normalized)

    @classmethod
    def _standard_namespace_declaration_signature(
        cls,
        namespace: str,
        name: str,
        return_type: str,
        parameters: str,
        suffix: str = "",
    ) -> Optional[str]:
        """Build a declaration only when all types are self-contained."""
        if not re.fullmatch(
            r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*", namespace
        ) or not re.fullmatch(r"[A-Za-z_]\w*", name):
            return None
        normalized_return = cls._standard_cxx_type_tokens(return_type)
        if normalized_return is None:
            return None

        values = KeilProjectToCMake._split_cxx_parameters(parameters)
        normalized_parameters: List[str] = []
        if values and values != ["void"]:
            for value in values:
                if cls._standard_cxx_parameter_type(value) is None:
                    return None
                value = re.sub(r"\s*=.*$", "", value, flags=re.DOTALL)
                value = re.sub(r"\b(?:__I|__IO|__O)\b", " ", value)
                normalized_parameters.append(re.sub(r"\s+", " ", value).strip())

        normalized_suffix = re.sub(r"\s+", " ", suffix).strip()
        if normalized_suffix and not re.fullmatch(
            r"(?:const|volatile|&|&&|noexcept(?:\s*\([^)]*\))?)(?:\s+"
            r"(?:const|volatile|&|&&|noexcept(?:\s*\([^)]*\))?))*",
            normalized_suffix,
        ):
            return None
        return (
            f"{normalized_return} {name}({', '.join(normalized_parameters)})"
            f"{(' ' + normalized_suffix) if normalized_suffix else ''};"
        )

    @staticmethod
    def _split_cxx_parameters(parameters: str) -> List[str]:
        values: List[str] = []
        current: List[str] = []
        depth = 0
        for character in parameters:
            if character in "([{<":
                depth += 1
            elif character in ")]}>" and depth:
                depth -= 1
            if character == "," and depth == 0:
                value = "".join(current).strip()
                if value:
                    values.append(value)
                current.clear()
            else:
                current.append(character)
        value = "".join(current).strip()
        if value:
            values.append(value)
        return values

    @staticmethod
    def _gcc_declared_namespace_functions(
        contents: List[str], *, already_sanitized: bool = False,
    ) -> Set[Tuple[str, str, Tuple[str, ...]]]:
        declared: Set[Tuple[str, str, Tuple[str, ...]]] = set()
        namespace_start_pattern = re.compile(
            r"\bnamespace\s+(?P<namespace>[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)"
            r"\s*\{"
        )
        class_start_pattern = re.compile(
            r"\b(?:class|struct|union)\s+[A-Za-z_]\w*"
            r"(?:\s*:[^{]+)?\s*\{"
        )
        qualified_pattern = re.compile(
            r"\b(?P<qualified>(?:[A-Za-z_]\w*::)+[A-Za-z_]\w*)\s*"
            r"\((?P<parameters>[^;{}]*)\)\s*(?:const|volatile|&{1,2}|"
            r"noexcept(?:\s*\([^)]*\))?)*\s*;"
        )

        def matching_brace(text: str, opening: int) -> int:
            depth = 1
            for index in range(opening + 1, len(text)):
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                    if depth == 0:
                        return index
            return -1

        def mask_spans(text: str, spans: List[Tuple[int, int]]) -> str:
            if not spans:
                return text
            masked = list(text)
            for start, end in spans:
                for index in range(start, min(end, len(masked))):
                    if masked[index] not in "\r\n":
                        masked[index] = " "
            return "".join(masked)

        def scan_namespace_block(body: str, namespace: str) -> None:
            nested_spans: List[Tuple[int, int]] = []
            cursor = 0
            while True:
                namespace_match = namespace_start_pattern.search(body, cursor)
                if namespace_match is None:
                    break
                opening = namespace_match.end() - 1
                closing = matching_brace(body, opening)
                if closing < 0:
                    break
                nested_name = namespace_match.group("namespace")
                full_namespace = (
                    f"{namespace}::{nested_name}"
                    if namespace
                    else nested_name
                )
                scan_namespace_block(
                    body[opening + 1 : closing], full_namespace
                )
                nested_spans.append((namespace_match.start(), closing + 1))
                cursor = closing + 1

            class_spans: List[Tuple[int, int]] = []
            for class_match in class_start_pattern.finditer(body):
                opening = class_match.end() - 1
                closing = matching_brace(body, opening)
                if closing >= 0:
                    class_spans.append((class_match.start(), closing + 1))

            visible_body = mask_spans(body, [*nested_spans, *class_spans])
            for function_match in re.finditer(
                r"\b(?P<name>[A-Za-z_]\w*)\s*"
                r"\((?P<parameters>[^;{}]*)\)\s*"
                r"(?:const|volatile|&{1,2}|"
                r"noexcept(?:\s*\([^)]*\))?)*\s*;",
                visible_body,
            ):
                name = function_match.group("name")
                if name in {"if", "for", "while", "switch", "catch"}:
                    continue
                key = KeilProjectToCMake._standard_namespace_function_key(
                    namespace,
                    name,
                    function_match.group("parameters"),
                )
                if key is not None:
                    declared.add(key)

        for content in contents:
            sanitized = (
                content if already_sanitized
                else KeilProjectToCMake._mask_c_comments_and_literals(content)
            )
            for match in qualified_pattern.finditer(sanitized):
                namespace, name = match.group("qualified").rsplit("::", 1)
                key = KeilProjectToCMake._standard_namespace_function_key(
                    namespace, name, match.group("parameters")
                )
                if key is not None:
                    declared.add(key)
            cursor = 0
            while True:
                namespace_match = namespace_start_pattern.search(
                    sanitized, cursor
                )
                if namespace_match is None:
                    break
                opening = namespace_match.end() - 1
                closing = matching_brace(sanitized, opening)
                if closing < 0:
                    break
                scan_namespace_block(
                    sanitized[opening + 1 : closing],
                    namespace_match.group("namespace"),
                )
                cursor = closing + 1
        return declared

    def _gcc_missing_namespace_declarations(
        self, source_files: Optional[List[str]] = None
    ) -> List[Tuple[str, str, str]]:
        source_paths = [
            Path(path)
            for path in (source_files or self.source_files)
            if Path(path).suffix.lower() in {".cpp", ".cc", ".cxx"}
        ]
        source_summaries: List[Tuple[Path, SourceAnalysis]] = []
        for path in source_paths:
            try:
                source_summaries.append((path, self._analyze_source_file(path)))
            except OSError:
                continue

        include_dirs = [
            Path(path)
            for path in self._ordered_include_paths()
            if Path(path).is_dir()
        ]
        include_pattern = re.compile(
            r"^\s*#\s*include\s*\"([^\"]+)\"", re.MULTILINE
        )
        header_suffixes = {".h", ".hh", ".hpp", ".hxx", ".inc"}
        include_cache: Dict[Tuple[Path, str], Optional[Path]] = {}

        def resolve_include(owner: Path, included: str) -> Optional[Path]:
            key = (owner.parent, included)
            if key in include_cache:
                return include_cache[key]
            relative = Path(included.replace("\\", "/"))
            candidates = [owner.parent / relative]
            candidates.extend(directory / relative for directory in include_dirs)
            selected = next(
                (
                    self._resolved_path(candidate)
                    for candidate in candidates
                    if candidate.is_file()
                ),
                None,
            )
            include_cache[key] = selected
            return selected

        pending: List[Tuple[Path, int]] = [
            (Path(path), 0)
            for path in self.header_files
            if Path(path).is_file()
        ]
        for source_path, summary in source_summaries:
            for included in summary.quoted_includes:
                selected = resolve_include(source_path, included)
                if selected is not None and selected.suffix.lower() in header_suffixes:
                    pending.append((selected, 1))

        seen: Set[str] = set()
        declared: Set[Tuple[str, str, Tuple[str, ...]]] = set()
        class_names: Set[str] = set()
        for _, summary in source_summaries:
            class_names.update(summary.class_names)
        while pending:
            path, depth = pending.pop()
            path = self._resolved_path(path)
            key = self._source_control_key(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if path.suffix.lower() in header_suffixes:
                sanitized = self._mask_c_comments_and_literals(content)
                declared.update(self._gcc_declared_namespace_functions(
                    [sanitized], already_sanitized=True
                ))
                class_names.update(re.findall(
                    r"\b(?:class|struct|union)\s+([A-Za-z_]\w*)", sanitized
                ))
            if depth >= 2:
                continue
            for included in include_pattern.findall(content):
                selected = resolve_include(path, included)
                if selected is not None and selected.suffix.lower() in header_suffixes:
                    pending.append((selected, depth + 1))

        declared_aliases = set(declared)
        using_namespaces = {
            namespace
            for _, summary in source_summaries
            for namespace in summary.using_namespaces
        }
        for declared_namespace, name, parameters in declared:
            for using_namespace in using_namespaces:
                prefix = f"{using_namespace}::"
                if declared_namespace.startswith(prefix):
                    alias_namespace = declared_namespace[len(prefix) :]
                    if alias_namespace:
                        declared_aliases.add(
                            (alias_namespace, name, parameters)
                        )
        missing: List[Tuple[str, str, str]] = []
        seen_functions: Set[Tuple[str, str, Tuple[str, ...]]] = set()
        for _, summary in source_summaries:
            for namespace, name, return_type, parameters, suffix in (
                summary.namespace_functions
            ):
                if namespace.rsplit("::", 1)[-1] in class_names:
                    continue
                declaration = self._standard_namespace_declaration_signature(
                    namespace, name, return_type, parameters, suffix
                )
                if declaration is None:
                    continue
                key = self._standard_namespace_function_key(
                    namespace, name, parameters
                )
                if key is None:
                    continue
                if (
                    key in declared_aliases
                    or key in seen_functions
                ):
                    continue
                seen_functions.add(key)
                missing.append((namespace, name, declaration))
        return missing

    def _generate_gcc_cxx_compat_header(
        self,
        dest: str,
        forced_headers: Optional[List[str]] = None,
        source_files: Optional[List[str]] = None,
    ) -> str:
        missing_namespace_declarations = self._gcc_missing_namespace_declarations(
            source_files
        )
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
        if missing_namespace_declarations:
            lines.extend(
                [
                    "#include <stddef.h>",
                    "#include <stdint.h>",
                    "",
                    "#ifdef __cplusplus",
                ]
            )
            namespaces: Dict[str, List[str]] = {}
            for namespace, _name, declaration in missing_namespace_declarations:
                namespaces.setdefault(namespace, []).append(declaration)
            for namespace, declarations in namespaces.items():
                indent = ""
                components = namespace.split("::")
                for component in components:
                    lines.append(f"{indent}namespace {component} {{")
                    indent += "    "
                for declaration in declarations:
                    lines.append(f"{indent}{declaration}")
                for _component in components:
                    indent = indent[:-4]
                    lines.append(f"{indent}}}")
            lines.extend(["#endif", ""])
            self._append_warning_once(
                "GCC C++ 缺失命名空间声明补充: "
                + ", ".join(
                    f"{namespace}::{name}"
                    for namespace, name, _declaration
                    in missing_namespace_declarations
                )
            )
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
            search_root = self._project_content_root()
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
        generated_linker = output_path / "cmake" / "generated_linker.ld"
        linker_dir = output_path / "cmake" / "linker"

        if source_path == generated_linker:
            self._augment_nordic_linker_script(source_path)
            return str(source_path)
        try:
            source_path.relative_to(linker_dir)
            self._augment_nordic_linker_script(source_path)
            return str(source_path)
        except ValueError:
            pass

        linker_dir.mkdir(parents=True, exist_ok=True)
        copied: Dict[Path, Path] = {}
        target_owners: Dict[Path, Path] = {}
        include_pattern = re.compile(
            r'(?mi)^(?P<prefix>\s*INCLUDE\s+)'
            r'(?:(?:"(?P<double>[^"]+)")|(?:\'(?P<single>[^\']+)\')|'
            r'(?P<bare>[^\s;]+))(?P<suffix>[^\r\n]*)$'
        )

        def copy_with_includes(current: Path) -> Path:
            current = current.resolve(strict=False)
            if current in copied:
                return copied[current]

            target = linker_dir / current.name
            owner = target_owners.get(target)
            if owner is not None and owner != current:
                digest = hashlib.sha1(str(current).encode("utf-8")).hexdigest()[:12]
                target = linker_dir / (
                    f"{self._safe_ascii_file_stem(current.stem)}_{digest}"
                    f"{current.suffix}"
                )
            copied[current] = target
            target_owners[target] = current

            try:
                content = current.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                shutil.copy2(current, target)
                return target

            def replace_include(match: re.Match[str]) -> str:
                include_name = (
                    match.group("double")
                    or match.group("single")
                    or match.group("bare")
                )
                include_path = Path(include_name)
                if not include_path.is_absolute():
                    include_path = current.parent / include_path
                include_path = include_path.resolve(strict=False)
                if include_path.is_file():
                    included_target = copy_with_includes(include_path)
                    staged_name = included_target.relative_to(linker_dir).as_posix()
                    return (
                        f'{match.group("prefix")}"{staged_name}"'
                        f'{match.group("suffix")}'
                    )

                self.warnings.append(
                    f"GCC 链接脚本依赖未找到: {include_name} ({current})"
                )
                return match.group(0)

            content = include_pattern.sub(replace_include, content)
            self._write_text_file(target, content)
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

    def _read_slib_layout(self):
        if not self.linker_script or not self._clean_device_name().upper().startswith("AT32F415"):
            return None
        scatter = Path(self.linker_script)
        if not scatter.is_file() or scatter.suffix.lower() != ".sct":
            return None
        text = scatter.read_text(encoding="utf-8")
        has_regions = re.search(
            r"\b(?:LR|ER)_SLIB_", re.sub(r";[^\r\n]*", "", text), re.I
        )
        has_symbols = any(
            self._arm_linker_symdefs_content(Path(path)) is not None
            for path in self.linker_inputs
        )
        if not has_regions and not (has_symbols and re.search(r"\bSLIB\b", text, re.I)):
            return None
        from slib_layout import parse_layout, read_symdefs

        export_flag = self._extract_first_flag(self.linker_flags, "--symdefs=")
        if export_flag:
            symbol_file = Path(export_flag.split("=", 1)[1].strip('"'))
            if not symbol_file.is_absolute():
                symbol_file = Path(self.project_dir) / symbol_file
            symbols = read_symdefs(symbol_file.read_text(encoding="utf-8"))
        else:
            symbol_files = [Path(path) for path in self.linker_inputs
                            if self._arm_linker_symdefs_content(Path(path)) is not None]
            if len(symbol_files) != 1:
                raise ValueError("SLIB 调用工程需要唯一的函数符号表")
            symbols = read_symdefs(symbol_files[0].read_text(encoding="utf-8"))
        if None in (self.flash_origin, self.flash_length, self.ram_origin, self.ram_length):
            raise ValueError("SLIB 转换需要完整的器件 Flash 与 RAM 容量")
        return parse_layout(text, symbols, (self.flash_origin, self.flash_length),
                            (self.ram_origin, self.ram_length))

    def _prepare_slib_sources(self, layout, source_files: List[str], dest: str,
                              linker_script: str) -> List[str]:
        from slib_layout import render_linker, runtime_source

        objects: Dict[str, str] = {}
        protected: List[str] = []
        selected = set(layout.code_objects + layout.data_objects + layout.ram_objects)
        for source in source_files:
            origin = self._prepared_source_origins.get(self._source_control_key(source), source)
            name = Path(origin).stem.lower()
            if name not in selected:
                continue
            if name in objects:
                raise ValueError(f"SLIB SCT 对象对应多个源文件: {name}")
            objects[name] = Path(source).name
            if name in layout.code_objects:
                protected.append(source)
        if set(objects) != selected:
            raise ValueError(f"SLIB SCT 对象缺少源文件: {sorted(selected - set(objects))}")
        linker = Path(linker_script)
        self._write_text_file(linker, render_linker(layout, linker.read_text(encoding="utf-8"), objects))
        runtime = Path(dest) / "cmake" / "sources" / "slib_ram_init.c"
        self._write_text_file(runtime, runtime_source(layout))
        source_files.append(str(runtime.resolve()))
        self._record_asset("slib_layout", linker, "generated", "review_required",
                           f"Converted static SLIB regions and fixed entry contract from {self.linker_script}")
        self._append_warning_once("SLIB 分区与专用 RAM 按 SCT 生成，函数入口由链接器校验")
        return protected

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

    def _write_gcc_cxx_multilib_detection(
        self, f: TextIOWrapper, cpu_options: List[str]
    ) -> None:
        f.write("# 根据当前工具链推导 GCC C++ multilib 头文件目录。\n")
        f.write("execute_process(\n")
        f.write('    COMMAND "${CMAKE_CXX_COMPILER}" -print-sysroot\n')
        f.write("    OUTPUT_VARIABLE ARM_GCC_SYSROOT\n")
        f.write("    OUTPUT_STRIP_TRAILING_WHITESPACE\n")
        f.write("    ERROR_QUIET\n")
        f.write(")\n")
        f.write('if(ARM_GCC_SYSROOT STREQUAL "")\n')
        f.write(
            '    get_filename_component(ARM_GCC_BIN_DIR '
            '"${CMAKE_CXX_COMPILER}" DIRECTORY)\n'
        )
        f.write(
            '    get_filename_component(ARM_GCC_ROOT '
            '"${ARM_GCC_BIN_DIR}" DIRECTORY)\n'
        )
        f.write('    set(ARM_GCC_SYSROOT "${ARM_GCC_ROOT}/arm-none-eabi")\n')
        f.write("endif()\n")
        f.write(
            'file(TO_CMAKE_PATH "${ARM_GCC_SYSROOT}" ARM_GCC_SYSROOT)\n'
        )
        f.write("execute_process(\n")
        f.write('    COMMAND "${CMAKE_CXX_COMPILER}" -dumpfullversion\n')
        f.write("    OUTPUT_VARIABLE ARM_GCC_VERSION\n")
        f.write("    OUTPUT_STRIP_TRAILING_WHITESPACE\n")
        f.write("    ERROR_QUIET\n")
        f.write(")\n")
        f.write('if(ARM_GCC_VERSION STREQUAL "")\n')
        f.write("    execute_process(\n")
        f.write('        COMMAND "${CMAKE_CXX_COMPILER}" -dumpversion\n')
        f.write("        OUTPUT_VARIABLE ARM_GCC_VERSION\n")
        f.write("        OUTPUT_STRIP_TRAILING_WHITESPACE\n")
        f.write("        COMMAND_ERROR_IS_FATAL ANY\n")
        f.write("    )\n")
        f.write("endif()\n")
        f.write("execute_process(\n")
        f.write('    COMMAND "${CMAKE_CXX_COMPILER}"\n')
        for option in cpu_options:
            f.write(f"        {option}\n")
        f.write("        -print-multi-directory\n")
        f.write("    OUTPUT_VARIABLE ARM_GCC_MULTILIB\n")
        f.write("    OUTPUT_STRIP_TRAILING_WHITESPACE\n")
        f.write("    COMMAND_ERROR_IS_FATAL ANY\n")
        f.write(")\n")
        f.write('string(REPLACE "\\\\" "/" ARM_GCC_MULTILIB '
                '"${ARM_GCC_MULTILIB}")\n')
        f.write(
            'set(ARM_GXX_STDINC '
            '"${ARM_GCC_SYSROOT}/include/c++/${ARM_GCC_VERSION}")\n'
        )
        f.write(
            'set(ARM_GXX_TARGET_STDINC "${ARM_GXX_STDINC}/arm-none-eabi")\n'
        )
        f.write(
            'set(ARM_GXX_MULTILIB_STDINC '
            '"${ARM_GXX_TARGET_STDINC}/${ARM_GCC_MULTILIB}")\n'
        )
        f.write(
            'if(NOT EXISTS "${ARM_GXX_MULTILIB_STDINC}/bits/c++config.h")\n'
        )
        f.write('    set(ARM_GCC_COMPAT_MULTILIB "${ARM_GCC_MULTILIB}")\n')
        f.write(
            '    string(REPLACE "v7e-m+fp/" "v7+fp/" '
            'ARM_GCC_COMPAT_MULTILIB "${ARM_GCC_COMPAT_MULTILIB}")\n'
        )
        f.write(
            '    string(REPLACE "v7e-m+dp/" "v7+fp/" '
            'ARM_GCC_COMPAT_MULTILIB "${ARM_GCC_COMPAT_MULTILIB}")\n'
        )
        f.write(
            '    if(EXISTS "${ARM_GXX_TARGET_STDINC}/'
            '${ARM_GCC_COMPAT_MULTILIB}/bits/c++config.h")\n'
        )
        f.write(
            '        set(ARM_GXX_MULTILIB_STDINC '
            '"${ARM_GXX_TARGET_STDINC}/${ARM_GCC_COMPAT_MULTILIB}")\n'
        )
        f.write("    endif()\n")
        f.write("endif()\n\n")

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
            "-fshort-wchar",
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
        for source in self._gcc_source_files():
            source_path = Path(source)
            if source_path.suffix.lower() not in {".c", ".cpp", ".cc", ".cxx"}:
                continue
            try:
                summary = self._analyze_source_file(source_path)
            except OSError:
                continue
            symbols.extend(summary.assembly_symbols)
        return sorted(self._dedupe_keep_order(symbols))

    def _gcc_uses_printf_float(self) -> bool:
        printf_float, _ = self._gcc_formatted_io_float_requirements()
        return printf_float

    def _gcc_uses_scanf_float(self) -> bool:
        _, scanf_float = self._gcc_formatted_io_float_requirements()
        return scanf_float

    def _gcc_formatted_io_float_requirements(self) -> tuple[bool, bool]:
        uses_printf_float = False
        uses_scanf_float = False
        for source in self._gcc_source_files():
            source_path = Path(source)
            if source_path.suffix.lower() not in {".c", ".cpp", ".cc", ".cxx"}:
                continue
            try:
                summary = self._analyze_source_file(source_path)
            except OSError:
                continue
            uses_printf_float = uses_printf_float or summary.printf_float
            uses_scanf_float = uses_scanf_float or summary.scanf_float
            if uses_printf_float and uses_scanf_float:
                break
        return uses_printf_float, uses_scanf_float

    def _gcc_include_reference(self, include_path: str, dest: str) -> str:
        return self._portable_cmake_reference(
            include_path,
            dest,
            "头文件目录",
            map_pack_include=True,
        )

    def _append_warning_once(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def _analyze_gcc_migration_issues(
        self, prepared_sources: List[str], linker_script: str
    ) -> None:
        source_summaries: List[tuple[Path, SourceAnalysis]] = []
        for source in prepared_sources:
            path = Path(source)
            if path.suffix.lower() not in {
                ".c",
                ".cpp",
                ".cc",
                ".cxx",
                ".s",
                ".asm",
            }:
                continue
            try:
                source_summaries.append((path, self._analyze_source_file(path)))
            except OSError:
                continue

        cxx_project = "CXX" in self._project_languages()
        startup_summaries = [
            summary
            for path, summary in source_summaries
            if path.name.lower().startswith("startup_")
        ]
        if (
            cxx_project
            and any(summary.has_text for summary in startup_summaries)
            and not any(summary.has_libc_init for summary in startup_summaries)
        ):
            self._append_warning_once(
                "迁移检查: C++ 工程启动文件未引用 __libc_init_array，"
                "全局对象构造函数可能不会执行"
            )

        try:
            linker_text = Path(linker_script).read_text(
                encoding="utf-8", errors="ignore"
            )
        except OSError:
            linker_text = ""
        if cxx_project and linker_text:
            missing_arrays = [
                section
                for section in (".preinit_array", ".init_array", ".fini_array")
                if section not in linker_text
            ]
            if missing_arrays:
                self._append_warning_once(
                    "迁移检查: C++ 链接脚本缺少构造析构表: "
                    + ", ".join(missing_arrays)
                )
            if ".ARM.exidx" not in linker_text:
                self._append_warning_once(
                    "迁移检查: 链接脚本缺少 .ARM.exidx，"
                    "异常展开或回溯功能可能不完整"
                )

        for index, (_, warning) in enumerate(self.GCC_MIGRATION_CHECKS):
            if any(summary.migration_checks[index] for _, summary in source_summaries):
                self._append_warning_once(warning)

        uses_heap = any(summary.uses_heap for _, summary in source_summaries)
        defines_sbrk = any(summary.declares_sbrk for _, summary in source_summaries)
        if uses_heap and not defines_sbrk:
            self._append_warning_once(
                "迁移检查: 工程调用 newlib 动态内存函数但未发现 _sbrk 实现，"
                "分配操作可能始终失败"
            )

    def _effective_defines(self, compiler: str) -> List[str]:
        defines: List[str] = []
        for define in sorted(self.defines):
            value = define.strip()
            if value.startswith("-D"):
                value = value[2:]
            if value:
                defines.append(value)

        defines = self._dedupe_keep_order(defines + self._inferred_device_defines())
        return [
            define
            for define in defines
            if self._macro_name(define) not in self.undefines
        ]

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
        if backend == "pyocd":
            return "daplink"
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

    def _default_openocd_target_script(self) -> Optional[str]:
        clean_device = self._clean_device_name().upper()
        vendor = (self.vendor or "").upper()

        if "ARTERY" in vendor or clean_device.startswith("AT32"):
            return "target/artery/at32f4x.cfg"
        if clean_device.startswith("STM32F0"):
            return "target/stm32f0x.cfg"
        if clean_device.startswith("STM32F1"):
            return "target/stm32f1x.cfg"
        if clean_device.startswith("STM32F2"):
            return "target/stm32f2x.cfg"
        if clean_device.startswith("STM32F3"):
            return "target/stm32f3x.cfg"
        if clean_device.startswith("STM32F4"):
            return "target/stm32f4x.cfg"
        if clean_device.startswith("STM32G0"):
            return "target/stm32g0x.cfg"
        if clean_device.startswith("STM32G4"):
            return "target/stm32g4x.cfg"
        if clean_device.startswith("STM32F7"):
            return "target/stm32f7x.cfg"
        if clean_device.startswith("STM32H7"):
            return "target/stm32h7x.cfg"
        if clean_device.startswith("STM32L4"):
            return "target/stm32l4x.cfg"
        if clean_device.startswith("STM32L0"):
            return "target/stm32l0.cfg"
        if clean_device.startswith("STM32L1"):
            return "target/stm32l1.cfg"
        if clean_device.startswith("STM32U5"):
            return "target/stm32u5x.cfg"
        if clean_device.startswith("STM32WBA"):
            return "target/stm32wbax.cfg"
        if clean_device.startswith("STM32WB"):
            return "target/stm32wbx.cfg"
        if clean_device.startswith("STM32WL"):
            return "target/stm32wlx.cfg"
        if clean_device.startswith("NRF51"):
            return "target/nordic/nrf51.cfg"
        if clean_device.startswith("NRF52"):
            return "target/nordic/nrf52.cfg"
        if clean_device.startswith("NRF53"):
            return "target/nordic/nrf53.cfg"
        if clean_device.startswith("NRF54L"):
            return "target/nordic/nrf54l.cfg"
        if clean_device.startswith("NRF91"):
            return "target/nordic/nrf91.cfg"
        if clean_device.startswith("MSP432"):
            return "target/ti_msp432.cfg"
        return None

    def _write_openocd_jlink_swd_wrapper(
        self, output_path: Path, target_script: str
    ) -> str:
        wrapper = output_path / ".vscode" / "openocd-jlink-swd.cfg"
        self._write_text_file(
            wrapper,
            "source [find interface/jlink.cfg]\n"
            "transport select swd\n"
            f"source [find {target_script}]\n",
        )
        relative = self._relative_workspace_path(wrapper, output_path)
        return f"${{workspaceFolder}}/{relative}"

    def _resolve_openocd_config_arguments(
        self, output_path: Path, probe: str
    ) -> tuple[str, List[str]]:
        probe, openocd_cfg = self._resolve_openocd_probe_and_config(probe)
        probe_label = self._probe_display_name(probe)
        if openocd_cfg is not None:
            return probe_label, [self._relative_workspace_path(openocd_cfg, output_path)]
        target_script = self._default_openocd_target_script()
        if target_script is None:
            warning = (
                f"OpenOCD 目标脚本无法确定，跳过 {probe_label} 配置: "
                f"{self.device_name or self.target_name}"
            )
            if warning not in self.warnings:
                self.warnings.append(warning)
            return probe_label, []
        if probe == "jlink":
            return probe_label, [
                self._write_openocd_jlink_swd_wrapper(output_path, target_script)
            ]
        return probe_label, [
            self._default_openocd_interface_script(probe),
            target_script,
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
        if self._generation_source_analysis is not None:
            self._generation_source_analysis.pop(self._source_control_key(path), None)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

        if path.suffix == ".sh":
            os.chmod(path, 0o755)

    def _build_script_content(self, options: GenerationOptions) -> str:
        build_dir = options.build_dir
        generator = options.generator
        multi_config = _is_multi_config_generator(generator)

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

            if options.compiler == "gcc":
                build_config = ' --config "%BUILD_TYPE%"' if multi_config else ""
                lines.extend(
                    [
                        'set "BUILD_TYPE=Debug"',
                        f'set "ACTIVE_BUILD_DIR={build_dir}"',
                        'set "BUILD_SWITCH="',
                        'if /I "%~1"=="release" (',
                        '    set "BUILD_TYPE=Release"',
                        f'    set "ACTIVE_BUILD_DIR={build_dir}-release"',
                        '    if /I "%~2"=="rebuild" set "BUILD_SWITCH=--clean-first"',
                        ') else if /I "%~1"=="rebuild" (',
                        '    set "BUILD_SWITCH=--clean-first"',
                        ')',
                        "",
                        f'cmake -S . -B "%ACTIVE_BUILD_DIR%" -G "{generator}" -DCMAKE_BUILD_TYPE=%BUILD_TYPE%',
                        "if errorlevel 1 exit /b %errorlevel%",
                        f'cmake --build "%ACTIVE_BUILD_DIR%"{build_config} %BUILD_SWITCH% --parallel',
                        "endlocal",
                        "",
                    ]
                )
            else:
                build_config = " --config Debug" if multi_config else ""
                lines.extend(
                    [
                        f'cmake -S . -B "{build_dir}" -G "{generator}" -DCMAKE_BUILD_TYPE=Debug',
                        "if errorlevel 1 exit /b %errorlevel%",
                        'set "BUILD_SWITCH="',
                        'if /I "%~1"=="rebuild" set "BUILD_SWITCH=--clean-first"',
                        f'cmake --build "{build_dir}"{build_config} %BUILD_SWITCH% --parallel',
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

        if options.compiler == "gcc":
            build_config = ' --config "$BUILD_TYPE"' if multi_config else ""
            lines.extend(
                [
                    'BUILD_TYPE="Debug"',
                    f'ACTIVE_BUILD_DIR="{build_dir}"',
                    'BUILD_SWITCH=""',
                    'if [ "${1:-}" = "release" ]; then',
                    '    BUILD_TYPE="Release"',
                    f'    ACTIVE_BUILD_DIR="{build_dir}-release"',
                    '    if [ "${2:-}" = "rebuild" ]; then BUILD_SWITCH="--clean-first"; fi',
                    'elif [ "${1:-}" = "rebuild" ]; then',
                    '    BUILD_SWITCH="--clean-first"',
                    "fi",
                    "",
                    f'cmake -S . -B "$ACTIVE_BUILD_DIR" -G "{generator}" -DCMAKE_BUILD_TYPE="$BUILD_TYPE"',
                    f'cmake --build "$ACTIVE_BUILD_DIR"{build_config} $BUILD_SWITCH --parallel',
                    "",
                ]
            )
        else:
            build_config = " --config Debug" if multi_config else ""
            lines.extend(
                [
                    f'cmake -S . -B "{build_dir}" -G "{generator}" -DCMAKE_BUILD_TYPE=Debug',
                    'BUILD_SWITCH=""',
                    'if [ "${1:-}" = "rebuild" ]; then BUILD_SWITCH="--clean-first"; fi',
                    f'cmake --build "{build_dir}"{build_config} $BUILD_SWITCH --parallel',
                    "",
                ]
            )
        return "\n".join(lines)

    def _armclang_toolchain_content(self, options: GenerationOptions) -> str:
        executable_suffix = self._host_command_names(options.host_os)["armclang_suffix"]
        system_processor = self._normalize_gcc_cpu_name(
            self.cpu or self._infer_cpu_from_device()
        ) or "arm"
        lines = [
            "set(CMAKE_SYSTEM_NAME Generic)",
            f"set(CMAKE_SYSTEM_PROCESSOR {system_processor})",
            "set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)",
            "if(POLICY CMP0123)",
            '    if(NOT CMAKE_SYSTEM_PROCESSOR STREQUAL "arm")',
            '        set(CMAKE_EXE_LINKER_FLAGS_INIT "--cpu=${CMAKE_SYSTEM_PROCESSOR}")',
            "    endif()",
            "endif()",
            "",
            'if(NOT DEFINED ENV{ARMCLANG_PATH})',
            '    message(FATAL_ERROR "env \\"ARMCLANG_PATH\\" not set")',
            "endif()",
            "",
            'file(TO_CMAKE_PATH "$ENV{ARMCLANG_PATH}" ARMCLANG_PATH)',
            f'set(_ARM_EXECUTABLE_SUFFIX "{executable_suffix}")',
            "",
            'set(CMAKE_C_COMPILER "${ARMCLANG_PATH}/armclang${_ARM_EXECUTABLE_SUFFIX}")',
            'set(CMAKE_CXX_COMPILER "${ARMCLANG_PATH}/armclang${_ARM_EXECUTABLE_SUFFIX}")',
            'set(CMAKE_ASM_COMPILER "${ARMCLANG_PATH}/armclang${_ARM_EXECUTABLE_SUFFIX}")',
            'set(CMAKE_LINKER "${ARMCLANG_PATH}/armlink${_ARM_EXECUTABLE_SUFFIX}")',
            'set(CMAKE_AR "${ARMCLANG_PATH}/armar${_ARM_EXECUTABLE_SUFFIX}")',
            'set(CMAKE_OBJCOPY "${ARMCLANG_PATH}/fromelf${_ARM_EXECUTABLE_SUFFIX}")',
            'set(FROMELF_EXECUTABLE "${ARMCLANG_PATH}/fromelf${_ARM_EXECUTABLE_SUFFIX}")',
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
            'set(ARM_TOOLCHAIN_ROOT "" CACHE PATH "Optional GNU Arm toolchain root")',
            "find_program(CMAKE_C_COMPILER NAMES arm-none-eabi-gcc)",
            "find_program(CMAKE_CXX_COMPILER NAMES arm-none-eabi-g++)",
            "find_program(CMAKE_ASM_COMPILER NAMES arm-none-eabi-gcc)",
            "if(ARM_TOOLCHAIN_ROOT)",
            "    if(NOT CMAKE_C_COMPILER)",
            '        find_program(CMAKE_C_COMPILER NAMES arm-none-eabi-gcc PATHS "${ARM_TOOLCHAIN_ROOT}/bin" NO_DEFAULT_PATH)',
            "    endif()",
            "    if(NOT CMAKE_CXX_COMPILER)",
            '        find_program(CMAKE_CXX_COMPILER NAMES arm-none-eabi-g++ PATHS "${ARM_TOOLCHAIN_ROOT}/bin" NO_DEFAULT_PATH)',
            "    endif()",
            "    if(NOT CMAKE_ASM_COMPILER)",
            '        find_program(CMAKE_ASM_COMPILER NAMES arm-none-eabi-gcc PATHS "${ARM_TOOLCHAIN_ROOT}/bin" NO_DEFAULT_PATH)',
            "    endif()",
            "endif()",
            "if(NOT CMAKE_C_COMPILER OR NOT CMAKE_CXX_COMPILER OR NOT CMAKE_ASM_COMPILER)",
            '    message(FATAL_ERROR "GNU Arm toolchain was not found on PATH or below ARM_TOOLCHAIN_ROOT")',
            "endif()",
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

    def _vscode_workspace_reference(
        self, path: str | Path, output_path: Path, role: str
    ) -> str:
        resolved = self._resolved_path(path)
        output_root = self._resolved_path(output_path)
        try:
            relative = os.path.relpath(resolved, output_root).replace("\\", "/")
        except ValueError:
            self._append_portability_warning(
                f"VS Code {role}与输出目录无法建立相对位置，"
                f"保留绝对位置: {resolved}"
            )
            return resolved.as_posix()
        workspace_folder = "${workspaceFolder}"
        if relative == ".":
            return workspace_folder
        return f"{workspace_folder}/{relative}"

    def _build_vscode_c_cpp_properties(
        self, output_path: Path, options: GenerationOptions
    ) -> Dict[str, Any]:
        include_path: List[str] = ["${workspaceFolder}"]
        for include_dir in self._dedupe_keep_order(
            [
                *self._ordered_include_paths(),
                *self._source_local_include_paths(),
            ]
        ):
            include_path.append(
                self._vscode_workspace_reference(
                    include_dir, output_path, "头文件目录"
                )
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
        project_dir = self._resolved_path(self.project_dir)
        candidate = project_dir.parent
        try:
            self._resolved_path(output_path).relative_to(candidate)
        except ValueError:
            return output_path

        for source in self.source_files:
            source_path = self._resolved_path(source)
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
        legacy_generated = self._is_legacy_generated_vscode_configuration(
            root_vscode
        )
        if existing and not ownership_file.exists() and not legacy_generated:
            self.warnings.append(
                "工程根目录已有 VS Code 配置，保留现有文件: "
                + ", ".join(sorted(existing))
            )
            return
        if legacy_generated and not ownership_file.exists():
            self.warnings.append("检测到旧版生成的 VS Code 配置，自动更新根目录任务")

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
        root_compile_commands = workspace_root / "compile_commands.json"
        if build_compile_commands.is_file():
            shutil.copy2(build_compile_commands, root_compile_commands)
        elif (ownership_file.exists() or legacy_generated) and (
            root_compile_commands.is_file() or root_compile_commands.is_symlink()
        ):
            root_compile_commands.unlink()

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

    @staticmethod
    def _is_legacy_generated_vscode_configuration(vscode_dir: Path) -> bool:
        tasks_file = vscode_dir / "tasks.json"
        settings_file = vscode_dir / "settings.json"
        if not tasks_file.is_file() or not settings_file.is_file():
            return False
        try:
            with open(tasks_file, "r", encoding="utf-8") as file:
                tasks = json.load(file).get("tasks", [])
            with open(settings_file, "r", encoding="utf-8") as file:
                settings = json.load(file)
        except (OSError, ValueError, TypeError):
            return False

        task_by_label = {
            str(task.get("label", "")): task
            for task in tasks
            if isinstance(task, dict)
        }
        generated_task_pattern = re.compile(
            r"^(Configure|Build|Rebuild|Clean) (GCC|ArmClang) (Debug|Release)$"
        )
        has_only_generated_task_labels = bool(task_by_label) and all(
            generated_task_pattern.fullmatch(label)
            or label.startswith("Flash ")
            or label.startswith("Start ")
            or label in {"Debug (Keil MDK)", "Refresh Compile Commands"}
            for label in task_by_label
        )
        has_generated_tasks = False
        for compiler_label in ("GCC", "ArmClang"):
            configure = task_by_label.get(f"Configure {compiler_label} Debug", {})
            build = task_by_label.get(f"Build {compiler_label} Debug", {})
            configure_args = [str(value) for value in configure.get("args", [])]
            build_args = [str(value) for value in build.get("args", [])]
            if (
                configure.get("command") == "cmake"
                and "-S" in configure_args
                and "-B" in configure_args
                and "-DCMAKE_BUILD_TYPE=Debug" in configure_args
                and build.get("command") == "cmake"
                and "--build" in build_args
            ):
                has_generated_tasks = True
                break
        build_directory = settings.get("cmake.buildDirectory")
        compile_commands = settings.get("C_Cpp.default.compileCommands")
        has_generated_settings = (
            isinstance(build_directory, str)
            and build_directory.startswith("${workspaceFolder}")
            and settings.get("cmake.copyCompileCommands")
            == "${workspaceFolder}/compile_commands.json"
            and compile_commands
            == f"{build_directory.rstrip('/')}/compile_commands.json"
        )
        return (
            has_only_generated_task_labels
            and has_generated_tasks
            and has_generated_settings
        )

    def _relative_workspace_path(self, path: Path, output_path: Path) -> str:
        return self._normalize_path(str(self._resolved_path(path)), str(output_path))

    def _artifact_path(
        self,
        options: GenerationOptions,
        suffix: str,
        build_dir: Optional[str] = None,
    ) -> str:
        active_build_dir = build_dir or options.build_dir
        return (
            f"${{workspaceFolder}}/{active_build_dir}/"
            f"{self._artifact_name()}{suffix}"
        )

    def _flash_load_address(self) -> int:
        return int(self.flash_origin if self.flash_origin is not None else 0x08000000)

    def _flash_load_address_text(self) -> str:
        return f"0x{self._flash_load_address():08X}"

    def _jlink_flash_device(self) -> str:
        return self._clean_device_name() or self.cpu or "Cortex-M4"

    @staticmethod
    def _clean_generated_vscode_assets(output_path: Path) -> None:
        vsc_dir = output_path / ".vscode"
        if KeilProjectToCMake._is_directory_link(vsc_dir):
            KeilProjectToCMake._remove_generated_directory(vsc_dir)
            return
        generated_names = (
            "jlink_flash_elf.jlink",
            "jlink_flash_hex.jlink",
            "jlink_flash_bin.jlink",
            "jlink_flash_elf_release.jlink",
            "jlink_flash_hex_release.jlink",
            "jlink_flash_bin_release.jlink",
            "openocd-jlink-swd.cfg",
        )
        for name in generated_names:
            path = vsc_dir / name
            if path.is_file() or path.is_symlink():
                path.unlink()

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
        if self._is_directory_link(vsc_dir):
            self._remove_generated_directory(vsc_dir)
        vsc_dir.mkdir(parents=True, exist_ok=True)

        flash_address = self._flash_load_address_text()
        scripts = {
            "jlink_flash_elf.jlink": f'r\nloadfile "{options.build_dir}/{self._artifact_name()}.elf"\nr\ng\nq\n',
            "jlink_flash_hex.jlink": f'r\nloadfile "{options.build_dir}/{self._artifact_name()}.hex"\nr\ng\nq\n',
            "jlink_flash_bin.jlink": f'r\nloadfile "{options.build_dir}/{self._artifact_name()}.bin" {flash_address}\nr\ng\nq\n',
            "jlink_flash_elf_release.jlink": f'r\nloadfile "{options.build_dir}-release/{self._artifact_name()}.elf"\nr\ng\nq\n',
            "jlink_flash_hex_release.jlink": f'r\nloadfile "{options.build_dir}-release/{self._artifact_name()}.hex"\nr\ng\nq\n',
            "jlink_flash_bin_release.jlink": f'r\nloadfile "{options.build_dir}-release/{self._artifact_name()}.bin" {flash_address}\nr\ng\nq\n',
        }

        for file_name, content in scripts.items():
            self._write_text_file(vsc_dir / file_name, content)

    def _build_vscode_tasks(
        self, output_path: Path, options: GenerationOptions
    ) -> Dict[str, Any]:
        commands = self._host_command_names(options.host_os)
        multi_config = _is_multi_config_generator(options.generator)
        compiler_label = "GCC" if options.compiler == "gcc" else "ArmClang"
        configure_label = f"Configure {compiler_label} Debug"
        build_label = f"Build {compiler_label} Debug"
        task_env = self._vscode_env(options)
        task_options = {"env": task_env} if task_env else {}
        flash_address = self._flash_load_address_text()

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
        if multi_config:
            build_args.extend(["--config", "Debug"])
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

        clean_args = [
            "--build",
            f"${{workspaceFolder}}/{options.build_dir}",
        ]
        if multi_config:
            clean_args.extend(["--config", "Debug"])
        clean_args.extend(["--target", "clean"])
        clean_task: Dict[str, Any] = {
            "label": f"Clean {compiler_label} Debug",
            "type": "shell",
            "command": commands["cmake"],
            "args": clean_args,
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

        if options.compiler == "gcc":
            release_build_dir = f"{options.build_dir}-release"
            release_configure_label = f"Configure {compiler_label} Release"
            release_build_label = f"Build {compiler_label} Release"
            release_configure_task: Dict[str, Any] = {
                "label": release_configure_label,
                "type": "shell",
                "command": commands["cmake"],
                "args": [
                    "-S",
                    "${workspaceFolder}",
                    "-B",
                    f"${{workspaceFolder}}/{release_build_dir}",
                    "-G",
                    options.generator,
                    "-DCMAKE_BUILD_TYPE=Release",
                ],
                "problemMatcher": [],
            }
            if task_options:
                release_configure_task["options"] = task_options

            release_build_args: List[str] = [
                "--build",
                f"${{workspaceFolder}}/{release_build_dir}",
            ]
            if multi_config:
                release_build_args.extend(["--config", "Release"])
            release_build_args.extend(
                ["--target", self._cmake_artifacts_target_name(), "--parallel"]
            )
            release_build_task: Dict[str, Any] = {
                "label": release_build_label,
                "type": "shell",
                "command": commands["cmake"],
                "args": release_build_args,
                "dependsOn": release_configure_label,
                "problemMatcher": [],
            }
            if task_options:
                release_build_task["options"] = task_options

            release_rebuild_args = list(release_build_args)
            release_rebuild_args.insert(-1, "--clean-first")
            release_rebuild_task: Dict[str, Any] = {
                "label": f"Rebuild {compiler_label} Release",
                "type": "shell",
                "command": commands["cmake"],
                "args": release_rebuild_args,
                "dependsOn": release_configure_label,
                "problemMatcher": [],
            }
            if task_options:
                release_rebuild_task["options"] = task_options

            release_clean_args = [
                "--build",
                f"${{workspaceFolder}}/{release_build_dir}",
            ]
            if multi_config:
                release_clean_args.extend(["--config", "Release"])
            release_clean_args.extend(["--target", "clean"])
            release_clean_task: Dict[str, Any] = {
                "label": f"Clean {compiler_label} Release",
                "type": "shell",
                "command": commands["cmake"],
                "args": release_clean_args,
                "dependsOn": release_configure_label,
                "problemMatcher": [],
            }
            if task_options:
                release_clean_task["options"] = task_options

            tasks.extend(
                [
                    release_configure_task,
                    release_build_task,
                    release_rebuild_task,
                    release_clean_task,
                ]
            )

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
                    if not openocd_configs:
                        continue
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
                                    f"program {{{self._artifact_path(options, '.bin')}}} {flash_address} verify reset exit",
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
                    for suffix in ("elf", "hex", "bin"):
                        program_args = [
                            "-c",
                            (
                                f"program {{{self._artifact_path(options, f'.{suffix}', release_build_dir)}}} "
                                + (
                                    f"{flash_address} verify reset exit"
                                    if suffix == "bin"
                                    else "verify reset exit"
                                )
                            ),
                        ]
                        tasks.append(
                            {
                                "label": (
                                    f"Flash {flash_probe_label} "
                                    f"{suffix.upper()} Release"
                                ),
                                "type": "shell",
                                "command": commands["openocd"],
                                "args": openocd_file_args + program_args,
                                "dependsOn": release_build_label,
                                "problemMatcher": [],
                            }
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
                    (
                        "Flash J-Link ELF Release",
                        "jlink_flash_elf_release.jlink",
                    ),
                    (
                        "Flash J-Link HEX Release",
                        "jlink_flash_hex_release.jlink",
                    ),
                    (
                        "Flash J-Link BIN Release",
                        "jlink_flash_bin_release.jlink",
                    ),
                ]:
                    depends_on = (
                        release_build_label
                        if label.endswith(" Release")
                        else build_label
                    )
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
                            "dependsOn": depends_on,
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
                    if not openocd_configs:
                        continue
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
                if not openocd_configs:
                    continue
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
        self,
        f: TextIOWrapper,
        toolchain_include: str,
        languages: List[str],
        policies: Optional[List[str]] = None,
        ninja_force_response_files: bool = True,
    ) -> None:
        project_name = (self.target_name or self.project_name or "project").replace('"', "_")
        f.write("# CMake file generated from Keil uVision project\n")
        f.write("# Generated by keil_uvprojx2cmake.py\n\n")
        f.write("cmake_minimum_required(VERSION 3.20)\n\n")
        for policy in policies or []:
            f.write(f"if(POLICY {policy})\n")
            f.write(f"    cmake_policy(SET {policy} NEW)\n")
            f.write("endif()\n")
        if policies:
            f.write("\n")
        f.write(f"include({toolchain_include})\n\n")
        response_file_setting = "ON" if ninja_force_response_files else "OFF"
        f.write(
            f'set(CMAKE_NINJA_FORCE_RESPONSE_FILE {response_file_setting} '
            'CACHE BOOL "Use response files with Ninja" FORCE)\n'
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

    def _write_source_compile_settings(
        self,
        f: TextIOWrapper,
        source_files: List[str],
        dest: str,
        compiler: str,
    ) -> None:
        for source_file in source_files:
            if Path(source_file).suffix.lower() in {".o", ".obj"}:
                continue
            settings = self._source_compile_settings_for(source_file)
            if not settings.macro_actions and not settings.include_paths:
                continue
            if compiler == "gcc":
                source_reference = self._portable_cmake_reference(
                    source_file,
                    dest,
                    "源文件",
                )
            else:
                source_reference = self._cmake_escape_argument(
                    self._normalize_path(source_file, dest)
                )
            compile_options: List[Tuple[str, bool]] = []
            for macro_name, definition in settings.macro_actions.items():
                compile_options.append((f"-U{macro_name}", False))
                if definition is not None:
                    compile_options.append((f"-D{definition}", False))

            for include_path in settings.include_paths:
                prepared_include = include_path
                if compiler == "gcc":
                    prepared_include = self._stage_gcc_include_directory(
                        self._gcc_freertos_port(Path(include_path)), dest
                    )
                if compiler == "gcc":
                    include_reference = self._portable_cmake_reference(
                        prepared_include,
                        dest,
                        "文件级头文件目录",
                        map_pack_include=True,
                    )
                    compile_options.append((f"-I{include_reference}", True))
                else:
                    absolute_include = Path(prepared_include).resolve(
                        strict=False
                    ).as_posix()
                    compile_options.append((f"-I{absolute_include}", False))

            if compile_options:
                f.write(
                    f'set_property(SOURCE "{source_reference}" APPEND PROPERTY\n'
                )
                f.write("    COMPILE_OPTIONS\n")
                for option, is_cmake_reference in compile_options:
                    escaped = (
                        option
                        if is_cmake_reference
                        else self._cmake_escape_argument(option)
                    )
                    f.write(f'    "{escaped}"\n')
                f.write(")\n\n")

    def _write_armclang_cmake_content(
        self, f: TextIOWrapper, dest: str, options: GenerationOptions
    ) -> None:
        cmake_target = self._cmake_target_name()
        self._write_cmake_header(
            f,
            "cmake/armclang.cmake",
            self._project_languages(),
            policies=["CMP0123"],
            ninja_force_response_files=False,
        )

        cpu_flags = self._dedupe_keep_order(self.cpu_flags + ["-gdwarf-4"])
        c_flags = " ".join(self.c_flags)
        cxx_flags = " ".join(self.cpp_flags)
        common_flags = " ".join(self.c_cpp_flags)
        asm_flags = " ".join(self.asm_flags)
        escaped_cpu_flags = self._cmake_escape_argument(" ".join(cpu_flags))
        escaped_c_flags = self._cmake_escape_argument(c_flags)
        escaped_cxx_flags = self._cmake_escape_argument(cxx_flags)
        escaped_common_flags = self._cmake_escape_argument(common_flags)
        escaped_asm_flags = self._cmake_escape_argument(asm_flags)

        f.write("# Compiler flags\n")
        f.write(f'set(CPU_FLAGS "{escaped_cpu_flags}")\n')
        f.write(
            f'set(CMAKE_C_FLAGS "${{CPU_FLAGS}} {escaped_c_flags} '
            f'{escaped_common_flags}")\n'
        )
        f.write(
            f'set(CMAKE_CXX_FLAGS "${{CPU_FLAGS}} {escaped_cxx_flags} '
            f'{escaped_common_flags}")\n'
        )
        f.write(
            f'set(CMAKE_ASM_FLAGS "${{CPU_FLAGS}} {escaped_asm_flags}")\n\n'
        )

        if self.linker_flags:
            f.write("# Linker flags\n")
            escaped_linker_flags = self._cmake_escape_argument(
                " ".join(self.linker_flags)
            )
            f.write(
                f'set(CMAKE_EXE_LINKER_FLAGS '
                f'"${{CMAKE_EXE_LINKER_FLAGS}} {escaped_linker_flags}")\n\n'
            )

        if self.linker_script:
            self._record_asset(
                "scatter_file",
                Path(self.linker_script),
                self._asset_source(Path(self.linker_script)),
                "exact",
                "Reused the Keil scatter file with ArmClang",
            )
            linker_reference = self._portable_cmake_reference(
                self.linker_script,
                dest,
                "链接描述文件",
            )
            f.write(f'set(LINKER_SCRIPT "{linker_reference}")\n')
            f.write(
                'set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} --strict --scatter \\"${LINKER_SCRIPT}\\"")\n\n'
            )

        defines = self._effective_defines("armclang")
        if defines:
            f.write("# Preprocessor definitions\n")
            f.write("add_definitions(\n")
            for define in defines:
                escaped = self._cmake_escape_argument(f"-D{define}")
                f.write(f'    "{escaped}"\n')
            f.write(")\n\n")

        if self.include_paths:
            f.write("# Include directories\n")
            f.write("add_compile_options(\n")
            for include_path in self._ordered_include_paths():
                absolute_include = Path(include_path).resolve(
                    strict=False
                ).as_posix()
                escaped = self._cmake_escape_argument(f"-I{absolute_include}")
                f.write(f'    "{escaped}"\n')
            f.write(")\n\n")

        armclang_sources = self._dedupe_keep_order(
            [*self._prepared_armclang_source_files(dest), *self.linker_inputs]
        )
        f.write("# Source files\n")
        f.write("set(SOURCES\n")
        for source_file in armclang_sources:
            f.write(f'    "{self._normalize_path(source_file, dest)}"\n')
        f.write(")\n\n")

        for linker_input in self._dedupe_keep_order(self.linker_inputs):
            reference = self._cmake_escape_argument(
                self._normalize_path(linker_input, dest)
            )
            f.write(
                f'set_source_files_properties("{reference}" PROPERTIES '
                'EXTERNAL_OBJECT TRUE)\n'
            )
        f.write(f"add_executable({cmake_target} ${{SOURCES}})\n")
        f.write(
            f'set_target_properties({cmake_target} PROPERTIES\n'
            f'    OUTPUT_NAME "{self._artifact_name()}"\n'
            '    SUFFIX ".axf"\n'
            '    RUNTIME_OUTPUT_DIRECTORY "$<1:${CMAKE_CURRENT_BINARY_DIR}>"\n'
            ')\n\n'
        )
        if self.undefines:
            f.write(f"target_compile_options({cmake_target} PRIVATE\n")
            for macro_name in sorted(self.undefines):
                escaped = self._cmake_escape_argument(f"-U{macro_name}")
                f.write(f'    "{escaped}"\n')
            f.write(")\n\n")
        self._write_source_compile_settings(
            f, armclang_sources, dest, "armclang"
        )

        if self.libraries:
            f.write("# Link libraries\n")
            f.write(f"target_link_libraries({cmake_target} PRIVATE\n")
            for lib in self.libraries:
                library_path = Path(lib)
                if library_path.is_absolute():
                    reference = self._portable_cmake_reference(
                        library_path,
                        dest,
                        "链接库",
                    )
                else:
                    reference = self._cmake_escape_argument(
                        lib.replace("\\", "/")
                    )
                f.write(f'    "{reference}"\n')
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
        slib_layout = self._read_slib_layout()
        startup_replacements: Dict[str, Path] = {}
        gd32_assets = None
        if self._clean_device_name().upper().startswith("GD32"):
            from gd32_assets import select_assets

            sources = [self._resolve_missing_source(Path(s)) for s in self._gcc_source_files()]
            startups = [s for s in sources if s.stem.lower().startswith("startup_gd32")]
            if len(startups) != 1:
                raise ValueError("GD32: exactly one SDK startup source is required")
            original = startups[0]
            gd32_assets = select_assets(
                self._clean_device_name(), self.uvprojx_path, self.target_name, sources,
                original, self._parse_armasm_vector_entries(original), self._gcc_stack_heap_sizes(),
            )
            startup_replacements[str(original)] = gd32_assets.startup
            linker_script = str(gd32_assets.linker)
            self.warnings.extend(gd32_assets.notes)
        else:
            linker_script = (
                self._generate_fallback_gcc_linker_script(dest)
                if slib_layout else self._guess_gcc_linker_script(dest)
            )
        if linker_script is None:
            linker_script = self._generate_fallback_gcc_linker_script(dest)
        if linker_script is None:
            raise FileNotFoundError(
                "Unable to locate a GCC linker script (.ld), and failed to auto-generate one from uvprojx memory info."
            )

        linker_path = Path(linker_script)
        linker_source = self._asset_source(linker_path, Path(dest))
        if gd32_assets:
            linker_support = "review_required"
            linker_reason = "Matched GD32 SDK startup and device linker; preserved Keil target memory and fixed sections"
        elif slib_layout:
            linker_support = "review_required"
            linker_reason = "Generated from the original AT32 SLIB scatter layout and symbol contract"
        elif linker_source == "generated":
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
        if gd32_assets:
            self._write_text_file(Path(linker_script), gd32_assets.content)

        languages = self._project_languages()
        self._write_cmake_header(
            f,
            "cmake/arm-none-eabi-toolchain.cmake",
            languages,
        )

        linker_reference = self._portable_cmake_reference(
            linker_script,
            dest,
            "链接脚本",
        )
        artifact_name = self._artifact_name()
        source_files = self._prepared_gcc_source_files(dest, startup_replacements)
        source_files.extend(self._gcc_linker_object_inputs())
        source_files = self._dedupe_keep_order(source_files)
        slib_sources = self._prepare_slib_sources(
            slib_layout, source_files, dest, linker_script
        ) if slib_layout else []
        self._emit_generation_progress(62, "生成运行时兼容文件")
        runtime_compat_source = self._generate_gcc_runtime_compat_source(
            dest, linker_script
        )
        if runtime_compat_source:
            source_files.append(runtime_compat_source)
        self._analyze_gcc_migration_issues(source_files, linker_script)
        gcc_libraries = self._prepared_gcc_libraries(dest)
        symbol_fragments = self._generate_gcc_symbol_fragments(dest)
        self._emit_generation_progress(70, "检查头文件兼容性")
        forced_headers = self._generate_legacy_stdint_compat_headers(dest)
        cxx_compat_header = self._generate_gcc_cxx_compat_header(
            dest, forced_headers, source_files
        )
        self._emit_generation_progress(78, "整理包含目录")
        gcc_include_paths = self._prepared_gcc_include_paths(dest)
        cpu_options = self._gcc_cpu_options()
        optimization_flag = self._gcc_optimization_flag()
        compatible_compile_options = self._gcc_compatible_compile_options()
        lto_default = "ON" if self._gcc_lto_enabled() else "OFF"
        inline_assembly_symbols = self._gcc_inline_assembly_symbols()
        uses_printf_float, uses_scanf_float = self._gcc_formatted_io_float_requirements()
        copy_if_exists_script = Path(dest) / "cmake" / "copy_if_exists.cmake"
        self._write_text_file(
            copy_if_exists_script,
            """if(NOT DEFINED INPUT_FILE OR NOT DEFINED OUTPUT_FILE)
    message(FATAL_ERROR "INPUT_FILE and OUTPUT_FILE are required")
endif()
if(EXISTS "${INPUT_FILE}")
    execute_process(
        COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${INPUT_FILE}" "${OUTPUT_FILE}"
        RESULT_VARIABLE COPY_RESULT
    )
    if(NOT COPY_RESULT EQUAL 0)
        message(FATAL_ERROR "Failed to copy ${INPUT_FILE}")
    endif()
endif()
""",
        )
        self._emit_generation_progress(82, "写入 CMake 目标配置")

        f.write(f'set(LINKER_SCRIPT "{linker_reference}")\n')
        f.write('get_filename_component(LINKER_SCRIPT_DIR "${LINKER_SCRIPT}" DIRECTORY)\n')
        f.write(f'set(MAP_FILE "${{CMAKE_CURRENT_BINARY_DIR}}/{artifact_name}.map")\n')
        f.write(f'set(ELF_FILE "{artifact_name}.elf")\n')
        f.write(f'set(HEX_FILE "{artifact_name}.hex")\n')
        f.write(f'set(BIN_FILE "{artifact_name}.bin")\n')
        f.write(f'set(ARTIFACT_STAMP "{artifact_name}.artifacts.stamp")\n')
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
        f.write('find_program(KEIL_GCC_COMPILER_CACHE NAMES sccache ccache)\n')
        f.write('if(KEIL_GCC_COMPILER_CACHE)\n')
        f.write('    set(CMAKE_C_COMPILER_LAUNCHER "${KEIL_GCC_COMPILER_CACHE}")\n')
        f.write('    set(CMAKE_CXX_COMPILER_LAUNCHER "${KEIL_GCC_COMPILER_CACHE}")\n')
        f.write('endif()\n')
        f.write('foreach(KEIL_LANGUAGE C CXX ASM)\n')
        f.write('    set(CMAKE_${KEIL_LANGUAGE}_FLAGS_DEBUG "")\n')
        f.write('    set(CMAKE_${KEIL_LANGUAGE}_FLAGS_RELEASE "")\n')
        f.write('    set(CMAKE_${KEIL_LANGUAGE}_FLAGS_RELWITHDEBINFO "")\n')
        f.write('    set(CMAKE_${KEIL_LANGUAGE}_FLAGS_MINSIZEREL "")\n')
        f.write('endforeach()\n')
        f.write("\n")

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
            source_reference = self._portable_cmake_reference(
                source_file,
                dest,
                "源文件",
            )
            f.write(f'    "{source_reference}"\n')
        f.write(")\n\n")

        f.write(f"add_executable({cmake_target} ${{PROJECT_SOURCES}})\n")
        f.write(
            f'set_target_properties({cmake_target} PROPERTIES\n'
            f'    OUTPUT_NAME "{artifact_name}"\n'
            '    SUFFIX ".elf"\n'
            '    RUNTIME_OUTPUT_DIRECTORY "$<1:${CMAKE_CURRENT_BINARY_DIR}>"\n'
            ')\n\n'
        )
        self._write_source_compile_settings(f, source_files, dest, "gcc")

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
                escaped = self._cmake_escape_argument(define)
                f.write(f'    "{escaped}"\n')
            f.write(")\n\n")

        if "CXX" in languages:
            self._write_gcc_cxx_multilib_detection(f, cpu_options)
            f.write(
                f"target_include_directories({cmake_target} SYSTEM PRIVATE\n"
            )
            f.write(
                '    "$<$<COMPILE_LANGUAGE:CXX>:'
                '${ARM_GXX_MULTILIB_STDINC}>"\n'
            )
            f.write(
                '    "$<$<COMPILE_LANGUAGE:CXX>:'
                '${ARM_GXX_TARGET_STDINC}>"\n'
            )
            f.write(")\n\n")

        f.write(f"target_compile_options({cmake_target} PRIVATE\n")
        for macro_name in sorted(self.undefines):
            escaped = self._cmake_escape_argument(f"-U{macro_name}")
            f.write(f'    "{escaped}"\n')
        if cxx_compat_header:
            normalized_compat = self._portable_cmake_reference(
                cxx_compat_header,
                dest,
                "强制包含头文件",
            )
            f.write('    "$<$<COMPILE_LANGUAGE:C,CXX>:-include>"\n')
            f.write(
                f'    "$<$<COMPILE_LANGUAGE:C,CXX>:{normalized_compat}>"\n'
            )
        for flag in cpu_options:
            f.write(f"    {flag}\n")
        f.write("    -ffunction-sections\n")
        f.write("    -fdata-sections\n")
        f.write("    -fno-common\n")
        f.write("    -pipe\n")
        f.write("    -Wall\n")
        f.write("    -Wextra\n")
        for flag in compatible_compile_options:
            f.write(f"    {flag}\n")
        lto_condition = (
            '$<AND:$<BOOL:${KEIL_GCC_ENABLE_LTO}>,'
            '$<NOT:$<CONFIG:Debug>>>'
        )
        f.write(f'    "$<{lto_condition}:-flto>"\n')
        f.write(
            f'    "$<{lto_condition}:-flto-partition=1to1>"\n'
        )
        f.write('    "$<$<CONFIG:Debug>:-Og>"\n')
        f.write('    "$<$<NOT:$<CONFIG:Debug>>:${KEIL_OPTIMIZATION_FLAG}>"\n')
        f.write(f"    $<$<COMPILE_LANGUAGE:C>:{self._gcc_c_standard()}>\n")
        f.write('    "$<$<CONFIG:Debug>:-g2>"\n')
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
        f.write(f'    "$<{lto_condition}:-flto>"\n')
        f.write(
            f'    "$<{lto_condition}:-flto-partition=1to1>"\n'
        )
        for symbol in inline_assembly_symbols:
            f.write(
                f'    "$<{lto_condition}:'
                f'-Wl,--undefined={symbol}>"\n'
            )
        f.write("    -T${LINKER_SCRIPT}\n")
        f.write('    "-L${LINKER_SCRIPT_DIR}"\n')
        for fragment in symbol_fragments:
            fragment_reference = self._portable_cmake_reference(
                fragment,
                dest,
                "链接符号片段",
            )
            f.write(f'    "-T{fragment_reference}"\n')
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
        if uses_scanf_float:
            f.write("    -u _scanf_float\n")
        f.write(")\n\n")

        for source in slib_sources:
            reference = self._portable_cmake_reference(source, dest, "SLIB 源文件")
            f.write(f'set_property(SOURCE "{reference}" APPEND PROPERTY COMPILE_OPTIONS\n'
                    '    -mpure-code -fno-jump-tables -fno-lto)\n')
        if slib_layout:
            f.write(f'set_property(TARGET {cmake_target} APPEND PROPERTY LINK_DEPENDS "${{LINKER_SCRIPT}}"\n')
            for fragment in symbol_fragments:
                reference = self._portable_cmake_reference(fragment, dest, "SLIB 符号表")
                f.write(f'    "{reference}"\n')
            f.write(")\n")
        if slib_layout and slib_layout.producer:
            from slib_layout import export_check_script

            script = Path(dest) / "cmake" / "linker" / "check_slib_exports.cmake"
            self._write_text_file(script, export_check_script(slib_layout.symbols))
            f.write(f'''add_custom_command(TARGET {cmake_target} POST_BUILD
    COMMAND "${{CMAKE_COMMAND}}" "-DNM=${{CMAKE_NM}}"
        "-DELF=$<TARGET_FILE:{cmake_target}>" "-DOUTPUT_DIR=${{CMAKE_CURRENT_BINARY_DIR}}"
        -P "${{CMAKE_CURRENT_LIST_DIR}}/cmake/linker/check_slib_exports.cmake"
    BYPRODUCTS "${{CMAKE_CURRENT_BINARY_DIR}}/slib_exports.symdefs"
        "${{CMAKE_CURRENT_BINARY_DIR}}/slib_exports.ld"
    VERBATIM)
''')

        libraries = self._dedupe_keep_order(gcc_libraries + ["m"])
        f.write(f"target_link_libraries({cmake_target} PRIVATE\n")
        for library in libraries:
            if Path(library).is_absolute():
                reference = self._portable_cmake_reference(
                    library,
                    dest,
                    "链接库",
                )
                f.write(f'    "{reference}"\n')
            else:
                escaped = self._cmake_escape_argument(library)
                f.write(f'    "{escaped}"\n')
        f.write(")\n\n")

        f.write(
            f"""add_custom_target({artifacts_target} ALL
    COMMAND "${{CMAKE_OBJCOPY}}" -O ihex "$<TARGET_FILE:{cmake_target}>" "${{HEX_FILE}}"
    COMMAND "${{CMAKE_OBJCOPY}}" -O binary "$<TARGET_FILE:{cmake_target}>" "${{BIN_FILE}}"
    COMMAND "${{CMAKE_SIZE}}" --format=berkeley "$<TARGET_FILE:{cmake_target}>"
    COMMAND "${{CMAKE_COMMAND}}" -E touch "${{ARTIFACT_STAMP}}"
    COMMAND "${{CMAKE_COMMAND}}"
        "-DINPUT_FILE=${{CMAKE_CURRENT_BINARY_DIR}}/compile_commands.json"
        "-DOUTPUT_FILE=${{CMAKE_CURRENT_SOURCE_DIR}}/compile_commands.json"
        -P "${{CMAKE_CURRENT_LIST_DIR}}/cmake/copy_if_exists.cmake"
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
    def _is_legacy_generated_cmake_file(path: Path) -> bool:
        if not (path.is_file() or path.is_symlink()):
            return False

        name = path.name.lower()
        if not (
            (name.startswith("startup_") and name.endswith("_gcc.s"))
            or name == "core_cm3_gcc.c"
            or name.endswith(".ld")
        ):
            return False

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False

        if name.startswith("startup_") and name.endswith("_gcc.s"):
            return content.startswith(
                "/* Auto-generated GNU startup file converted from a Keil "
                "ARMASM startup file. */"
            )
        if name == "core_cm3_gcc.c":
            return 'strex' in content and '"=&r" (result)' in content
        return content.startswith("/* Converted from ARM Linker SYMDEFS. */")

    @staticmethod
    def _is_directory_link(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            junction_check = getattr(path, "is_junction", None)
            if callable(junction_check) and junction_check():
                return True
            os_junction_check = getattr(os.path, "isjunction", None)
            if callable(os_junction_check) and os_junction_check(path):
                return True
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
            reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            return bool(attributes & reparse_point)
        except OSError:
            return False

    @staticmethod
    def _remove_generated_directory(path: Path) -> None:
        if KeilProjectToCMake._is_directory_link(path):
            if path.is_symlink():
                path.unlink()
            else:
                path.rmdir()
        elif path.is_dir():
            shutil.rmtree(path)

    @staticmethod
    def _clean_generated_cmake_assets(output_path: Path) -> None:
        generated_cmake_dir = output_path / "cmake"
        if KeilProjectToCMake._is_directory_link(generated_cmake_dir):
            KeilProjectToCMake._remove_generated_directory(generated_cmake_dir)
            return
        if not generated_cmake_dir.is_dir():
            return

        owned_directories = (
            "compat-includes",
            "includes",
            "libraries",
            "linker",
            "sources",
        )
        owned_files = (
            "arm-none-eabi-toolchain.cmake",
            "armclang.cmake",
            "copy_if_exists.cmake",
            "gcc_compat.h",
            "gcc_runtime_compat.c",
            "generated_linker.ld",
        )
        for name in owned_directories:
            path = generated_cmake_dir / name
            KeilProjectToCMake._remove_generated_directory(path)
        for name in owned_files:
            path = generated_cmake_dir / name
            if path.is_file() or path.is_symlink():
                path.unlink()
        for path in generated_cmake_dir.iterdir():
            if KeilProjectToCMake._is_legacy_generated_cmake_file(path):
                path.unlink()

        try:
            generated_cmake_dir.rmdir()
        except OSError:
            # 保留输出目录中来源未知的文件。
            pass

    def _generate_configured_output(
        self,
        output_dir: Optional[str],
        options: GenerationOptions,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> bool:
        self.last_error = ""
        self.warnings = list(self._parsed_warnings)
        self.asset_records = list(self._parsed_asset_records)
        parsed_source_files = list(self.source_files)
        parsed_include_paths = set(self.include_paths)
        parsed_include_path_order = list(self.include_path_order)
        output_path = Path(output_dir) if output_dir else self._default_output_path(options)
        output_path.mkdir(parents=True, exist_ok=True)
        previous_callback = self._generation_progress_callback
        previous_resolved_paths = self._generation_resolved_paths
        previous_source_analysis = self._generation_source_analysis
        self._generation_progress_callback = progress_callback

        try:
            self._emit_generation_progress(0, "准备输出目录")
            self._copy_staged_source_mappings = []
            self._prepared_source_origins = {}
            self._lvgl_lazy_new_allocator_cache = None
            self._repository_root_cache = None
            validation_report = output_path / "build-validation.json"
            if validation_report.is_file() or validation_report.is_symlink():
                validation_report.unlink()
            self._clean_generated_cmake_assets(output_path)
            self._generation_resolved_paths = {}
            self._generation_source_analysis = {}
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
                self._clean_generated_vscode_assets(output_path)
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
        except OperationCancelled:
            raise
        except Exception as e:
            self.last_error = f"Error generating CMake files: {e}"
            print(self.last_error, file=sys.stderr)
            return False
        finally:
            self.source_files = parsed_source_files
            self.include_paths = parsed_include_paths
            self.include_path_order = parsed_include_path_order
            self._generation_progress_callback = previous_callback
            self._generation_resolved_paths = previous_resolved_paths
            self._generation_source_analysis = previous_source_analysis

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
        cancel_check: Optional[Callable[[], bool]] = None,
        activity_callback: Optional[Callable[[BuildActivity], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> BuildValidationReport:
        options = generation_options.normalized()
        output_path = Path(output_dir) if output_dir else self._default_output_path(options)
        validator = BuildValidator(
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            activity_callback=activity_callback,
            log_callback=log_callback,
        )
        artifact_suffixes = (
            [".elf", ".hex", ".bin"] if options.compiler == "gcc" else [".axf"]
        )
        expected_artifacts = [
            (
                output_path
                / str(options.build_dir)
                / f"{self._artifact_name()}{suffix}"
            ).resolve(strict=False)
            for suffix in artifact_suffixes
        ]
        report = validator.validate(
            output_path.resolve(strict=False), options, expected_artifacts
        )
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
