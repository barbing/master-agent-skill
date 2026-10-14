# Windows validation

The runtime and the manual coordination mode are ported to native Windows. The port is checked on macOS by simulating the Windows branches. This plan checks it on a Windows machine. Steps 1 to 5 call no model and install nothing. Step 6 is a live run and needs an explicitly authorized budget.

## What the port changed

| Area | macOS / Linux | Windows |
| --- | --- | --- |
| Project lease | `fcntl.flock` | `msvcrt.locking` on byte 0 of the lock file |
| Owned processes | new session; `killpg` ends the group | new process group in a kill-on-close job object; `TerminateJobObject` ends every member, with `taskkill /T /F` as the fallback |
| Candidate reads | directory descriptors with `O_NOFOLLOW` | reparse-point checks on every component, then the open handle's final path (`GetFinalPathNameByHandleW`) must be the expected one |
| Links | symlinks | symlinks, junctions and mount points; OneDrive placeholders count as ordinary files |
| Child environments | `PATH`, `LANG` | also `SYSTEMROOT`, `COMSPEC`, `PATHEXT`, the profile folders and `PYTHONUTF8=1` |
| Codex CLI | `codex` on `PATH`, app bundles | `codex.exe` from `PATH`, the standalone installer or npm's platform package; the `codex.cmd` shim is never run |
| Sandbox contract | `codex sandbox` | the same unified `codex sandbox`; MXC or elevated required |
| Conformance probe | writes and reads | the same; it now also refuses when a sandboxed command can read a sentinel in the shared temp folder or the control state |
| Claude actors | supported | refused before any session: Claude Code's Bash sandbox does not run on native Windows |
| Paths in handoffs | POSIX relative paths | also refuses `D:x`, `a:b`, device names (`nul`, `com1`, …) and trailing dots or spaces |
| Text | UTF-8 | UTF-8 named on every read, write and pipe; a handoff with a byte-order mark is refused, as on every platform |
| Commands in reports | `bash`, `sh` quoting | `powershell`, single-quote quoting |

## Machine

- **Windows 11.** MXC, Codex's preferred Windows sandbox, needs 24H2 (build 26100.9278) or 25H2 (26200.9278) or later. Otherwise use the elevated sandbox, which needs Codex's one-time administrator-approved setup.
- **Python 3.12 or later from python.org** (`py --version`), and Git for Windows.
- **Codex CLI 0.162 or later**, from the standalone installer or npm, signed in. `codex --version` must work in a new terminal.
- **Codex sandbox mode: MXC or elevated.** The unelevated sandbox cannot enforce read restrictions, and the runtime's probe refuses it by design. The runtime never changes Codex settings; choosing the mode is the machine owner's decision.
- **Run every command from an ordinary PowerShell window at the repository root.** Do not run them inside an agent's sandbox. There, writes to Git metadata and shared temp folders are refused, and the runtime's own sandbox cannot start inside another one. An agent following this plan must request unsandboxed execution for these commands.
- **Optional: Developer Mode.** It lets the symlink tests run; without it they are skipped, and the junction tests cover Windows.
- **State directory: `$HOME\mas-state`.** Keep it under your profile, which only your account can read, and outside OneDrive-synced folders. A folder such as `C:\mas` inherits rules that let other local accounts read it, and the probe then correctly refuses to run.

## 1. Record the environment

```powershell
git rev-parse HEAD
py -c "import sys, platform; print(sys.version); print(platform.platform())"
codex --version
where.exe codex
(Get-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem).LongPathsEnabled
```

If `LongPathsEnabled` is 0, keep the checkout and the state directory paths short.

## 2. Machine readiness

```powershell
py -m master_agent_runtime --state $HOME\mas-state doctor | Tee-Object -FilePath doctor.json
```

Expect `"ready": true`. Each check proves one mechanism:

| Check | Pass means |
| --- | --- |
| `python` | 3.12 or later. A `python install` warning names a Microsoft Store alias. |
| `long paths` | Enabled, or a warning with the state path's length. |
| `project lease` | The byte lock is exclusive, refuses a second holder and is released. |
| `process tree` | A child and its grandchild ended together. `"contained": true` means a job object held them. |
| `codex version`, `codex app-server` | The native `codex.exe` starts and speaks JSON-RPC over pipes. `windows_sandbox` shows the configured mode, if one is set. |
| `codex sandbox contract` | `sandbox --include-managed-config --permission-profile runtime-validator`. |
| `check profile probe`, `owner profile probe` | Each reports `own_workspace_write: succeeded, outside_temp_write: blocked, outside_temp_read: blocked` (or `hidden`), `protected_read: blocked` (or `hidden`). |
| `sandboxed check` | A Python check ran inside the sandbox and read its candidate. |
| `claude` | `skipped` on native Windows, by design. |

**If a probe fails**, its evidence file (named in `doctor.json`) lists every effect. Do not work around a failing probe; report it.
- `outside_temp_write: succeeded`: sandboxed commands can write outside their workspace.
- `outside_temp_read` or `protected_read: succeeded`: they can read outside their grant. Check the sandbox mode, and that the state directory is under `$HOME`.
- Codex's known open bug: deny rules may go unenforced for reads in `codex sandbox` (openai/codex#43362, seen in 0.153.4, elevated). The `protected_read` case detects it.

## 3. Unit tests

```powershell
py -m unittest discover -s tests -v 2>&1 | Tee-Object -FilePath unittest.log
```

Expect 423 tests and `OK`, with 15 skips (17 without Developer Mode) and no others:
- `test_claude_adapter.py`: every test in `ClaudeActorTests` and `ClaudeControllerTests` ("Claude actors are refused on native Windows").
- `test_runtime_permissions.py`: `test_future_system_temporary_files_are_denied_and_ambient_workspace_rejected` (macOS only). `test_directory_symlink_in_launcher_chain_is_admitted_without_home_wide_read` is also skipped without Developer Mode.
- `test_module_runtime.py`: `test_ungranted_files_and_indirect_symlinks_are_rejected`, without Developer Mode only.
- `test_runtime_windows_compat.py`: `SimulatedWindowsReadTests`, which the native `WindowsOnlyTests` replace.

## 4. The pilots' owner suite

```powershell
py -m unittest discover -s pilots -p "owned_*_test.py" -v 2>&1 | Tee-Object -FilePath pilots.log
```

Expect 20 tests and `OK`. These files are frozen: a failure points at the runtime or the machine, never at them, and they must not be edited.

## 5. Release gate

```powershell
py scripts/release_validate.py 2>&1 | Tee-Object -FilePath gate.log
```

Expect every step `PASS`; `plugin-eval` is skipped when unavailable. If Codex's `skill-creator` and PyYAML are installed, also pass `--quick-validate $HOME\.codex\skills\.system\skill-creator\scripts\quick_validate.py`.

## 6. Optional live run (needs an authorized budget)

Only with explicit authorization to spend tokens. It needs a Codex economy-tier model the account offers, and stays within a 2,000,000-token envelope. The project is one module: `calc.py` must make `add(2, 3) == 5`. The policy is the lightest one the README documents (checks-only acceptance, no plan, no probes), so a run is an owner turn and the Master's integration review.

```powershell
$smoke = "$HOME\mas-smoke"
$python = py -c "import sys; print(sys.executable)"
New-Item -ItemType Directory -Force "$smoke\project\checks" | Out-Null
"def add(a, b):`n    return 0`n" | Set-Content -NoNewline -Encoding ascii "$smoke\project\calc.py"
@'
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("calc", pathlib.Path(sys.argv[1]) / "calc.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.add(2, 3) == 5 and module.add(-1, 1) == 0
print("ok")
'@ | Set-Content -Encoding ascii "$smoke\project\checks\run.py"
$check = @{argv = @($python, "{context}/checks/run.py", "{candidate}"); timeout_seconds = 120}
$model = "ECONOMY-MODEL"   # an economy-tier Codex model this account offers
$handoff = @{root = "$smoke\project"; objective = "calc.add(a, b) returns the sum of two integers."
  token_budget = 2000000; integration_validation = @($check)
  modules = @(@{key = "calc"; objective = "Implement add(a, b) in calc.py so it returns a + b."; paths = @("calc.py"); validation = @($check)})
  runtime_policy = @{provider = "codex"; master = @{provider = "codex"; model = $model}
    modules = @{calc = @{provider = "codex"; model = $model}}
    planning = $false; parallel_dependents = "off"; master_probes = $false}}
# JSON has no byte-order mark, and Windows PowerShell 5.1's Set-Content -Encoding utf8 writes one.
[System.IO.File]::WriteAllText("$smoke\handoff.json", ($handoff | ConvertTo-Json -Depth 6))
py -m master_agent_runtime --state $HOME\mas-state init "$smoke\handoff.json"
py -m master_agent_runtime --state $HOME\mas-state run PROJECT_ID
```

Expect `awaiting_acceptance` and a report under `$HOME\mas-state\projects\PROJECT_ID\reports\`; the report's accept command is quoted for PowerShell. Accepting is the authorizer's decision.

## What to report

- The commit, and the environment from step 1.
- `doctor.json`, and the evidence folder it names.
- `unittest.log`, `pilots.log` and `gate.log`.
- For any failure: the full traceback, the Codex sandbox mode, and whether the terminal ran inside an agent's sandbox.
- For step 6, if run: `run`'s JSON output, the report, and the `health` output.
