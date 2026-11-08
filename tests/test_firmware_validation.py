from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from firmware_validation import inspect_elf, target_regions
from sdk_sources import BatchError


class FirmwareValidationTests(unittest.TestCase):
    regions = [{"kind": "flash", "origin": 0x08000000, "length": 0x1000},
               {"kind": "ram", "origin": 0x20000000, "length": 0x1000}]

    def fixture(self, path, stack=0x20001000, reset=0x08000009, entry=0x08000009):
        names = b"\0.shstrtab\0.isr_vector\0"
        data = bytearray(512)
        struct.pack_into("<16sHHIIIIIHHHHHH", data, 0, b"\x7fELF\x01\x01\x01" + bytes(9),
                         2, 40, 1, entry, 52, 256, 0x5000000, 52, 32, 1, 40, 3, 1)
        struct.pack_into("<IIIIIIII", data, 52, 1, 128, 0x08000000, 0x08000000, 16, 16, 5, 4)
        struct.pack_into("<II", data, 128, stack, reset)
        struct.pack_into("<IIIIIIIIII", data, 296, 1, 3, 0, 0, 200, len(names), 0, 0, 1, 0)
        struct.pack_into("<IIIIIIIIII", data, 336, 11, 1, 2, 0x08000000, 128, 8, 0, 0, 4, 0)
        data[200:200 + len(names)] = names
        path.write_bytes(data)
        return data

    def test_valid_vector_and_memory_and_missing_metadata(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.elf"
            self.fixture(path)
            report = inspect_elf(path, self.regions)
            self.assertEqual(report["status"], "verified")
            self.assertFalse(report["hardware_tested"])
            self.assertEqual(inspect_elf(path, [])["status"], "review_required")

    def test_rejects_bad_stack_reset_memory_and_truncated_elf(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.elf"
            for values, message in (({"stack": 0x20001004}, "aligned"), ({"stack": 0x30000000}, "RAM"),
                                    ({"reset": 0x0800000B}, "differs"), ({"entry": 0x08000008}, "Thumb")):
                self.fixture(path, **values)
                with self.subTest(values=values), self.assertRaisesRegex(BatchError, message):
                    inspect_elf(path, self.regions)
            data = self.fixture(path)
            struct.pack_into("<I", data, 52 + 20, 0x2000)
            path.write_bytes(data)
            with self.assertRaisesRegex(BatchError, "exceeds target memory"):
                inspect_elf(path, self.regions)
            path.write_bytes(data[:10])
            with self.assertRaisesRegex(BatchError, "truncated"):
                inspect_elf(path, self.regions)

    def test_vector_must_be_in_the_loaded_image(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.elf"
            data = self.fixture(path)
            struct.pack_into("<I", data, 336 + 16, 136)
            path.write_bytes(data)
            with self.assertRaisesRegex(BatchError, "load image"):
                inspect_elf(path, self.regions)

    def test_target_memory_override_and_cpu_range_forms(self):
        target = ET.fromstring('''<Target><TargetOption><TargetCommonOption>
            <Cpu>IROM(0x08000000,0x10000) IRAM(0x20000000-0x20001fff)</Cpu>
            </TargetCommonOption></TargetOption><OnChipMemories><OCR_RVCT4>
            <StartAddress>0x08004000</StartAddress><Size>0x8000</Size>
            </OCR_RVCT4></OnChipMemories></Target>''')
        regions = target_regions(target)
        self.assertEqual(regions[0], {"kind": "flash", "origin": 0x08004000, "length": 0x8000})
        self.assertEqual(regions[1]["length"], 0x2000)


if __name__ == "__main__":
    unittest.main()
