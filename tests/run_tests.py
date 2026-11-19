#!/usr/bin/env python3
"""Run launchd-why against the fake plists in tests/fixtures.

The fixtures are copied to a temporary folder first, so nothing in the
repository and nothing in ~/Library/LaunchAgents is touched. launchctl is
never asked (--offline). HOME is pointed at an empty temporary folder so
your own shell profile does not change the results.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "launchd_why.py")
SECRET = "dummy-value-must-never-be-printed"

# label -> finding ids that must appear (any one of a tuple is enough)
EXPECT = {
    "com.example.broken_format": ["lint_fail"],
    "com.example.protected_log": ["protected_log"],
    "com.example.relative_path": ["rel_path"],
    "com.example.no_path_claude": [("cmd_missing", "cmd_missing_noshell"), "env_missing"],
    "com.example.tilde_redirect": ["tilde_arg", "shell_token"],
    "com.example.typo_key": ["unknown_key_typo", "no_trigger"],
    "com.example.bad_calendar": ["cal_bad"],
    "com.example.missing_script": ["script_missing"],
    "com.example.not_executable": ["prog_not_exec"],
    "com.example.crlf": ["script_crlf"],
    "com.example.missing_logdir": ["logdir_missing"],
    "com.example.path_tilde": ["path_tilde"],
    "com.example.script_bare_cmds": [("cmd_missing", "cmd_missing_noshell"), "sourced_missing"],
    "com.example.env_in_plist": ["env_found", "node_missing"],
    "com.example.empty_env": ["env_empty"],
    "com.example.args_string": ["args_not_array"],
    "com.example.workdir_missing": ["workdir_missing"],
    "com.example.good": [],
    "com.example.stale_output": ["expect_stale"],
}
# extra checks for counts
PROTECTED_LOG_COUNT = 2


def prepare(tmp):
    fx = os.path.join(tmp, "fixtures")
    shutil.copytree(os.path.join(HERE, "fixtures"), fx)
    work = os.path.join(tmp, "work")
    os.makedirs(os.path.join(work, "logs"))
    for root, _dirs, files in os.walk(fx):
        for name in files:
            p = os.path.join(root, name)
            with open(p, "rb") as f:
                data = f.read()
            data = data.replace(b"@FIXTURES@", fx.encode()).replace(b"@WORK@", work.encode())
            with open(p, "wb") as f:
                f.write(data)
    os.chmod(os.path.join(fx, "scripts", "not_exec.sh"), 0o644)
    os.chmod(os.path.join(fx, "scripts", "crlf.sh"), 0o755)
    os.chmod(os.path.join(fx, "scripts", "uses_claude.sh"), 0o755)
    os.chmod(os.path.join(fx, "bin", "claude"), 0o755)
    for p in os.listdir(fx):
        if p.endswith(".plist"):
            os.chmod(os.path.join(fx, p), 0o644)
    # an output file last updated 3 days ago, for the stale check
    stale = os.path.join(work, "report.txt")
    with open(stale, "w") as f:
        f.write("old\n")
    old = time.time() - 3 * 86400
    os.utime(stale, (old, old))
    return fx, work, stale


def run_tool(args, home):
    env = dict(os.environ, HOME=home, LANG="C")
    p = subprocess.run([sys.executable, TOOL] + args, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def main():
    ok = True
    results = []
    with tempfile.TemporaryDirectory(prefix="launchd-why-test-") as tmp:
        fx, work, stale = prepare(tmp)
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        for label, want in EXPECT.items():
            plist = os.path.join(fx, label + ".plist")
            extra = ["--expect", stale] if label == "com.example.stale_output" else []
            code, out, err = run_tool(["--offline", "--json", "--lang", "en", plist] + extra, home)
            if err.strip():
                results.append((label, False, "stderr: " + err.strip().splitlines()[-1]))
                ok = False
                continue
            data = json.loads(out)
            ids = [f["id"] for f in data["jobs"][0]["findings"]]
            levels = [f["level"] for f in data["jobs"][0]["findings"]]
            missing = []
            for w in want:
                alts = w if isinstance(w, tuple) else (w,)
                if not any(a in ids for a in alts):
                    missing.append("/".join(alts))
            good = not missing
            note = "found: " + ", ".join(sorted(set(ids))) if ids else "no findings"
            if label == "com.example.good" and any(l in ("FAIL", "WARN") for l in levels):
                good = False
                note = "expected clean, got " + ", ".join(ids)
            if label == "com.example.protected_log" and ids.count("protected_log") != PROTECTED_LOG_COUNT:
                good = False
                note = "expected both log paths flagged"
            if missing:
                note = "missing: " + ", ".join(missing) + " | " + note
            results.append((label, good, note))
            ok &= good

        # fresh output file: must be reported as updated, no failure
        fresh = os.path.join(work, "fresh.txt")
        with open(fresh, "w") as f:
            f.write("new\n")
        code, out, err = run_tool(["--offline", "--json", os.path.join(fx, "com.example.good.plist"),
                                   "--expect", fresh, "--max-age", "26"], home)
        ids = [f["id"] for f in json.loads(out)["jobs"][0]["findings"]] if out else []
        good = "expect_ok" in ids and code == 0
        results.append(("expect fresh file", good, ", ".join(ids) or err.strip()))
        ok &= good
        # old output file with --max-age
        code, out, err = run_tool(["--offline", "--json", os.path.join(fx, "com.example.good.plist"),
                                   "--expect", stale, "--max-age", "26"], home)
        ids = [f["id"] for f in json.loads(out)["jobs"][0]["findings"]] if out else []
        good = "expect_old" in ids and code == 1
        results.append(("expect --max-age", good, ", ".join(ids) or err.strip()))
        ok &= good

        # whole folder, both languages, text and json: no crash, secret never shown
        for lang in ("ja", "en"):
            for fmt in ([], ["--json"]):
                code, out, err = run_tool(["--offline", "--verbose", "--dir", fx, "--lang", lang] + fmt, home)
                leak = SECRET in out or SECRET in err
                crashed = "Traceback" in err
                good = not leak and not crashed and code == 1
                results.append(("folder %s %s" % (lang, "json" if fmt else "text"), good,
                                "secret leaked" if leak else ("crash" if crashed else "exit %d" % code)))
                ok &= good

    width = max(len(r[0]) for r in results)
    for label, good, note in results:
        print("%s  %-*s  %s" % ("PASS" if good else "FAIL", width, label, note))
    print("\n%s: %d/%d" % ("ALL PASSED" if ok else "SOME FAILED",
                           sum(1 for r in results if r[1]), len(results)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
