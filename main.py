# -*- coding: utf-8 -*-
import os
import sys

from PySide6.QtCore import QDir, QSettings, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTextBrowser,
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


class Widget(QWidget):
    def __init__(self):
        super().__init__()

        self.uvproj_filepath = ""
        self.output_directory = ""
        self.uv4_path = ""

        self.setup_ui()
        self.setWindowTitle("Keil UVPROJX to CMake Converter")
        self.resize(960, 680)
        self.load_config()

    def closeEvent(self, event):
        self.save_config()
        event.accept()

    def setup_ui(self):
        label_uvproj = QLabel("uvprojx 路径:")
        label_uv4 = QLabel("Keil MDK 路径:")
        label_output = QLabel("输出目录:")
        label_compiler = QLabel("编译器:")
        label_host = QLabel("宿主系统:")
        label_generator = QLabel("Generator:")
        label_probe = QLabel("硬件探针:")
        label_debugger = QLabel("调试后端:")
        label_build_dir = QLabel("构建目录:")
        label_info = QLabel("当前配置:")
        label_log = QLabel("转换日志:")

        self.lineeditUvProjFilepath = QLineEdit()
        self.lineeditUv4Path = QLineEdit()
        self.lineeditOutputDirectory = QLineEdit()
        self.lineeditBuildDir = QLineEdit()

        self.comboCompiler = QComboBox()
        for key, label in SUPPORTED_COMPILERS.items():
            self.comboCompiler.addItem(label, key)

        self.comboHostOs = QComboBox()
        for key, label in SUPPORTED_HOST_SYSTEMS.items():
            self.comboHostOs.addItem(label, key)

        self.comboGenerator = QComboBox()
        self.comboGenerator.setEditable(True)

        self.comboDebugProbe = QComboBox()
        for key, label in SUPPORTED_DEBUG_PROBES.items():
            self.comboDebugProbe.addItem(label, key)

        self.comboDebugger = QComboBox()
        for key, label in SUPPORTED_DEBUG_BACKENDS.items():
            self.comboDebugger.addItem(label, key)
        self._sync_debug_backend_options()

        self.checkboxExportVSCode = QCheckBox("生成 VS Code 配置")

        self.pushbuttonBrowseUvProj = QPushButton("浏览")
        self.pushbuttonBrowseUv4 = QPushButton("浏览")
        self.pushbuttonBrowseOutput = QPushButton("浏览")
        self.pushbuttonConvert = QPushButton("转换")

        self.listWidgetOriginalInfo = QListWidget()
        self.textBrowserLog = QTextBrowser()

        layout_uvproj = QHBoxLayout()
        layout_uvproj.addWidget(label_uvproj)
        layout_uvproj.addWidget(self.lineeditUvProjFilepath)
        layout_uvproj.addWidget(self.pushbuttonBrowseUvProj)

        layout_uv4 = QHBoxLayout()
        layout_uv4.addWidget(label_uv4)
        layout_uv4.addWidget(self.lineeditUv4Path)
        layout_uv4.addWidget(self.pushbuttonBrowseUv4)

        layout_output = QHBoxLayout()
        layout_output.addWidget(label_output)
        layout_output.addWidget(self.lineeditOutputDirectory)
        layout_output.addWidget(self.pushbuttonBrowseOutput)

        layout_profile_1 = QHBoxLayout()
        layout_profile_1.addWidget(label_compiler)
        layout_profile_1.addWidget(self.comboCompiler)
        layout_profile_1.addWidget(label_host)
        layout_profile_1.addWidget(self.comboHostOs)
        layout_profile_1.addWidget(self.checkboxExportVSCode)

        layout_profile_2 = QHBoxLayout()
        layout_profile_2.addWidget(label_generator)
        layout_profile_2.addWidget(self.comboGenerator)
        layout_profile_2.addWidget(label_probe)
        layout_profile_2.addWidget(self.comboDebugProbe)
        layout_profile_2.addWidget(label_debugger)
        layout_profile_2.addWidget(self.comboDebugger)
        layout_profile_2.addWidget(label_build_dir)
        layout_profile_2.addWidget(self.lineeditBuildDir)

        layout_actions = QHBoxLayout()
        layout_actions.addStretch(1)
        layout_actions.addWidget(self.pushbuttonConvert)

        layout_info = QVBoxLayout()
        layout_info.addWidget(label_info)
        layout_info.addWidget(self.listWidgetOriginalInfo)

        layout_log = QVBoxLayout()
        layout_log.addWidget(label_log)
        layout_log.addWidget(self.textBrowserLog)

        layout_bottom = QHBoxLayout()
        layout_bottom.addLayout(layout_info)
        layout_bottom.addLayout(layout_log)

        layout = QVBoxLayout()
        layout.addLayout(layout_uvproj)
        layout.addLayout(layout_uv4)
        layout.addLayout(layout_output)
        layout.addLayout(layout_profile_1)
        layout.addLayout(layout_profile_2)
        layout.addLayout(layout_actions)
        layout.addLayout(layout_bottom)
        self.setLayout(layout)

        self.pushbuttonBrowseUvProj.clicked.connect(
            self.on_pushbuttonBrowseUvProj_clicked
        )
        self.pushbuttonBrowseUv4.clicked.connect(self.on_pushbuttonBrowseUv4_clicked)
        self.pushbuttonBrowseOutput.clicked.connect(
            self.on_pushbuttonBrowseOutput_clicked
        )
        self.pushbuttonConvert.clicked.connect(self.on_pushbuttonConvert_clicked)
        self.comboHostOs.currentIndexChanged.connect(self.on_host_os_changed)
        self.comboCompiler.currentIndexChanged.connect(self.on_compiler_changed)
        self.comboDebugProbe.currentIndexChanged.connect(self.on_debug_probe_changed)
        self.comboDebugger.currentIndexChanged.connect(self.refresh_original_info)

    def _set_combo_data(self, combo: QComboBox, value: str):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _sync_debug_backend_options(self):
        probe = self.comboDebugProbe.currentData() or "default"
        current_backend = self.comboDebugger.currentData() or "default"
        if probe not in {"default", "all", "jlink"} and current_backend == "jlink":
            current_backend = "default"

        self.comboDebugger.blockSignals(True)
        self.comboDebugger.clear()
        for key, label in SUPPORTED_DEBUG_BACKENDS.items():
            if probe not in {"default", "all", "jlink"} and key == "jlink":
                continue
            self.comboDebugger.addItem(label, key)

        self._set_combo_data(self.comboDebugger, current_backend)
        if self.comboDebugger.currentIndex() < 0:
            self._set_combo_data(self.comboDebugger, "default")
        self.comboDebugger.blockSignals(False)

    def _update_generator_options(self, preferred: str = ""):
        preferred = preferred or self.comboGenerator.currentText().strip()
        host_os = self.comboHostOs.currentData() or "windows"
        generators = get_supported_generators(host_os)

        self.comboGenerator.blockSignals(True)
        self.comboGenerator.clear()
        for generator in generators:
            self.comboGenerator.addItem(generator, generator)

        if preferred and preferred not in generators:
            self.comboGenerator.addItem(preferred, preferred)

        target = preferred or DEFAULT_GENERATOR_BY_HOST.get(host_os, "Ninja")
        index = self.comboGenerator.findText(target)
        if index >= 0:
            self.comboGenerator.setCurrentIndex(index)
        else:
            self.comboGenerator.setEditText(target)
        self.comboGenerator.blockSignals(False)

    def _suggest_output_directory(self) -> str:
        uvproj_path = self.lineeditUvProjFilepath.text().strip()
        if not uvproj_path:
            return ""

        project_dir = os.path.dirname(uvproj_path)
        compiler = self.comboCompiler.currentData() or "armclang"
        if compiler == "gcc" and os.path.basename(project_dir).lower() in {
            "mdk_v5",
            "mdk",
            "uvprojx",
        }:
            return os.path.dirname(project_dir)
        return project_dir

    def _current_options(self) -> GenerationOptions:
        return GenerationOptions(
            compiler=self.comboCompiler.currentData() or "armclang",
            generator=self.comboGenerator.currentText().strip()
            or DEFAULT_GENERATOR_BY_HOST.get(self.comboHostOs.currentData(), "Ninja"),
            host_os=self.comboHostOs.currentData() or "windows",
            debugger=self.comboDebugger.currentData() or "default",
            debug_probe=self.comboDebugProbe.currentData() or "default",
            debug_backend=self.comboDebugger.currentData() or "default",
            build_dir=self.lineeditBuildDir.text().strip() or None,
            export_vsc_settings=self.checkboxExportVSCode.isChecked(),
        ).normalized()

    def load_config(self):
        settings = QSettings("config.ini", QSettings.Format.IniFormat)
        detected_uv4_path = find_keil_mdk_root() or ""

        self.uvproj_filepath = str(settings.value("uvproj_filepath", ""))
        self.output_directory = str(settings.value("output_directory", ""))
        self.uv4_path = str(settings.value("uv4_path", detected_uv4_path))

        compiler = str(settings.value("compiler", "armclang"))
        host_os = str(settings.value("host_os", "windows"))
        generator = str(
            settings.value("generator", DEFAULT_GENERATOR_BY_HOST.get(host_os, "Ninja"))
        )
        debugger = str(settings.value("debugger", "default"))
        debug_probe = str(settings.value("debug_probe", "default"))
        debug_backend = str(settings.value("debug_backend", ""))
        if not debug_backend:
            debug_backend = debugger or "default"
        build_dir = str(settings.value("build_dir", ""))
        export_vsc = str(settings.value("export_vsc_settings", "true")).lower() in {
            "1",
            "true",
            "yes",
        }

        self.lineeditUvProjFilepath.setText(self.uvproj_filepath)
        self.lineeditUv4Path.setText(self.uv4_path)
        self.lineeditOutputDirectory.setText(self.output_directory)
        self.lineeditBuildDir.setText(build_dir)
        self.checkboxExportVSCode.setChecked(export_vsc)

        self._set_combo_data(self.comboCompiler, compiler)
        self._set_combo_data(self.comboHostOs, host_os)
        self._update_generator_options(generator)
        self._set_combo_data(self.comboDebugProbe, debug_probe)
        self._sync_debug_backend_options()
        self._set_combo_data(self.comboDebugger, debug_backend)
        self.refresh_original_info()

    def save_config(self):
        settings = QSettings("config.ini", QSettings.Format.IniFormat)
        settings.setValue("uvproj_filepath", self.lineeditUvProjFilepath.text().strip())
        settings.setValue(
            "output_directory", self.lineeditOutputDirectory.text().strip()
        )
        settings.setValue("uv4_path", self.lineeditUv4Path.text().strip())
        settings.setValue("compiler", self.comboCompiler.currentData())
        settings.setValue("host_os", self.comboHostOs.currentData())
        settings.setValue("generator", self.comboGenerator.currentText().strip())
        settings.setValue("debugger", self.comboDebugger.currentData())
        settings.setValue("debug_probe", self.comboDebugProbe.currentData())
        settings.setValue("debug_backend", self.comboDebugger.currentData())
        settings.setValue("build_dir", self.lineeditBuildDir.text().strip())
        settings.setValue(
            "export_vsc_settings", "true" if self.checkboxExportVSCode.isChecked() else "false"
        )
        settings.sync()

    def refresh_original_info(self):
        self.listWidgetOriginalInfo.clear()
        info_items = [
            ("uvprojx", self.lineeditUvProjFilepath.text().strip()),
            ("Keil MDK", self.lineeditUv4Path.text().strip()),
            ("输出目录", self.lineeditOutputDirectory.text().strip()),
            ("编译器", self.comboCompiler.currentText()),
            ("Generator", self.comboGenerator.currentText().strip()),
            ("宿主系统", self.comboHostOs.currentText()),
            ("硬件探针", self.comboDebugProbe.currentText()),
            ("调试后端", self.comboDebugger.currentText()),
            ("构建目录", self.lineeditBuildDir.text().strip() or "(默认)"),
            ("VS Code", "启用" if self.checkboxExportVSCode.isChecked() else "关闭"),
        ]
        for label, value in info_items:
            if value:
                self.listWidgetOriginalInfo.addItem(f"{label}: {value}")

    def show_message(self, message: str):
        self.textBrowserLog.append(message)

    def on_host_os_changed(self):
        self._update_generator_options()
        self.refresh_original_info()

    def on_debug_probe_changed(self):
        self._sync_debug_backend_options()
        self.refresh_original_info()

    def on_compiler_changed(self):
        uvproj_path = self.lineeditUvProjFilepath.text().strip()
        current_output = self.lineeditOutputDirectory.text().strip()
        if uvproj_path:
            project_dir = os.path.dirname(uvproj_path)
            parent_dir = os.path.dirname(project_dir)
            suggested = self._suggest_output_directory()
            if suggested and (
                not current_output
                or current_output == project_dir
                or current_output == parent_dir
            ):
                self.lineeditOutputDirectory.setText(suggested)
        self.refresh_original_info()

    def on_pushbuttonBrowseUvProj_clicked(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择 UVPROJX 文件", QDir.homePath(), "UVPROJX Files (*.uvprojx)"
        )
        if not file_path:
            return

        _, ext = os.path.splitext(file_path)
        if ext.lower() != ".uvprojx":
            QMessageBox.critical(self, "错误", "请选择有效的 UVPROJX 文件。")
            return

        self.uvproj_filepath = file_path.replace("/", os.sep)
        self.lineeditUvProjFilepath.setText(self.uvproj_filepath)
        suggested_output = self._suggest_output_directory() or os.path.dirname(
            self.uvproj_filepath
        )
        self.lineeditOutputDirectory.setText(suggested_output)
        self.refresh_original_info()

        self.show_message(f"已选择 UVPROJX 文件: {self.uvproj_filepath}")
        self.show_message(f"建议输出目录: {suggested_output}")

    def on_pushbuttonBrowseUv4_clicked(self):
        dir_path = QFileDialog.getExistingDirectory(
            self,
            "选择 Keil MDK 目录",
            self.lineeditUv4Path.text().strip() or QDir.homePath(),
        )
        if not dir_path:
            return

        self.uv4_path = dir_path.replace("/", os.sep)
        self.lineeditUv4Path.setText(self.uv4_path)
        self.refresh_original_info()
        self.show_message(f"已选择 Keil MDK 目录: {self.uv4_path}")

    def on_pushbuttonBrowseOutput_clicked(self):
        dir_path = QFileDialog.getExistingDirectory(
            self,
            "选择输出目录",
            self.lineeditOutputDirectory.text().strip()
            or os.path.dirname(self.lineeditUvProjFilepath.text().strip())
            or QDir.homePath(),
        )
        if not dir_path:
            return

        self.output_directory = dir_path.replace("/", os.sep)
        self.lineeditOutputDirectory.setText(self.output_directory)
        self.refresh_original_info()
        self.show_message(f"已选择输出目录: {self.output_directory}")

    def on_pushbuttonConvert_clicked(self):
        self.uvproj_filepath = self.lineeditUvProjFilepath.text().strip()
        self.output_directory = self.lineeditOutputDirectory.text().strip()
        self.uv4_path = self.lineeditUv4Path.text().strip()

        if not self.uvproj_filepath:
            QMessageBox.critical(self, "错误", "请先选择 UVPROJX 文件。")
            return

        if not self.output_directory:
            self.output_directory = self._suggest_output_directory()
            self.lineeditOutputDirectory.setText(self.output_directory)

        options = self._current_options()
        parser = KeilProjectToCMake(self.uvproj_filepath, self.uv4_path or None)

        if not parser.parse():
            self.show_message("解析失败，请检查工程路径和 Keil MDK 路径。")
            return

        self.uv4_path = parser.uv4_path
        self.lineeditUv4Path.setText(self.uv4_path)
        self.refresh_original_info()

        if not parser.generate_cmake(
            self.output_directory,
            options.export_vsc_settings,
            options,
        ):
            self.show_message("生成 CMake 失败。")
            return

        self.show_message(
            "转换完成: "
            f"compiler={options.compiler}, "
            f"generator={options.generator}, "
            f"host={options.host_os}, "
            f"probe={options.debug_probe}, "
            f"backend={options.debug_backend}"
        )

        if (
            QMessageBox.question(
                self,
                "提示",
                "转换完成，是否打开输出目录？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_directory))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    widget = Widget()
    widget.show()
    sys.exit(app.exec())
