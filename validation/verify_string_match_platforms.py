"""Fork-only native platform evidence; never included in the upstream change."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

ROOT = Path.cwd()
OUT = ROOT / "evidence"
OUT.mkdir(exist_ok=True)
BASE = "7a23d8e3fedb010070ef1a03873c55500c7aa1a9"
FIX = "ce1ffb3945b358dc0f66a3d0cde3e5b53acb912b"
TEST262 = "5ef1e5723be95296f36afb0386676fed0205869c"
WINDOWS = sys.platform == "win32"
SUFFIX = ".exe" if WINDOWS else ""
ENV = os.environ.copy()
ENV["ASAN_OPTIONS"] = "halt_on_error=1"
ENV["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
REPORT = {"platform": platform.platform(), "python": sys.version,
          "base": BASE, "fix": FIX, "test262": TEST262,
          "runs": [], "source_hashes": {}, "failures": []}


def save():
    (OUT / "report.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def check(condition, message):
    if not condition:
        REPORT["failures"].append(message)
    save()


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args])


def run(name, args, cwd, timeout=1200):
    args = list(map(str, args))
    start = time.monotonic()
    print(name, " ".join(args), flush=True)
    log = OUT / (name + ".log")
    with log.open("wb") as stream:
        try:
            process = subprocess.run(args, cwd=cwd, env=ENV,
                                     stdout=stream, stderr=subprocess.STDOUT,
                                     timeout=timeout, check=False)
            code = process.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    result = {"name": name, "command": args, "cwd": str(cwd),
              "returncode": code, "seconds": time.monotonic() - start,
              "log": log.name}
    output = log.read_text(errors="replace")
    summaries = re.findall(r"Result: (\d+)/(\d+) errors?([^\r\n]*)", output)
    if summaries:
        errors, count, rest = summaries[-1]
        result["summary"] = {"errors": int(errors), "count": int(count),
                             "rest": rest.strip()}
    REPORT["runs"].append(result)
    save()
    print(json.dumps(result), flush=True)
    return result, output


def successful(result):
    check(result["returncode"] == 0, result["name"] + " did not exit zero")


def main():
    result, _ = run("tool-versions", ["cmake", "--version"], ROOT)
    successful(result)
    result, _ = run("ninja-version", ["ninja", "--version"], ROOT)
    successful(result)
    sources = {"base": ROOT / "baseline", "fix": ROOT / "fixed"}
    for label, source in sources.items():
        expected = BASE if label == "base" else FIX
        assert git(source, "rev-parse", "HEAD").decode().strip() == expected
        assert git(source / "test262", "rev-parse", "HEAD").decode().strip() == TEST262
        assert not git(source, "status", "--porcelain", "--untracked-files=no").strip()
        hashes = {}
        for raw in git(source, "ls-files", "-z").split(b"\0"):
            if not raw:
                continue
            name = raw.decode()
            path = source / name
            if path.is_file():
                hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        REPORT["source_hashes"][label] = hashes
    save()

    # Every group has the exact upstream test definitions and assertions.
    # Only the four final calls are replaced to isolate the three old failures.
    fixed = sources["fix"]
    regression = (fixed / "tests/test_string_match.js").read_text()
    groups = ["test_matchall_order", "test_regexp_create",
              "test_regexp_pattern_conversion", "test_regexp_create_exceptions"]
    calls = "\n".join(name + "();" for name in groups) + "\n"
    assert regression.endswith(calls)
    fixture_dir = OUT / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "assert.js").write_bytes((fixed / "tests/assert.js").read_bytes())
    REPORT["group_fixture_hashes"] = {}
    for name in groups:
        path = fixture_dir / (name + ".mjs")
        path.write_text(regression[:-len(calls)] + name + "();\n")
        REPORT["group_fixture_hashes"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (fixture_dir / "test_string_match.mjs").write_bytes((fixed / "tests/test_string_match.js").read_bytes())
    save()

    local = {}
    for label, source in sources.items():
        build = source / "build"
        config = ["cmake", "-S", source, "-B", build, "-G", "Ninja",
                  "-DQJS_BUILD_WERROR=ON", "-DQJS_BUILD_EXAMPLES=ON",
                  "-DCMAKE_BUILD_TYPE=" + ("Debug" if WINDOWS else "RelWithDebInfo")]
        if not WINDOWS:
            config += ["-DCMAKE_C_COMPILER=clang", "-DQJS_ENABLE_ASAN=ON", "-DQJS_ENABLE_UBSAN=ON"]
        result, _ = run(label + "-configure", config, ROOT)
        successful(result)
        if result["returncode"] != 0:
            continue
        result, _ = run(label + "-build", ["cmake", "--build", build, "--parallel", "3"], ROOT)
        successful(result)
        if result["returncode"] != 0:
            continue
        qjs = build / ("qjs" + SUFFIX)
        runner = build / ("run-test262" + SUFFIX)
        result, _ = run(label + "-api", [build / ("api-test" + SUFFIX)], source)
        successful(result)
        result, _ = run(label + "-regexp-api", [build / ("lre-test" + SUFFIX)], source)
        successful(result)
        for name in groups:
            result, output = run(label + "-" + name, [qjs, "-m", name + ".mjs"], fixture_dir)
            expected_failure = label == "base" and name != "test_regexp_create_exceptions"
            if expected_failure:
                check(isinstance(result["returncode"], int) and result["returncode"] != 0,
                      result["name"] + " did not reproduce original failure")
                diagnostic = ("cannot read property '0' of null" if name == "test_regexp_pattern_conversion"
                              else "assertion failed")
                check(diagnostic in output, result["name"] + " did not show original regression diagnostic")
                check(name in output, result["name"] + " did not identify the regression group")
                check("AddressSanitizer" not in output and "runtime error:" not in output,
                      result["name"] + " had a sanitizer failure instead of a regression assertion")
            else:
                successful(result)
        result, _ = run(label + "-whole-regression", [qjs, "-m", "test_string_match.mjs"], fixture_dir)
        if label == "base":
            check(isinstance(result["returncode"], int) and result["returncode"] != 0,
                  "original engine unexpectedly passed complete regression")
        else:
            successful(result)

        result, output = run(label + "-local", [runner, "-c", "tests.conf", "-t", "1"], source)
        local[label] = result
        successful(result)
        summary = result.get("summary", {})
        check(summary.get("count") == (120 if label == "base" else 121),
              label + " local test count changed")
        for method in ("match", "matchAll", "search"):
            result, _ = run(label + "-test262-" + method,
                            [runner, "-c", "test262.conf", "-d",
                             "test262/test/built-ins/String/prototype/" + method, "-t", "2"], source)
            successful(result)
            check(result.get("summary", {}).get("errors") == 0,
                  result["name"] + " contains test failures")
            check(result.get("summary", {}).get("count", 0) > 0,
                  result["name"] + " did not execute tests")

        # macOS sanitizer conformance is a previously untested configuration.
        # Windows follows upstream's native local/API coverage plus the relevant
        # conformance directories, without inventing platform expectations.
        if not WINDOWS:
            result, _ = run(label + "-full-test262", [runner, "-c", "test262.conf", "-a", "-t", "2"], source, 1800)
            successful(result)
            check(result.get("summary", {}).get("count", 0) > 80000,
                  result["name"] + " did not execute the full conformance set")

        check(not git(source, "diff", "--name-only").strip(),
              label + " tracked sources or expectations changed during validation")

    if "base" in local and "fix" in local:
        old = local["base"].get("summary", {})
        new = local["fix"].get("summary", {})
        check(old.get("errors") == new.get("errors"), "local suite gained failures")
        check(old.get("rest") == new.get("rest"), "local exclusion/skip counts changed")
        if old.get("errors", 0):
            # Baseline failures are reported, never called a clean suite pass.
            REPORT["local_baseline_failures_require_manual_audit"] = True
    full = [r for r in REPORT["runs"] if r["name"].endswith("-full-test262")]
    if len(full) == 2:
        check(full[0].get("summary") == full[1].get("summary"), "full conformance summary changed")
    REPORT["finished"] = True
    save()
    return int(bool(REPORT["failures"]))


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        save()
