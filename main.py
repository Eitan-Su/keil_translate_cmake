# -*- coding: utf-8 -*-
import os
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QDir,
    QObject,
    QPropertyAnimation,
    QSettings,
    QSize,
    QStringListModel,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from get_keil_mdk_info import find_keil_mdk_root
from keil_to_cmake import (
    DEFAULT_GENERATOR_BY_HOST,
    GenerationOptions,
    KeilProjectToCMake,
    SUPPORTED_COMPILERS,
    SUPPORTED_DEBUG_BACKENDS,
    SUPPORTED_DEBUG_PROBES,
    SUPPORTED_HOST_SYSTEMS,
    get_supported_generators,
)


def _config_file_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.ini"
    return Path(__file__).resolve().parent / "config.ini"


def _resource_path(relative_path: str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path / relative_path


CHECKBOX_CHECK_ICON = _resource_path("assets/checkbox-check.svg").as_posix()


APP_STYLE = (
    _resource_path("assets/keilforge.qss").read_text(encoding="utf-8")
    .replace("__CHECKBOX_CHECK_ICON__", CHECKBOX_CHECK_ICON)
    .replace("__CHEVRON_ICON__", _resource_path("assets/chevron-down.svg").as_posix())
)


def _ui_icon(name: str, color: str = "#647557", size: int = 24) -> QIcon:
    paths = {
        "project": '<rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8 8h8M8 12h5M8 16h7"/>',
        "build": '<path d="M4 6h16M4 12h16M4 18h16"/><path d="M9 3v6M15 9v6M9 15v6"/>',
        "debug": '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"/>',
        "log": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m7 9 3 3-3 3M13 15h4"/>',
        "folder": '<path d="M3 7V6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
        "arrow": '<path d="M4 12h15m-6-6 6 6-6 6"/>',
    }
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="1.5" '
        'stroke-linecap="round" stroke-linejoin="round">' + paths[name] + '</svg>'
    )
    renderer = QSvgRenderer(svg.encode("utf-8"))
    icon = QIcon()
    for scale in (1, 2):
        pixmap = QPixmap(size * scale, size * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        icon.addPixmap(pixmap)
    return icon


class PathHistoryEdit(QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._history = []
        self._history_model = QStringListModel(self)
        self._history_completer = QCompleter(self._history_model, self)
        self._history_completer.setCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive
        )
        self._history_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._history_completer.setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion
        )
        self._history_completer.popup().setMouseTracking(True)
        self.setCompleter(self._history_completer)

    def set_history(self, values):
        self._history = list(values)
        self._history_model.setStringList(self._history)

    def history(self):
        return list(self._history)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if not self._history:
            return
        self._history_completer.setCompletionPrefix("")
        self._history_completer.complete()


class ProjectDropArea(QFrame):
    project_dropped = Signal(str)

    def __init__(self):
        super().__init__(objectName="projectDrop")
        self.setAcceptDrops(True)

    @staticmethod
    def project_from_mime(mime):
        urls = mime.urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            return ""
        path = Path(urls[0].toLocalFile())
        if path.suffix.lower() not in {".uvprojx", ".uvproj"} or not path.is_file():
            return ""
        return str(path)

    def _highlight(self, active):
        self.setProperty("dragging", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def dragEnterEvent(self, event):
        if self.isEnabled() and self.project_from_mime(event.mimeData()):
            self._highlight(True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self._highlight(False)
        event.accept()

    def dropEvent(self, event):
        self._highlight(False)
        project = self.project_from_mime(event.mimeData())
        if self.isEnabled() and project:
            self.project_dropped.emit(project)
            event.acceptProposedAction()
        else:
            event.ignore()


class ConversionWorker(QObject):
    log = Signal(str)
    progress = Signal(int, str)
    memory_usage = Signal(int, int, int, int)
    finished = Signal(bool, str, str, float)

    def __init__(
        self,
        project_file: str,
        output_dir: str,
        uv4_path: str,
        target_name: str,
        options: GenerationOptions,
        validate_build: bool,
    ):
        super().__init__()
        self.project_file = project_file
        self.output_dir = output_dir
        self.uv4_path = uv4_path
        self.target_name = target_name
        self.options = options
        self.validate_build = validate_build

    @staticmethod
    def _map_progress(value, start, end):
        value = max(0, min(100, int(value)))
        return start + round((end - start) * value / 100)

    def _generation_progress(self, value, message):
        end = 45 if self.validate_build else 99
        self.progress.emit(self._map_progress(value, 20, end), message)

    def _validation_progress(self, value, message):
        self.progress.emit(self._map_progress(value, 45, 99), message)

    @staticmethod
    def _memory_bytes(value, unit):
        factors = {"B": 1, "KB": 1024, "MB": 1024 * 1024}
        return int(float(value) * factors.get(unit.upper(), 1))

    @classmethod
    def _parse_memory_regions(cls, output, parser=None):
        values = []
        pattern = re.compile(
            r"^\s*([A-Za-z_][A-Za-z0-9_.-]*):\s+([0-9.]+)\s+(B|KB|MB)\s+"
            r"([0-9.]+)\s+(B|KB|MB)",
            re.MULTILINE | re.IGNORECASE,
        )
        for match in pattern.finditer(output or ""):
            values.append(
                (
                    match.group(1),
                    cls._memory_bytes(match.group(2), match.group(3)),
                    cls._memory_bytes(match.group(4), match.group(5)),
                )
            )
        if values:
            return values

        armclang_size = re.search(
            r"Program\s+Size:\s*Code\s*=\s*(\d+)\s+"
            r"RO-data\s*=\s*(\d+)\s+RW-data\s*=\s*(\d+)\s+"
            r"ZI-data\s*=\s*(\d+)",
            output or "",
            re.IGNORECASE,
        )
        if armclang_size:
            code, ro_data, rw_data, zi_data = map(
                int, armclang_size.groups()
            )
            flash_total = int(getattr(parser, "flash_length", 0) or 0)
            ram_total = int(getattr(parser, "ram_length", 0) or 0)
            values.extend(
                [
                    ("FLASH", code + ro_data + rw_data, flash_total),
                    ("RAM", rw_data + zi_data, ram_total),
                ]
            )
        return values

    @classmethod
    def _parse_memory_usage(cls, output, parser):
        values = {
            name.upper(): (used, total)
            for name, used, total in cls._parse_memory_regions(output, parser)
        }

        flash_used, flash_total = values.get(
            "FLASH", (0, int(parser.flash_length or 0))
        )
        ram_used, ram_total = values.get("RAM", (0, int(parser.ram_length or 0)))
        return flash_used, flash_total, ram_used, ram_total

    @staticmethod
    def _format_size(value):
        value = int(value)
        for unit in ("B", "KB", "MB"):
            if value < 1024 or unit == "MB":
                if unit == "B":
                    return f"{value} {unit}"
                return f"{value:.2f} {unit}"
            value /= 1024
        return f"{value:.2f} MB"

    def _build_success_message(self, parser, memory_regions):
        build_dir = Path(self.output_dir) / str(self.options.build_dir)
        artifact_name = parser._artifact_name()
        artifact_suffixes = (
            (".elf", ".hex", ".bin")
            if self.options.compiler == "gcc"
            else (".axf",)
        )
        firmware_files = [
            build_dir / f"{artifact_name}{suffix}"
            for suffix in artifact_suffixes
        ]
        firmware_lines = [
            f"{path.name}: {self._format_size(path.stat().st_size)}"
            for path in firmware_files
            if path.is_file()
        ]

        memory_lines = []
        for name, used, total in memory_regions:
            percent = used * 100 / total if total else 0
            memory_lines.append(
                f"{name}: {self._format_size(used)} / "
                f"{self._format_size(total)} ({percent:.1f}%)"
            )

        probes = [
            SUPPORTED_DEBUG_PROBES.get(probe, probe)
            for probe in parser._selected_probes(self.options)
        ]
        backends = [
            SUPPORTED_DEBUG_BACKENDS.get(backend, backend)
            for backend in parser._selected_debug_backends(self.options)
        ]
        sections = ["构建验证通过。"]
        if firmware_lines:
            sections.append("固件文件\n" + "\n".join(firmware_lines))
        if memory_lines:
            sections.append("内存占用\n" + "\n".join(memory_lines))
        sections.append(
            "调试配置\n"
            f"探针: {', '.join(probes) if probes else '未启用'}\n"
            f"后端: {', '.join(backends) if backends else '未启用'}\n"
            f"VS Code 配置: {'生成' if self.options.export_vsc_settings else '未生成'}\n"
            f"CMake 生成器: {self.options.generator}"
        )
        sections.append(f"输出目录\n{Path(self.output_dir).resolve()}")
        return "\n\n".join(sections)

    @Slot()
    def run(self):
        validation_started = None
        try:
            success_message = "CMake 工程生成完成。"
            self.progress.emit(2, "初始化转换任务")
            self.log.emit(
                f"构建配置: 编译器={self.options.compiler} | "
                f"生成器={self.options.generator} | 构建目录={self.options.build_dir}"
            )
            self.progress.emit(5, "解析 Keil 工程")
            self.log.emit("解析 Keil 工程...")
            parser = KeilProjectToCMake(
                self.project_file,
                self.uv4_path or None,
                target_name=self.target_name or None,
            )
            if not parser.parse():
                self.finished.emit(False, parser.last_error or "解析失败", "", -1.0)
                return

            self.progress.emit(20, "工程解析完成")
            self.log.emit(
                f"目标: {parser.target_name} | 设备: {parser.device_name} | 源文件: {len(parser.source_files)}"
            )
            self.log.emit("生成 CMake、工具链和 VS Code 配置...")
            if not parser.generate_cmake(
                self.output_dir,
                self.options.export_vsc_settings,
                self.options,
                progress_callback=self._generation_progress,
            ):
                self.finished.emit(False, parser.last_error or "生成失败", "", -1.0)
                return

            for warning in parser.warnings:
                self.log.emit(f"警告: {warning}")

            if self.validate_build:
                validation_started = time.monotonic()
                self.log.emit("执行 CMake 配置与编译验证...")
                report = parser.validate_generated_build(
                    self.output_dir,
                    self.options,
                    timeout_seconds=600,
                    progress_callback=self._validation_progress,
                )
                if report.status != "success":
                    categories = ", ".join(
                        diagnostic.category for diagnostic in report.diagnostics
                    )
                    result = report.build or report.configure
                    raw_detail = "\n".join(
                        part
                        for part in (result.error, result.stderr, result.stdout)
                        if part
                    )
                    detail_lines = [
                        line.strip() for line in raw_detail.splitlines() if line.strip()
                    ]
                    important_lines = [
                        line
                        for line in detail_lines
                        if any(
                            marker in line.lower()
                            for marker in (
                                "error",
                                "fatal",
                                "undefined reference",
                                "not set",
                                "not found",
                                "failed",
                            )
                        )
                    ]
                    summary_lines = (important_lines or detail_lines)[-8:]
                    summary = "\n".join(summary_lines)
                    if summary:
                        self.log.emit("构建错误摘要:\n" + summary)
                    message = f"构建验证失败: {categories}"
                    if summary:
                        message += "\n\n" + summary
                    validation_seconds = time.monotonic() - validation_started
                    self.finished.emit(
                        False, message, self.output_dir, validation_seconds
                    )
                    return
                build_output = "\n".join(
                    part
                    for part in (report.build.stdout, report.build.stderr)
                    if part
                )
                memory_regions = self._parse_memory_regions(build_output, parser)
                self.memory_usage.emit(*self._parse_memory_usage(build_output, parser))
                success_message = self._build_success_message(
                    parser, memory_regions
                )
                artifact_labels = (
                    "ELF、HEX、BIN" if self.options.compiler == "gcc" else "AXF"
                )
                self.log.emit(
                    f"构建验证通过，{artifact_labels} 产物生成完成。"
                )

            self.progress.emit(100, "转换完成")
            validation_seconds = (
                time.monotonic() - validation_started
                if validation_started is not None
                else -1.0
            )
            self.finished.emit(
                True, success_message, self.output_dir, validation_seconds
            )
        except Exception as exc:
            validation_seconds = (
                time.monotonic() - validation_started
                if validation_started is not None
                else -1.0
            )
            self.finished.emit(
                False, str(exc), self.output_dir, validation_seconds
            )


class MainWindow(QMainWindow):
    DEFAULT_WINDOW_SIZE = (1120, 760)
    MINIMUM_WINDOW_SIZE = (560, 520)
    COMPACT_LAYOUT_WIDTH = 940

    def __init__(self):
        super().__init__()
        self.worker_thread = None
        self.worker = None
        self.stage_badges = []
        self.stage_names = []
        self.stage_details = []
        self.current_stage = 0
        self.validation_started = None
        self.validation_timer = QTimer(self)
        self.validation_timer.setInterval(100)
        self.validation_timer.timeout.connect(self._refresh_validation_elapsed)
        self._entrance_animation = None
        self.setWindowTitle("KeilForge")
        self.setMinimumSize(*self.MINIMUM_WINDOW_SIZE)
        self.resize(*self.DEFAULT_WINDOW_SIZE)
        self._build_ui()
        self._connect_signals()
        self._load_config()

    def _build_ui(self):
        root = QWidget()
        root.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.nav_frame = QFrame(objectName="topNav")
        nav_layout = QHBoxLayout(self.nav_frame)
        nav_layout.setContentsMargins(4, 4, 4, 4)
        nav_layout.setSpacing(4)
        self.nav_buttons = []
        self.nav_labels = (
            ("工作台", "工作台", "工程转换"),
            ("构建", "构建设置", "构建配置"),
            ("调试", "调试设置", "调试与烧录"),
            ("日志", "运行日志", "运行日志"),
        )
        for index, (_, text, tooltip) in enumerate(self.nav_labels):
            button = QToolButton(objectName="navButtonActive" if index == 0 else "navButton")
            button.setText(text)
            button.setIcon(_ui_icon(("project", "build", "debug", "log")[index]))
            button.setIconSize(QSize(17, 17))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(tooltip)
            button.setToolTip(tooltip)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            nav_layout.addWidget(button)
            self.nav_buttons.append(button)

        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        self.top_bar = QFrame(objectName="topBar")
        self.top_layout = QGridLayout(self.top_bar)
        self.top_layout.setContentsMargins(32, 14, 32, 14)
        self.top_layout.setHorizontalSpacing(24)
        self.top_layout.setVerticalSpacing(12)

        self.top_brand = QWidget(objectName="topBrand")
        brand_layout = QVBoxLayout(self.top_brand)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(0)
        brand_layout.addWidget(QLabel("KeilForge", objectName="brand"))
        brand_layout.addWidget(QLabel("KEIL TO CMAKE", objectName="topEyebrow"))
        self.top_layout.addWidget(self.top_brand, 0, 0)
        self.top_layout.addWidget(self.nav_frame, 0, 1, Qt.AlignmentFlag.AlignCenter)
        self.top_layout.setColumnStretch(1, 1)
        self.status_pill = QLabel("就绪", objectName="statusPill")
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_pill.setFixedHeight(28)
        self.status_pill.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.top_layout.addWidget(self.status_pill, 0, 2, Qt.AlignmentFlag.AlignRight)
        self.open_output_button = QPushButton("输出目录", objectName="secondaryButton")
        self.open_output_button.setIcon(_ui_icon("folder"))
        self.open_output_button.setToolTip("打开生成工程的文件夹")
        self.open_output_button.setFixedHeight(38)
        self.open_output_button.setEnabled(False)
        self.validate_check = QCheckBox(
            "生成后执行编译验证", objectName="validationCheck"
        )
        self.validate_check.setChecked(True)
        self.validate_check.setToolTip(
            "勾选后将在 CMake 工程生成完成后执行配置、编译和固件产物检查。"
        )
        self.convert_button = QPushButton("转换并验证", objectName="primary")
        self.convert_button.setFixedWidth(174)
        self.convert_button.setIcon(_ui_icon("arrow", "#ffffff"))
        self.convert_button.setIconSize(QSize(18, 18))
        self.convert_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.convert_button.setShortcut("Ctrl+Return")
        shell_layout.addWidget(self.top_bar)

        self.progress = QProgressBar(objectName="mainProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFormat("0% · 就绪")

        self.page_stack = QStackedWidget()

        project_page = QScrollArea()
        project_page.setWidgetResizable(True)
        project_page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        project_surface = QWidget(objectName="pageSurface")
        self.project_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, project_surface)
        self.project_layout.setContentsMargins(36, 30, 36, 30)
        self.project_layout.setSpacing(36)
        content = QWidget(objectName="pageSurface")
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content_layout = QVBoxLayout(content)
        self.content_layout = content_layout
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(20)

        heading = QHBoxLayout()
        heading_box = QVBoxLayout()
        heading_box.setSpacing(9)
        heading_box.addWidget(QLabel("工程转换", objectName="pageTitle"))
        self.project_description = QLabel("从 Keil 工程开始，生成完整的 CMake 工程。", objectName="muted")
        self.project_description.setWordWrap(True)
        heading_box.addWidget(self.project_description)
        heading.addLayout(heading_box, 1)
        content_layout.addLayout(heading)

        pipeline = QFrame(objectName="pipeline")
        self.pipeline = pipeline
        pipeline_layout = QHBoxLayout(pipeline)
        pipeline_layout.setContentsMargins(0, 0, 0, 4)
        pipeline_layout.setSpacing(9)
        stages = (
            ("1", "选择工程", "Keil uVision"),
            ("2", "解析配置", "源文件与器件"),
            ("3", "生成工程", "CMake 工具链"),
            ("4", "编译验证", "检查生成固件"),
        )
        for index, (number, name, detail) in enumerate(stages):
            stage = QFrame(objectName="stage")
            stage_layout = QHBoxLayout(stage)
            stage_layout.setContentsMargins(0, 0, 0, 0)
            stage_layout.setSpacing(8)
            badge = QLabel(number, objectName="stageIndex")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setProperty("state", "complete" if index == 0 else "pending")
            copy = QVBoxLayout()
            copy.setSpacing(0)
            name_label = QLabel(name, objectName="stageName")
            name_label.setProperty("state", "complete" if index == 0 else "pending")
            name_label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            copy.addWidget(name_label)
            detail_label = QLabel(detail, objectName="stageDetail")
            detail_label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            copy.addWidget(detail_label)
            stage_layout.addWidget(badge)
            stage_layout.addLayout(copy)
            pipeline_layout.addWidget(stage, 1)
            self.stage_badges.append(badge)
            self.stage_names.append(name_label)
            self.stage_details.append(detail_label)
            if index < len(stages) - 1:
                arrow = QLabel("›", objectName="muted")
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                pipeline_layout.addWidget(arrow)
        content_layout.addWidget(pipeline)

        self.project_drop = ProjectDropArea()
        drop_layout = QVBoxLayout(self.project_drop)
        drop_layout.setContentsMargins(22, 22, 22, 22)
        drop_layout.setSpacing(20)
        drop_header = QHBoxLayout()
        drop_header.setSpacing(16)
        document_icon = QLabel(objectName="documentIcon")
        document_icon.setPixmap(
            _ui_icon("project", size=40).pixmap(QSize(40, 40), self.devicePixelRatioF())
        )
        document_icon.setFixedSize(60, 60)
        document_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_header.addWidget(document_icon)
        drop_copy = QVBoxLayout()
        drop_copy.setSpacing(5)
        self.inspector_project = QLabel("拖入 Keil 工程", objectName="projectName")
        self.inspector_project.setMinimumWidth(0)
        self.inspector_project.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        drop_copy.addWidget(self.inspector_project)
        drop_copy.addWidget(QLabel("支持 .uvprojx 与 .uvproj 文件", objectName="muted"))
        drop_header.addLayout(drop_copy, 1)
        drop_layout.addLayout(drop_header)
        project_row = QHBoxLayout()
        project_row.setSpacing(10)
        self.project_edit = PathHistoryEdit()
        self.project_edit.setAcceptDrops(False)
        self.project_edit.setPlaceholderText("选择文件，或输入工程文件位置")
        self.project_edit.setAccessibleName("Keil 工程")
        self.project_edit.setMinimumWidth(0)
        self.project_button = QPushButton("选择文件", objectName="importButton")
        self.project_button.setCursor(Qt.CursorShape.PointingHandCursor)
        project_row.addWidget(self.project_edit, 1)
        project_row.addWidget(self.project_button)
        drop_layout.addLayout(project_row)
        content_layout.addWidget(self.project_drop)

        file_grid = QGridLayout()
        file_grid.setHorizontalSpacing(10)
        file_grid.setVerticalSpacing(9)
        file_grid.setColumnStretch(0, 1)
        file_grid.setColumnStretch(1, 0)
        self.output_edit, self.output_button = self._path_field(
            file_grid, 0, "保存到", "选择工程后自动填写输出目录"
        )
        content_layout.addLayout(file_grid)

        self.compiler_combo = self._combo(SUPPORTED_COMPILERS)
        self.host_combo = self._combo(SUPPORTED_HOST_SYSTEMS)
        self.generator_combo = QComboBox()
        self.generator_combo.setEditable(True)
        self.probe_combo = self._combo(SUPPORTED_DEBUG_PROBES)
        self.backend_combo = self._combo(SUPPORTED_DEBUG_BACKENDS)
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("留空时使用第一个 Target")
        self.build_dir_edit = QLineEdit()
        self.build_dir_edit.setPlaceholderText("build-gcc")

        context = QFrame(objectName="outputSummary")
        context_layout = QHBoxLayout(context)
        context_layout.setContentsMargins(0, 18, 0, 0)
        context_layout.setSpacing(24)
        target_box = QVBoxLayout()
        self.inspector_target = self._metric("工程目标", "自动识别", target_box)
        context_layout.addLayout(target_box, 1)
        probe_box = QVBoxLayout()
        self.inspector_probe = self._metric("调试探针", "全部", probe_box)
        context_layout.addLayout(probe_box, 1)
        self.settings_button = QPushButton("调整设置", objectName="secondaryButton")
        self.settings_button.setIcon(_ui_icon("build"))
        context_layout.addWidget(self.settings_button)
        content_layout.addWidget(context)
        content_layout.addStretch(1)

        inspector = QFrame(objectName="inspector")
        self.inspector_panel = inspector
        inspector.setMinimumWidth(0)
        inspector_layout = QVBoxLayout(inspector)
        inspector_layout.setContentsMargins(26, 24, 26, 24)
        inspector_layout.setSpacing(9)
        inspector_layout.addWidget(QLabel("OUTPUT", objectName="eyebrow"))
        inspector_layout.addWidget(QLabel("生成预览", objectName="previewTitle"))
        self.inspector_compiler = QLabel(objectName="muted")
        self.inspector_compiler.setWordWrap(True)
        inspector_layout.addWidget(self.inspector_compiler)
        inspector_layout.addSpacing(4)
        self._preview_file("CMakeLists.txt", "构建入口", inspector_layout)
        self._preview_file("cmake/", "工具链配置", inspector_layout)
        self.vscode_preview, _ = self._preview_file(".vscode/", "调试与任务", inspector_layout)
        self.firmware_preview, self.firmware_preview_name = self._preview_file(
            "ELF · HEX · BIN", "编译产物", inspector_layout
        )
        self.summary_detail = QLabel(objectName="muted")
        self.summary_detail.setWordWrap(True)
        inspector_layout.addWidget(self.summary_detail)
        inspector_layout.addSpacing(4)
        result_row = QHBoxLayout()
        status_box = QVBoxLayout()
        self.inspector_status = self._metric("运行状态", "等待转换", status_box)
        result_row.addLayout(status_box, 1)
        time_box = QVBoxLayout()
        self.inspector_validation_time = self._metric("验证耗时", "待验证", time_box)
        result_row.addLayout(time_box, 1)
        inspector_layout.addLayout(result_row)
        inspector_layout.addWidget(QLabel("固件占用", objectName="fieldLabel"))
        flash_row = QHBoxLayout()
        flash_row.addWidget(QLabel("FLASH", objectName="stageName"))
        flash_row.addStretch(1)
        self.flash_usage_label = QLabel("构建后显示", objectName="muted")
        self.flash_usage_label.setWordWrap(True)
        flash_row.addWidget(self.flash_usage_label)
        inspector_layout.addLayout(flash_row)
        self.flash_bar = QProgressBar(objectName="memoryBar")
        self.flash_bar.setRange(0, 100)
        self.flash_bar.setValue(0)
        self.flash_bar.setTextVisible(False)
        inspector_layout.addWidget(self.flash_bar)
        ram_row = QHBoxLayout()
        ram_row.addWidget(QLabel("RAM", objectName="stageName"))
        ram_row.addStretch(1)
        self.ram_usage_label = QLabel("构建后显示", objectName="muted")
        self.ram_usage_label.setWordWrap(True)
        ram_row.addWidget(self.ram_usage_label)
        inspector_layout.addLayout(ram_row)
        self.ram_bar = QProgressBar(objectName="memoryBar")
        self.ram_bar.setRange(0, 100)
        self.ram_bar.setValue(0)
        self.ram_bar.setTextVisible(False)
        inspector_layout.addWidget(self.ram_bar)
        inspector_layout.addStretch(1)

        self.project_layout.addWidget(content, 3)
        self.project_layout.addWidget(inspector, 2)
        project_page.setWidget(project_surface)
        self.page_stack.addWidget(project_page)

        config_page = QScrollArea()
        config_page.setWidgetResizable(True)
        config_content = QWidget(objectName="pageSurface")
        config_layout = QVBoxLayout(config_content)
        self.config_layout = config_layout
        config_layout.setContentsMargins(36, 30, 36, 32)
        config_layout.setSpacing(18)
        config_layout.addWidget(QLabel("构建配置", objectName="pageTitle"))
        config_description = QLabel("选择编译器与构建方式，配置工程目标。", objectName="muted")
        config_description.setWordWrap(True)
        config_layout.addWidget(config_description)
        profile_grid = QGridLayout()
        profile_grid.setHorizontalSpacing(16)
        profile_grid.setVerticalSpacing(12)

        fields = [
            ("编译器", self.compiler_combo),
            ("宿主系统", self.host_combo),
            ("CMake 生成器", self.generator_combo),
            ("Keil 目标名称", self.target_edit),
            ("构建目录名称", self.build_dir_edit),
        ]
        self.profile_grid = profile_grid
        self.profile_fields = []
        for index, (label, widget) in enumerate(fields):
            row, column = divmod(index, 2)
            field = QWidget()
            field.setMinimumWidth(0)
            box = QVBoxLayout(field)
            box.setContentsMargins(0, 0, 0, 6)
            box.setSpacing(9)
            box.addWidget(QLabel(label, objectName="fieldLabel"))
            box.addWidget(widget)
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            profile_grid.addWidget(field, row, column)
            self.profile_fields.append(field)
        profile_grid.setColumnStretch(0, 1)
        profile_grid.setColumnStretch(1, 1)
        config_layout.addLayout(profile_grid)

        toggles = QHBoxLayout()
        self.vscode_check = QCheckBox("生成 VS Code 配置与调试任务")
        self.vscode_check.setChecked(True)
        toggles.addWidget(self.vscode_check)
        toggles.addStretch(1)
        config_layout.addLayout(toggles)
        config_layout.addWidget(QFrame(objectName="sectionRule"))
        mdk_grid = QGridLayout()
        mdk_grid.setHorizontalSpacing(10)
        mdk_grid.setVerticalSpacing(9)
        mdk_grid.setColumnStretch(0, 1)
        self.uv4_edit, self.uv4_button = self._path_field(
            mdk_grid, 0, "Keil MDK 安装目录 · 可选", "自动检测安装目录"
        )
        config_layout.addLayout(mdk_grid)
        config_layout.addStretch(1)
        config_page.setWidget(config_content)
        self.page_stack.addWidget(config_page)

        debug_page = QScrollArea()
        debug_page.setWidgetResizable(True)
        debug_content = QWidget(objectName="pageSurface")
        debug_layout = QVBoxLayout(debug_content)
        self.debug_layout = debug_layout
        debug_layout.setContentsMargins(36, 30, 36, 32)
        debug_layout.setSpacing(22)
        debug_layout.addWidget(QLabel("调试与烧录", objectName="pageTitle"))
        debug_description = QLabel("为选定探针生成调试与固件烧录任务。", objectName="muted")
        debug_description.setWordWrap(True)
        debug_layout.addWidget(debug_description)
        debug_layout.addSpacing(8)
        debug_grid = QGridLayout()
        debug_grid.setHorizontalSpacing(16)
        debug_grid.setVerticalSpacing(14)
        for column, (label, widget) in enumerate(
            (("调试探针", self.probe_combo), ("调试后端", self.backend_combo))
        ):
            box = QVBoxLayout()
            box.setSpacing(9)
            box.addWidget(QLabel(label, objectName="fieldLabel"))
            box.addWidget(widget)
            debug_grid.addLayout(box, 0, column)
        debug_layout.addLayout(debug_grid)
        debug_assets = QFrame(objectName="outputSummary")
        debug_assets_layout = QVBoxLayout(debug_assets)
        debug_assets_layout.setContentsMargins(0, 22, 0, 12)
        debug_assets_layout.setSpacing(12)
        debug_assets_layout.addWidget(QLabel("调试配置文件", objectName="section"))
        debug_assets_layout.addWidget(QLabel(".vscode/launch.json", objectName="muted"))
        debug_assets_layout.addWidget(QLabel(".vscode/tasks.json", objectName="muted"))
        debug_assets_layout.addWidget(QLabel("OpenOCD · J-Link · ST-Link 烧录任务", objectName="muted"))
        debug_layout.addWidget(debug_assets)
        debug_actions = QHBoxLayout()
        self.debug_open_output_button = QPushButton("打开输出目录")
        self.debug_open_vscode_button = QPushButton("打开 .vscode")
        debug_actions.addWidget(self.debug_open_output_button)
        debug_actions.addWidget(self.debug_open_vscode_button)
        debug_actions.addStretch(1)
        debug_layout.addLayout(debug_actions)
        debug_layout.addStretch(1)
        debug_page.setWidget(debug_content)
        self.page_stack.addWidget(debug_page)

        console = QFrame(objectName="console")
        console_layout = QVBoxLayout(console)
        console_layout.setContentsMargins(28, 26, 28, 28)
        console_layout.setSpacing(18)
        console_header = QHBoxLayout()
        console_header.setContentsMargins(0, 0, 0, 0)
        console_header.addWidget(QLabel("运行日志", objectName="consoleTitle"))
        console_header.addStretch(1)
        self.clear_log_button = QPushButton("清空", objectName="consoleButton")
        console_header.addWidget(self.clear_log_button)
        console_layout.addLayout(console_header)
        self.log_browser = QTextBrowser()
        self.log_browser.setPlaceholderText("转换与构建过程将显示在这里。")
        self.log_browser.setMinimumHeight(180)
        console_layout.addWidget(self.log_browser, 1)
        self.page_stack.addWidget(console)
        shell_layout.addWidget(self.page_stack, 1)

        action_bar = QFrame(objectName="actionBar")
        self.action_layout = QHBoxLayout(action_bar)
        self.action_layout.setContentsMargins(32, 16, 28, 16)
        self.action_layout.setSpacing(16)
        self.action_layout.addWidget(self.open_output_button)
        self.action_layout.addStretch(1)
        self.action_layout.addWidget(self.validate_check)
        self.action_layout.addWidget(self.convert_button)
        shell_layout.addWidget(action_bar)
        shell_layout.addWidget(self.progress)

        status_bar = QFrame(objectName="statusBar")
        status_layout = QHBoxLayout(status_bar)
        status_layout.setContentsMargins(32, 7, 28, 7)
        self.progress_label = QLabel("选择工程后即可开始", objectName="railCaption")
        self.progress_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        status_layout.addWidget(self.progress_label, 1)
        status_layout.addStretch(1)
        self.shortcut_label = QLabel("Ctrl + Enter  开始转换", objectName="railCaption")
        status_layout.addWidget(self.shortcut_label)
        shell_layout.addWidget(status_bar)

        root_layout.addWidget(shell)
        self.setCentralWidget(root)
        self._fade_effect = QGraphicsOpacityEffect(root)
        root.setGraphicsEffect(self._fade_effect)
        self._fade_effect.setOpacity(0.0)

    @staticmethod
    def _preview_file(name, description, layout):
        frame = QFrame(objectName="previewFile")
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(_ui_icon("folder" if name.endswith("/") else "project").pixmap(18, 18))
        row.addWidget(icon)
        title = QLabel(name, objectName="fileName")
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(title, 1)
        row.addWidget(QLabel(description, objectName="fileDescription"))
        layout.addWidget(frame)
        return frame, title

    @staticmethod
    def _metric(label, value, layout):
        frame = QFrame(objectName="metric")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(0, 2, 0, 5)
        frame_layout.setSpacing(4)
        frame_layout.addWidget(QLabel(label.upper(), objectName="fieldLabel"))
        value_label = QLabel(value, objectName="metricValue")
        value_label.setWordWrap(True)
        value_label.setMinimumWidth(0)
        value_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        frame_layout.addWidget(value_label)
        layout.addWidget(frame)
        return value_label

    def _path_field(self, grid, row, label, placeholder):
        label_widget = QLabel(label, objectName="fieldLabel")
        edit = PathHistoryEdit()
        edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        edit.setMinimumWidth(0)
        edit.setPlaceholderText(placeholder)
        edit.setAccessibleName(label)
        label_widget.setBuddy(edit)
        button = QPushButton("浏览", objectName="fieldButton")
        button.setFixedWidth(88)
        button.setIcon(_ui_icon("folder"))
        button.setIconSize(QSize(16, 16))
        button.setAccessibleName("选择" + label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        grid.addWidget(label_widget, row * 3, 0, 1, 2)
        grid.addWidget(edit, row * 3 + 1, 0)
        grid.addWidget(button, row * 3 + 1, 1)
        grid.setRowMinimumHeight(row * 3 + 2, 10)
        return edit, button

    @staticmethod
    def _combo(values):
        combo = QComboBox()
        for key, label in values.items():
            combo.addItem({"all": "全部", "default": "自动选择"}.get(key, label), key)
        return combo

    def _connect_signals(self):
        for index, button in enumerate(self.nav_buttons):
            button.clicked.connect(
                lambda checked=False, page_index=index: self._show_page(page_index)
            )
        self.project_button.clicked.connect(self._browse_project)
        self.project_drop.project_dropped.connect(self._select_project)
        self.settings_button.clicked.connect(lambda: self._show_page(1))
        self.output_button.clicked.connect(self._browse_output)
        self.uv4_button.clicked.connect(self._browse_uv4)
        self.convert_button.clicked.connect(self._start_conversion)
        self.validate_check.toggled.connect(self._sync_validation_action)
        self.open_output_button.clicked.connect(self._open_output)
        self.clear_log_button.clicked.connect(self.log_browser.clear)
        self.debug_open_output_button.clicked.connect(self._open_output)
        self.debug_open_vscode_button.clicked.connect(self._open_vscode)
        self.host_combo.currentIndexChanged.connect(self._update_generators)
        self.host_combo.currentIndexChanged.connect(self._sync_backends)
        self.probe_combo.currentIndexChanged.connect(self._sync_backends)
        self.project_edit.textChanged.connect(self._project_path_changed)
        self.compiler_combo.currentIndexChanged.connect(self._refresh_inspector)
        self.compiler_combo.currentIndexChanged.connect(self._sync_backends)
        self.vscode_check.toggled.connect(self._refresh_inspector)
        self.target_edit.textChanged.connect(self._refresh_inspector)
        self.probe_combo.currentIndexChanged.connect(self._refresh_inspector)

    def _update_nav_labels(self, *args):
        expanded = self.width() >= 760
        for button, (compact, full, _) in zip(self.nav_buttons, self.nav_labels):
            button.setText(full if expanded else compact)

    @classmethod
    def _is_compact_width(cls, width):
        return int(width) < cls.COMPACT_LAYOUT_WIDTH

    @staticmethod
    def _conversion_action_text(validate_build):
        return "转换并验证" if validate_build else "仅转换"

    def _sync_validation_action(self, validate_build):
        self.convert_button.setText(
            self._conversion_action_text(validate_build)
        )
        self.convert_button.setToolTip(
            "生成 CMake 工程并执行编译验证。"
            if validate_build
            else "仅生成 CMake 工程。"
        )
        if hasattr(self, "summary_detail"):
            self._refresh_output_summary()

    def _update_compact_layout(self):
        if not hasattr(self, "shortcut_label"):
            return
        compact = self._is_compact_width(self.width())
        narrow = self.width() < 760
        self.open_output_button.setText("目录" if narrow else "输出目录")
        self.validate_check.setText(
            "编译验证" if narrow else "生成后编译验证"
        )
        self.convert_button.setFixedWidth(152 if narrow else 174)
        self.shortcut_label.setVisible(not narrow)
        self.pipeline.setVisible(self.height() >= 600)
        for detail in self.stage_details:
            detail.setVisible(False)
        for name in self.stage_names:
            name.setVisible(True)
        self.project_layout.setDirection(
            QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
        )
        self.project_layout.setStretch(0, 0 if compact else 3)
        self.project_layout.setStretch(1, 0 if compact else 2)
        margin = 22 if narrow else 36
        for layout in (self.project_layout, self.config_layout, self.debug_layout):
            layout.setContentsMargins(margin, 26 if narrow else 30, margin, 24)
        self.action_layout.setContentsMargins(22 if narrow else 32, 14, 22 if narrow else 28, 14)
        if getattr(self, "_narrow_fields", None) != narrow:
            self._narrow_fields = narrow
            self.top_layout.removeWidget(self.nav_frame)
            if narrow:
                self.top_layout.addWidget(self.nav_frame, 1, 0, 1, 3)
            else:
                self.top_layout.addWidget(self.nav_frame, 0, 1, Qt.AlignmentFlag.AlignCenter)
            for field in self.profile_fields:
                self.profile_grid.removeWidget(field)
            for index, field in enumerate(self.profile_fields):
                row, column = (index, 0) if narrow else divmod(index, 2)
                self.profile_grid.addWidget(field, row, column)
            self.profile_grid.setColumnStretch(1, 0 if narrow else 1)
        self._update_nav_labels()

    def _show_page(self, index):
        if not 0 <= index < self.page_stack.count():
            return
        changed = self.page_stack.currentIndex() != index
        self.page_stack.setCurrentIndex(index)
        for button_index, button in enumerate(self.nav_buttons):
            button.setObjectName(
                "navButtonActive" if button_index == index else "navButton"
            )
            self._refresh_dynamic_style(button)
        if changed:
            if not hasattr(self, "_page_effect"):
                self._page_effect = QGraphicsOpacityEffect(self.page_stack)
                self.page_stack.setGraphicsEffect(self._page_effect)
                self._page_animation = QPropertyAnimation(self._page_effect, b"opacity", self)
                self._page_animation.setDuration(160)
                self._page_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._page_animation.stop()
            self._page_animation.setStartValue(0.55)
            self._page_animation.setEndValue(1.0)
            self._page_animation.start()

    def _browse_project(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Keil 工程",
            QDir.homePath(),
            "Keil Projects (*.uvprojx *.uvproj)",
        )
        if not file_path:
            return
        self._select_project(file_path)

    def _select_project(self, file_path):
        self.project_edit.setText(QDir.toNativeSeparators(file_path))
        self._remember_path(self.project_edit, file_path)

    @staticmethod
    def _default_output_directory(project_path):
        project_path = project_path.strip()
        if not project_path:
            return ""
        return str(Path(project_path).parent / "cmake-generated")

    def _project_path_changed(self, project_path):
        output = self._default_output_directory(project_path)
        self.output_edit.setText(QDir.toNativeSeparators(output))
        self._refresh_inspector()

    def _browse_output(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self.output_edit.text().strip() or QDir.homePath()
        )
        if directory:
            self.output_edit.setText(QDir.toNativeSeparators(directory))
            self._remember_path(self.output_edit, directory)

    def _browse_uv4(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择 Keil MDK 目录", self.uv4_edit.text().strip() or QDir.homePath()
        )
        if directory:
            self.uv4_edit.setText(QDir.toNativeSeparators(directory))

    def _set_combo(self, combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    @staticmethod
    def _generator_target(host, preferred, current, host_changed):
        if host_changed:
            return DEFAULT_GENERATOR_BY_HOST.get(host, "Ninja")
        return preferred or current or DEFAULT_GENERATOR_BY_HOST.get(host, "Ninja")

    def _update_generators(self, preferred=""):
        host_changed = not isinstance(preferred, str)
        current = self.generator_combo.currentText().strip()
        if host_changed:
            preferred = ""
        host = self.host_combo.currentData() or "windows"
        values = get_supported_generators(host)
        self.generator_combo.blockSignals(True)
        self.generator_combo.clear()
        self.generator_combo.addItems(values)
        target = self._generator_target(host, preferred, current, host_changed)
        index = self.generator_combo.findText(target)
        if index >= 0:
            self.generator_combo.setCurrentIndex(index)
        else:
            self.generator_combo.setEditText(target)
        self.generator_combo.blockSignals(False)

    def _sync_backends(self):
        probe = self.probe_combo.currentData() or "default"
        host = self.host_combo.currentData() or "windows"
        compiler = self.compiler_combo.currentData() or "gcc"
        current = self.backend_combo.currentData() or "default"
        self.backend_combo.blockSignals(True)
        self.backend_combo.clear()
        for key, label in SUPPORTED_DEBUG_BACKENDS.items():
            if probe not in {"default", "all", "jlink"} and key == "jlink":
                continue
            if probe not in {"default", "all", "daplink", "ulink"} and key == "pyocd":
                continue
            if (host != "windows" or compiler != "armclang") and key == "keil":
                continue
            self.backend_combo.addItem({"all": "全部", "default": "自动选择"}.get(key, label), key)
        self._set_combo(self.backend_combo, current)
        self.backend_combo.blockSignals(False)

    def _refresh_inspector(self):
        project_path = self.project_edit.text().strip()
        self.inspector_project.setText(Path(project_path).name if project_path else "拖入 Keil 工程")
        self.inspector_project.setWordWrap(False)
        self.inspector_project.setToolTip(project_path)
        self.inspector_compiler.setText(self.compiler_combo.currentText() or "未选择")
        self.inspector_target.setText(self.target_edit.text().strip() or "自动识别")
        self.inspector_probe.setText(
            "全部探针" if self.probe_combo.currentData() == "all" else self.probe_combo.currentText() or "未选择"
        )
        self._refresh_output_summary()

    def _refresh_output_summary(self):
        self.vscode_preview.setVisible(self.vscode_check.isChecked())
        self.firmware_preview.setVisible(self.validate_check.isChecked())
        self.firmware_preview_name.setText(
            "ELF · HEX · BIN" if self.compiler_combo.currentData() == "gcc" else "AXF 固件"
        )
        self.summary_detail.setText(
            "生成工程后执行编译验证" if self.validate_check.isChecked() else "生成工程配置，跳过编译"
        )

    @staticmethod
    def _format_memory_size(value):
        if value >= 1024 * 1024:
            return f"{value / (1024 * 1024):.2f} MiB"
        if value >= 1024:
            return f"{value / 1024:.1f} KiB"
        return f"{value} B"

    @Slot(int, int, int, int)
    def _update_memory_usage(self, flash_used, flash_total, ram_used, ram_total):
        def update(label, bar, used, total):
            percent = round(used * 100 / total, 1) if total else 0.0
            label.setText(f"{self._format_memory_size(used)} / {self._format_memory_size(total)} · {percent:.1f}%")
            bar.setValue(max(0, min(100, round(percent))))

        update(self.flash_usage_label, self.flash_bar, flash_used, flash_total)
        update(self.ram_usage_label, self.ram_bar, ram_used, ram_total)

    @staticmethod
    def _refresh_dynamic_style(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _set_status(self, text, state):
        self.status_pill.setText(text)
        self.status_pill.setProperty("state", state)
        self._refresh_dynamic_style(self.status_pill)

    def _set_stage_state(self, stage_index, state):
        if not 0 <= stage_index < len(self.stage_badges):
            return
        for widget in (self.stage_badges[stage_index], self.stage_names[stage_index]):
            widget.setProperty("state", state)
            self._refresh_dynamic_style(widget)

    def _reset_stages(self):
        self.current_stage = 0
        for index in range(len(self.stage_badges)):
            self._set_stage_state(index, "running" if index == 0 else "pending")

    def _advance_stage(self, stage_index):
        stage_index = max(0, min(stage_index, len(self.stage_badges) - 1))
        for index in range(stage_index):
            self._set_stage_state(index, "complete")
        self._set_stage_state(stage_index, "running")
        for index in range(stage_index + 1, len(self.stage_badges)):
            self._set_stage_state(index, "pending")
        self.current_stage = stage_index

    def _handle_worker_log(self, message):
        if "解析 Keil 工程" in message:
            self._advance_stage(0)
        elif "目标:" in message:
            self._advance_stage(1)
        elif "生成 CMake" in message:
            self._advance_stage(2)
        elif "编译验证" in message or "构建验证" in message:
            self._advance_stage(3)
            if message.startswith("执行 CMake 配置与编译验证"):
                self._start_validation_timer()
        self._log(message)

    @staticmethod
    def _format_duration(seconds):
        seconds = max(0.0, float(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours >= 1:
            return f"{int(hours)} 小时 {int(minutes):02d} 分 {seconds:04.1f} 秒"
        if minutes >= 1:
            return f"{int(minutes)} 分 {seconds:04.1f} 秒"
        return f"{seconds:.1f} 秒"

    @classmethod
    def _with_validation_duration(cls, message, validation_seconds):
        timing_text = (
            cls._format_duration(validation_seconds)
            if validation_seconds >= 0
            else "未执行"
        )
        return f"{message}\n\n验证耗时\n{timing_text}"

    def _start_validation_timer(self):
        if self.validation_started is not None:
            return
        self.validation_started = time.monotonic()
        self.inspector_validation_time.setText("0.0 秒")
        self.validation_timer.start()

    def _refresh_validation_elapsed(self):
        if self.validation_started is None:
            return
        elapsed = time.monotonic() - self.validation_started
        self.inspector_validation_time.setText(self._format_duration(elapsed))

    def _stop_validation_timer(self, validation_seconds):
        self.validation_timer.stop()
        self.validation_started = None
        if validation_seconds >= 0:
            self.inspector_validation_time.setText(
                self._format_duration(validation_seconds)
            )
        else:
            self.inspector_validation_time.setText("未执行")

    @Slot(int, str)
    def _update_conversion_progress(self, value, message):
        value = max(0, min(100, value))
        self.progress.setRange(0, 100)
        self.progress.setValue(value)
        self.progress.setFormat(f"{value}% · {message}")
        self.progress_label.setText(f"{message} · {value}%")
        self.progress_label.setToolTip(message)

    def _options(self):
        return GenerationOptions(
            compiler=self.compiler_combo.currentData() or "gcc",
            generator=self.generator_combo.currentText().strip() or "Ninja",
            host_os=self.host_combo.currentData() or "windows",
            debugger=self.backend_combo.currentData() or "all",
            debug_probe=self.probe_combo.currentData() or "all",
            debug_backend=self.backend_combo.currentData() or "all",
            build_dir=self.build_dir_edit.text().strip() or None,
            export_vsc_settings=self.vscode_check.isChecked(),
        ).normalized()

    def _start_conversion(self):
        project_file = self.project_edit.text().strip()
        output_dir = self.output_edit.text().strip()
        if not project_file or not Path(project_file).is_file():
            QMessageBox.critical(self, "工程无效", "请选择有效的 Keil 工程文件。")
            return
        if not output_dir:
            QMessageBox.critical(self, "输出目录无效", "请选择输出目录。")
            return

        try:
            options = self._options()
        except Exception as exc:
            QMessageBox.critical(self, "配置错误", str(exc))
            return

        self._remember_path(self.project_edit, project_file)
        self._remember_path(self.output_edit, output_dir)
        self._save_config()
        self.convert_button.setEnabled(False)
        self.project_drop.setEnabled(False)
        self.validate_check.setEnabled(False)
        self.open_output_button.setEnabled(False)
        self._set_status("转换中", "running")
        self.inspector_status.setText("正在转换")
        self.validation_timer.stop()
        self.validation_started = None
        self.inspector_validation_time.setText(
            "等待开始" if self.validate_check.isChecked() else "未执行"
        )
        memory_state = "正在构建" if self.validate_check.isChecked() else "未执行验证"
        self.flash_usage_label.setText(memory_state)
        self.ram_usage_label.setText(memory_state)
        self.flash_bar.setValue(0)
        self.ram_bar.setValue(0)
        self._reset_stages()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("0% · 准备转换")
        self.progress_label.setText("准备转换 · 0%")
        self.log_browser.clear()
        self._log(f"输入: {project_file}")
        self._log(f"输出: {output_dir}")
        self._log(
            f"编译器: {self.compiler_combo.currentText()} ({options.compiler})"
        )
        self._log(f"CMake 生成器: {options.generator}")

        self.worker_thread = QThread(self)
        self.worker = ConversionWorker(
            project_file,
            output_dir,
            self.uv4_edit.text().strip(),
            self.target_edit.text().strip(),
            options,
            self.validate_check.isChecked(),
        )
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self._handle_worker_log)
        self.worker.progress.connect(self._update_conversion_progress)
        self.worker.memory_usage.connect(self._update_memory_usage)
        self.worker.finished.connect(self._conversion_finished)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self._release_worker)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    @Slot()
    def _release_worker(self):
        if self.sender() is self.worker_thread:
            self.worker_thread = None
            self.worker = None

    @staticmethod
    def _completion_status(success, validation_seconds):
        validation_completed = validation_seconds >= 0
        if success:
            text = "构建通过" if validation_completed else "转换完成"
        else:
            text = "构建失败" if validation_completed else "转换失败"
        return text, validation_completed

    @Slot(bool, str, str, float)
    def _conversion_finished(self, success, message, output_dir, validation_seconds):
        self._stop_validation_timer(validation_seconds)
        message = self._with_validation_duration(message, validation_seconds)
        self.progress.setRange(0, 100)
        if success:
            self.progress.setValue(100)
            self.progress.setFormat("100% · 转换完成")
        else:
            self.progress.setFormat(f"{self.progress.value()}% · 转换失败")
        self.convert_button.setEnabled(True)
        self.project_drop.setEnabled(True)
        self.validate_check.setEnabled(True)
        self.open_output_button.setEnabled(bool(output_dir))
        self._set_status(
            "成功" if success else "失败",
            "success" if success else "failed",
        )
        inspector_text, validation_completed = self._completion_status(
            success, validation_seconds
        )
        self.inspector_status.setText(inspector_text)
        self.progress_label.setText(inspector_text)
        if success:
            for index in range(len(self.stage_badges)):
                state = (
                    "pending"
                    if index == len(self.stage_badges) - 1
                    and not validation_completed
                    else "complete"
                )
                self._set_stage_state(index, state)
        else:
            self._set_stage_state(self.current_stage, "failed")
            if self.flash_usage_label.text() == "正在构建":
                self.flash_usage_label.setText("构建失败")
                self.ram_usage_label.setText("构建失败")
        self._log(message)
        if success:
            QMessageBox.information(self, "转换完成", message)
        else:
            self._show_page(3)
            QMessageBox.critical(self, "转换失败", message)

    def _open_output(self):
        output = self.output_edit.text().strip()
        if output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(output))

    def _open_vscode(self):
        output = Path(self.output_edit.text().strip())
        vscode_dir = output / ".vscode"
        if vscode_dir.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(vscode_dir)))
            return
        QMessageBox.information(
            self,
            "调试配置尚未生成",
            "完成转换后可打开输出目录中的 .vscode 配置。",
        )

    def _log(self, message):
        self.log_browser.append(message)

    @staticmethod
    def _history_values(value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        return [text] if text else []

    @staticmethod
    def _merge_path_history(path, history, limit=12):
        values = []
        candidates = [path, *history]
        seen = set()
        for candidate in candidates:
            value = str(candidate).strip()
            if not value:
                continue
            key = os.path.normcase(os.path.normpath(value))
            if key in seen:
                continue
            seen.add(key)
            values.append(value)
            if len(values) >= limit:
                break
        return values

    def _remember_path(self, edit, path):
        edit.set_history(self._merge_path_history(path, edit.history()))

    def _load_config(self):
        settings = QSettings(str(_config_file_path()), QSettings.Format.IniFormat)
        project_path = str(settings.value("uvproj_filepath", ""))
        output_directory = str(settings.value("output_directory", ""))
        project_history = self._history_values(
            settings.value("uvproj_history", [])
        )
        output_history = self._history_values(
            settings.value("output_history", [])
        )
        self.project_edit.set_history(
            self._merge_path_history(project_path, project_history)
        )
        self.output_edit.set_history(
            self._merge_path_history(output_directory, output_history)
        )
        self.project_edit.setText(project_path)
        self.output_edit.setText(output_directory)
        self.uv4_edit.setText(
            str(settings.value("uv4_path", find_keil_mdk_root() or ""))
        )
        self.target_edit.setText(str(settings.value("target_name", "")))
        self.build_dir_edit.setText(str(settings.value("build_dir", "")))
        self._set_combo(self.compiler_combo, str(settings.value("compiler", "gcc")))
        self._set_combo(self.host_combo, str(settings.value("host_os", "windows")))
        self._update_generators(str(settings.value("generator", "Ninja")))
        self._set_combo(self.probe_combo, str(settings.value("debug_probe", "all")))
        self._sync_backends()
        self._set_combo(self.backend_combo, str(settings.value("debug_backend", "all")))
        self.vscode_check.setChecked(
            str(settings.value("export_vsc_settings", "true")).lower()
            in {"1", "true", "yes"}
        )
        self.validate_check.setChecked(
            str(settings.value("validate_build", "true")).lower()
            in {"1", "true", "yes"}
        )
        self._update_nav_labels()
        self._update_compact_layout()
        self._refresh_inspector()

    def showEvent(self, event):
        super().showEvent(event)
        if self._entrance_animation is not None:
            return
        self._entrance_animation = QPropertyAnimation(
            self._fade_effect, b"opacity", self
        )
        self._entrance_animation.setDuration(320)
        self._entrance_animation.setStartValue(0.0)
        self._entrance_animation.setEndValue(1.0)
        self._entrance_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._entrance_animation.finished.connect(self._finish_entrance)
        self._entrance_animation.start()

    def _finish_entrance(self):
        self.centralWidget().setGraphicsEffect(None)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_compact_layout()

    def _save_config(self):
        settings = QSettings(str(_config_file_path()), QSettings.Format.IniFormat)
        values = {
            "uvproj_filepath": self.project_edit.text().strip(),
            "output_directory": self.output_edit.text().strip(),
            "uvproj_history": self.project_edit.history(),
            "output_history": self.output_edit.history(),
            "uv4_path": self.uv4_edit.text().strip(),
            "target_name": self.target_edit.text().strip(),
            "build_dir": self.build_dir_edit.text().strip(),
            "compiler": self.compiler_combo.currentData(),
            "host_os": self.host_combo.currentData(),
            "generator": self.generator_combo.currentText().strip(),
            "debug_probe": self.probe_combo.currentData(),
            "debug_backend": self.backend_combo.currentData(),
            "export_vsc_settings": "true" if self.vscode_check.isChecked() else "false",
            "validate_build": "true" if self.validate_check.isChecked() else "false",
        }
        for key, value in values.items():
            settings.setValue(key, value)
        settings.sync()

    def closeEvent(self, event):
        self._save_config()
        if self.worker_thread and self.worker_thread.isRunning():
            QMessageBox.information(self, "任务运行中", "转换完成后再关闭窗口。")
            event.ignore()
            return
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(_resource_path("assets/keilforge-app-icon.svg"))))
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
