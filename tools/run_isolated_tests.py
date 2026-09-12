"""Cross-platform release-gate runner: one pytest process per test module."""
from __future__ import annotations
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = sorted((ROOT / "tests").glob("test_*.py"))
failed: list[str] = []
passed_files = 0
TIMEOUT_SECONDS = int(os.getenv("BARON_TEST_TIMEOUT", "60"))


def terminate_tree(proc: subprocess.Popen) -> None:
    """Terminate a child process tree on Windows and a process group on POSIX."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # taskkill is the supported Windows equivalent of killpg for a
            # subprocess tree. /T also terminates pytest-created descendants.
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (FileNotFoundError, ProcessLookupError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


for path in TESTS:
    print(f"\n=== {path.name} ===", flush=True)
    # Use the tiny wrapper so pytest/background workers are fully contained in
    # this child process and cannot poison the release runner's interpreter.
    cmd = [sys.executable, str(ROOT / "tools" / "run_one_testfile.py"), str(path)]
    kwargs = dict(
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
    )
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    try:
        rc = proc.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT: {path.name} ({TIMEOUT_SECONDS}s)", flush=True)
        failed.append(path.name)
        terminate_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        continue
    if rc != 0:
        failed.append(path.name)
    else:
        passed_files += 1

print(f"\nISOLATED TEST FILES: {passed_files}/{len(TESTS)} passed")
if failed:
    print("FAILED/TIMED-OUT FILES:")
    for name in failed:
        print(f" - {name}")
    raise SystemExit(1)
print("ALL ISOLATED TEST FILES PASSED")
