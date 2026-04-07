import ctypes
import os
from configparser import ConfigParser
from pathlib import Path
from typing import Optional


COMMON_ROOTS = (
    Path.home() / "AppData" / "Local" / "Keil_v5",
    Path("C:/Keil_v5"),
    Path("C:/Keil"),
)

COMMON_DRIVE_SUFFIXES = (
    Path("Keil_v5"),
    Path("Keil"),
    Path("install/keil5 mdk"),
)

ENV_ROOT_KEYS = ("KEIL_MDK_ROOT", "UV4_ROOT", "KEIL_ROOT")


def _strip_quotes(value: str) -> str:
    return value.strip().strip('"')


def _normalize_root(path_like: str | Path) -> Path:
    path = Path(_strip_quotes(str(path_like))).expanduser()
    name = path.name.lower()

    if path.is_file():
        if name == "tools.ini":
            return path.parent
        if name == "uv4.exe":
            return path.parent.parent

    if name == "uv4":
        return path.parent

    return path


def _iter_drive_roots():
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except AttributeError:
        system_drive = os.environ.get("SystemDrive", "C:")
        yield Path(f"{system_drive}/")
        return

    for index in range(26):
        if bitmask & (1 << index):
            yield Path(f"{chr(ord('A') + index)}:/")


def _find_tools_ini(root: Path) -> Optional[Path]:
    config_file = root / "TOOLS.INI"
    if config_file.is_file():
        return config_file

    if not root.is_dir():
        return None

    for candidate in root.glob("*/TOOLS.INI"):
        if candidate.is_file():
            return candidate

    return None


def _append_candidate(
    candidates: list[Path], seen: set[str], value: Optional[str | Path]
) -> None:
    if not value:
        return

    path = _normalize_root(value)
    key = os.path.normcase(str(path))
    if key in seen:
        return

    seen.add(key)
    candidates.append(path)


def find_keil_mdk_root(preferred: Optional[str] = None) -> Optional[str]:
    candidates: list[Path] = []
    seen: set[str] = set()

    _append_candidate(candidates, seen, preferred)

    for env_key in ENV_ROOT_KEYS:
        _append_candidate(candidates, seen, os.environ.get(env_key))

    for root in COMMON_ROOTS:
        _append_candidate(candidates, seen, root)

    for drive_root in _iter_drive_roots():
        for suffix in COMMON_DRIVE_SUFFIXES:
            _append_candidate(candidates, seen, drive_root / suffix)

    for candidate in candidates:
        config_file = _find_tools_ini(candidate)
        if config_file is not None:
            return str(config_file.parent)

    return None


def _resolve_ini_path(root: Path, value: str) -> str:
    value = _strip_quotes(value)
    if not value:
        return ""

    path = Path(value)
    if not path.is_absolute():
        path = root / path

    return str(path.resolve(strict=False))


def _resolve_armclang_path(root: Path, path_value: str, path1_value: str) -> str:
    base_path = _resolve_ini_path(root, path_value)
    suffix_path = _strip_quotes(path1_value)

    if not base_path:
        return _resolve_ini_path(root, suffix_path)

    armclang_path = Path(base_path)
    if suffix_path:
        suffix = Path(suffix_path)
        if suffix.is_absolute():
            armclang_path = suffix
        else:
            armclang_path = armclang_path / suffix

    return str(armclang_path.resolve(strict=False))


def get_keil_mdk_info(uv4_root: Optional[str]) -> Optional[dict]:
    root_path = find_keil_mdk_root(uv4_root)
    if root_path is None:
        return None

    config = ConfigParser()
    root = Path(root_path)
    config_file = root / "TOOLS.INI"
    if not config_file.is_file():
        return None

    config.read(config_file)

    mdk_info = {
        "UV4_ROOT": str(root),
        "TOOLS_INI": str(config_file),
    }

    rte_path = _resolve_ini_path(root, config.get("UV2", "RTEPATH", fallback=""))
    if rte_path:
        mdk_info["RTEPATH"] = rte_path

    armclang_path = _resolve_armclang_path(
        root,
        config.get("ARMADS", "PATH", fallback=""),
        config.get("ARMADS", "PATH1", fallback=""),
    )
    if armclang_path:
        mdk_info["ARMCLANG_PATH"] = armclang_path

    uv4_exe = root / "UV4" / "UV4.exe"
    if uv4_exe.is_file():
        mdk_info["UV4_EXE"] = str(uv4_exe)

    return mdk_info


if __name__ == "__main__":
    uv4_root = find_keil_mdk_root()
    print(f"Using UV4 root: {uv4_root}")
    mdk_info = get_keil_mdk_info(uv4_root)
    print(mdk_info)
