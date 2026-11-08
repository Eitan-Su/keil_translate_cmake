"""AT32 SLIB 静态分区转换；不支持的 SCT 结构明确报错。"""

from dataclasses import dataclass
import re
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Region:
    name: str
    origin: int
    length: int
    body: str

    @property
    def end(self) -> int:
        return self.origin + self.length


def _blocks(text: str) -> List[Region]:
    header = re.compile(r"\s*([A-Za-z_]\w*)\s+(0x[\da-fA-F]+)\s+(0x[\da-fA-F]+)\s*\{")
    regions = []
    cursor = 0
    while text[cursor:].strip():
        match = header.match(text, cursor)
        if match is None:
            raise ValueError("SLIB SCT 仅支持静态十六进制地址和长度")
        start = cursor = match.end()
        depth = 1
        while cursor < len(text) and depth:
            depth += (text[cursor] == "{") - (text[cursor] == "}")
            cursor += 1
        if depth:
            raise ValueError("SLIB SCT 区域括号未闭合")
        region = Region(match[1], int(match[2], 16), int(match[3], 16), text[start:cursor - 1])
        if region.length <= 0 or region.end > 0x100000000:
            raise ValueError("SLIB SCT 区域地址或长度无效")
        regions.append(region)
    return regions


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def _objects(body: str, attributes: str) -> Tuple[str, ...]:
    pattern = re.compile(r"([A-Za-z_][\w-]*)\.o\s*\(\s*" + attributes + r"\s*\)", re.I)
    result = tuple(match[1].lower() for match in pattern.finditer(body))
    if not result or pattern.sub("", body).strip() or len(set(result)) != len(result):
        raise ValueError("SLIB SCT 对象选择器不受支持")
    return result


def read_symdefs(content: str) -> Dict[str, int]:
    if not content.startswith("#<SYMDEFS>#"):
        raise ValueError("SLIB 需要 ARM Linker SYMDEFS 函数符号表")
    symbols: Dict[str, int] = {}
    for line in content.splitlines()[1:]:
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*(0x[\da-fA-F]+)\s+T\s+([A-Za-z_]\w*)\s*", line)
        if match is None or not int(match[1], 16) & 1:
            raise ValueError("SLIB 导出接口必须为具有 Thumb 地址位的函数符号")
        address, name = int(match[1], 16), match[2]
        if name in symbols or address in symbols.values():
            raise ValueError("SLIB 符号表包含重复名称或地址")
        symbols[name] = address
    if not symbols:
        raise ValueError("SLIB 函数符号表为空")
    return symbols


@dataclass(frozen=True)
class SlibLayout:
    flash: Region
    ram: Region
    reserved_ram: Region
    code: Optional[Region]
    constants: Optional[Region]
    support: Optional[Region]
    code_objects: Tuple[str, ...]
    data_objects: Tuple[str, ...]
    ram_objects: Tuple[str, ...]
    symbols: Dict[str, int]

    @property
    def producer(self) -> bool:
        return self.code is not None


def parse_layout(text: str, symbols: Dict[str, int], flash: Tuple[int, int],
                 ram: Tuple[int, int]) -> SlibLayout:
    text = re.sub(r";[^\r\n]*", "", text)
    loads = _blocks(text)
    load_names = {region.name for region in loads}
    producer = "LR_SLIB_INST" in load_names
    expected = {"LR_IROM1", "LR_SLIB_C_LIB", "LR_SLIB_INST", "LR_SLIB_DATA"} if producer else {"LR_IROM1"}
    if load_names != expected or len(loads) != len(expected):
        raise ValueError("SLIB SCT 加载区域组合不受支持")
    children = [item for region in loads for item in _blocks(region.body)]
    regions = {item.name: item for item in children}
    if len(regions) != len(children):
        raise ValueError("SLIB SCT 包含重复执行区域名称")
    main_load = next(region for region in loads if region.name == "LR_IROM1")
    main_flash = regions.get("ER_IROM1")
    if main_flash is None or (main_flash.origin, main_flash.length) != (main_load.origin, main_load.length):
        raise ValueError("SLIB 主代码区域必须原位执行")
    if _compact(main_flash.body) != "*.O(RESET,+FIRST)*(INROOT$$SECTIONS).ANY(+RO)":
        raise ValueError("SLIB 主代码选择器不受支持")
    main_ram = regions.get("RW_IRAM2" if producer else "RW_IRAM1")
    if main_ram is None or _compact(main_ram.body) != ".ANY(+RW+ZI)":
        raise ValueError("SLIB 应用 RAM 选择器不受支持")
    if main_ram.origin <= ram[0] or main_ram.end > ram[0] + ram[1]:
        raise ValueError("SLIB 应用 RAM 必须避开低地址保留区")
    reserved = Region("SLIB_RAM", ram[0], main_ram.origin - ram[0], "")
    code = constants = support = None
    code_objects = data_objects = ram_objects = ()
    if producer:
        if set(regions) != {"ER_IROM1", "RW_IRAM1", "RW_IRAM2", "ER_SLIB_C_LIB", "ER_SLIB_INST", "ER_SLIB_DATA"}:
            raise ValueError("SLIB SCT 执行区域组合不受支持")
        for load in loads:
            if load.name != "LR_IROM1":
                children = _blocks(load.body)
                if (len(children) != 1 or children[0].name != load.name.replace("LR_", "ER_", 1)
                        or (children[0].origin, children[0].length) != (load.origin, load.length)):
                    raise ValueError("SLIB 保护区必须原位执行")
        if {item.name for item in _blocks(main_load.body)} != {"ER_IROM1", "RW_IRAM1", "RW_IRAM2"}:
            raise ValueError("SLIB 主加载区必须包含应用和专用 RAM")
        support, code, constants = (regions[name] for name in ("ER_SLIB_C_LIB", "ER_SLIB_INST", "ER_SLIB_DATA"))
        if _compact(support.body) != "*ARMLIB*(+RO)":
            raise ValueError("SLIB 运行库选择器不受支持")
        code_objects = _objects(code.body, r"\+XO")
        data_objects = _objects(constants.body, r"\+RO")
        ram_objects = _objects(regions["RW_IRAM1"].body, r"\+RW\s*\+ZI")
        if set(code_objects) & set(data_objects):
            raise ValueError("SLIB 指令与常量对象选择器发生重叠")
        if not set(ram_objects).issubset(code_objects):
            raise ValueError("SLIB 专用 RAM 对象必须属于保护代码")
        if (regions["RW_IRAM1"].origin, regions["RW_IRAM1"].length) != (reserved.origin, reserved.length):
            raise ValueError("SLIB 专用 RAM 与应用 RAM 分区不匹配")
        if (main_flash.end, support.end, code.end) != (support.origin, code.origin, constants.origin):
            raise ValueError("SLIB Flash 分区必须按应用、运行库、保护代码、常量连续排列")
        if any(not code.origin <= (value & ~1) < code.end for value in symbols.values()):
            raise ValueError("SLIB 导出函数地址超出保护代码区域")
    else:
        if set(regions) != {"ER_IROM1", "RW_IRAM1"}:
            raise ValueError("SLIB 调用工程包含不受支持的区域")
        if any(not main_flash.end <= (value & ~1) < flash[0] + flash[1] for value in symbols.values()):
            raise ValueError("SLIB 导入函数必须位于应用 Flash 之外")
    if main_flash.origin != flash[0] or any(region.end > flash[0] + flash[1] for region in loads):
        raise ValueError("SLIB Flash 分区超出器件内存范围")
    if any(region.origin % 4 or region.length % 4 for region in (main_ram, reserved)):
        raise ValueError("SLIB RAM 区域必须按四字节对齐")
    return SlibLayout(main_flash, main_ram, reserved, code, constants, support,
                      code_objects, data_objects, ram_objects, symbols)


def render_linker(layout: SlibLayout, base: str, objects: Dict[str, str]) -> str:
    """在现有启动符号约定下生成分区；支持的保护状态仅包含零初始化数据。"""
    def selectors(names: Tuple[str, ...], sections: str, keep: bool = False) -> str:
        lines = []
        for name in names:
            filename = objects[name]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
                raise ValueError("SLIB 对象文件名包含不受支持的字符")
            for suffix in (".obj", ".o"):
                item = f"*{filename}{suffix}({sections})"
                lines.append(f"    KEEP({item})" if keep else f"    {item}")
        return "\n".join(lines)

    memory = [f"  FLASH (rx) : ORIGIN = 0x{layout.flash.origin:08X}, LENGTH = 0x{layout.flash.length:X}",
              f"  RAM (xrw) : ORIGIN = 0x{layout.ram.origin:08X}, LENGTH = 0x{layout.ram.length:X}"]
    extra = []
    if layout.producer:
        for name, region in (("SLIB_SUPPORT", layout.support), ("SLIB_CODE", layout.code),
                             ("SLIB_DATA", layout.constants), ("SLIB_RAM", layout.reserved_ram)):
            memory.append(f"  {name} : ORIGIN = 0x{region.origin:08X}, LENGTH = 0x{region.length:X}")
        for index, (name, address) in enumerate(sorted(layout.symbols.items(), key=lambda item: item[1])):
            extra.extend([f"  .slib_api_{index} 0x{address & ~1:08X} : {{",
                          selectors(layout.code_objects, f".text.{name}", True),
                          "  } > SLIB_CODE",
                          f'  ASSERT(({name} | 1) == 0x{address:08X}, "SLIB export address mismatch: {name}")'])
        extra.extend(["  .slib_code : {", selectors(layout.code_objects, ".text*", True), "  } > SLIB_CODE",
                      "  .slib_constants : {", selectors(layout.data_objects + layout.code_objects, ".rodata*", True), "  } > SLIB_DATA",
                      "  .slib_support : { *.a:*(.text* .rodata* .glue_7* .eh_frame*) } > SLIB_SUPPORT",
                      "  .slib_bss (NOLOAD) : {", selectors(layout.ram_objects, ".bss* COMMON"), "    . = ALIGN(4);", "  } > SLIB_RAM",
                      "  .slib_initialized_data : {", selectors(layout.ram_objects, ".data*"), "  } > SLIB_RAM AT > SLIB_DATA",
                      '  ASSERT(SIZEOF(.slib_initialized_data) == 0, "SLIB initialized private data needs a shared startup contract")'])
    memory_block = "MEMORY\n{\n" + "\n".join(memory) + "\n}"
    base, count = re.subn(r"MEMORY\s*\{[^}]*\}", lambda _: memory_block, base, count=1)
    if count != 1:
        raise ValueError("SLIB 基础链接脚本缺少 MEMORY")
    base, count = re.subn(r"SECTIONS\s*\{", lambda _: "SECTIONS\n{\n" + "\n".join(extra), base, count=1)
    if count != 1:
        raise ValueError("SLIB 基础链接脚本缺少 SECTIONS")
    return "/* AT32 SLIB static layout converted from the original SCT. */\n" + base


def runtime_source(layout: SlibLayout) -> str:
    return f'''/* Initialize the RAM reserved for the SLIB provider on either image's reset. */
#include <stdint.h>
static void keil_slib_zero_ram(void)
{{
    volatile uint32_t *p = (volatile uint32_t *)0x{layout.reserved_ram.origin:08X}u;
    while (p < (volatile uint32_t *)0x{layout.reserved_ram.end:08X}u)
        *p++ = 0;
}}
__attribute__((used, section(".preinit_array")))
static void (*const keil_slib_preinit)(void) = keil_slib_zero_ram;
'''


def export_check_script(symbols: Dict[str, int]) -> str:
    lines = ['execute_process(COMMAND "${NM}" --defined-only --format=posix "${ELF}"',
             '    RESULT_VARIABLE status OUTPUT_VARIABLE table ERROR_VARIABLE error)',
             'if(NOT status EQUAL 0)', '    message(FATAL_ERROR "SLIB symbol inspection failed: ${error}")', 'endif()',
             'set(symdefs "#<SYMDEFS># Generated from the GCC ELF\\n")', 'set(fragment "/* Actual GCC SLIB exports. */\\n")']
    for name, address in sorted(symbols.items()):
        lines.extend([f'string(REGEX MATCH "(^|\\n){name} [Tt] ([0-9a-fA-F]+)" found "${{table}}")',
                      'if(NOT found)', f'    message(FATAL_ERROR "SLIB export missing: {name}")', 'endif()',
                      'math(EXPR actual "0x${CMAKE_MATCH_2} | 1" OUTPUT_FORMAT HEXADECIMAL)',
                      f'if(NOT actual EQUAL {address})', f'    message(FATAL_ERROR "SLIB export address mismatch: {name}")', 'endif()',
                      f'string(APPEND symdefs "${{actual}} T {name}\\n")',
                      f'string(APPEND fragment "PROVIDE({name} = ${{actual}});\\n")'])
    lines.extend(['file(WRITE "${OUTPUT_DIR}/slib_exports.symdefs" "${symdefs}")',
                  'file(WRITE "${OUTPUT_DIR}/slib_exports.ld" "${fragment}")'])
    return "\n".join(lines) + "\n"
