"""GD32 SDK startup/linker selection for static Keil memory layouts."""

from dataclasses import dataclass
from pathlib import Path
import re
import xml.etree.ElementTree as ET


@dataclass
class Gd32Assets:
    startup: Path
    linker: Path
    content: str
    notes: list[str]


def _without_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)


def _number(value: str) -> int:
    match = re.fullmatch(r"(0x[0-9a-f]+|[0-9]+)([km]?)", value.strip(), re.I)
    if not match:
        raise ValueError(f"GD32: unsupported memory expression: {value}")
    return int(match[1], 16 if match[1].lower().startswith("0x") else 10) * {
        "": 1, "k": 1024, "m": 1024 * 1024,
    }[match[2].lower()]


def _fixed_sections(sources: list[Path]) -> dict[str, int]:
    sections: dict[str, int] = {}
    attribute = re.compile(
        r'\b(?P<symbol>[A-Za-z_]\w*)\s*(?:\[[^\]\n]*\]\s*)?'
        r'__attribute__\s*\(\(\s*(?:at\s*\(\s*(?P<address>0x[0-9a-fA-F]+)\s*\)'
        r'|section\s*\(\s*"(?P<section>\.[\w.]+)"\s*\))'
    )
    for source in sources:
        if source.suffix.lower() not in {".c", ".cpp", ".cc", ".cxx"}:
            continue
        text = source.read_text(encoding="utf-8", errors="ignore")
        if "__attribute__" not in text or not re.search(r"\bat\s*\(", text):
            continue
        symbols: dict[str, dict[str, set]] = {}
        for match in attribute.finditer(_without_comments(text)):
            values = symbols.setdefault(match["symbol"], {"address": set(), "section": set()})
            for key in values:
                if match[key]:
                    values[key].add(match[key])
        for symbol, values in symbols.items():
            if not values["address"]:
                continue
            if len(values["address"]) != 1 or len(values["section"]) != 1:
                raise ValueError(f"GD32: ambiguous fixed-address declaration: {source}: {symbol}")
            section = next(iter(values["section"]))
            address = int(next(iter(values["address"])), 16)
            if section in sections and sections[section] != address:
                raise ValueError(f"GD32: conflicting fixed addresses for {section}")
            sections[section] = address
    return sections


def select_assets(
    device: str, project: Path, target_name: str, sources: list[Path],
    original_startup: Path, arm_vectors: list[str], stack_heap: tuple[int, int],
) -> Gd32Assets:
    match = re.fullmatch(r"(GD32[A-Z]\d{3})[A-Z]([0-9A-Z])[A-Z0-9_-]*", device, re.I)
    if not match:
        raise ValueError(f"GD32: unsupported device identifier: {device}")
    expected = f"{match[1].lower()}x{match[2].upper()}_flash.ld"
    gcc_dir = (original_startup.parent if original_startup.parent.name.lower() == "gcc"
               else original_startup.parent.parent / "GCC")
    startup = gcc_dir / (original_startup.stem + ".S")
    sdk_linker = gcc_dir / "Ld" / expected
    if not startup.is_file() or not sdk_linker.is_file():
        raise ValueError(f"GD32: matching SDK GCC startup/linker pair missing: {startup}, {sdk_linker}")
    startup_text = _without_comments(startup.read_text(encoding="utf-8"))
    vectors = re.search(r"__gVectors:\s*(.*?)\.size\s+__gVectors", startup_text, re.S)
    entries = re.findall(r"\.word\s+([A-Za-z_]\w*|0)\b", vectors[1]) if vectors else []
    if not arm_vectors and original_startup.resolve() == startup.resolve():
        arm_vectors = entries
    if len(arm_vectors) < 2 or entries[1:] != arm_vectors[1:]:
        raise ValueError(f"GD32: GCC and Keil interrupt vectors differ: {startup}")
    if not re.search(r"\.section\s+\.vectors\b", startup_text):
        raise ValueError(f"GD32: unsupported GCC vector section: {startup}")

    tree = ET.parse(project)
    target = next(t for t in tree.findall(".//Targets/Target") if t.findtext("TargetName") == target_name)
    if target.findtext(".//LDads/useFile") == "1":
        raise ValueError("GD32: custom scatter layout requires explicit conversion")
    memories = target.find(".//OnChipMemories")
    if memories is None:
        raise ValueError("GD32: target memory configuration is missing")
    for flag in ("hadIROM2", "hadIRAM2"):
        if target.findtext(f".//ArmAdsMisc/{flag}") == "1":
            raise ValueError("GD32: multiple memory regions require explicit conversion")
    layout = {}
    for name, nodes in (("FLASH", ("OCR_RVCT4", "IROM")), ("RAM", ("OCR_RVCT9", "IRAM"))):
        for node_name in nodes:
            node = memories.find(node_name)
            if node is not None:
                origin = _number(node.findtext("StartAddress", "0"))
                size = _number(node.findtext("Size", "0"))
                if size:
                    layout[name] = (origin, size)
                    break
        if name not in layout:
            raise ValueError(f"GD32: {name} configuration is missing")

    # 仅检查工程实际引用的源码目录，避免选中相邻例程的专用分区。
    local = {p.parent / expected for p in sources if p.suffix.lower() in {".c", ".cpp", ".cc", ".cxx"}}
    local = {p.resolve() for p in local if p.is_file()}
    if len(local) > 1:
        raise ValueError(f"GD32: ambiguous project linker scripts: {sorted(local)}")
    linker = next(iter(local)) if local else sdk_linker
    content = linker.read_text(encoding="utf-8")
    clean = _without_comments(content)
    for symbol in ("__gVectors", "_sp", "_sidata", "_sdata", "_edata", "_sbss", "_ebss"):
        pattern = rf"\b{symbol}\b" if symbol == "__gVectors" else rf"\b{symbol}\s*="
        if not re.search(pattern, clean):
            raise ValueError(f"GD32: linker contract is missing {symbol}: {linker}")
    if not re.search(r"KEEP\s*\(\s*\*\(\s*\.vectors\s*\)\s*\)", clean):
        raise ValueError(f"GD32: linker does not retain the GCC vector table: {linker}")
    memory_pattern = re.compile(
        r"(?m)^(\s*(FLASH|RAM)\s*\([^)]*\)\s*:\s*ORIGIN\s*=\s*)"
        r"([^,\n]+)(,\s*LENGTH\s*=\s*)([^\n}]+)"
    )
    regions = list(memory_pattern.finditer(clean))
    if sorted(m[2] for m in regions) != ["FLASH", "RAM"]:
        raise ValueError(f"GD32: unsupported linker memory layout: {linker}")
    notes = []
    for region in regions:
        configured = layout[region[2]]
        if configured != (_number(region[3]), _number(region[5])):
            notes.append(f"GD32 {region[2]} uses Keil target configuration: 0x{configured[0]:08X}+0x{configured[1]:X}")
    content = memory_pattern.sub(
        lambda m: f"{m[1]}0x{layout[m[2]][0]:08X}{m[4]}0x{layout[m[2]][1]:X}", content,
    )
    for symbol, size in zip(("__stack_size", "__heap_size"), stack_heap):
        content, count = re.subn(
            rf"(?m)^\s*{symbol}\s*=\s*[^;]+;", f"  {symbol} = 0x{size:X};", content,
        )
        if count != 1:
            raise ValueError(f"GD32: unsupported {symbol} reservation: {linker}")

    for section, address in _fixed_sections(sources).items():
        origin, length = layout["FLASH"]
        if not origin < address < origin + length:
            raise ValueError(f"GD32: fixed section {section} is outside target FLASH")
        section_match = re.search(rf"(?m)^\s*{re.escape(section)}\s+(0x[0-9a-fA-F]+)\s*:", clean)
        if section_match:
            if int(section_match[1], 16) != address:
                raise ValueError(f"GD32: fixed section address mismatch: {section}")
        else:
            block = f"  {section} 0x{address:08X} : {{ KEEP(*({section})) }} > FLASH\n"
            content, count = re.subn(r"(?m)^(\s*\.text\s*:)", lambda m: block + m[1], content, count=1)
            if count != 1:
                raise ValueError("GD32: cannot place the fixed section before .text")
        content += f'\nASSERT(ADDR({section}) == 0x{address:08X}, "GD32 fixed section address mismatch")\n'
    content += (
        '\nASSERT(ADDR(.vectors) == ORIGIN(FLASH), "GD32 vector address mismatch")\n'
        'ASSERT(_sp <= ORIGIN(RAM) + LENGTH(RAM), "GD32 stack exceeds RAM")\n'
        'PROVIDE(__HeapBase = end);\nPROVIDE(__HeapLimit = _heap_end);\n'
    )
    return Gd32Assets(startup, linker, content, notes)
