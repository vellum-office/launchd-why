# launchd-why

[日本語の説明はこちら](README.ja.md)

One command that tells you why a scheduled launchd job on your Mac did not run.

It is written for people who run Claude Code (`claude -p`) or other scripts unattended from `~/Library/LaunchAgents`, but it works for any user LaunchAgent. It reads your plist files, the scripts they start and the launchd state, then prints each problem with one line on what is happening and one line on how to fix it.

- A single Python file, standard library only. Nothing to install.
- Read-only. It never loads, unloads, starts or edits a job.
- Environment variable values are never printed or saved. You only see "set" or "not set".
- Log lines are never printed. Only the kind of error and how many times it appears.

## Install

Requires macOS and Python 3.8 or later (the `python3` from Apple's Command Line Tools is fine).

```sh
curl -O https://raw.githubusercontent.com/vellum-office/launchd-why/main/launchd_why.py
chmod +x launchd_why.py
mkdir -p ~/bin && mv launchd_why.py ~/bin/launchd-why
```

Or simply run it with `python3 launchd_why.py`.

## Usage

```sh
# check every job in ~/Library/LaunchAgents (clean jobs are hidden)
launchd-why

# only jobs whose plist or script mentions claude
launchd-why --only-claude

# one job, by label or by plist path
launchd-why com.example.nightly
launchd-why ~/Library/LaunchAgents/com.example.nightly.plist

# also check that the job's output file was updated after the last scheduled run
launchd-why com.example.nightly --expect ~/reports/nightly.md

# ... or that it is not older than 26 hours
launchd-why com.example.nightly --expect ~/reports/nightly.md --max-age 26

# a variable your script needs (value is never shown)
launchd-why com.example.nightly --require-env MY_SERVICE_TOKEN

# Japanese or English output (default follows LANG)
launchd-why --lang en
```

Other options: `--dir DIR` (check plists in another folder), `--offline` (files only, do not ask launchctl), `--env-file FILE` (an extra file that provides variables), `--grace MIN` (minutes allowed after the scheduled time, default 30), `--stuck-minutes N` (default 120), `--json`, `--verbose`.

Exit code: 1 if any problem (FAIL) was found, otherwise 0.

### Example output

```
== com.example.nightly
   /Users/you/Library/LaunchAgents/com.example.nightly.plist
   [FAIL] The log path (StandardOutPath) is in a protected folder (Downloads): /Users/you/Downloads/nightly.log. The job fails before your script starts and nothing is logged (often exit code 78).
          Fix: Put logs under ~/Library/Logs/ or another unprotected folder.
   [FAIL] claude is not found on launchd's PATH (launchd default PATH /usr/bin:/bin:/usr/sbin:/sbin). In your shell it is at /opt/homebrew/bin/claude. This gives exit code 127 (command not found).
          Fix: Call claude by absolute path in the script, or add PATH (including /opt/homebrew/bin) to EnvironmentVariables.
```

## What it checks

1. Plist format and program
   - `plutil -lint`, missing Label, ProgramArguments written as a string, unknown keys that look like typos (for example `StartCalenderInterval`), invalid StartCalendarInterval values, no trigger at all, `Disabled` set in the file, bad file ownership or permissions.
   - The program and the script it runs exist and are executable, relative paths, `~` or `$HOME` passed literally (launchd does not use a shell), shell syntax such as `>` or `|` in arguments, CRLF line endings and missing interpreters in the `#!` line.
2. Last exit status
   - Reads `launchctl print gui/<uid>/<label>`: not loaded, disabled, loaded from a different file, edited but not reloaded, last exit code with a short explanation (1, 2, 78, 126, 127, signals and the sysexits values), and scheduled jobs that have been running for a long time.
3. Protected folders
   - StandardOutPath or StandardErrorPath inside `~/Desktop`, `~/Documents`, `~/Downloads` or iCloud Drive. launchd cannot open the log there, so the job dies before your script starts and leaves nothing behind (exit 78 is typical). Missing or read-only log folders.
   - Scripts, working folders and sourced files in those folders are reported as a risk: they work only if the interpreter was granted access.
4. PATH
   - launchd starts jobs with `PATH=/usr/bin:/bin:/usr/sbin:/sbin`. The tool works out the PATH your job really gets (plist EnvironmentVariables, login shell flags, `export PATH=` and `brew shellenv` lines in the script) and checks that `claude`, `node`, `npx`, `python3`, `uv`, `jq`, `gh` and similar commands are found.
   - Warns when launchd would run a different `python3` than your terminal does.
   - Catches the npm-installed `claude` that needs `node`, when `node` is not on the job's PATH.
   - Catches `~` or `$HOME` inside the PATH value (launchd does not expand them).
5. Environment variables
   - For jobs that call `claude`, looks for `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` in the plist, the script, files the script sources, `--env-file` files and `launchctl getenv`. Reports present, empty or missing. Values are never printed.
   - Warns when a file holding such a value can be read by other users.
6. Timing (optional)
   - With `--expect`, compares the output file's modification time with the last time StartCalendarInterval or StartInterval should have fired. If the exit code was 0 but the file was not updated, it says so: exit code 0 does not prove the job did its work.
7. Log hints
   - Scans the last 200 lines of the job's logs for "command not found", "Operation not permitted", "Permission denied", "bad interpreter", authentication failures (expired token, not logged in), usage limits and Python import errors.

## Limits

- Sleep and power. If the Mac is asleep at the scheduled time, launchd runs a StartCalendarInterval job once after wake. If the Mac is shut down, the run is skipped. StartInterval does not catch up missed runs either. The tool cannot tell whether the Mac was awake at a given time; the `--expect` check only shows that the output was not updated.
- launchd does not record when a job last started. The tool uses the log and output file times as clues.
- It does not detect a Claude Code session waiting on a permission prompt. That would depend on internal, unpublished logs that change with updates.
- Only user LaunchAgents in the GUI session (`gui/<uid>`). LaunchDaemons in `/Library` and jobs of other users are not checked. Run it while logged in to the desktop; over SSH launchctl may not show the GUI domain.
- Script reading is a best effort. It understands sh, bash, zsh and Python files, follows `source` up to two levels and reads simple `export PATH=` lines. It does not run your script, evaluate conditions or follow functions.
- It cannot read the macOS privacy database, so for scripts in protected folders it reports a risk, not a certainty. If the job's last exit code is 0, the note is downgraded to INFO.
- The output contains file paths from your Mac. Check before pasting it somewhere public.
- Tested on macOS 26 with Python 3.9 and 3.14. The `launchctl print` format is not a stable interface and may change between macOS versions.

## Tests

```sh
python3 tests/run_tests.py
```

The tests copy the fake plists in `tests/fixtures` to a temporary folder and run the tool with `--offline`. They do not touch `~/Library/LaunchAgents` or launchd.

## License

MIT. See [LICENSE](LICENSE).
