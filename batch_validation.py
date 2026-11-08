"""Batch SDK conversion, resumable builds, and grouped diagnostics."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from firmware_validation import inspect_elf, target_regions
from keil_to_cmake import BuildValidator
from sdk_sources import (BatchError, Workspace, canonical, digest, exclusive_lock, files,
                         identity, load_catalog, member_path, prepare_sdk, snapshot)


def matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) or (p.startswith("**/") and fnmatch.fnmatchcase(name, p[3:])) for p in patterns)


def discover(entry: dict, root: Path) -> tuple[list[dict], list[dict]]:
    targets, errors = [], []
    include = entry.get("include", ["**/*.uvprojx", "**/*.uvproj"])
    for project in files(root):
        relative = project.relative_to(root).as_posix()
        if project.suffix.lower() not in {".uvprojx", ".uvproj"} or not matches(relative, include) or matches(relative, entry.get("exclude", [])):
            continue
        try:
            tree = ET.parse(project)
            seen = set()
            nodes = tree.findall(".//Targets/Target")
            if not nodes:
                raise BatchError("Keil project has no targets")
            for target in nodes:
                name = target.findtext("TargetName", "").strip()
                if not name or name in seen:
                    raise BatchError("Missing or duplicate TargetName")
                seen.add(name)
                if entry.get("targets") and not matches(name, entry["targets"]):
                    continue
                output_name = target.findtext("TargetOption/TargetCommonOption/OutputName") or name
                member_path(output_name)
                if (output_name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f;$]', output_name)
                        or output_name.rstrip(" .") != output_name):
                    raise BatchError(f"Unsafe output filename: {output_name!r}")
                targets.append({"id": identity([entry["id"], relative, name])[:16], "sdk": entry["id"],
                    "version": entry["version"], "project": str(project), "relative_project": relative,
                    "target": name, "output_name": output_name,
                    "device": target.findtext("TargetOption/TargetCommonOption/Device", ""),
                    "cpu": target.findtext("TargetOption/TargetCommonOption/Cpu", ""), "regions": target_regions(target)})
        except (ET.ParseError, ValueError, OSError) as exc:
            errors.append({"sdk": entry["id"], "project": relative, "stage": "discover", "message": str(exc)})
    return sorted(targets, key=lambda t: (t["relative_project"], t["target"])), errors


def diagnose(stage: str, log: Path | None, error: str = "") -> dict:
    patterns = [
        ("missing_header", r"fatal error:.*(?:\.h|\.hpp).*No such file"),
        ("undefined_symbol", r"undefined reference to|undefined symbol"),
        ("memory_overflow", r"region .* overflowed|will not fit in region"),
        ("abi_mismatch", r"uses VFP register arguments|conflicting CPU architectures|file format not recognized"),
        ("assembly_syntax", r"bad instruction|unknown pseudo-op|assembler messages"),
        ("duplicate_symbol", r"multiple definition of|duplicate symbol"),
        ("linker_script", r"cannot open linker script|syntax error.*\.ld"),
        ("unsupported_layout", r"GD32:|SLIB.*(?:unsupported|missing)|unsupported.*layout|requires explicit conversion"),
        ("missing_source", r"Cannot find source file|No rule to make target|fatal error:.*No such file"),
        ("compile_error", r"\berror:"),
        ("configure_error", r"CMake Error"),
    ]
    category, message = stage + "_failed", error
    if "timed out" in error.lower():
        category = "timeout"
    elif "interrupted" in error.lower():
        category = "interrupted"
    elif "tool missing" in error.lower():
        category = "missing_tool"
    elif stage in {"integrity", "artifact"}:
        category = stage
    else:
        found = {}
        if log and log.exists():
            with log.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    for kind, pattern in patterns:
                        if kind not in found and re.search(pattern, line, re.I):
                            found[kind] = line.strip()[:1600]
        for kind, _ in patterns:
            if kind in found:
                category, message = kind, found[kind]
                break
    signature = re.sub(r"^.*?(?=(?:fatal error:|undefined reference to|region .* overflowed|will not fit in region))", "", message)
    signature = re.sub(r"(?:[A-Za-z]:[\\/]|/)[^\s:'\"]+", "<path>", signature)
    signature = re.sub(r"0x[0-9a-fA-F]+|:\d+(?::\d+)?", "<number>", signature)
    return {"category": category, "message": message, "signature": identity([category, signature])[:16]}


class Snapshots:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()

    def get(self, path: Path):
        key = str(canonical(path))
        with self.lock:
            if key not in self.values:
                self.values[key] = self.read(Path(key))
            return self.values[key]

    @staticmethod
    def read(path: Path):
        return snapshot(path) if path.exists() else {"missing": True}

    def hashes(self, paths):
        return {str(canonical(p)): identity(self.get(p)) for p in paths}


class Commands:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.children = set()
        self.lock = threading.Lock()
        self.cancelled = threading.Event()

    def stop(self):
        self.cancelled.set()
        with self.lock:
            for process in self.children:
                BuildValidator._terminate_process_tree(process)

    def run(self, args: list[str], directory: Path, phase: str, timeout: int) -> dict:
        directory = self.workspace.mkdir(directory)
        log = self.workspace.output(directory / (phase + ".log"))
        start, code, error = time.monotonic(), None, ""
        try:
            with log.open("wb") as stream:
                with self.lock:
                    if self.cancelled.is_set():
                        raise BatchError("Batch interrupted")
                    process = subprocess.Popen(args, cwd=directory, env=self.workspace.environment(directory),
                        stdout=stream, stderr=subprocess.STDOUT, **BuildValidator._process_group_options())
                    self.children.add(process)
                try:
                    code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    BuildValidator._terminate_process_tree(process)
                    code, error = process.poll(), f"Command timed out after {timeout} seconds"
                finally:
                    with self.lock:
                        self.children.discard(process)
        except (OSError, BatchError) as exc:
            error = str(exc)
        if self.cancelled.is_set():
            error = "Batch interrupted"
        return {"command": args, "return_code": code, "error": error, "log": str(log),
                "log_sha256": digest(log) if log.is_file() else None,
                "seconds": round(time.monotonic() - start, 3), "success": code == 0 and not error}


def dependencies(report: dict, sdk: Path, generated: Path) -> list[Path]:
    paths = [Path(p) for key in ("source_files", "include_paths", "libraries") for p in report.get(key, [])]
    paths.extend(Path(a["path"]) for a in report.get("assets", []) if a.get("path"))
    external = {canonical(p) for p in paths if not canonical(p).is_relative_to(sdk)
                and not canonical(p).is_relative_to(generated)}
    if any(generated.is_relative_to(p) for p in external):
        raise BatchError("External dependency directory contains batch outputs; use a narrower include directory")
    return sorted(external)


def reusable(workspace: Workspace, previous: dict, fingerprint: str, snapshots: Snapshots) -> bool:
    if not isinstance(previous, dict) or previous.get("fingerprint") != fingerprint or previous.get("status") not in {"success", "review_required", "failed"}:
        return False
    try:
        if not previous.get("dependencies_complete") or not previous.get("commands") or not previous.get("generated_inputs"):
            return False
        generated = workspace.output(Path(previous["generated_directory"]))
        actual = {str(p) for p in files(generated)} - {str(generated / "compile_commands.json")}
        if actual != set(previous["generated_inputs"]):
            return False
        if previous["status"] == "failed" and not previous.get("diagnostic"):
            return False
        for name, expected in previous.get("dependencies", {}).items():
            if identity(snapshots.get(Path(name))) != expected:
                return False
        for name, expected in previous.get("generated_inputs", {}).items():
            if digest(workspace.output(Path(name))) != expected:
                return False
        for item in previous.get("artifacts", {}).values():
            if digest(workspace.output(Path(item["file"]))) != item["sha256"]:
                return False
        for command in previous.get("commands", {}).values():
            if digest(workspace.output(Path(command["log"]))) != command.get("log_sha256"):
                return False
        if previous["status"] in {"success", "review_required"}:
            if set(previous.get("artifacts", {})) != {".elf", ".bin", ".hex"} or not previous.get("firmware"):
                return False
            if any(not previous["commands"].get(phase, {}).get("success") for phase in ("conversion", "configure", "build", "size")):
                return False
        if previous.get("diagnostic", {}).get("category") in {"integrity", "timeout", "interrupted"}:
            return False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return True


def build_target(workspace: Workspace, output: Path, target: dict, sdk: Path, fingerprint: str,
                 options: dict, tools: dict, snapshots: Snapshots, commands: Commands) -> dict:
    workspace.mkdir(output / "runs")
    job = workspace.output(output / "runs" / uuid.uuid4().hex[:12])
    job.mkdir()
    generated, build = workspace.output(job / "g"), workspace.output(job / "b")
    result = {**target, "fingerprint": fingerprint, "status": "running", "stage": "conversion",
              "commands": {}, "job": str(job), "generated_directory": str(generated), "dependencies": {}}
    started = time.monotonic()
    workspace.json(job / "result.json", result)

    def execute(phase, args):
        result["stage"] = phase
        command = commands.run(args, job, phase, options["timeout"])
        result["commands"][phase] = command
        workspace.json(job / "result.json", result)
        if not command["success"]:
            raise BatchError(command["error"] or f"{phase} exited {command['return_code']}")

    try:
        execute("conversion", [sys.executable, "-B", str(workspace.root / "keil_to_cmake.py"), target["project"],
            "--destination", str(generated), "--target-name", target["target"], "--compiler", "gcc",
            "--generator", options["generator"], "--host-os", "windows" if os.name == "nt" else "linux"])
        report = json.loads((generated / "conversion-report.json").read_text(encoding="utf-8"))
        result["dependencies"] = snapshots.hashes(dependencies(report, sdk, generated))
        result["dependencies_complete"] = True
        result["warnings"] = report.get("warnings", [])
        result["generated_inputs"] = {str(p): digest(p) for p in files(generated)}
        execute("configure", [tools["cmake"]["path"], "-S", str(generated), "-B", str(build), "-G", options["generator"],
            f"-DCMAKE_MAKE_PROGRAM={Path(tools['make']['path']).as_posix()}", f"-DCMAKE_BUILD_TYPE={options['configuration']}",
            f"-DCMAKE_C_COMPILER={Path(tools['gcc']['path']).as_posix()}",
            f"-DCMAKE_ASM_COMPILER={Path(tools['gcc']['path']).as_posix()}",
            f"-DCMAKE_CXX_COMPILER={Path(tools['g++']['path']).as_posix()}",
            "-DKEIL_GCC_COMPILER_CACHE=OFF", "-DCMAKE_EXPORT_NO_PACKAGE_REGISTRY=ON"])
        execute("build", [tools["cmake"]["path"], "--build", str(build), "--parallel", str(options["build_jobs"])])
        result["stage"] = "artifact"
        artifacts = {}
        for suffix in (".elf", ".hex", ".bin"):
            path = workspace.output(build / (target["output_name"] + suffix))
            if not path.is_file() or path.stat().st_size == 0:
                raise BatchError(f"Missing firmware artifact: {path}")
            artifacts[suffix] = {"file": str(path), "bytes": path.stat().st_size, "sha256": digest(path)}
        result["artifacts"] = artifacts
        execute("size", [tools["size"]["path"], artifacts[".elf"]["file"]])
        result["stage"] = "artifact"
        result["firmware"] = inspect_elf(Path(artifacts[".elf"]["file"]), target["regions"])
        result["status"] = "success" if result["firmware"]["status"] == "verified" else "review_required"
        result["stage"] = "complete"
    except (OSError, ValueError, KeyError) as exc:
        result["status"] = "failed"
        log = result["commands"].get(result["stage"], {}).get("log")
        result["diagnostic"] = diagnose(result["stage"], Path(log) if log else None, str(exc))
    try:
        for name, expected in result.get("generated_inputs", {}).items():
            if digest(workspace.output(Path(name))) != expected:
                raise BatchError(f"Generated input changed during build: {name}")
        if result.get("generated_inputs"):
            added = {str(p) for p in files(generated)} - set(result["generated_inputs"]) - {str(generated / "compile_commands.json")}
            if added:
                raise BatchError(f"Unexpected generated inputs during build: {sorted(added)}")
    except (OSError, ValueError) as exc:
        result.update(status="failed", stage="integrity", diagnostic=diagnose("integrity", None, str(exc)))
    result["seconds"] = round(time.monotonic() - started, 3)
    workspace.json(job / "result.json", result)
    workspace.json(output / "results" / (target["id"] + ".json"), result)
    return result


def summarize(workspace: Workspace, output: Path, plan: dict, results: list[dict], cached=0) -> dict:
    results = sorted(results, key=lambda r: r["id"])
    groups = {}
    for result in results:
        if result["status"] != "failed":
            continue
        diagnostic = result["diagnostic"]
        key = diagnostic["category"] + ":" + diagnostic["signature"]
        group = groups.setdefault(key, {**diagnostic, "targets": [], "representative_log": next(
            (c["log"] for c in reversed(list(result["commands"].values()))), None)})
        group["targets"].append(result["id"])
    projects = {t["project"] for t in plan["targets"]}
    passed_ids = {r["id"] for r in results if r["status"] == "success"}
    fully_passed = {p for p in projects if all(t["id"] in passed_ids for t in plan["targets"] if t["project"] == p)}
    summary = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "targets": len(plan["targets"]), "completed": len(results), "cached": cached,
        "counts": dict(Counter(r["status"] for r in results)), "sdk_errors": plan["sdk_errors"],
        "projects": len(projects), "passed_projects": len(fully_passed),
        "partially_passed_projects": len({r["project"] for r in results if r["status"] == "success"} - fully_passed),
        "verified_rate": len(passed_ids) / len(plan["targets"]) if plan["targets"] else 0,
        "build_succeeded_targets": sum(r.get("commands", {}).get("build", {}).get("success", False) for r in results),
        "hardware_tested": False,
        "results": results, "failure_groups": sorted(groups.values(), key=lambda g: -len(g["targets"]))}
    workspace.json(output / "summary.json", summary)
    rows = ["# SDK Batch Validation", "", f"Targets: {summary['completed']}/{summary['targets']}; cached: {cached}",
            f"Status counts: {summary['counts']}", f"SDK errors: {len(summary['sdk_errors'])}", "",
            "| SDK | Project | Target | Status | Stage |", "| --- | --- | --- | --- | --- |"]
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    rows.extend("| " + " | ".join(cell(r[k]) for k in ("sdk", "relative_project", "target", "status", "stage")) + " |" for r in results)
    rows.extend(["", "## Failure Groups", ""])
    for group in summary["failure_groups"]:
        rows.extend([f"### {group['category']} ({len(group['targets'])})", "", group["message"], "",
                     f"Log: {group['representative_log']}", ""])
    if plan["sdk_errors"]:
        rows.extend(["## SDK Errors", "", *[f"{e['sdk']}: {e['message']}" for e in plan["sdk_errors"]], ""])
    workspace.output(output / "summary.md").write_text("\n".join(rows), encoding="utf-8")
    return summary


def tool_facts(workspace: Workspace, output: Path, options: dict) -> dict:
    names = {"cmake": "cmake", "gcc": "arm-none-eabi-gcc", "size": "arm-none-eabi-size",
             "make": options.get("make_program") or ("ninja" if options["generator"] == "Ninja" else "make")}
    facts = {}
    for name, executable in names.items():
        path = shutil.which(executable)
        if not path:
            raise BatchError(f"Tool missing: {executable}")
        response = subprocess.run([path, "--version"], cwd=workspace.root, env=workspace.environment(output),
                                  capture_output=True, text=True, errors="replace", timeout=20, check=True)
        facts[name] = {"path": str(canonical(Path(path))), "version": response.stdout.splitlines()[0], "sha256": digest(Path(path))}
    gcc_bin = Path(facts["gcc"]["path"]).parent
    for name in ("g++", "as", "ld", "ar", "objcopy", "objdump", "ranlib"):
        path = gcc_bin / ("arm-none-eabi-" + name + (".exe" if os.name == "nt" else ""))
        if not path.is_file():
            raise BatchError(f"Tool missing: {path}")
        facts[name] = {"path": str(path), "sha256": digest(path)}
    # 编译器内部程序、头文件和运行库同样决定固件内容。
    facts["runtime"] = {str(p): identity(snapshot(p)) for p in (
        gcc_bin.parent / "lib" / "gcc", gcc_bin.parent / "libexec" / "gcc",
        gcc_bin.parent / "arm-none-eabi" / "include", gcc_bin.parent / "arm-none-eabi" / "lib"
    ) if p.is_dir()}
    return facts


def run_batch(args, root: Path) -> int:
    workspace = Workspace(root)
    output, cache = workspace.output(root / args.out), workspace.output(root / args.cache)
    if output.is_relative_to(cache) or cache.is_relative_to(output):
        raise BatchError("Output and cache directories must be separate")
    if args.command == "report":
        if not (output / "plan.json").is_file():
            raise BatchError("No saved plan; run discover or run first")
        workspace.claim(output, "results")
        with exclusive_lock(workspace, output / ".batch.lock"):
            plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
            results = []
            for target in plan["targets"]:
                path = workspace.output(output / "results" / (target["id"] + ".json"))
                if not path.is_file():
                    continue
                result = json.loads(path.read_text(encoding="utf-8"))
                expected = plan.get("fingerprints", {}).get(target["id"])
                if expected and result.get("fingerprint") == expected:
                    results.append(result)
            summary = summarize(workspace, output, plan, results, plan.get("cached", 0))
            print(json.dumps({k: summary[k] for k in ("targets", "completed", "counts")}), flush=True)
            return 0 if results and len(results) == len(plan["targets"]) and not plan["sdk_errors"] and all(r["status"] == "success" for r in results) else 2
    overrides = {}
    for item in args.local_sdk:
        identifier, separator, path = item.partition("=")
        if not separator or not path or identifier in overrides:
            raise BatchError("--local-sdk requires unique SDK_ID=DIRECTORY entries")
        overrides[identifier] = path
    entries = load_catalog(canonical(root / args.catalog), args.sdk, overrides)
    for entry in entries:
        source = entry["source"]
        if source["type"] == "local" and source.get("root"):
            sdk = Path(source["root"])
            for directory in (output, cache):
                if sdk.is_relative_to(directory) or directory.is_relative_to(sdk):
                    raise BatchError(f"SDK inputs and output/cache must not overlap: {sdk}")
    workspace.claim(output, "results")
    workspace.claim(cache, "cache")
    with exclusive_lock(workspace, output / ".batch.lock"):
        plan = {"schema_version": 1, "targets": [], "sdks": {}, "sdk_errors": []}
        snapshots = Snapshots()
        with exclusive_lock(workspace, cache / ".cache.lock"):
            for entry in entries:
                try:
                    sdk = prepare_sdk(workspace, cache, entry, args.seven_zip)
                    content = snapshots.get(sdk)
                    info = {"root": str(sdk), "version": entry["version"], "files": len(content), "fingerprint": identity(content)}
                    workspace.json(output / "inputs" / (entry["id"] + ".json"), content)
                    targets, errors = discover(entry, sdk)
                    if not targets:
                        raise BatchError("No Keil targets matched the SDK filters")
                    if args.project:
                        targets = [t for t in targets if matches(t["relative_project"], args.project)]
                    plan["sdks"][entry["id"]] = info
                    plan["targets"].extend(targets)
                    plan["sdk_errors"].extend(errors)
                    print(json.dumps({"sdk": entry["id"], "targets": len(targets), "files": len(content)}), flush=True)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    plan["sdk_errors"].append({"sdk": entry["id"], "stage": "prepare", "message": str(exc)})
                    print(json.dumps(plan["sdk_errors"][-1]), flush=True)
        if args.limit:
            plan["targets"] = plan["targets"][:args.limit]
        workspace.json(output / "plan.json", plan)
        if args.command in {"fetch", "discover"}:
            return 2 if plan["sdk_errors"] or not plan["targets"] else 0
        if not plan["targets"]:
            plan["sdk_errors"].append({"sdk": "selection", "stage": "discover", "message": "No targets matched the selection"})
            workspace.json(output / "plan.json", plan)
            summarize(workspace, output, plan, [])
            return 2
        options = {"generator": args.generator, "configuration": args.configuration,
                   "build_jobs": args.build_jobs, "timeout": args.timeout, "make_program": args.make_program}
        try:
            tools = tool_facts(workspace, output, options)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            plan["sdk_errors"].append({"sdk": "toolchain", "stage": "prerequisite", "message": str(exc)})
            workspace.json(output / "plan.json", plan)
            summarize(workspace, output, plan, [])
            return 2
        core = {p.name: digest(p) for p in sorted(root.glob("*.py"))}
        core["tools/run_regression.py"] = digest(root / "tools" / "run_regression.py")
        environment = {k: os.environ.get(k, "") for k in ("ARM_GCC_TOOLCHAIN", "KEIL_MDK_ROOT", "UV4_ROOT", "KEIL_ROOT", "CMSIS_PACK_ROOT", "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH", "CFLAGS", "CXXFLAGS", "LDFLAGS")}
        fingerprint = identity({"core": core, "tools": tools, "options": options, "environment": environment,
                                "python": sys.version, "executable": sys.executable})
        workspace.json(output / "context.json", {"core": core, "tools": tools, "options": options, "fingerprint": fingerprint})
        commands, results, pending, cached = Commands(workspace), [], [], 0
        plan["fingerprints"] = {}
        for target in plan["targets"]:
            key = identity([fingerprint, target, plan["sdks"][target["sdk"]]["fingerprint"]])
            plan["fingerprints"][target["id"]] = key
            previous_path = workspace.output(output / "results" / (target["id"] + ".json"))
            previous = {}
            if previous_path.is_file():
                try:
                    previous = json.loads(previous_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            if not args.force and reusable(workspace, previous, key, snapshots) and not (args.retry_failed and previous["status"] != "success"):
                results.append(previous)
                cached += 1
            else:
                pending.append((target, key))
        plan["cached"] = cached
        workspace.json(output / "plan.json", plan)
        summarize(workspace, output, plan, results, cached)
        executor = ThreadPoolExecutor(max_workers=args.jobs)
        futures = []
        try:
            for target, key in pending:
                sdk = Path(plan["sdks"][target["sdk"]]["root"])
                futures.append(executor.submit(build_target, workspace, output, target, sdk, key, options, tools, snapshots, commands))
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                summarize(workspace, output, plan, results, cached)
                print(json.dumps({"completed": len(results), "total": len(plan["targets"]), "sdk": result["sdk"],
                                  "target": result["target"], "status": result["status"], "stage": result["stage"]}), flush=True)
        except BaseException:
            commands.stop()
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        changed = []
        for location, before in snapshots.values.items():
            try:
                if Snapshots.read(Path(location)) != before:
                    changed.append(location)
            except (OSError, ValueError):
                changed.append(location)
        current_core = {p.name: digest(p) for p in sorted(root.glob("*.py"))}
        current_core["tools/run_regression.py"] = digest(root / "tools" / "run_regression.py")
        if current_core != core:
            changed.append(str(root / "keil_to_cmake.py"))
        if changed:
            for result in results:
                result.update(status="failed", stage="integrity", diagnostic=diagnose("integrity", None, "Inputs changed during batch: " + ", ".join(changed)))
                workspace.json(output / "results" / (result["id"] + ".json"), result)
                workspace.json(workspace.output(Path(result["job"])) / "result.json", result)
        workspace.json(output / "integrity.json", {"changed": changed, "success": not changed, "inputs_checked": len(snapshots.values)})
        summary = summarize(workspace, output, plan, sorted(results, key=lambda r: r["id"]), cached)
        print(json.dumps({k: summary[k] for k in ("targets", "completed", "cached", "counts", "sdk_errors")}), flush=True)
        return 0 if not plan["sdk_errors"] and all(r["status"] == "success" for r in results) else 2
