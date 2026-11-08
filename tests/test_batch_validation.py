import io
import json
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from batch_validation import Commands, Snapshots, diagnose, discover, reusable, run_batch, summarize
from sdk_sources import BatchError, Workspace, digest, identity, snapshot
from tools.run_regression import parser


def minimal_sdk(root, targets=("First", "Second")):
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.c").write_text('''
void Reset_Handler(void) { for (;;) {} }
__attribute__((used, section(".isr_vector")))
const unsigned int vectors[] = {0x20002000, (unsigned int)Reset_Handler};
''', encoding="utf-8")
    nodes = []
    for name in targets:
        nodes.append(f'''<Target><TargetName>{name}</TargetName><TargetOption><TargetCommonOption>
        <Device>STM32F103C8</Device><Vendor>STMicroelectronics</Vendor>
        <Cpu>IRAM(0x20000000,0x2000) IROM(0x08000000,0x10000) CPUTYPE("Cortex-M3")</Cpu>
        <OutputName>demo</OutputName></TargetCommonOption><TargetArmAds><Cads><VariousControls>
        <IncludePath>.</IncludePath></VariousControls></Cads></TargetArmAds></TargetOption>
        <Groups><Group><GroupName>Source</GroupName><Files><File><FileName>main.c</FileName>
        <FileType>1</FileType><FilePath>main.c</FilePath></File></Files></Group></Groups></Target>''')
    project = root / "demo.uvprojx"
    project.write_text("<Project><Targets>" + "".join(nodes) + "</Targets></Project>", encoding="utf-8")
    return project


class BatchValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = Workspace(self.root)
        self.entry = {"id": "demo", "version": "1"}

    def test_discovery_all_targets_legacy_format_and_filters(self):
        sdk = self.root / "sdk"
        project = minimal_sdk(sdk)
        project.with_suffix(".uvproj").write_bytes(project.read_bytes())
        targets, errors = discover(self.entry, sdk)
        self.assertEqual((len(targets), errors), (4, []))
        self.assertEqual(len({t["id"] for t in targets}), 4)
        chosen, _ = discover({**self.entry, "exclude": ["*.uvproj"], "targets": ["Second"]}, sdk)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["target"], "Second")
        self.assertEqual(chosen[0]["regions"][0]["origin"], 0x08000000)

    def test_discovery_invalid_xml_and_unsafe_output(self):
        sdk = self.root / "sdk"
        project = minimal_sdk(sdk, targets=("Demo",))
        project.write_text(project.read_text().replace("<OutputName>demo", "<OutputName>../demo"), encoding="utf-8")
        (sdk / "broken.uvproj").write_text("<Project>", encoding="utf-8")
        targets, errors = discover(self.entry, sdk)
        self.assertEqual(len(targets), 0)
        self.assertEqual(len(errors), 2)

    def test_grouping_ignores_project_directories_and_keeps_symbols(self):
        log = self.root / "build.log"
        signatures = []
        for name in ("D:/one/main.c:1:2", "E:/two/main.c:24:5"):
            log.write_text(name + ": undefined reference to `missing_function'\n", encoding="utf-8")
            diagnostic = diagnose("build", log)
            signatures.append(diagnostic["signature"])
            self.assertEqual(diagnostic["category"], "undefined_symbol")
        self.assertEqual(signatures[0], signatures[1])
        log.write_text("E:/two/main.c:24:5: undefined reference to `other_function'\n", encoding="utf-8")
        self.assertNotEqual(diagnose("build", log)["signature"], signatures[0])
        log.write_text("main.c:2: fatal error: absent.h: No such file or directory\n", encoding="utf-8")
        self.assertEqual(diagnose("build", log)["category"], "missing_header")

    def evidence(self):
        generated = self.workspace.mkdir(self.root / "generated")
        source = generated / "main.c"
        source.write_text("int a;", encoding="utf-8")
        external = self.workspace.mkdir(self.root / "external")
        (external / "a.h").write_text("#define A 1", encoding="utf-8")
        log = self.root / "command.log"
        log.write_text("success", encoding="utf-8")
        artifacts = {}
        for suffix in (".elf", ".bin", ".hex"):
            file = self.root / ("demo" + suffix)
            file.write_bytes(b"fixture")
            artifacts[suffix] = {"file": str(file), "sha256": digest(file)}
        return {"fingerprint": "key", "status": "success", "dependencies_complete": True,
                "generated_directory": str(generated),
                "dependencies": {str(external): identity(snapshot(external))},
                "generated_inputs": {str(source): digest(source)}, "artifacts": artifacts,
                "firmware": {"status": "verified"},
                "commands": {name: {"log": str(log), "log_sha256": digest(log), "success": True}
                             for name in ("conversion", "configure", "build", "size")}}

    def test_resume_invalidates_on_changed_context_or_incomplete_results(self):
        result = self.evidence()
        self.assertTrue(reusable(self.workspace, result, "key", Snapshots()))
        for changed in ({"fingerprint": "different"}, {"status": "running"}, {"artifacts": {}},
                        {"commands": {}}, {"generated_inputs": {}}, {"dependencies_complete": False}):
            with self.subTest(changed=changed):
                self.assertFalse(reusable(self.workspace, {**result, **changed}, "key", Snapshots()))

    def test_resume_invalidates_on_changed_inputs_artifacts_and_logs(self):
        result = self.evidence()
        for file in [Path(next(iter(result["generated_inputs"]))), self.root / "external/a.h",
                     self.root / "demo.elf", self.root / "command.log"]:
            original = file.read_bytes()
            file.write_bytes(b"modified")
            with self.subTest(file=file):
                self.assertFalse(reusable(self.workspace, result, "key", Snapshots()))
            file.write_bytes(original)
        (self.root / "external/new.h").write_text("new", encoding="utf-8")
        self.assertFalse(reusable(self.workspace, result, "key", Snapshots()))

    def test_failed_results_need_complete_dependencies_and_diagnostic(self):
        result = {**self.evidence(), "status": "failed", "diagnostic": {"category": "missing_header"}}
        self.assertTrue(reusable(self.workspace, result, "key", Snapshots()))
        result["diagnostic"] = {"category": "timeout"}
        self.assertFalse(reusable(self.workspace, result, "key", Snapshots()))

    def test_resume_detects_added_generated_input_and_previously_missing_dependency(self):
        result = self.evidence()
        extra = self.root / "generated/new.h"
        extra.write_text("added", encoding="utf-8")
        self.assertFalse(reusable(self.workspace, result, "key", Snapshots()))
        extra.unlink()
        missing = self.root / "external/missing.h"
        result["dependencies"].update(Snapshots().hashes([missing]))
        self.assertTrue(reusable(self.workspace, result, "key", Snapshots()))
        missing.write_text("restored", encoding="utf-8")
        self.assertFalse(reusable(self.workspace, result, "key", Snapshots()))

    def test_summary_distinguishes_fully_passed_projects_and_groups_errors(self):
        project = "sdk/demo.uvprojx"
        targets = [{"id": str(n), "project": project, "target": str(n), "sdk": "demo", "relative_project": project} for n in range(3)]
        error = diagnose("build", None, "build failed")
        results = [{**t, "status": "failed" if n else "success", "stage": "build", "diagnostic": error, "commands": {}} for n, t in enumerate(targets)]
        summary = summarize(self.workspace, self.root, {"targets": targets, "sdk_errors": []}, results)
        self.assertEqual(summary["passed_projects"], 0)
        self.assertEqual(summary["partially_passed_projects"], 1)
        self.assertEqual(len(summary["failure_groups"]), 1)
        self.assertEqual(len(summary["failure_groups"][0]["targets"]), 2)
        self.assertAlmostEqual(summary["verified_rate"], 1 / 3)

    def test_commands_timeout_and_cancellation(self):
        commands = Commands(self.workspace)
        result = commands.run([sys.executable, "-B", "-c", "import time; time.sleep(30)"], self.root / "job", "test", 0.2)
        self.assertFalse(result["success"])
        self.assertEqual(diagnose("build", None, result["error"])["category"], "timeout")
        self.assertFalse(commands.children)
        commands.stop()
        cancelled = commands.run([sys.executable, "-B", "-c", "raise SystemExit(9)"], self.root / "cancelled", "test", 2)
        self.assertIn("interrupted", cancelled["error"])
        self.assertIsNone(cancelled["return_code"])

    def test_cancelled_running_command_is_marked_for_retry(self):
        commands = Commands(self.workspace)
        original_stop = commands.stop
        cancelled = threading.Event()

        def stop():
            cancelled.set()
            original_stop()

        timer = threading.Timer(0.5, stop)
        timer.start()
        try:
            result = commands.run([sys.executable, "-B", "-c", "import time; time.sleep(30)"], self.root / "running", "test", 10)
        finally:
            timer.cancel()
            timer.join()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(diagnose("build", None, result["error"])["category"], "interrupted")
        previous = {**self.evidence(), "status": "failed", "diagnostic": {"category": "interrupted"}}
        self.assertFalse(reusable(self.workspace, previous, "key", Snapshots()))

    def test_report_does_not_mix_old_fingerprints_or_require_a_catalog(self):
        output = self.root / "output"
        self.workspace.claim(output, "results")
        target = {"id": "a", "project": "demo.uvprojx"}
        self.workspace.json(output / "plan.json", {"targets": [target], "sdk_errors": [], "fingerprints": {"a": "new"}})
        self.workspace.json(output / "results/a.json", {**target, "fingerprint": "old", "status": "success"})
        args = parser().parse_args(["report", "--out", str(output), "--cache", str(self.root / "cache"), "--catalog", "absent.json"])
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(run_batch(args, self.root), 2)
        self.assertEqual(json.loads((output / "summary.json").read_text())["completed"], 0)

    def test_cli_rejects_zero_concurrency_and_conflicting_retry_options(self):
        for args in (["--jobs", "0"], ["--timeout", "-1"], ["--force", "--retry-failed"]):
            with self.subTest(args=args), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(args)
        self.assertEqual(parser().parse_args([]).command, "run")

    def test_output_input_overlap_is_rejected_before_writing(self):
        sdk = self.root / "sdk"
        minimal_sdk(sdk)
        catalog = self.root / "catalog.json"
        catalog.write_text(json.dumps({"schema_version": 1, "sdks": [
            {**self.entry, "source": {"type": "local", "root": str(sdk)}}]}), encoding="utf-8")
        args = parser().parse_args(["run", "--catalog", str(catalog), "--out", str(sdk / "build"), "--cache", str(self.root / "cache")])
        with self.assertRaisesRegex(BatchError, "overlap"):
            run_batch(args, self.root)
        self.assertFalse((sdk / "build").exists())


if __name__ == "__main__":
    unittest.main()
