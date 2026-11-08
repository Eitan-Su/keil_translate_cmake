"""Run reproducible SDK batches from this checkout."""

import sys

sys.dont_write_bytecode = True

import argparse
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from batch_validation import run_batch
from sdk_sources import BatchError


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("command", choices=("fetch", "discover", "run", "report"), nargs="?", default="run")
    command.add_argument("--catalog", default="tools/sdk_catalog.json")
    command.add_argument("--sdk", action="append", default=[], help="SDK id; repeat to select several")
    command.add_argument("--local-sdk", action="append", default=[], metavar="ID=DIRECTORY", help="use a local SDK instead of downloading")
    command.add_argument("--cache", default=".cache/sdk-cache")
    command.add_argument("--out", default=".cache/sdk-regression")
    command.add_argument("--seven-zip", help="7z, 7za or 7zr executable for .7z SDKs")
    command.add_argument("--project", action="append", default=[], help="SDK-relative project glob; repeat for alternatives")
    command.add_argument("--limit", type=positive, help="maximum number of selected targets")
    command.add_argument("--generator", choices=("Ninja", "MinGW Makefiles", "Unix Makefiles"), default="Ninja")
    command.add_argument("--make-program", help="Ninja or Make executable")
    command.add_argument("--configuration", choices=("Debug", "Release"), default="Debug")
    command.add_argument("--jobs", type=positive, default=2, help="simultaneous target builds")
    command.add_argument("--build-jobs", type=positive, default=2, help="compiler processes per target")
    command.add_argument("--timeout", type=positive, default=300, help="timeout in seconds for each target command")
    retry = command.add_mutually_exclusive_group()
    retry.add_argument("--retry-failed", action="store_true", help="retry failed or review-required targets")
    retry.add_argument("--force", action="store_true", help="build all selected targets in new attempt directories")
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    os.chdir(ROOT)
    try:
        return run_batch(args, ROOT)
    except KeyboardInterrupt:
        print("Batch interrupted. Completed results and attempt logs were retained.", file=sys.stderr)
        return 130
    except (BatchError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Batch error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
