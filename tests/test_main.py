import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from keil_to_cmake import GenerationOptions
from main import APP_STYLE, ConversionWorker, MainWindow


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

    def test_default_output_directory_follows_project_directory(self):
        with TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "sdk" / "mdk" / "sample.uvprojx"

            output = MainWindow._default_output_directory(str(project))

            self.assertEqual(output, str(project.parent / "cmake-generated"))

    def test_default_output_directory_is_empty_without_project(self):
        self.assertEqual(MainWindow._default_output_directory("  "), "")

    def test_conversion_progress_mapping_clamps_and_scales(self):
        self.assertEqual(ConversionWorker._map_progress(-20, 20, 45), 20)
        self.assertEqual(ConversionWorker._map_progress(50, 20, 40), 30)
        self.assertEqual(ConversionWorker._map_progress(120, 45, 99), 99)

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


if __name__ == "__main__":
    unittest.main()
