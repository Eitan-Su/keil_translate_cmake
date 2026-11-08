import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QBoxLayout
from PySide6.QtCore import QEventLoop, QMimeData, QPoint, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from keil_to_cmake import GenerationOptions
from main import APP_STYLE, CHECKBOX_CHECK_ICON, ConversionWorker, MainWindow, ProjectDropArea


class MainWindowPathTests(unittest.TestCase):
    def test_passive_text_widgets_use_transparent_backgrounds(self):
        self.assertNotRegex(
            APP_STYLE,
            r"QWidget\s*\{[^}]*background\s*:",
        )
        self.assertRegex(
            APP_STYLE,
            r"QLabel, QCheckBox, QSvgWidget\s*\{[^}]*background\s*:\s*transparent",
        )
        self.assertRegex(
            APP_STYLE,
            r"QWidget#pageSurface[^{}]*\{[^}]*background\s*:",
        )

    def test_checked_checkbox_uses_checkmark_svg(self):
        icon_path = Path(CHECKBOX_CHECK_ICON)

        self.assertTrue(icon_path.is_file())
        self.assertRegex(
            APP_STYLE,
            r'QCheckBox::indicator:checked\s*\{[^}]*'
            r'image:\s*url\("[^"]*checkbox-check\.svg"\)',
        )
        svg = icon_path.read_text(encoding="utf-8")
        self.assertIn('stroke="#ffffff"', svg)
        self.assertIn("<path", svg)

    def test_history_popup_highlights_hovered_item(self):
        self.assertRegex(
            APP_STYLE,
            r"QAbstractItemView::item:hover[^{}]*\{[^}]*background\s*:",
        )

    def test_scrollbars_use_compact_hover_style(self):
        self.assertRegex(
            APP_STYLE,
            r"QScrollBar::handle:vertical:hover[^{}]*\{[^}]*background\s*:",
        )
        self.assertRegex(
            APP_STYLE,
            r"QScrollBar::handle:horizontal\s*\{[^}]*border-radius\s*:\s*\d+px",
        )

    def test_default_window_and_compact_breakpoint(self):
        self.assertEqual(MainWindow.DEFAULT_WINDOW_SIZE, (1120, 760))
        self.assertEqual(MainWindow.MINIMUM_WINDOW_SIZE, (560, 520))
        self.assertTrue(MainWindow._is_compact_width(560))
        self.assertFalse(MainWindow._is_compact_width(1120))

    def test_validation_choice_updates_conversion_action_text(self):
        self.assertEqual(
            MainWindow._conversion_action_text(True),
            "转换并验证",
        )
        self.assertEqual(
            MainWindow._conversion_action_text(False),
            "仅转换",
        )

    def test_host_change_uses_default_generator(self):
        self.assertEqual(
            MainWindow._generator_target(
                "linux", "", "MinGW Makefiles", host_changed=True
            ),
            "Ninja",
        )
        self.assertEqual(
            MainWindow._generator_target(
                "linux", "Custom Generator", "Ninja", host_changed=False
            ),
            "Custom Generator",
        )

    def test_default_output_directory_follows_project_directory(self):
        with TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "sdk" / "mdk" / "sample.uvprojx"

            output = MainWindow._default_output_directory(str(project))

            self.assertEqual(output, str(project.parent / "cmake-generated"))

    def test_default_output_directory_is_empty_without_project(self):
        self.assertEqual(MainWindow._default_output_directory("  "), "")

    def test_path_history_keeps_recent_unique_entries(self):
        history = MainWindow._merge_path_history(
            r"D:\projects\new\project.uvprojx",
            [
                r"D:\projects\old\project.uvprojx",
                r"D:\projects\new\project.uvprojx",
            ],
        )

        self.assertEqual(
            history,
            [
                r"D:\projects\new\project.uvprojx",
                r"D:\projects\old\project.uvprojx",
            ],
        )

    def test_path_history_accepts_legacy_string_setting(self):
        self.assertEqual(
            MainWindow._history_values(r"D:\projects\sample.uvprojx"),
            [r"D:\projects\sample.uvprojx"],
        )

    def test_conversion_progress_mapping_clamps_and_scales(self):
        self.assertEqual(ConversionWorker._map_progress(-20, 20, 45), 20)
        self.assertEqual(ConversionWorker._map_progress(50, 20, 40), 30)
        self.assertEqual(ConversionWorker._map_progress(120, 45, 99), 99)

    def test_formats_validation_duration(self):
        self.assertEqual(MainWindow._format_duration(8.24), "8.2 秒")
        self.assertEqual(MainWindow._format_duration(65.27), "1 分 05.3 秒")
        self.assertEqual(
            MainWindow._format_duration(3723.18),
            "1 小时 02 分 03.2 秒",
        )

    def test_final_message_contains_validation_duration(self):
        self.assertEqual(
            MainWindow._with_validation_duration("构建验证通过。", 12.34),
            "构建验证通过。\n\n验证耗时\n12.3 秒",
        )
        self.assertEqual(
            MainWindow._with_validation_duration("CMake 工程生成完成。", -1.0),
            "CMake 工程生成完成。\n\n验证耗时\n未执行",
        )

    def test_completion_status_distinguishes_generation_from_validation(self):
        self.assertEqual(
            MainWindow._completion_status(True, -1.0),
            ("转换完成", False),
        )
        self.assertEqual(
            MainWindow._completion_status(True, 2.5),
            ("构建通过", True),
        )
        self.assertEqual(
            MainWindow._completion_status(False, 2.5),
            ("构建失败", True),
        )

    def test_completion_status_distinguishes_conversion_from_validation(self):
        self.assertEqual(
            MainWindow._completion_status(True, -1.0),
            ("转换完成", False),
        )
        self.assertEqual(
            MainWindow._completion_status(False, -1.0),
            ("转换失败", False),
        )
        self.assertEqual(
            MainWindow._completion_status(True, 0.0),
            ("构建通过", True),
        )
        self.assertEqual(
            MainWindow._completion_status(False, 0.0),
            ("构建失败", True),
        )

    def test_parses_all_linker_memory_regions(self):
        output = (
            "Memory region         Used Size  Region Size  %age Used\n"
            "           FLASH:      529372 B         1 MB     50.48%\n"
            "             RAM:      298472 B       384 KB     75.91%\n"
            "        RW_IRAM2:        160 KB       160 KB    100.00%\n"
        )

        regions = ConversionWorker._parse_memory_regions(output)

        self.assertEqual(
            regions,
            [
                ("FLASH", 529372, 1024 * 1024),
                ("RAM", 298472, 384 * 1024),
                ("RW_IRAM2", 160 * 1024, 160 * 1024),
            ],
        )

    def test_parses_armclang_program_size(self):
        output = "Program Size: Code=388 RO-data=16 RW-data=0 ZI-data=96\n"
        parser = SimpleNamespace(
            flash_length=1024 * 1024,
            ram_length=128 * 1024,
        )

        regions = ConversionWorker._parse_memory_regions(output, parser)

        self.assertEqual(
            regions,
            [
                ("FLASH", 404, 1024 * 1024),
                ("RAM", 96, 128 * 1024),
            ],
        )
        self.assertEqual(
            ConversionWorker._parse_memory_usage(output, parser),
            (404, 1024 * 1024, 96, 128 * 1024),
        )

    def test_build_success_message_contains_firmware_memory_and_debug_info(self):
        with TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            build_dir = output / "build-gcc"
            build_dir.mkdir()
            for suffix in (".elf", ".hex", ".bin"):
                (build_dir / f"firmware{suffix}").write_bytes(b"firmware")

            options = GenerationOptions(
                compiler="gcc",
                generator="Ninja",
                debug_probe="all",
                debug_backend="all",
                build_dir="build-gcc",
                export_vsc_settings=True,
            ).normalized()
            worker = ConversionWorker(
                "project.uvprojx", str(output), "", "", options, True
            )
            parser = SimpleNamespace(
                _artifact_name=lambda: "firmware",
                _selected_probes=lambda _options: ["stlink", "jlink"],
                _selected_debug_backends=lambda _options: ["openocd", "jlink"],
            )

            message = worker._build_success_message(
                parser,
                [
                    ("FLASH", 512 * 1024, 1024 * 1024),
                    ("RAM", 128 * 1024, 256 * 1024),
                ],
            )

            for expected in (
                "firmware.elf",
                "FLASH: 512.00 KB / 1.00 MB (50.0%)",
                "RAM: 128.00 KB / 256.00 KB (50.0%)",
                "探针: ST-Link, J-Link",
                "后端: OpenOCD, J-Link GDB Server",
                "VS Code 配置: 生成",
            ):
                self.assertIn(expected, message)

    def test_armclang_build_success_message_reports_axf_firmware(self):
        with TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            build_dir = output / "build"
            build_dir.mkdir()
            (build_dir / "firmware.axf").write_bytes(b"firmware")
            options = GenerationOptions(
                compiler="armclang", build_dir="build"
            ).normalized()
            worker = ConversionWorker(
                "project.uvprojx", str(output), "", "", options, True
            )
            parser = SimpleNamespace(
                _artifact_name=lambda: "firmware",
                _selected_probes=lambda _options: [],
                _selected_debug_backends=lambda _options: [],
            )

            message = worker._build_success_message(parser, [])

            self.assertIn("firmware.axf", message)
            self.assertNotIn("firmware.elf", message)


class MainWindowInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(APP_STYLE)

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config_patch = patch("main._config_file_path", return_value=Path(self.temporary.name) / "config.ini")
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        with patch("main.find_keil_mdk_root", return_value=""):
            self.window = MainWindow()
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.window.close)

    def test_narrow_layout_keeps_preview_accessible_and_actions_visible(self):
        window = self.window
        window.resize(560, 640)
        self.app.processEvents()
        self.assertEqual(window.project_layout.direction(), QBoxLayout.Direction.TopToBottom)
        self.assertTrue(window.inspector_panel.isVisible())
        self.assertTrue(window.convert_button.isVisible())
        self.assertTrue(window.validate_check.isVisible())
        for widget in (window.convert_button, window.validate_check, window.project_button):
            self.assertTrue(widget.parentWidget().rect().contains(widget.geometry()))
        page = window.page_stack.widget(0)
        page.ensureWidgetVisible(window.ram_bar)
        self.app.processEvents()
        self.assertGreater(page.verticalScrollBar().value(), 0)
        for index in range(4):
            window.nav_buttons[index].click()
            self.assertEqual(window.page_stack.currentIndex(), index)
        window._show_page(1)
        self.app.processEvents()
        self.assertEqual([window.profile_grid.getItemPosition(i)[1] for i in range(5)], [0] * 5)
        window.resize(1120, 760)
        window._show_page(0)
        self.app.processEvents()
        self.assertEqual(window.project_layout.direction(), QBoxLayout.Direction.LeftToRight)
        self.assertTrue(window.inspector_panel.isVisible())
        self.assertTrue(window.top_brand.isVisible())

    def test_output_summary_matches_compiler_and_selected_exports(self):
        window = self.window
        window.validate_check.setChecked(False)
        window.vscode_check.setChecked(False)
        self.assertEqual(window.convert_button.text(), "仅转换")
        self.assertTrue(window.firmware_preview.isHidden())
        self.assertTrue(window.vscode_preview.isHidden())
        window._set_combo(window.compiler_combo, "armclang")
        window.validate_check.setChecked(True)
        self.assertFalse(window.firmware_preview.isHidden())
        self.assertIn("AXF", window.firmware_preview_name.text())
        self.assertNotIn("ELF", window.firmware_preview_name.text())
        window._set_combo(window.compiler_combo, "gcc")
        self.assertIn("ELF", window.firmware_preview_name.text())
        self.assertEqual(window._options().compiler, "gcc")

    def test_smallest_window_shows_import_and_conversion_actions(self):
        window = self.window
        window.resize(*window.MINIMUM_WINDOW_SIZE)
        self.app.processEvents()
        viewport = window.page_stack.widget(0).viewport()
        position = window.project_button.mapTo(viewport, QPoint(0, 0))
        self.assertTrue(viewport.rect().contains(window.project_button.rect().translated(position)))
        self.assertTrue(window.convert_button.parentWidget().rect().contains(window.convert_button.geometry()))

    def test_drop_import_updates_project_output_and_history(self):
        project = Path(self.temporary.name) / "example.UVPROJX"
        project.write_text("<Project />", encoding="utf-8")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(project))])
        enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(self.window.project_drop, enter)
        self.assertTrue(enter.isAccepted())
        self.app.sendEvent(self.window.project_drop, drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(Path(self.window.project_edit.text()), project)
        self.assertEqual(Path(self.window.output_edit.text()), project.parent / "cmake-generated")
        self.assertEqual(Path(self.window.project_edit.history()[0]), project)
        self.assertFalse(self.window.project_drop.property("dragging"))

    def test_drop_rejects_remote_multiple_missing_and_nonproject_inputs(self):
        project = Path(self.temporary.name) / "example.uvproj"
        project.touch()
        wrong = Path(self.temporary.name) / "example.txt"
        wrong.touch()
        for urls in ([QUrl("https://example.invalid/example.uvproj")],
                     [QUrl.fromLocalFile(str(project))] * 2,
                     [QUrl.fromLocalFile(str(project.with_name("missing.uvproj")))],
                     [QUrl.fromLocalFile(str(wrong))],
                     [QUrl.fromLocalFile(self.temporary.name)]):
            with self.subTest(url_count=len(urls)):
                mime = QMimeData()
                mime.setUrls(urls)
                self.assertEqual(ProjectDropArea.project_from_mime(mime), "")

    def test_progress_and_completion_keep_visible_status_and_output_action(self):
        window = self.window
        window._update_conversion_progress(52, "正在编译")
        self.assertIn("52%", window.progress_label.text())
        window._update_memory_usage(1024, 4096, 256, 1024)
        self.assertEqual(window.flash_bar.value(), 25)
        with patch("main.QMessageBox.information"):
            window._conversion_finished(True, "完成", self.temporary.name, -1)
        self.assertEqual(window.progress_label.text(), "转换完成")
        self.assertTrue(window.open_output_button.isEnabled())
        self.assertEqual(window.stage_badges[-1].property("state"), "pending")
        with patch("main.QMessageBox.critical"):
            window._conversion_finished(False, "编译失败", self.temporary.name, 2)
        self.assertEqual(window.page_stack.currentIndex(), 3)
        self.assertEqual(window.progress_label.text(), "构建失败")

    def test_finished_worker_releases_thread_before_window_closes(self):
        window = self.window
        project = Path(self.temporary.name) / "project.uvprojx"
        project.write_text("<Project />", encoding="utf-8")
        window.project_edit.setText(str(project))
        window.validate_check.setChecked(False)
        parser = SimpleNamespace(parse=lambda: True, generate_cmake=lambda *a, **k: True,
                                 target_name="Demo", device_name="STM32F103C8", source_files=[], warnings=[])
        with patch("main.KeilProjectToCMake", return_value=parser), patch("main.QMessageBox.information"):
            window.convert_button.click()
            loop = QEventLoop()
            window.worker_thread.finished.connect(loop.quit)
            QTimer.singleShot(3000, loop.quit)
            loop.exec()
            self.app.processEvents()
        self.assertIsNone(window.worker_thread)
        self.assertIsNone(window.worker)
        self.assertTrue(window.close())


if __name__ == "__main__":
    unittest.main()
