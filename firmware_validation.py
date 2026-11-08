"""Structural checks for little-endian Cortex-M ELF firmware."""

from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET

from sdk_sources import BatchError


def target_regions(target: ET.Element) -> list[dict]:
    regions = []
    memories = target.find(".//OnChipMemories")
    if memories is not None:
        for kind, nodes in (("flash", ("OCR_RVCT4", "IROM")), ("ram", ("OCR_RVCT9", "IRAM"))):
            for name in nodes:
                node = memories.find(name)
                if node is not None:
                    origin, size = int(node.findtext("StartAddress", "0"), 0), int(node.findtext("Size", "0"), 0)
                    if size:
                        regions.append({"kind": kind, "origin": origin, "length": size})
                        break
        for name, kind in (("IROM2", "flash"), ("IRAM2", "ram")):
            node = memories.find(name)
            if node is not None:
                origin, size = int(node.findtext("StartAddress", "0"), 0), int(node.findtext("Size", "0"), 0)
                if size:
                    regions.append({"kind": kind, "origin": origin, "length": size})
    cpu = target.findtext("TargetOption/TargetCommonOption/Cpu", "")
    for prefix, kind in (("IROM", "flash"), ("IRAM", "ram")):
        if any(r["kind"] == kind for r in regions):
            continue
        for match in re.finditer(rf'{prefix}\d*\(\s*(0x[\da-f]+)\s*([,-])\s*(0x[\da-f]+)\s*\)', cpu, re.I):
            origin, value = int(match[1], 16), int(match[3], 16)
            regions.append({"kind": kind, "origin": origin, "length": value if match[2] == "," else value - origin + 1})
    if any(r["length"] <= 0 or r["origin"] < 0 or r["origin"] + r["length"] > 2 ** 32 for r in regions):
        raise BatchError("Invalid target memory region")
    return regions


def inspect_elf(path: Path, regions: list[dict]) -> dict:
    data = path.read_bytes()

    def require(condition, reason):
        if not condition:
            raise BatchError(f"ELF validation: {reason}")

    def read(format_name, offset):
        require(0 <= offset <= len(data) - struct.calcsize(format_name), "truncated ELF table")
        return struct.unpack_from(format_name, data, offset)

    require(data[:6] == b"\x7fELF\x01\x01", "expected ELF32 little-endian firmware")
    header = read("<16sHHIIIIIHHHHHH", 0)
    require(header[1:3] == (2, 40) and header[3] == 1, "expected ARM executable")
    require(header[9] == 32 and header[11] == 40, "unsupported ELF table entry size")
    require(header[12] > 0 and header[13] < header[12], "missing section table")
    sections = [read("<IIIIIIIIII", header[6] + i * header[11]) for i in range(header[12])]

    def contents(section):
        require(section[4] + section[5] <= len(data), "truncated ELF section")
        return data[section[4]:section[4] + section[5]]

    names = contents(sections[header[13]])

    def string(table, offset):
        require(0 <= offset < len(table) and b"\0" in table[offset:], "invalid string table offset")
        return table[offset:table.index(b"\0", offset)].decode("utf-8", errors="replace")

    named = {string(names, s[0]): s for s in sections}
    symbols = {}
    if ".symtab" in named:
        symtab = named[".symtab"]
        require(symtab[6] < len(sections) and symtab[9] == 16 and symtab[5] % 16 == 0, "invalid symbol table")
        strings = contents(sections[symtab[6]])
        contents(symtab)
        for offset in range(symtab[4], symtab[4] + symtab[5], 16):
            name, value, size, info, other, index = read("<IIIBBH", offset)
            if name and index:
                symbols[string(strings, name)] = value
    segments = [read("<IIIIIIII", header[5] + i * header[9]) for i in range(header[10])]
    loads = [s for s in segments if s[0] == 1 and s[5]]
    require(bool(loads), "missing loadable segments")
    require(any(s[6] & 1 and s[2] <= (header[4] & ~1) < s[2] + s[5] for s in loads), "entry outside executable segments")
    require(header[4] & 1, "reset entry is not Thumb")
    for segment in loads:
        require(segment[1] + segment[4] <= len(data) and segment[4] <= segment[5], "invalid load segment")

    def contains(address, size, kind=None):
        return any((kind is None or region["kind"] == kind) and region["origin"] <= address
                   and address + size <= region["origin"] + region["length"] for region in regions)

    complete = all(any(r["kind"] == kind for r in regions) for kind in ("flash", "ram"))
    if complete:
        for segment in loads:
            require(contains(segment[2], segment[5]), "load segment exceeds target memory")
            if segment[4]:
                require(contains(segment[3], segment[4]), "load image exceeds target memory")
    vector = next((named[name] for name in (".isr_vector", ".vectors", ".vector_table")
                   if name in named and named[name][5] >= 8), None)
    if vector is None:
        address = next((symbols[n] for n in ("__Vectors", "__gVectors", "g_pfnVectors", "__isr_vector") if n in symbols), None)
        vector = next((s for s in sections if s[3] == address and s[5] >= 8 and s[2] & 2), None)
    report = {"entry": header[4], "load_segments": len(loads), "regions": regions,
              "memory_checked": complete, "vector_checked": vector is not None,
              "hardware_tested": False}
    if vector is not None:
        require(vector[1] == 1 and vector[2] & 2, "vector section is not allocated data")
        contents(vector)
        require(any(s[2] <= vector[3] and vector[3] + vector[5] <= s[2] + s[4]
                    and s[1] + vector[3] - s[2] == vector[4] for s in loads), "vectors are not in the load image")
        stack, reset = read("<II", vector[4])
        require(reset == header[4], "vector reset differs from ELF entry")
        require(stack % 8 == 0, "initial stack is not 8-byte aligned")
        if complete:
            require(contains(stack - 4, 4, "ram"), "initial stack is outside target RAM")
            require(contains(vector[3], vector[5], "flash"), "vectors are outside target FLASH")
            require(vector[3] == min(r["origin"] for r in regions if r["kind"] == "flash"), "vectors do not start at target FLASH origin")
        report.update(vector_address=vector[3], initial_stack=stack)
    report["status"] = "verified" if complete and vector is not None else "review_required"
    return report
