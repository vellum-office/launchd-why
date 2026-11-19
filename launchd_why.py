#!/usr/bin/env python3
"""launchd-why: find out why a scheduled launchd job on macOS does not run.

Made for people who run Claude Code (claude -p) or other scripts unattended
from ~/Library/LaunchAgents. One command, read-only, standard library only.

The tool never changes anything. It only runs:
  plutil -lint, launchctl list, launchctl print, launchctl print-disabled,
  launchctl getenv, ps -o etime=
Environment variable values are never printed or stored; only "set / not set".
"""

import argparse
import datetime as dt
import difflib
import glob
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys

VERSION = "0.1.0"
HOME = os.path.expanduser("~")
UID = os.getuid()
DEFAULT_LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
AGENTS_DIR = os.path.join(HOME, "Library", "LaunchAgents")
MAX_READ = 1024 * 1024  # never read more than 1 MB of any file

# Folders protected by macOS privacy controls (TCC). A launchd job usually
# cannot open files there unless the program has been granted access.
PROTECTED_NAMES = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")
PROTECTED_RE = re.compile(
    r"^(?:~|/Users/[^/]+)/(Desktop|Documents|Downloads|Library/Mobile Documents)(?:/|$)")

# Commands that are commonly installed outside launchd's short default PATH.
WATCH_CMDS = ("claude", "node", "npx", "npm", "python3", "python", "uv", "uvx",
              "bun", "deno", "jq", "gh", "brew", "codex", "pnpm", "yarn")
AUTH_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
PY_RE = re.compile(r"^python(\d+(\.\d+)?)?$")

KNOWN_KEYS = {
    "Label", "Disabled", "UserName", "GroupName", "inetdCompatibility",
    "LimitLoadToHosts", "LimitLoadFromHosts", "LimitLoadToSessionType",
    "LimitLoadToHardware", "LimitLoadFromHardware", "Program", "ProgramArguments",
    "EnableGlobbing", "EnableTransactions", "EnablePressuredExit", "OnDemand",
    "ServiceIPC", "KeepAlive", "RunAtLoad", "RootDirectory", "WorkingDirectory",
    "EnvironmentVariables", "Umask", "TimeOut", "ExitTimeOut", "ThrottleInterval",
    "InitGroups", "WatchPaths", "QueueDirectories", "StartOnMount", "StartInterval",
    "StartCalendarInterval", "StandardInPath", "StandardOutPath",
    "StandardErrorPath", "Debug", "WaitForDebugger", "SoftResourceLimits",
    "HardResourceLimits", "Nice", "ProcessType", "AbandonProcessGroup",
    "LowPriorityIO", "LowPriorityBackgroundIO", "MaterializeDatalessFiles",
    "LaunchOnlyOnce", "MachServices", "Sockets", "LaunchEvents", "SessionCreate",
    "LegacyTimers", "AssociatedBundleIdentifiers", "BundleProgram",
    "ServiceDescription", "POSIXSpawnType", "HopefullyExitsFirst",
    "HopefullyExitsLast", "KeepAliveOnIdleExit", "SuccessfulExit",
}
CAL_RANGES = {"Minute": (0, 59), "Hour": (0, 23), "Day": (1, 31),
              "Weekday": (0, 7), "Month": (1, 12)}
TRIGGER_KEYS = ("StartInterval", "StartCalendarInterval", "KeepAlive", "WatchPaths",
                "QueueDirectories", "StartOnMount", "Sockets", "MachServices",
                "LaunchEvents")

# Patterns searched in the tail of log files. Only the category is reported,
# never the line itself (logs can contain secrets).
LOG_PATTERNS = [
    ("log_notfound", re.compile(r"command not found|No such file or directory", re.I)),
    ("log_tcc", re.compile(r"Operation not permitted", re.I)),
    ("log_perm", re.compile(r"Permission denied", re.I)),
    ("log_interp", re.compile(r"bad interpreter", re.I)),
    ("log_auth", re.compile(
        r"Invalid API key|authentication_error|OAuth token has expired|"
        r"Please run /login|not logged in|token (?:has )?expired|401 Unauthorized", re.I)),
    ("log_limit", re.compile(
        r"credit balance is too low|usage limit|rate[ _]limit|limit reached|"
        r"overloaded_error", re.I)),
    ("log_pymod", re.compile(r"ModuleNotFoundError|ImportError", re.I)),
    ("log_trace", re.compile(r"Traceback \(most recent call last\)")),
]

# --------------------------------------------------------------------------
# Messages. Each key has (Japanese, English). "<key>.fix" is the fix line.
# --------------------------------------------------------------------------
M = {
    "lint_fail": ("plist の書式が壊れています（plutil -lint で失敗）。launchd は読み込めません。",
                  "The plist is malformed (plutil -lint failed). launchd cannot load it."),
    "lint_fail.fix": ("plutil -lint で場所を確かめて直し、読み込み直してください。",
                      "Run plutil -lint to see where it breaks, fix it, then reload the job."),
    "parse_fail": ("plist を読み込めませんでした: {err}",
                   "Could not parse the plist: {err}"),
    "parse_fail.fix": ("XML の閉じ忘れや & の書き方を確かめてください。",
                       "Check for unclosed tags or a bare & in the XML."),
    "not_dict": ("plist の一番外側が辞書（dict）ではありません。", "The plist top level is not a dictionary."),
    "not_dict.fix": ("<plist><dict>...</dict></plist> の形にしてください。",
                     "Use the <plist><dict>...</dict></plist> layout."),
    "no_label": ("Label がありません。", "Label is missing."),
    "no_label.fix": ("<key>Label</key> にファイル名と同じ名前を書いてください。",
                     "Add <key>Label</key> with the same name as the file."),
    "label_mismatch": ("Label（{label}）とファイル名が違います。", "Label ({label}) does not match the file name."),
    "label_mismatch.fix": ("動きはしますが、取り違えのもとです。同じにそろえると調べやすくなります。",
                           "It still works, but matching them avoids confusion."),
    "bad_perm": ("plist のファイルの持ち主か書き込み権限がおかしく、launchd が読み込みを断ります（{why}）。",
                 "The plist owner or write permission is wrong, so launchd refuses to load it ({why})."),
    "bad_perm.fix": ("chmod 644 と、自分の持ち物にする（chown）で直してください。",
                     "Fix with chmod 644 and make sure you own the file (chown)."),
    "unknown_key_typo": ("知らないキー {key} があります。{sugg} の書き間違いでは？ 間違いなら、その設定は効いていません。",
                         "Unknown key {key}. Did you mean {sugg}? If so, that setting is ignored."),
    "unknown_key_typo.fix": ("キーの名前を {sugg} に直してください。", "Rename the key to {sugg}."),
    "unknown_key": ("知らないキー {key} があります（launchd は無視します）。", "Unknown key {key} (launchd ignores it)."),
    "unknown_key.fix": ("意図した設定か、man launchd.plist で確かめてください。",
                        "Check man launchd.plist to confirm it is intended."),
    "disabled_key": ("plist の中で Disabled が true です。", "Disabled is set to true inside the plist."),
    "disabled_key.fix": ("動かしたいなら Disabled を消すか false にして、読み込み直してください。",
                         "Remove Disabled or set it to false, then reload."),
    "no_program": ("Program も ProgramArguments もありません。何も実行されません。",
                   "Neither Program nor ProgramArguments is set. Nothing will run."),
    "no_program.fix": ("ProgramArguments に、実行ファイルの絶対パスと引数を配列で書いてください。",
                       "Set ProgramArguments to an array: absolute path of the program, then its arguments."),
    "args_not_array": ("ProgramArguments が配列ではありません（文字列で書かれています）。",
                       "ProgramArguments is not an array (it is written as a string)."),
    "args_not_array.fix": ("<array><string>/絶対パス</string><string>引数</string></array> のように1語ずつ分けてください。",
                           "Split it into <array><string>/absolute/path</string><string>arg</string></array>."),
    "no_trigger": ("自分から動き出す設定（StartCalendarInterval・StartInterval・RunAtLoad など）がありません。",
                   "No trigger is set (StartCalendarInterval, StartInterval, RunAtLoad, ...). The job never starts by itself."),
    "no_trigger.fix": ("時刻で動かすなら StartCalendarInterval、間隔なら StartInterval を足してください。",
                       "Add StartCalendarInterval for a time of day, or StartInterval for a fixed interval."),
    "cal_bad": ("StartCalendarInterval の値がおかしいです: {detail}",
                "StartCalendarInterval has an invalid value: {detail}"),
    "cal_bad.fix": ("使えるキーは Minute 0-59・Hour 0-23・Day 1-31・Weekday 0-7・Month 1-12 の整数だけです。",
                    "Allowed keys are integers: Minute 0-59, Hour 0-23, Day 1-31, Weekday 0-7, Month 1-12."),
    "prog_missing": ("実行ファイルが見つかりません: {path}", "Program not found: {path}"),
    "prog_missing.fix": ("パスの打ち間違い・移動・アンインストールを確かめ、今ある場所の絶対パスに直してください。",
                         "Check for typos or a moved or removed program, and use its current absolute path."),
    "prog_not_exec": ("実行ファイルに実行の権限がありません: {path}（終了コード 126 になります）",
                      "Program is not executable: {path} (exit code 126)."),
    "prog_not_exec.fix": ("chmod +x で権限を付けるか、ProgramArguments の先頭を /bin/zsh や python の絶対パスにしてください。",
                          "Run chmod +x, or put /bin/zsh or the interpreter's absolute path first in ProgramArguments."),
    "prog_bare": ("実行ファイルが名前だけで書かれています（{cmd}）。launchd の PATH で探すと {where}。",
                  "The program is given by name only ({cmd}). Looked up in launchd's PATH: {where}."),
    "prog_bare.fix": ("名前だけでなく絶対パスで書いてください（which {cmd} で場所を確かめられます）。",
                      "Use the absolute path instead (find it with: which {cmd})."),
    "rel_path": ("相対パスが使われています: {path}。launchd は / から始めるので、たいてい見つかりません。",
                 "Relative path used: {path}. launchd starts in /, so it is usually not found."),
    "rel_path.fix": ("絶対パスにするか、WorkingDirectory を設定してください。",
                     "Use an absolute path, or set WorkingDirectory."),
    "tilde_arg": ("~ や $ を含む値があります: {val}。launchd はシェルを通さないので、そのままの文字として渡ります。",
                  "A value contains ~ or $: {val}. launchd does not use a shell, so it is passed literally."),
    "tilde_arg.fix": ("/Users/あなたの名前/... のような絶対パスに書き換えてください。",
                      "Write the full absolute path instead (/Users/you/...)."),
    "shell_token": ("シェルの記号 {tok} が引数に入っていますが、シェルを通していません。リダイレクトやパイプは効きません。",
                    "Shell syntax {tok} is in the arguments, but no shell is used. Redirects and pipes do not work."),
    "shell_token.fix": ("/bin/zsh -c \"...\" の形にするか、ログは StandardOutPath で受けてください。",
                        "Wrap it in /bin/zsh -c \"...\", or use StandardOutPath for output."),
    "script_missing": ("スクリプトが見つかりません: {path}", "Script not found: {path}"),
    "script_missing.fix": ("パスを確かめ、今ある場所の絶対パスに直してください。",
                           "Check the path and use the script's current absolute path."),
    "script_crlf": ("スクリプトの1行目が Windows の改行（CRLF）です。\"bad interpreter\" で動きません: {path}",
                    "The script's first line ends with CRLF. It fails with \"bad interpreter\": {path}"),
    "script_crlf.fix": ("改行を LF に直してください（例: sed -i '' $'s/\\r$//' ファイル）。",
                        "Convert line endings to LF (for example: sed -i '' $'s/\\r$//' file)."),
    "shebang_missing": ("1行目の #! で指定した {interp} が見つかりません: {path}",
                        "The interpreter {interp} named in the #! line was not found: {path}"),
    "shebang_missing.fix": ("#! の行を、今ある実行ファイルの絶対パスに直してください。",
                            "Point the #! line at an interpreter that exists (absolute path)."),
    "shebang_env": ("1行目が #!/usr/bin/env {cmd} ですが、launchd の PATH では {cmd} が見つかりません: {path}",
                    "The first line is #!/usr/bin/env {cmd}, but {cmd} is not on launchd's PATH: {path}"),
    "shebang_env.fix": ("#! に {cmd} の絶対パスを書くか、plist の EnvironmentVariables に PATH を足してください。",
                        "Use the absolute path of {cmd} in the #! line, or add PATH to EnvironmentVariables."),
    "protected_access": ("{what} が保護されたフォルダ（{folder}）の中にあります: {path}。動かす側（python や zsh）にアクセスの許可がないと \"Operation not permitted\" で落ちます。",
                         "{what} is inside a protected folder ({folder}): {path}. Unless the interpreter (python, zsh) has been granted access, it fails with \"Operation not permitted\"."),
    "protected_access.fix": ("いま動いていれば許可済みです。新しい Mac や再インストールのあとに止まりやすいので、保護されていない場所へ移すと確実です。",
                             "If it runs today, access was granted. It tends to break after a new Mac or reinstall, so moving it out is safer."),
    "empty_plist": ("plist の中身が空です。launchd は何もしません。", "The plist is empty. launchd does nothing with it."),
    "empty_plist.fix": ("使っていないなら、そのアプリの説明に従って片付けてください。", "If unused, remove it as the app's documentation describes."),
    "loaded_other": ("この名前のジョブは、別のファイルから読み込まれています: {other}。このファイルを直しても効きません。",
                     "launchd loaded this label from another file: {other}. Editing this file has no effect."),
    "loaded_other.fix": ("どちらを使うか決め、もう一方を片付けてから読み込み直してください。",
                         "Decide which file to keep, remove the other, then reload."),
    "protected_log": ("ログの出し先（{key}）が保護されたフォルダ（{folder}）の中です: {path}。ジョブはスクリプトを始める前に落ち、ログにも何も残りません（終了コード 78 のことが多い）。",
                      "The log path ({key}) is in a protected folder ({folder}): {path}. The job fails before your script starts and nothing is logged (often exit code 78)."),
    "protected_log.fix": ("ログは ~/Library/Logs/ の下などに置いてください。",
                          "Put logs under ~/Library/Logs/ or another unprotected folder."),
    "logdir_missing": ("ログのフォルダがありません（{key}）: {dir}。launchd はフォルダを作らないので、ジョブが始まらないことがあります。",
                       "The log folder does not exist ({key}): {dir}. launchd does not create folders, so the job may fail to start."),
    "logdir_missing.fix": ("mkdir -p でフォルダを作ってください。", "Create it with mkdir -p."),
    "logdir_readonly": ("ログのフォルダに書き込めません（{key}）: {dir}",
                        "The log folder is not writable ({key}): {dir}"),
    "logdir_readonly.fix": ("フォルダの権限を確かめてください。", "Check the folder permissions."),
    "no_log": ("StandardErrorPath（エラーの出し先）がありません。落ちても理由が残りません。",
               "StandardErrorPath is not set. If the job fails, the reason is lost."),
    "no_log.fix": ("StandardOutPath と StandardErrorPath を ~/Library/Logs/ の下に設定すると、次から原因が見えます。",
                   "Set StandardOutPath and StandardErrorPath under ~/Library/Logs/ to see errors next time."),
    "workdir_missing": ("WorkingDirectory がありません: {path}（終了コード 78 になりやすい）",
                        "WorkingDirectory does not exist: {path} (often exit code 78)."),
    "workdir_missing.fix": ("フォルダを作るか、パスを直してください。", "Create the folder or fix the path."),
    "path_tilde": ("EnvironmentVariables の PATH に ~ や $ が入っています。launchd は展開しないので、その場所は探されません。",
                   "The PATH in EnvironmentVariables contains ~ or $. launchd does not expand it, so those entries are not searched."),
    "path_tilde.fix": ("/Users/あなたの名前/.local/bin のように絶対パスで書いてください。",
                       "Write each entry as an absolute path, for example /Users/you/.local/bin."),
    "cmd_missing": ("{cmd} が launchd の PATH では見つかりません（{src}）。ターミナルでは {shell} にあります。終了コード 127（command not found）になります。",
                    "{cmd} is not found on launchd's PATH ({src}). In your shell it is at {shell}. This gives exit code 127 (command not found)."),
    "cmd_missing_noshell": ("{cmd} が launchd の PATH では見つかりません（{src}）。",
                            "{cmd} is not found on launchd's PATH ({src})."),
    "cmd_missing.fix": ("スクリプトの中で {cmd} を絶対パスで呼ぶか、plist の EnvironmentVariables に PATH（{dir} を含む）を足してください。",
                        "Call {cmd} by absolute path in the script, or add PATH (including {dir}) to EnvironmentVariables."),
    "cmd_missing_noshell.fix": ("{cmd} の場所を確かめ、絶対パスで呼んでください。",
                                "Find where {cmd} is installed and call it by absolute path."),
    "cmd_differs": ("{cmd} が、launchd では {ld}、ターミナルでは {shell} を指します。別物なら、入れた部品や版が違って落ちます（{src}）。",
                    "{cmd} resolves to {ld} under launchd but {shell} in your shell. If they differ, packages or versions differ ({src})."),
    "cmd_differs.fix": ("意図した方の絶対パスで呼んでください。", "Call the one you intend by absolute path."),
    "abs_missing": ("スクリプトの中の {path} がありません（{src}）。", "{path} referenced in the script does not exist ({src})."),
    "abs_missing.fix": ("今ある場所に直してください（which で確かめられます）。", "Fix the path (check with which)."),
    "node_missing": ("{cmd}（{path}）は node で動く作りですが、launchd の PATH に node がありません。",
                     "{cmd} ({path}) runs on node, but node is not on launchd's PATH."),
    "node_missing.fix": ("PATH に node のフォルダを足すか、node の絶対パスで {cmd} を起動してください。",
                         "Add node's folder to PATH, or start {cmd} with node's absolute path."),
    "sourced_missing": ("スクリプトが読み込むファイルがありません: {path}", "A file the script sources does not exist: {path}"),
    "sourced_missing.fix": ("パスを確かめてください。~ や $HOME は絶対パスにすると確実です。",
                            "Check the path. Absolute paths are safer than ~ or $HOME."),
    "env_missing": ("{names} がどこにも見つかりません（plist・スクリプト・読み込むファイル・launchctl setenv）。",
                    "{names} was not found anywhere (plist, script, sourced files, launchctl setenv)."),
    "env_missing.fix": ("claude -p を無人で動かすなら、claude setup-token で作ったトークンを、権限 600 のファイルから読み込む形が確実です。キーチェーンのログインだけでも動く場合はあります。",
                        "For unattended claude -p, load a token from claude setup-token from a file with mode 600. A Keychain login alone may also work."),
    "env_missing_req": ("指定した環境変数 {name} がどこにも見つかりません。", "Required variable {name} was not found anywhere."),
    "env_missing_req.fix": ("plist の EnvironmentVariables か、スクリプトが読み込むファイルで渡してください。",
                            "Provide it via EnvironmentVariables or a file the script sources."),
    "env_empty": ("環境変数 {name} は書かれていますが、中身が空です（{src}）。", "Variable {name} is present but empty ({src})."),
    "env_empty.fix": ("値を入れ直してください。", "Set a value."),
    "env_found": ("環境変数 {name}: あり（{src}）", "Variable {name}: set ({src})"),
    "secret_file_perm": ("鍵の入ったファイルを他の人も読めます（権限 {mode}）: {path}",
                         "A file holding a secret is readable by others (mode {mode}): {path}"),
    "secret_file_perm.fix": ("chmod 600 にしてください。", "Run chmod 600."),
    "not_loaded": ("ファイルはありますが、launchd に読み込まれていません。",
                   "The file exists but the job is not loaded in launchd."),
    "not_loaded.fix": ("launchctl bootstrap gui/$(id -u) {path} で読み込んでください。",
                       "Load it with: launchctl bootstrap gui/$(id -u) {path}"),
    "disabled": ("launchd 側で無効（disabled）になっています。読み込んでも動きません。",
                 "The job is disabled in launchd. It will not run even if loaded."),
    "disabled.fix": ("launchctl enable gui/$(id -u)/{label} で有効にしてください。",
                     "Enable it with: launchctl enable gui/$(id -u)/{label}"),
    "loaded_differs": ("読み込まれている中身が、ファイルと違います（{what}）。ファイルを直したあと、読み込み直していません。",
                       "The loaded job differs from the file ({what}). The file was edited but not reloaded."),
    "loaded_differs.fix": ("launchctl bootout gui/$(id -u)/{label} のあと bootstrap で読み込み直してください。",
                           "Reload: launchctl bootout gui/$(id -u)/{label}, then bootstrap it again."),
    "exit_bad": ("最後の終了コードは {code} です: {meaning}", "Last exit code is {code}: {meaning}"),
    "exit_bad.fix": ("{fix}", "{fix}"),
    "exit_ok": ("最後の終了コードは 0（正常）。実行回数 {runs}。", "Last exit code is 0 (success). Runs: {runs}."),
    "never_ran": ("読み込んでから、まだ1回も終わっていません（実行回数 {runs}）。", "The job has not finished since it was loaded (runs: {runs})."),
    "never_ran.fix": ("予定の時刻を過ぎていても 0 なら、時刻の書き方と、その時刻に Mac が電源オフでなかったかを確かめてください。",
                      "If the scheduled time has passed, check the schedule keys and whether the Mac was powered off."),
    "running_long": ("いま実行中で、{mins} 分たっています。止まって待っている可能性があります。",
                     "It is running now and has been for {mins} minutes. It may be stuck."),
    "running_long.fix": ("ログの最後を見て、何を待っているか確かめてください。", "Check the end of the log to see what it is waiting for."),
    "launchctl_fail": ("launchctl で状態を読めませんでした（GUI のログイン中でない可能性）。",
                       "Could not read the state from launchctl (maybe not in a GUI login session)."),
    "launchctl_fail.fix": ("Mac の画面にログインした状態で、もう一度実行してください。", "Run again while logged in to the Mac desktop."),
    "expect_missing": ("成果のファイルがありません: {path}", "Expected output file does not exist: {path}"),
    "expect_missing.fix": ("ジョブが一度も最後まで進んでいない可能性があります。ログを確かめてください。",
                           "The job may never have completed. Check the log."),
    "expect_stale": ("成果のファイルが、最後の予定（{sched}）のあと更新されていません（最終更新 {mtime}）。{extra}",
                     "The output file was not updated after the last scheduled run ({sched}). Last modified {mtime}. {extra}"),
    "expect_stale_ok_exit": ("終了コードは 0 なので、スクリプトが失敗を 0 で終えている可能性があります。",
                             "Exit code was 0, so the script may be hiding a failure."),
    "expect_stale.fix": ("その時刻に Mac が電源オフ・スリープでなかったか、ログに何が出たかを確かめてください。",
                         "Check whether the Mac was off or asleep at that time, and what the log says."),
    "expect_old": ("成果のファイルが {hours} 時間更新されていません（上限 {limit} 時間）: {path}",
                   "The output file has not been updated for {hours} hours (limit {limit}): {path}"),
    "expect_old.fix": ("ログと最後の終了コードを確かめてください。", "Check the log and the last exit code."),
    "expect_ok": ("成果のファイルは最後の予定（{sched}）のあとに更新されています。",
                  "The output file was updated after the last scheduled run ({sched})."),
    "sched_info": ("前回の予定: {sched}。ログの最終更新: {logm}。", "Last scheduled time: {sched}. Log last modified: {logm}."),
    "log_notfound": ("ログに \"command not found\" か \"No such file\" があります（{n} 回・{file}）。",
                     "The log contains \"command not found\" or \"No such file\" ({n} times, {file})."),
    "log_notfound.fix": ("PATH か、パスの打ち間違いです。上の PATH の項目も見てください。",
                         "PATH or a wrong path. See the PATH findings above."),
    "log_tcc": ("ログに \"Operation not permitted\" があります（{n} 回・{file}）。保護されたフォルダに触れています。",
                "The log contains \"Operation not permitted\" ({n} times, {file}). Something touches a protected folder."),
    "log_tcc.fix": ("触るファイルを保護されていない場所へ移すか、システム設定のフルディスクアクセスを見直してください。",
                    "Move the files out of protected folders, or review Full Disk Access in System Settings."),
    "log_perm": ("ログに \"Permission denied\" があります（{n} 回・{file}）。", "The log contains \"Permission denied\" ({n} times, {file})."),
    "log_perm.fix": ("実行の権限（chmod +x）とフォルダの権限を確かめてください。", "Check chmod +x and folder permissions."),
    "log_interp": ("ログに \"bad interpreter\" があります（{n} 回・{file}）。", "The log contains \"bad interpreter\" ({n} times, {file})."),
    "log_interp.fix": ("1行目の #! と改行（CRLF）を確かめてください。", "Check the #! line and CRLF line endings."),
    "log_auth": ("ログに認証の失敗らしい行があります（{n} 回・{file}）。トークンの期限切れやログアウトの可能性。",
                 "The log has lines that look like authentication failures ({n} times, {file}). The token may have expired."),
    "log_auth.fix": ("ターミナルで claude -p \"hi\" が通るか確かめ、トークンを作り直してください。",
                     "Check that claude -p \"hi\" works in a terminal, then renew the token."),
    "log_limit": ("ログに利用の上限らしい行があります（{n} 回・{file}）。", "The log has lines that look like usage or rate limits ({n} times, {file})."),
    "log_limit.fix": ("上限が戻る時刻まで待つか、実行の数を減らしてください。", "Wait for the limit to reset, or run fewer jobs."),
    "log_pymod": ("ログに Python の部品が見つからない行があります（{n} 回・{file}）。", "The log shows Python import errors ({n} times, {file})."),
    "log_pymod.fix": ("launchd が使う python が、部品を入れた python（venv など）と同じか確かめ、その絶対パスで呼んでください。",
                      "Make sure launchd uses the same python (for example your venv) and call it by absolute path."),
    "log_trace": ("ログに Python のエラー（Traceback）があります（{n} 回・{file}）。", "The log contains Python tracebacks ({n} times, {file})."),
    "log_trace.fix": ("ログの最後の Traceback を読んでください。", "Read the last traceback in the log."),
    "target_missing": ("{target} という plist もジョブも見つかりません。", "No plist or loaded job named {target} was found."),
    "target_missing.fix": ("ls ~/Library/LaunchAgents で名前を確かめてください。", "Check the name with: ls ~/Library/LaunchAgents"),
    "all_ok": ("見つかった問題はありません。", "No problems found."),
}

EXIT_INFO = {
    1: ("スクリプトの中でエラーが起きて終わりました。", "The script ended with an error.",
        "StandardErrorPath のログの最後を見てください。", "Read the end of the StandardErrorPath log."),
    2: ("使い方の誤りか、シェルの文法の誤りです。", "Usage error or shell syntax error.",
        "ターミナルで同じコマンドを /bin/zsh -n などで確かめてください。", "Run the same command in a terminal, or check syntax with zsh -n."),
    64: ("引数の誤り（EX_USAGE）。", "Wrong arguments (EX_USAGE).", "ProgramArguments を確かめてください。", "Check ProgramArguments."),
    69: ("必要なものが使えない（EX_UNAVAILABLE）。", "A required service is unavailable (EX_UNAVAILABLE).", "ログを確かめてください。", "Check the log."),
    70: ("プログラムの内部エラー（EX_SOFTWARE）。", "Internal software error (EX_SOFTWARE).", "ログを確かめてください。", "Check the log."),
    71: ("OS のエラー（EX_OSERR）。", "Operating system error (EX_OSERR).", "ログを確かめてください。", "Check the log."),
    73: ("ファイルを作れない（EX_CANTCREAT）。", "Cannot create a file (EX_CANTCREAT).", "書き込み先の権限と場所を確かめてください。", "Check where it writes and the permissions."),
    74: ("読み書きのエラー（EX_IOERR）。", "Input/output error (EX_IOERR).", "ディスクと権限を確かめてください。", "Check disk and permissions."),
    75: ("一時的な失敗（EX_TEMPFAIL）。", "Temporary failure (EX_TEMPFAIL).", "ネットワークなどを確かめてください。", "Check network and retry."),
    77: ("権限がない（EX_NOPERM）。", "Permission denied (EX_NOPERM).", "権限と保護されたフォルダを確かめてください。", "Check permissions and protected folders."),
    78: ("launchd がジョブの準備に失敗しました（EX_CONFIG）。スクリプトは始まっていません。よくある原因は、ログの出し先が保護されたフォルダにある・ログのフォルダや WorkingDirectory が無い、です。",
         "launchd failed to set up the job (EX_CONFIG). Your script did not start. Common causes: log path in a protected folder, missing log folder or WorkingDirectory.",
         "上の「ログの出し先」と WorkingDirectory の項目を見てください。", "See the log path and WorkingDirectory findings above."),
    126: ("見つかったが実行できない（権限か、保護されたフォルダ）。", "Found but not executable (permission, or a protected folder).",
          "chmod +x と、置き場所を確かめてください。", "Check chmod +x and where the file lives."),
    127: ("コマンドが見つからない（command not found）。launchd の PATH が短いのが一番多い原因です。",
          "Command not found. launchd's short PATH is the most common cause.",
          "コマンドを絶対パスで書くか、EnvironmentVariables に PATH を足してください。", "Use absolute paths, or add PATH to EnvironmentVariables."),
}
SIGNALS = {2: "SIGINT", 6: "SIGABRT", 9: "SIGKILL", 11: "SIGSEGV", 15: "SIGTERM"}


class Ctx:
    def __init__(self, lang):
        self.lang = lang

    def t(self, mid, **kw):
        pair = M.get(mid)
        if pair is None:
            return mid
        s = pair[0] if self.lang == "ja" else pair[1]
        try:
            return s.format(**kw)
        except (KeyError, IndexError):
            return s


class Report:
    def __init__(self, label, plist_path):
        self.label = label
        self.plist = plist_path
        self.findings = []  # (level, id, message, fix)

    def add(self, ctx, level, mid, fix=True, **kw):
        msg = ctx.t(mid, **kw)
        fx = ctx.t(mid + ".fix", **kw) if fix and (mid + ".fix") in M else ""
        self.findings.append((level, mid, msg, fx))

    def worst(self):
        order = {"FAIL": 3, "WARN": 2, "INFO": 1, "OK": 0}
        return max([order[f[0]] for f in self.findings] or [0])


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 999, "", str(e)


def read_text(path, limit=MAX_READ):
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def read_tail(path, nbytes=64 * 1024):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def expand(p, workdir=None):
    """Expand ~ and $HOME the way a shell would (used for script text only)."""
    if p is None:
        return None
    p = p.strip().strip("'\"")
    p = p.replace("${HOME}", HOME).replace("$HOME", HOME)
    if p.startswith("~/") or p == "~":
        p = HOME + p[1:]
    if workdir and not p.startswith("/"):
        p = os.path.join(workdir, p)
    return p


def protected_folder(path):
    if not path:
        return None
    m = PROTECTED_RE.match(path)
    if m:
        return m.group(1)
    for name in PROTECTED_NAMES:
        base = os.path.join(HOME, name)
        if path == base or path.startswith(base + "/"):
            return name
    return None


def resolve(cmd, dirs):
    for d in dirs:
        if not d:
            continue
        c = os.path.join(d, cmd)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def fmt_time(ts):
    if ts is None:
        return "-"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def first_line(path):
    try:
        with open(path, "rb") as f:
            return f.readline(512)
    except OSError:
        return b""


def is_text_script(path):
    return first_line(path).startswith(b"#!")


def login_shell_dirs(shell_name):
    """PATH entries a login shell would add (path_helper + profile files)."""
    dirs = []
    for line in (read_text("/etc/paths") or "").splitlines():
        if line.strip():
            dirs.append(line.strip())
    for f in sorted(glob.glob("/etc/paths.d/*")):
        for line in (read_text(f) or "").splitlines():
            if line.strip():
                dirs.append(line.strip())
    if shell_name == "zsh":
        files = ["~/.zprofile", "~/.zlogin"]
    else:
        files = ["~/.bash_profile", "~/.bash_login", "~/.profile"]
    return dirs, [expand(f) for f in files]


# --------------------------------------------------------------------------
# shell text scanning
# --------------------------------------------------------------------------
PATH_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?PATH=(.+?)\s*(?:#.*)?$")
ENV_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
SOURCE_RE = re.compile(r"^\s*(?:source|\.)\s+([^\s;&|]+)")
BREW_RE = re.compile(r"(/[\w./-]+)/bin/brew\s+shellenv")
CMD_TOKEN_RE = re.compile(
    r"(?:^|[\s;|&(`]|\$\()(" + "|".join(WATCH_CMDS) + r")(?=$|[\s;|&)`])", re.M)
PY_CMD_RE = re.compile(r"[\"'](" + "|".join(WATCH_CMDS) + r")[\"']")
ABS_CMD_RE = re.compile(r"(?<![\w.~/$}-])((?:~|\$HOME|\$\{HOME\})?/[\w.@+-]+(?:/[\w.@+~-]+)*/(?:" + "|".join(WATCH_CMDS) + r"))(?=$|[\s;|&)`\"'])", re.M)


def strip_comments(text):
    out = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def apply_path_assign(value, current):
    v = value.strip().strip("'\"")
    v = v.replace("${PATH}", "\0").replace("$PATH", "\0")
    parts = []
    for item in v.split(":"):
        if item == "\0":
            parts.extend(current)
            continue
        item = item.replace("\0", "")
        item = expand(item)
        if "$" in item or not item:
            continue
        parts.append(item)
    return parts


class ShellScan:
    """What a shell script or command string sets up and calls."""

    def __init__(self):
        self.path_changes = []      # list of raw values in order
        self.env_names = {}         # name -> (empty?, source)
        self.sourced = []           # (path, exists)
        self.cmds = set()           # bare watched commands
        self.abs_cmds = set()       # absolute paths of watched commands
        self.brew_prefixes = []

    def scan(self, text, src, is_python=False, follow_sources=True, depth=0):
        body = strip_comments(text)
        if is_python:
            for m in PY_CMD_RE.finditer(body):
                self.cmds.add(m.group(1))
            for m in ABS_CMD_RE.finditer(body):
                self.abs_cmds.add(m.group(1))
            for name in AUTH_VARS:
                if re.search(r"os\.environ\[[\"']%s[\"']\]\s*=" % name, body):
                    self.env_names.setdefault(name, (False, src))
            return
        for line in body.splitlines():
            m = PATH_ASSIGN_RE.match(line)
            if m:
                self.path_changes.append(m.group(1))
            m = ENV_ASSIGN_RE.match(line)
            if m and m.group(1) != "PATH":
                val = m.group(2).strip().strip("'\"")
                self.env_names[m.group(1)] = (val == "", src)
            m = BREW_RE.search(line)
            if m:
                self.brew_prefixes.append(m.group(1))
            m = SOURCE_RE.match(line)
            if m and follow_sources:
                p = expand(m.group(1))
                if "$" in p:
                    continue
                exists = os.path.isfile(p)
                self.sourced.append((p, exists))
                if exists and depth < 2:
                    t = read_text(p)
                    if t is not None:
                        self.scan(t, p, depth=depth + 1)
        for m in CMD_TOKEN_RE.finditer(body):
            self.cmds.add(m.group(1))
        for m in ABS_CMD_RE.finditer(body):
            self.abs_cmds.add(m.group(1))

    def effective_path(self, base):
        cur = list(base)
        for pref in self.brew_prefixes:
            cur = [pref + "/bin", pref + "/sbin"] + cur
        for raw in self.path_changes:
            cur = apply_path_assign(raw, cur)
        return cur


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------
def cal_entries(value):
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    return None


def cal_match(d, t):
    lw = (t.weekday() + 1) % 7  # launchd: 0 = Sunday
    for k, v in d.items():
        if k == "Minute" and t.minute != v:
            return False
        if k == "Hour" and t.hour != v:
            return False
        if k == "Day" and t.day != v:
            return False
        if k == "Month" and t.month != v:
            return False
        if k == "Weekday" and lw != (v % 7):
            return False
    return True


def last_calendar_time(entries, now, lookback_days=62):
    best = None
    for d in entries:
        if not isinstance(d, dict):
            continue
        t = now.replace(second=0, microsecond=0)
        stop = t - dt.timedelta(days=lookback_days)
        step = dt.timedelta(minutes=1)
        while t > stop:
            if cal_match(d, t):
                if best is None or t > best:
                    best = t
                break
            t -= step
    return best


# --------------------------------------------------------------------------
# launchctl state
# --------------------------------------------------------------------------
def launchctl_print(label):
    for domain in ("gui/%d" % UID, "user/%d" % UID):
        code, out, _ = run(["launchctl", "print", "%s/%s" % (domain, label)])
        if code == 0 and out:
            return out
    return None


def parse_print(out):
    info = {}
    m = re.search(r"^\s*state = (.+)$", out, re.M)
    info["state"] = m.group(1).strip() if m else None
    m = re.search(r"^\s*last exit code = (.+)$", out, re.M)
    info["exit_raw"] = m.group(1).strip() if m else None
    m = re.search(r"^\s*last terminating signal = (.+)$", out, re.M)
    info["signal"] = m.group(1).strip() if m else None
    m = re.search(r"^\s*runs = (\d+)", out, re.M)
    info["runs"] = int(m.group(1)) if m else None
    m = re.search(r"^\s*pid = (\d+)", out, re.M)
    info["pid"] = int(m.group(1)) if m else None
    m = re.search(r"^\s*path = (.+)$", out, re.M)
    info["path"] = m.group(1).strip() if m else None
    m = re.search(r"^\s*stdout path = (.+)$", out, re.M)
    info["stdout"] = m.group(1).strip() if m else None
    m = re.search(r"^\s*stderr path = (.+)$", out, re.M)
    info["stderr"] = m.group(1).strip() if m else None
    m = re.search(r"^\targuments = \{\n(.*?)^\t\}", out, re.M | re.S)
    if m:
        info["arguments"] = [l.strip() for l in m.group(1).splitlines() if l.strip()]
    else:
        info["arguments"] = None
    return info


def disabled_labels():
    code, out, _ = run(["launchctl", "print-disabled", "gui/%d" % UID])
    res = set()
    if code != 0:
        return res
    for m in re.finditer(r"\"([^\"]+)\"\s*=>\s*(disabled|true)", out):
        res.add(m.group(1))
    return res


def elapsed_minutes(pid):
    code, out, _ = run(["ps", "-o", "etime=", "-p", str(pid)])
    if code != 0 or not out.strip():
        return None
    s = out.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, mi, _ = parts
    return days * 1440 + h * 60 + mi


def explain_exit(ctx, code):
    if code < 0 or code > 128:
        sig = -code if code < 0 else code - 128
        name = SIGNALS.get(sig, "signal %d" % sig)
        if ctx.lang == "ja":
            return ("シグナル %s で止められました（メモリ不足・時間切れ・ログアウト・手で止めた、など）。" % name,
                    "ログの最後と、同じ時刻に何があったかを確かめてください。")
        return ("Killed by %s (out of memory, timeout, logout, or stopped by hand)." % name,
                "Check the end of the log and what happened at that time.")
    if code in EXIT_INFO:
        ja, en, fja, fen = EXIT_INFO[code]
        return (ja, fja) if ctx.lang == "ja" else (en, fen)
    if ctx.lang == "ja":
        return ("プログラムが返した値です。", "そのプログラムの説明とログで意味を確かめてください。")
    return ("Value returned by the program.", "Check the program's documentation and the log.")


# --------------------------------------------------------------------------
# main check
# --------------------------------------------------------------------------
def check_plist(ctx, path, opts, ld_state):
    rep = Report(os.path.basename(path)[:-6] if path.endswith(".plist") else os.path.basename(path), path)

    # 1. format -----------------------------------------------------------
    code, out, err = run(["plutil", "-lint", path]) if shutil.which("plutil") else (0, "", "")
    if code != 0:
        rep.add(ctx, "FAIL", "lint_fail")
    try:
        with open(path, "rb") as f:
            pl = plistlib.load(f)
    except Exception as e:  # noqa: BLE001 - any parse error is a finding
        if code == 0:
            rep.add(ctx, "FAIL", "parse_fail", err=type(e).__name__)
        return rep
    if not isinstance(pl, dict):
        rep.add(ctx, "FAIL", "not_dict")
        return rep
    if not pl:
        rep.add(ctx, "WARN", "empty_plist")
        return rep

    label = pl.get("Label")
    if not label:
        rep.add(ctx, "FAIL", "no_label")
    else:
        rep.label = label
        if os.path.basename(path) != label + ".plist":
            rep.add(ctx, "INFO", "label_mismatch", label=label)

    try:
        st = os.stat(path)
        why = []
        if st.st_uid != UID and st.st_uid != 0:
            why.append("owner uid %d" % st.st_uid)
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            why.append("mode %o" % (st.st_mode & 0o777))
        if why and path.startswith(AGENTS_DIR + "/"):
            rep.add(ctx, "FAIL", "bad_perm", why=", ".join(why))
    except OSError:
        pass

    for k in pl:
        if k in KNOWN_KEYS:
            continue
        sugg = difflib.get_close_matches(k, KNOWN_KEYS, n=1, cutoff=0.8)
        if sugg:
            rep.add(ctx, "WARN", "unknown_key_typo", key=k, sugg=sugg[0])
        else:
            rep.add(ctx, "INFO", "unknown_key", key=k)
    if pl.get("Disabled") is True:
        rep.add(ctx, "WARN", "disabled_key")

    if not any(k in pl for k in TRIGGER_KEYS) and pl.get("RunAtLoad") is not True:
        rep.add(ctx, "WARN", "no_trigger")

    cal = None
    if "StartCalendarInterval" in pl:
        cal = cal_entries(pl["StartCalendarInterval"])
        if cal is None:
            rep.add(ctx, "FAIL", "cal_bad", detail="not a dict or array")
        else:
            bad = []
            for d in cal:
                if not isinstance(d, dict):
                    bad.append(repr(d)[:40])
                    continue
                for k, v in d.items():
                    if k not in CAL_RANGES:
                        s = difflib.get_close_matches(k, CAL_RANGES, n=1, cutoff=0.6)
                        bad.append("%s%s" % (k, " -> " + s[0] if s else ""))
                    elif not isinstance(v, int) or isinstance(v, bool) or not (CAL_RANGES[k][0] <= v <= CAL_RANGES[k][1]):
                        bad.append("%s=%r" % (k, v))
            if bad:
                rep.add(ctx, "FAIL", "cal_bad", detail=", ".join(bad))
                cal = [d for d in cal if isinstance(d, dict) and all(k in CAL_RANGES for k in d)]

    # program and arguments -------------------------------------------------
    env = pl.get("EnvironmentVariables") if isinstance(pl.get("EnvironmentVariables"), dict) else {}
    workdir = pl.get("WorkingDirectory")
    if isinstance(workdir, str):
        if not os.path.isdir(workdir):
            rep.add(ctx, "FAIL", "workdir_missing", path=workdir)
        pf = protected_folder(workdir)
        if pf:
            rep.add(ctx, "WARN", "protected_access", what="WorkingDirectory", folder=pf, path=workdir)
    else:
        workdir = None

    raw_path = env.get("PATH")
    if isinstance(raw_path, str) and raw_path.strip():
        if "~" in raw_path or "$" in raw_path:
            rep.add(ctx, "FAIL", "path_tilde")
        base_dirs = [d for d in raw_path.split(":") if d and "~" not in d and "$" not in d]
        path_src = "EnvironmentVariables PATH"
    else:
        base_dirs = DEFAULT_LAUNCHD_PATH.split(":")
        path_src = ("launchd の初期の PATH %s" if ctx.lang == "ja" else "launchd default PATH %s") % DEFAULT_LAUNCHD_PATH

    args = pl.get("ProgramArguments")
    program = pl.get("Program")
    bad_args = args is not None and not isinstance(args, list)
    if bad_args:
        rep.add(ctx, "FAIL", "args_not_array")
        args = None
    if args is None and not program and not bad_args:
        rep.add(ctx, "FAIL", "no_program")
    argv = [str(a) for a in (args or [])]
    if program and not argv:
        argv = [str(program)]
    prog = str(program) if program else (argv[0] if argv else None)

    scan = ShellScan()
    eff_base = list(base_dirs)
    scripts_to_check = []   # (path, needs_exec)
    uses_shell_string = False

    if prog:
        prog_res = prog
        if "/" not in prog:
            found = resolve(prog, base_dirs)
            where = found or ("not found" if ctx.lang == "en" else "見つかりません")
            rep.add(ctx, "FAIL" if not found else "WARN", "prog_bare", cmd=prog, where=where)
            prog_res = found
        elif not prog.startswith("/"):
            if prog.startswith("~") or "$" in prog:
                rep.add(ctx, "FAIL", "tilde_arg", val=prog)
                prog_res = None
            elif workdir:
                prog_res = os.path.join(workdir, prog)
            else:
                rep.add(ctx, "FAIL", "rel_path", path=prog)
                prog_res = None
        if prog_res:
            pf = protected_folder(prog_res)
            if pf:
                rep.add(ctx, "WARN", "protected_access", what="Program", folder=pf, path=prog_res)
            if not os.path.exists(prog_res):
                rep.add(ctx, "FAIL", "prog_missing", path=prog_res)
                prog_res = None
            elif not os.access(prog_res, os.X_OK) or os.path.isdir(prog_res):
                rep.add(ctx, "FAIL", "prog_not_exec", path=prog_res)

        name = os.path.basename(prog)
        rest = argv[1:]
        # /usr/bin/env cmd ...
        if name == "env":
            while rest and (rest[0].startswith("-") or "=" in rest[0]):
                rest = rest[1:]
            if rest:
                name = os.path.basename(rest[0])
                if "/" not in rest[0]:
                    if not resolve(rest[0], base_dirs):
                        rep.add(ctx, "FAIL", "prog_bare", cmd=rest[0],
                                where=("not found" if ctx.lang == "en" else "見つかりません"))
                rest = rest[1:]
        if name in SHELLS:
            flags = ""
            i = 0
            while i < len(rest) and rest[i].startswith("-") and rest[i] != "--":
                flags += rest[i].lstrip("-")
                i += 1
            if "l" in flags or "login" in flags:
                ld, files = login_shell_dirs(name)
                eff_base = ld + eff_base
                for f in files:
                    t = read_text(f) if os.path.isfile(f) else None
                    if t:
                        scan.scan(t, f, follow_sources=True)
            if name == "zsh":
                zenv = expand("~/.zshenv")
                t = read_text(zenv) if os.path.isfile(zenv) else None
                if t:
                    scan.scan(t, zenv)
            if "i" in flags:
                rc = expand("~/.zshrc" if name == "zsh" else "~/.bashrc")
                t = read_text(rc) if os.path.isfile(rc) else None
                if t:
                    scan.scan(t, rc)
            if "c" in flags and i < len(rest):
                uses_shell_string = True
                scan.scan(rest[i], "ProgramArguments -c")
            elif i < len(rest):
                scripts_to_check.append((rest[i], False))
        elif PY_RE.match(name):
            i = 0
            while i < len(rest) and rest[i].startswith("-"):
                if rest[i] in ("-c", "-m"):
                    if rest[i] == "-c" and i + 1 < len(rest):
                        scan.scan(rest[i + 1], "python -c", is_python=True)
                    i = len(rest)
                    break
                i += 1
            if i < len(rest):
                scripts_to_check.append((rest[i], False))
        elif name == "node":
            if rest and not rest[0].startswith("-"):
                scripts_to_check.append((rest[0], False))
        else:
            if name in WATCH_CMDS and prog.startswith("/"):
                scan.abs_cmds.add(prog)
            if prog_res and os.path.isfile(prog_res) and is_text_script(prog_res):
                scripts_to_check.append((prog_res, True))

        # literal ~ $ and shell syntax in args (no shell involved)
        literal_args = argv[1:] if not uses_shell_string else []
        for a in literal_args:
            if a.startswith("~") or re.search(r"\$\{?[A-Za-z_]", a):
                rep.add(ctx, "FAIL", "tilde_arg", val=a)
        for a in literal_args:
            if a in (">", ">>", "|", "&&", "||", "2>&1", ";", "<"):
                rep.add(ctx, "FAIL", "shell_token", tok=a)
                break

    # scripts ----------------------------------------------------------------
    for spath, needs_exec in scripts_to_check:
        if spath.startswith("~") or "$" in spath:
            continue  # already reported as tilde_arg
        if not spath.startswith("/"):
            if workdir:
                spath = os.path.join(workdir, spath)
            else:
                rep.add(ctx, "FAIL", "rel_path", path=spath)
                continue
        pf = protected_folder(spath)
        if pf:
            rep.add(ctx, "WARN", "protected_access", what="Script", folder=pf, path=spath)
        if not os.path.isfile(spath):
            rep.add(ctx, "FAIL", "script_missing", path=spath)
            continue
        line1 = first_line(spath)
        if line1.startswith(b"#!"):
            if line1.rstrip(b"\n").endswith(b"\r"):
                rep.add(ctx, "FAIL", "script_crlf", path=spath)
            parts = line1[2:].decode("utf-8", "replace").strip().split()
            if parts:
                interp = parts[0]
                if os.path.basename(interp) == "env" and len(parts) > 1:
                    target = [p for p in parts[1:] if not p.startswith("-")]
                    if target and needs_exec and not resolve(target[0], eff_base):
                        rep.add(ctx, "FAIL", "shebang_env", cmd=target[0], path=spath)
                elif needs_exec and not os.path.exists(interp):
                    rep.add(ctx, "FAIL", "shebang_missing", interp=interp, path=spath)
        text = read_text(spath)
        if text is None:
            continue
        is_py = spath.endswith(".py") or b"python" in line1
        is_sh = spath.endswith((".sh", ".zsh", ".bash")) or any(
            s.encode() in line1 for s in SHELLS)
        if is_py:
            scan.scan(text, spath, is_python=True)
        elif is_sh:
            scan.scan(text, spath)

    # PATH and commands ------------------------------------------------------
    eff = scan.effective_path(eff_base)
    shell_path = os.environ.get("PATH", "").split(":")
    for p, exists in scan.sourced:
        if not exists:
            rep.add(ctx, "FAIL", "sourced_missing", path=p)
        else:
            pf = protected_folder(p)
            if pf:
                rep.add(ctx, "WARN", "protected_access", what="Sourced file", folder=pf, path=p)
    src_label = path_src if not scan.path_changes and not scan.brew_prefixes else ("スクリプトの中で設定した PATH" if ctx.lang == "ja" else "PATH set in script")
    for cmd in sorted(scan.cmds):
        ld = resolve(cmd, eff)
        sh = resolve(cmd, shell_path)
        if not ld:
            if sh:
                rep.add(ctx, "FAIL", "cmd_missing", cmd=cmd, src=src_label, shell=sh,
                        dir=os.path.dirname(sh))
            else:
                rep.add(ctx, "FAIL", "cmd_missing_noshell", cmd=cmd, src=src_label)
        elif sh and os.path.realpath(ld) != os.path.realpath(sh):
            rep.add(ctx, "WARN", "cmd_differs", cmd=cmd, ld=ld, shell=sh, src=src_label)
        target = ld
        if target and cmd in ("claude", "codex", "npx", "npm", "pnpm", "yarn"):
            if b"node" in first_line(target) and not resolve("node", eff):
                rep.add(ctx, "FAIL", "node_missing", cmd=cmd, path=target)
    for ap in sorted(scan.abs_cmds):
        apx = expand(ap)
        if not os.path.exists(apx):
            rep.add(ctx, "FAIL", "abs_missing", path=apx, src="script")
        else:
            pf = protected_folder(apx)
            if pf:
                rep.add(ctx, "WARN", "protected_access", what="Program", folder=pf, path=apx)
            if b"node" in first_line(apx) and not resolve("node", eff):
                rep.add(ctx, "FAIL", "node_missing", cmd=os.path.basename(apx), path=apx)

    # environment variables ----------------------------------------------------
    uses_claude = "claude" in scan.cmds or any(a.endswith("/claude") for a in scan.abs_cmds) \
        or (prog and os.path.basename(prog) == "claude")
    sources = {}  # name -> list of (empty, src)
    for k, v in env.items():
        sources.setdefault(k, []).append((not str(v).strip(), "plist EnvironmentVariables"))
    for k, (empty, src) in scan.env_names.items():
        sources.setdefault(k, []).append((empty, src))
    extra_files = list(opts.env_file or [])
    for f in extra_files:
        t = read_text(expand(f))
        if t is None:
            continue
        for line in t.splitlines():
            m = ENV_ASSIGN_RE.match(line)
            if m:
                sources.setdefault(m.group(1), []).append(
                    (m.group(2).strip().strip("'\"") == "", expand(f)))
    secret_files = set()

    def check_name(name):
        hits = sources.get(name, [])
        if not hits and not opts.offline:
            code_, out_, _ = run(["launchctl", "getenv", name])
            if code_ == 0 and out_.strip():
                hits = [(False, "launchctl setenv")]
            out_ = None  # drop value right away
        return hits

    wanted = list(opts.require_env or [])
    for name in wanted:
        hits = check_name(name)
        if not hits:
            rep.add(ctx, "FAIL", "env_missing_req", name=name)
        for empty, src in hits:
            if empty:
                rep.add(ctx, "FAIL", "env_empty", name=name, src=src)
            else:
                rep.add(ctx, "INFO", "env_found", fix=False, name=name, src=src)
                if src.startswith("/"):
                    secret_files.add(src)
    if uses_claude and not any(n in wanted for n in AUTH_VARS):
        found_any = False
        for name in AUTH_VARS:
            hits = check_name(name)
            for empty, src in hits:
                if empty:
                    found_any = True  # reported as empty instead of missing
                    rep.add(ctx, "FAIL", "env_empty", name=name, src=src)
                else:
                    found_any = True
                    rep.add(ctx, "INFO", "env_found", fix=False, name=name, src=src)
                    if src.startswith("/"):
                        secret_files.add(src)
                    elif src == "plist EnvironmentVariables":
                        secret_files.add(path)
        if not found_any:
            rep.add(ctx, "WARN", "env_missing", names=" / ".join(AUTH_VARS))
    for f in sorted(secret_files):
        try:
            mode = os.stat(f).st_mode & 0o777
            if mode & 0o044:
                rep.add(ctx, "WARN", "secret_file_perm", mode="%o" % mode, path=f)
        except OSError:
            pass

    # 3. log paths --------------------------------------------------------------
    for key in ("StandardOutPath", "StandardErrorPath"):
        lp = pl.get(key)
        if not isinstance(lp, str):
            continue
        if lp.startswith("~") or "$" in lp:
            rep.add(ctx, "FAIL", "tilde_arg", val=lp)
            continue
        pf = protected_folder(lp)
        if pf:
            rep.add(ctx, "FAIL", "protected_log", key=key, folder=pf, path=lp)
            continue
        d = os.path.dirname(lp) or "/"
        if not lp.startswith("/"):
            rep.add(ctx, "FAIL", "rel_path", path=lp)
        elif not os.path.isdir(d):
            rep.add(ctx, "FAIL", "logdir_missing", key=key, dir=d)
        elif not os.access(d, os.W_OK):
            rep.add(ctx, "FAIL", "logdir_readonly", key=key, dir=d)
    if not isinstance(pl.get("StandardErrorPath"), str):
        rep.add(ctx, "INFO", "no_log")

    # 2. launchd state ----------------------------------------------------------
    info = None
    if not opts.offline and label:
        if label in ld_state.get("disabled", set()):
            rep.add(ctx, "FAIL", "disabled", label=label)
        out = launchctl_print(label)
        if out is None:
            if ld_state.get("list_ok"):
                rep.add(ctx, "WARN", "not_loaded", path=path)
            else:
                rep.add(ctx, "WARN", "launchctl_fail")
        else:
            info = parse_print(out)
            diffs = []
            if info.get("path") and os.path.realpath(info["path"]) != os.path.realpath(path):
                rep.add(ctx, "WARN", "loaded_other", other=info["path"])
            if info.get("arguments") is not None and isinstance(args, list):
                if info["arguments"] != [str(a) for a in args]:
                    diffs.append("ProgramArguments")
            for key, ik in (("StandardOutPath", "stdout"), ("StandardErrorPath", "stderr")):
                if isinstance(pl.get(key), str) and info.get(ik) and info[ik] != pl[key]:
                    diffs.append(key)
            if diffs:
                rep.add(ctx, "WARN", "loaded_differs", what=", ".join(diffs), label=label)
            raw = info.get("exit_raw")
            if raw and raw.startswith("(never"):
                rep.add(ctx, "INFO", "never_ran", runs=info.get("runs"))
            elif raw:
                m = re.match(r"-?\d+", raw)
                if m:
                    c = int(m.group(0))
                    if c == 0:
                        rep.add(ctx, "OK", "exit_ok", fix=False, runs=info.get("runs"))
                    else:
                        meaning, fx = explain_exit(ctx, c)
                        rep.add(ctx, "FAIL", "exit_bad", code=c, meaning=meaning, fix=fx)
            scheduled = ("StartCalendarInterval" in pl or "StartInterval" in pl) and not pl.get("KeepAlive")
            if scheduled and info.get("pid") and info.get("state") == "running":
                mins = elapsed_minutes(info["pid"])
                if mins is not None and mins >= opts.stuck_minutes:
                    rep.add(ctx, "WARN", "running_long", mins=mins)

    # a protected location that demonstrably works is only worth a note
    if any(f[1] == "exit_ok" for f in rep.findings):
        rep.findings = [("INFO",) + f[1:] if f[1] == "protected_access" else f for f in rep.findings]

    # 6. timing -------------------------------------------------------------------
    now = dt.datetime.now()
    sched = None
    if cal:
        sched = last_calendar_time(cal, now - dt.timedelta(minutes=opts.grace))
    elif isinstance(pl.get("StartInterval"), int) and pl["StartInterval"] > 0:
        sched = now - dt.timedelta(seconds=pl["StartInterval"] * 2, minutes=opts.grace)
    log_paths = [p for p in (pl.get("StandardErrorPath"), pl.get("StandardOutPath"))
                 if isinstance(p, str) and os.path.isfile(p)]
    log_m = max([os.path.getmtime(p) for p in log_paths], default=None)
    if opts.expect:
        ep = expand(opts.expect)
        if not os.path.exists(ep):
            rep.add(ctx, "FAIL", "expect_missing", path=ep)
        else:
            em = os.path.getmtime(ep)
            if opts.max_age is not None:
                age_h = (now.timestamp() - em) / 3600
                if age_h > opts.max_age:
                    rep.add(ctx, "FAIL", "expect_old", hours="%.1f" % age_h,
                            limit=opts.max_age, path=ep)
            if sched is not None:
                if em < sched.timestamp():
                    extra = ""
                    if info and (info.get("exit_raw") or "").startswith("0"):
                        extra = ctx.t("expect_stale_ok_exit")
                    rep.add(ctx, "FAIL", "expect_stale", sched=sched.strftime("%Y-%m-%d %H:%M"),
                            mtime=fmt_time(em), extra=extra)
                else:
                    rep.add(ctx, "OK", "expect_ok", fix=False, sched=sched.strftime("%Y-%m-%d %H:%M"))
    if cal and sched is not None and opts.verbose:
        rep.add(ctx, "INFO", "sched_info", fix=False,
                sched=sched.strftime("%Y-%m-%d %H:%M"), logm=fmt_time(log_m))

    # log tail patterns ------------------------------------------------------------
    seen = set()
    for lp in log_paths:
        if lp in seen:
            continue
        seen.add(lp)
        tail = read_tail(lp)
        if not tail:
            continue
        lines = tail.splitlines()[-200:]
        for key, rx in LOG_PATTERNS:
            n = sum(1 for l in lines if rx.search(l))
            if n:
                lvl = "INFO" if key in ("log_limit", "log_trace") else "WARN"
                rep.add(ctx, lvl, key, n=n, file=lp)
        lines = None
    return rep


# --------------------------------------------------------------------------
# targets and output
# --------------------------------------------------------------------------
def find_targets(opts):
    targets = []
    if opts.targets:
        for t in opts.targets:
            if os.path.isfile(t):
                targets.append(os.path.abspath(t))
            else:
                cand = os.path.join(AGENTS_DIR, t if t.endswith(".plist") else t + ".plist")
                if os.path.isfile(cand):
                    targets.append(cand)
                else:
                    out = None if opts.offline else launchctl_print(t)
                    p = parse_print(out).get("path") if out else None
                    if p and os.path.isfile(p):
                        targets.append(p)
                    else:
                        targets.append(t)  # reported as missing
    else:
        d = opts.dir or AGENTS_DIR
        targets = sorted(glob.glob(os.path.join(d, "*.plist")))
    if opts.only_claude:
        keep = []
        for t in targets:
            txt = read_text(t) or ""
            hit = "claude" in txt.lower()
            if not hit:
                for m in re.finditer(r"<string>(/[^<]+)</string>", txt):
                    p = m.group(1)
                    if os.path.isfile(p) and os.path.getsize(p) < MAX_READ and is_text_script(p):
                        if "claude" in (read_text(p) or "").lower():
                            hit = True
                            break
            if hit:
                keep.append(t)
        targets = keep
    return targets


TAGS = {"ja": {"FAIL": "問題", "WARN": "注意", "INFO": "参考", "OK": "正常"},
        "en": {"FAIL": "FAIL", "WARN": "WARN", "INFO": "INFO", "OK": " OK "}}


def render_text(ctx, reports, opts):
    out = []
    shown = 0
    fails = warns = 0
    for r in reports:
        w = r.worst()
        if w >= 3:
            fails += 1
        elif w == 2:
            warns += 1
        if w < 2 and not opts.verbose and len(reports) > 1:
            continue
        shown += 1
        out.append("== %s" % r.label)
        out.append("   %s" % r.plist)
        items = [f for f in r.findings if opts.verbose or f[0] in ("FAIL", "WARN") or len(reports) == 1]
        if not any(f[0] in ("FAIL", "WARN") for f in r.findings):
            out.append("   [%s] %s" % (TAGS[ctx.lang]["OK"], ctx.t("all_ok")))
        for level, _key, msg, fx in items:
            out.append("   [%s] %s" % (TAGS[ctx.lang][level], msg))
            if fx and level in ("FAIL", "WARN", "INFO"):
                arrow = "直し方: " if ctx.lang == "ja" else "Fix: "
                out.append("          %s%s" % (arrow, fx))
        out.append("")
    total = len(reports)
    if ctx.lang == "ja":
        out.append("調べたジョブ %d 本：問題あり %d・注意だけ %d・問題なし %d" % (total, fails, warns, total - fails - warns))
        if total > 1 and not opts.verbose:
            out.append("（問題なしのジョブは省きました。全部見るには --verbose）")
    else:
        out.append("Checked %d job(s): %d with problems, %d with warnings only, %d clean"
                   % (total, fails, warns, total - fails - warns))
        if total > 1 and not opts.verbose:
            out.append("(Clean jobs are hidden. Use --verbose to show all.)")
    return "\n".join(out)


def render_json(reports):
    return json.dumps({
        "tool": "launchd-why", "version": VERSION,
        "jobs": [{"label": r.label, "plist": r.plist,
                  "findings": [{"level": l, "id": k, "message": m, "fix": f}
                               for l, k, m, f in r.findings]} for r in reports],
    }, ensure_ascii=False, indent=2)


def main(argv=None):
    lang_default = "ja" if os.environ.get("LANG", "").lower().startswith("ja") else "en"
    ap = argparse.ArgumentParser(
        prog="launchd-why",
        description="Find out why a scheduled launchd job (for example claude -p) does not run. Read-only.")
    ap.add_argument("targets", nargs="*", help="job label or plist path (default: all in ~/Library/LaunchAgents)")
    ap.add_argument("--dir", help="check every *.plist in this folder instead of ~/Library/LaunchAgents")
    ap.add_argument("--only-claude", action="store_true", help="only jobs whose plist or script mentions claude")
    ap.add_argument("--lang", choices=("ja", "en"), default=lang_default, help="output language")
    ap.add_argument("--expect", help="output file the job should update (one target only)")
    ap.add_argument("--max-age", type=float, help="hours: fail if --expect file is older than this")
    ap.add_argument("--grace", type=int, default=30, help="minutes allowed after the scheduled time (default 30)")
    ap.add_argument("--require-env", action="append", metavar="NAME", help="variable that must be provided (repeatable)")
    ap.add_argument("--env-file", action="append", metavar="FILE", help="extra file that provides variables (repeatable)")
    ap.add_argument("--stuck-minutes", type=int, default=120, help="warn if running longer than this (default 120)")
    ap.add_argument("--offline", action="store_true", help="do not ask launchctl (check files only)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("-v", "--verbose", action="store_true", help="show clean jobs and info lines")
    ap.add_argument("--version", action="version", version="launchd-why " + VERSION)
    opts = ap.parse_args(argv)
    ctx = Ctx(opts.lang)

    targets = find_targets(opts)
    if opts.expect and len(targets) != 1:
        ap.error("--expect needs exactly one target job")
    if not targets:
        print(ctx.t("all_ok") if not opts.json else render_json([]))
        return 0

    ld_state = {}
    if not opts.offline:
        code, _, _ = run(["launchctl", "list"])
        ld_state["list_ok"] = code == 0
        ld_state["disabled"] = disabled_labels()

    reports = []
    for t in targets:
        if not os.path.isfile(t):
            r = Report(t, t)
            r.add(ctx, "FAIL", "target_missing", target=t)
            reports.append(r)
            continue
        reports.append(check_plist(ctx, t, opts, ld_state))
    print(render_json(reports) if opts.json else render_text(ctx, reports, opts))
    return 1 if any(r.worst() >= 3 for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
