"""Versioned SDK catalogs, bounded output, and verified archive caches."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import urllib.parse
import urllib.request
import uuid
import zipfile


class BatchError(ValueError):
    pass


def canonical(path: Path) -> Path:
    text = str(Path(path).resolve())
    if os.name == "nt" and text.startswith("\\\\?\\") and len(text) > 6 and text[5] == ":":
        text = text[4:]
    return Path(text)


def linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


class Workspace:
    def __init__(self, root: Path):
        self.root = canonical(root)

    def output(self, path: Path) -> Path:
        path = Path(os.path.abspath(path))
        for parent in (path, *path.parents):
            if os.path.lexists(parent) and linked(parent):
                raise BatchError(f"Output contains a link or reparse point: {parent}")
        path = canonical(path)
        if not path.is_relative_to(self.root) or path == self.root:
            raise BatchError(f"Output must be inside the project: {path}")
        if path.relative_to(self.root).parts[0].lower() in {".git", ".agents", ".codex"}:
            raise BatchError(f"Reserved output directory: {path}")
        return path

    def mkdir(self, path: Path) -> Path:
        if canonical(path) == self.root:
            self.output(self.root / ".boundary-check")
            return self.root
        path = self.output(path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def json(self, path: Path, value) -> None:
        path = self.output(path)
        self.mkdir(path.parent)
        temporary = self.output(path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp"))
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def environment(self, directory: Path) -> dict:
        env = os.environ.copy()
        temporary = str(self.mkdir(directory / "tmp"))
        for key in ("TEMP", "TMP", "TMPDIR"):
            env[key] = temporary
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", CCACHE_DISABLE="1")
        return env

    def claim(self, directory: Path, kind: str) -> None:
        directory = self.output(directory)
        marker = self.output(directory / ".sdk-batch.json")
        expected = {"schema_version": 1, "kind": kind, "workspace": str(self.root)}
        if directory.exists():
            if marker.is_file():
                if json.loads(marker.read_text(encoding="utf-8")) != expected:
                    raise BatchError(f"Output belongs to a different batch workspace: {directory}")
                list(files(directory))
                return
            if any(directory.iterdir()):
                raise BatchError(f"Output must be empty or owned by this batch tool: {directory}")
        self.json(marker, expected)


@contextmanager
def exclusive_lock(workspace: Workspace, path: Path):
    path = workspace.output(path)
    workspace.mkdir(path.parent)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BatchError(f"Another batch process is using {path.parent}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def files(root: Path):
    if linked(root):
        raise BatchError(f"SDK input contains a link: {root}")
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as iterator:
            entries = sorted(iterator, key=lambda e: e.name)
        directories = []
        for entry in entries:
            path = Path(entry.path)
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise BatchError(f"SDK input contains a link: {path}")
            if stat.S_ISDIR(info.st_mode):
                directories.append(path)
            elif stat.S_ISREG(info.st_mode):
                yield path
            else:
                raise BatchError(f"Input is not a regular file: {path}")
        pending.extend(reversed(directories))


def snapshot(root: Path) -> dict:
    if root.is_file():
        return {".": digest(root)}
    if not root.is_dir():
        raise BatchError(f"Input is missing: {root}")
    return {path.relative_to(root).as_posix(): digest(path) for path in files(root)}


def member_path(name: str) -> Path:
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    reserved = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
    if (not parts or normalized.startswith("/") or any(
        part in {"..", "."} or re.search(r'[<>:"|?*]', part) or part.rstrip(" .") != part
        or reserved.match(part) or any(ord(c) < 32 for c in part) for part in parts
    )):
        raise BatchError(f"Unsafe archive member: {name!r}")
    return Path(*parts)


def load_catalog(path: Path, selected: list[str], overrides: dict[str, str]) -> list[dict]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("sdks"), list):
        raise BatchError("Catalog requires schema_version=1 and an sdks array")
    result, identifiers = [], set()
    for original in value["sdks"]:
        if not isinstance(original, dict):
            raise BatchError("Each SDK must be an object")
        entry = dict(original)
        identifier = entry.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", identifier) or identifier in identifiers:
            raise BatchError(f"Invalid or duplicate SDK id: {identifier!r}")
        identifiers.add(identifier)
        if not isinstance(entry.get("version"), str) or not entry["version"] or not isinstance(entry.get("source"), dict):
            raise BatchError(f"SDK version and source are required: {identifier}")
        for key in ("include", "exclude", "targets"):
            if key in entry and (not isinstance(entry[key], list) or any(not isinstance(x, str) for x in entry[key])):
                raise BatchError(f"{identifier}.{key} must be an array of patterns")
        if selected and identifier not in selected:
            continue
        if identifier in overrides:
            entry["source"] = {"type": "local", "root": str(canonical(Path(overrides[identifier])))}
        source = dict(entry["source"])
        for field in ("root", "root_env", "url", "sha256", "format"):
            if field in source and not isinstance(source[field], str):
                raise BatchError(f"{identifier}.source.{field} must be a string")
        if source.get("type") == "local":
            if not (source.get("root") or source.get("root_env")):
                raise BatchError(f"Local SDK requires root or root_env: {identifier}")
            root = source.get("root") or os.environ.get(source.get("root_env", ""))
            if root:
                location = Path(root)
                source["root"] = str(canonical(location if location.is_absolute() else path.parent / location))
        elif source.get("type") == "archive":
            parsed = urllib.parse.urlparse(source.get("url", ""))
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise BatchError(f"SDK download requires a public HTTPS URL: {identifier}")
            if not isinstance(source.get("layers", []), list):
                raise BatchError(f"{identifier}.source.layers must be an array")
            for layer in [source, *source.get("layers", [])]:
                if not isinstance(layer, dict) or not isinstance(layer.get("sha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", layer["sha256"]):
                    raise BatchError(f"Pinned SHA-256 is required: {identifier}")
                if not isinstance(layer.get("format"), str) or layer["format"] not in {"zip", "tar", "7z"}:
                    raise BatchError(f"Unsupported archive format: {identifier}")
                if layer is not source:
                    if not isinstance(layer.get("member"), str):
                        raise BatchError(f"Inner archive requires a member name: {identifier}")
                    member_path(layer["member"])
            if source.get("root", ".") != ".":
                member_path(source["root"])
        else:
            raise BatchError(f"Unknown source type: {identifier}")
        entry["source"] = source
        result.append(entry)
    unknown = (set(selected) | set(overrides)) - identifiers
    if unknown:
        raise BatchError(f"Unknown SDK ids: {sorted(unknown)}")
    if not result:
        raise BatchError("No SDK selected")
    return result


def unpack(workspace: Workspace, archive: Path, destination: Path, format_name: str,
           seven_zip: str | None = None) -> None:
    destination = workspace.output(destination)
    if destination.exists():
        raise BatchError(f"Extraction requires a fresh directory: {destination}")
    seen, total_size = set(), 0

    def check(name, size=0):
        nonlocal total_size
        relative = member_path(name)
        key = relative.as_posix().casefold()
        if key in seen:
            raise BatchError(f"Duplicate archive member: {name}")
        seen.add(key)
        total_size += size
        if size < 0 or size > 4 * 1024 ** 3:
            raise BatchError(f"Archive member exceeds 4 GiB: {name}")
        if total_size > 16 * 1024 ** 3 or len(seen) > 200000:
            raise BatchError("Archive exceeds extraction limits")
        return workspace.output(destination / relative)

    if format_name == "zip":
        with zipfile.ZipFile(archive) as source:
            targets = []
            for entry in source.infolist():
                if entry.is_dir() and entry.filename in (".", "./"):
                    continue
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise BatchError(f"Archive contains a special file: {entry.filename}")
                targets.append((entry, check(entry.filename, entry.file_size)))
            workspace.mkdir(destination)
            for entry, target in targets:
                if entry.is_dir():
                    workspace.mkdir(target)
                else:
                    workspace.mkdir(target.parent)
                    with source.open(entry) as incoming, target.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
    elif format_name == "tar":
        with tarfile.open(archive) as source:
            targets = []
            for entry in source.getmembers():
                if entry.isdir() and entry.name in (".", "./"):
                    continue
                if not (entry.isdir() or entry.isfile()):
                    raise BatchError(f"Archive contains a special file: {entry.name}")
                targets.append((entry, check(entry.name, entry.size)))
            workspace.mkdir(destination)
            for entry, target in targets:
                if entry.isdir():
                    workspace.mkdir(target)
                else:
                    workspace.mkdir(target.parent)
                    with source.extractfile(entry) as incoming, target.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
    elif format_name == "7z":
        executable = seven_zip or next((p for n in ("7z", "7za", "7zr") if (p := shutil.which(n))), None)
        if not executable:
            raise BatchError("7z archive requires --seven-zip or 7z/7za/7zr on PATH")
        env = workspace.environment(destination.parent)
        listing = subprocess.run([executable, "l", "-slt", "-ba", "-sccUTF-8", str(archive)],
                                 cwd=workspace.root, env=env, capture_output=True, timeout=120, check=True)
        text = listing.stdout.decode("utf-8")
        if re.search(r"(?:symbolic link|hard link|reparse) =", text, re.I):
            raise BatchError("Archive contains links")
        for block in re.split(r"\r?\n\s*\r?\n", text):
            fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
            if "Path" in fields:
                check(fields["Path"], int(fields.get("Size", "0")))
        if not seen:
            raise BatchError("Archive is empty or its listing is unsupported")
        workspace.mkdir(destination)
        with workspace.output(destination.parent / (destination.name + ".extract.log")).open("wb") as log:
            subprocess.run([executable, "x", "-y", f"-o{destination}", str(archive)],
                           cwd=workspace.root, env=env, stdout=log, stderr=subprocess.STDOUT,
                           timeout=300, check=True)
        list(files(destination))
    else:
        raise BatchError(f"Unsupported archive format: {format_name}")


def prepare_sdk(workspace: Workspace, cache: Path, entry: dict, seven_zip: str | None = None) -> Path:
    source = entry["source"]
    if source["type"] == "local":
        if not source.get("root"):
            raise BatchError(f"Set {source.get('root_env', 'SDK root')} or use --local-sdk {entry['id']}=DIRECTORY")
        root = canonical(Path(source["root"]))
        if not root.is_dir():
            raise BatchError(f"SDK directory is missing: {root}")
        return root
    key = identity(source)
    # 目录采用短标识，完整来源及 SHA-256 仍由 ready.json 验证。
    location = workspace.output(cache / "sdks" / key[:20])
    marker = workspace.output(location / "ready.json")
    if marker.is_file():
        ready = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(ready, dict) or not isinstance(ready.get("root"), str) or not isinstance(ready.get("files"), dict):
            raise BatchError(f"Invalid SDK cache marker; use a fresh --cache directory: {marker}")
        root = workspace.output(location / member_path(ready["root"]))
        if ready.get("source") != source or snapshot(root) != ready.get("files"):
            raise BatchError(f"SDK cache changed; use a fresh --cache directory: {location}")
        return root
    downloads = workspace.mkdir(cache / "downloads")
    archive = workspace.output(downloads / (source["sha256"].lower() + ".archive"))
    if archive.exists() and digest(archive) != source["sha256"].lower():
        raise BatchError(f"Download cache checksum mismatch: {archive}")
    if not archive.exists():
        partial = workspace.output(downloads / (uuid.uuid4().hex + ".part"))
        request = urllib.request.Request(source["url"], headers={"User-Agent": "KeilForge-sdk-batch/1"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("xb") as stream:
            if urllib.parse.urlparse(response.url).scheme != "https":
                raise BatchError("SDK download redirected outside HTTPS")
            shutil.copyfileobj(response, stream)
        if digest(partial) != source["sha256"].lower():
            raise BatchError(f"Downloaded SDK checksum mismatch; evidence retained: {partial}")
        partial.replace(archive)
    attempt = workspace.mkdir(location / ("extract-" + uuid.uuid4().hex[:8]))
    layers = [source, *source.get("layers", [])]
    for index, layer in enumerate(layers):
        if index:
            archive = destination / member_path(layer["member"])
            if not archive.is_file() or digest(archive) != layer["sha256"].lower():
                raise BatchError(f"Inner archive checksum mismatch: {archive}")
        destination = workspace.output(attempt / str(index))
        unpack(workspace, archive, destination, layer["format"], seven_zip)
    root = destination if source.get("root", ".") == "." else destination / member_path(source["root"])
    if not root.is_dir():
        raise BatchError(f"SDK root is absent from archive: {root}")
    workspace.json(marker, {"source": source, "root": root.relative_to(location).as_posix(), "files": snapshot(root)})
    return root
