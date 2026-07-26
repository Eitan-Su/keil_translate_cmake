import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from keil_to_cmake import GenerationOptions
from main import APP_STYLE, CHECKBOX_CHECK_ICON, ConversionWorker, MainWindow


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
            r"QWidget#pageSurface\s*\{[^}]*background\s*:\s*#edf2f7",
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
            r"QAbstractItemView::item:hover\s*\{[^}]*background\s*:\s*#dceefa",
        )

    def test_scrollbars_use_compact_hover_style(self):
        self.assertRegex(
            APP_STYLE,
            r"QScrollBar::handle:vertical:hover\s*\{[^}]*background\s*:\s*#1688d0",
        )
        self.assertRegex(
            APP_STYLE,
            r"QScrollBar::handle:horizontal\s*\{[^}]*border-radius\s*:\s*4px",
        )
        self.assertRegex(
            APP_STYLE,
            r"QSplitter::handle:horizontal:hover\s*\{[^}]*border-left\s*:\s*1px solid #1688d0",
        )
        self.assertRegex(
            APP_STYLE,
            r"QSplitter::handle\s*\{[^}]*background\s*:\s*transparent",
        )

    def test_default_window_uses_compact_dimensions(self):
        self.assertEqual(MainWindow.DEFAULT_WINDOW_SIZE, (929, 480))
        self.assertEqual(MainWindow.MINIMUM_WINDOW_SIZE, (520, 440))
        self.assertTrue(MainWindow._is_compact_width(560))
        self.assertFalse(MainWindow._is_compact_width(800))

    def test_validation_choice_updates_conversion_action_text(self):
        self.assertEqual(
            MainWindow._conversion_action_text(True),
            "转换并验证",
        )
        self.assertEqual(
            MainWindow._conversion_action_text(False),
            "仅转换",
        )

    def test_collapsed_inspector_width_is_not_persisted(self):
        self.assertEqual(MainWindow._normalized_inspector_width(0), 270)
        self.assertEqual(MainWindow._normalized_inspector_width(120), 240)
        self.assertEqual(MainWindow._normalized_inspector_width(300), 300)
        self.assertEqual(MainWindow._normalized_inspector_width(900), 340)

    def test_invalid_navigation_width_uses_bounded_default(self):
        self.assertEqual(MainWindow._normalized_nav_width("invalid"), 84)
        self.assertEqual(MainWindow._normalized_nav_width(0), 72)
        self.assertEqual(MainWindow._normalized_nav_width(120), 120)
        self.assertEqual(MainWindow._normalized_nav_width(900), 180)

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


if __name__ == "__main__":
    unittest.main()
