# Native Windows backend (preview)

Upstream baseline: AutoDL Remote 0.9.0, commit `578a1759580b37360defcb4a41e373d1649a523f`.

The local agent continues to own the project and MCP session. A project marker
selects a native Windows backend; other projects continue through the original Bash CLI.
No remote Codex, WSL, Bash, tmux, Python packages, or daemon is installed.
The explicitly invoked `init` command uploads two small versioned Python scripts into
`<remote root>/.autodl-remote/windows/worker-<hash>/` and verifies their SHA-256.

## Prerequisites

- Local Python 3.9+ and OpenSSH `ssh`/`sftp`; Node.js for MCP.
- Remote native Windows OpenSSH Server, Windows PowerShell 5.1, Python 3.9+,
  and Task Scheduler for detached jobs. Use an existing non-administrator experiment account.
- TCP connectivity and key/SSH-agent authentication. Verify the host fingerprint through a trusted
  channel before accepting it. The backend uses `StrictHostKeyChecking=yes` and `BatchMode=yes`.
- An existing project directory on a local NTFS drive (for example `D:/GPUProjects/demo`),
  and the absolute path of the remote Python executable. The remote account needs write access to
  this project. The task account must have permission to register and run its own S4U scheduled tasks.
- Confirm GPU/PyTorch compatibility under both SSH and the scheduled-task account before training.
  S4U tasks may have different environment variables and no network-share access.

## Local Windows CLI

From the **local project** (not the plugin repository):

```powershell
$cli = 'E:\Dev\AutoDL-Remote\plugins\autodl-remote\windows\controller.py'
python $cli bind --target gpu4090 --remote 'D:/GPUProjects/demo' --python 'D:/Envs/torch/python.exe'
python $cli doctor
python $cli init
python $cli put src
python $cli exec -- python -c "import torch; print(torch.cuda.is_available())"
python $cli run submit --name seed-01 --model demo --tag seed=1 -- python src/train.py --seed 1
python $cli job status seed-01
python $cli job tail --lines 80 seed-01
python $cli get outputs/metrics.json collected/seed-01/metrics.json
```

Replace all example paths and alias with verified values. `bind` writes only the local marker,
refuses overwrites, and does not contact the server. Add `.autodl-remote-windows.json` to the
local project's `.gitignore`; it contains connection metadata, not credentials. `init` requires
an already-existing remote root and uses the native Win32-OpenSSH SFTP drive-path form `/D:/...`.
`--key` and `--port` are optional at bind time. SSH config aliases can supply authentication.

The `autodl-remote.cmd` beside the controller is an optional native Windows wrapper. The Bash CLI
also supports `autodl-remote windows bind ...` and automatically dispatches project-scoped
commands based on the nearest marker. On native Windows the MCP server invokes Python directly.
Set `AUTODL_REMOTE_WINDOWS_PYTHON` to an explicit **local** interpreter path if required.

## MCP

Use the existing plugin MCP server (`node .../plugins/autodl-remote/mcp/server.mjs`).
New tools:

- `autodl_windows_bind`: local project root, SSH target, remote root, remote python.exe, optional port/key.
- `autodl_windows_init`: explicit versioned worker upload.

The following existing tools transparently select the Windows backend for a Windows-bound project:
`autodl_doctor`, `autodl_model_dir`, `autodl_file`, `autodl_transfer`, `autodl_exec`,
`autodl_job`, and `autodl_run`. Account tools remain the original Linux/global interface.
The MCP result envelope (stdout/stderr/exitCode) is unchanged; Windows worker job/list output is JSON.

## Semantics and boundaries

- Python aliases use the configured remote interpreter. Structured argv is encoded as UTF-8 JSON and
  executed with Python `subprocess` and `shell=False`; quote, space, Unicode and empty arguments
  are retained. Commands exceeding the safe CMD encoded-command length are rejected; upload scripts
  instead. Default allowed executables are the configured Python and nvidia-smi; other executables
  require the existing explicit dangerous-command approval flag. This is **not a sandbox**: Python
  itself can execute arbitrary code. The OS account and ACLs are the security boundary.
- Explicit file/directory transfer uses SFTP without deletion. Windows path checks reject drive
  escapes, reserved names, reparse points, and project metadata paths. No whole-root uploads or
  downloads; 5000-entry transfer limit; local downloads refuse existing files unless direct CLI
  `--overwrite` is supplied. `put --dry-run` plans locally; `get --dry-run` queries the manifest.
  Transfers are per-file (not rsync delta sync) and not transactional; do not perform concurrent
  writes to the same destination. This guard is not protection against a hostile concurrent user.
- Detached jobs register a uniquely named, manual-start Task Scheduler task under the authenticated
  user, using S4U/Limited. The wrapper must join a kill-on-close Windows Job Object before starting
  any child; failure is recorded and no child is started. Output, metadata and exit status reside in
  `.autodl-remote/windows/runs/<name>/`. Duplicate names are rejected. Cancellation targets the exact
  scheduled task, checks its state, and verifies the wrapper has exited before recording cancellation.
  Successful task submission does not assert that training has started or that GPU is available.
  Completed task definitions and logs remain for inspection; no automatic deletion or global cleanup.
- `fleet`, `dashboard`, `tmux`, `shutdown`, `--replace`, `--delete`, `--follow`, `--stdin`, `--script`,
  per-call account/root override, and native Windows global account management are not implemented in
  this preview. These fail explicitly for Windows projects; Linux projects retain upstream behavior.
- Windows project and Linux project markers at the same directory are treated as a conflict. Unset
  `AUTODL_REMOTE_CONFIG` before Windows operations. UNC/network shares, IPv6 literals, reparse-point
  roots and paths with SFTP glob metacharacters are excluded from this preview.

## Verification

Run locally in the patched repository:

```powershell
python tests/test-windows-remote.py
node --test tests/test-windows-routing.mjs
node --test tests/test-autodl-remote-mcp.mjs
```

Run the upstream Bash tests in a supported Bash environment:

```bash
bash tests/test-autodl-remote.sh
```

Live acceptance is mandatory on the actual Windows GPU server:

1. Verify TCP/SSH fingerprint/key login, then `doctor` reports the correct host, Python and GPU.
2. Run `init`, upload an explicitly selected test file, read it back and download to a new path;
   compare local SHA-256, including a path containing spaces/Unicode. Verify rejected traversal.
3. Run a foreground GPU tensor smoke test and a Python command that deliberately exits 7. Check the
   actual exit code in the CLI/MCP result.
4. Submit a small named task using `examples/windows-smoke.py` (copy the file into the bound local
   project's `smoke/` directory and `put smoke/windows-smoke.py`). Disconnect/reconnect SSH while it
   runs; verify output, status, final exit code and GPU use under the scheduler account.
5. Submit a second task with `--child --seconds 120`; record the child PID and wrapper status. Stop
   the named job using `job stop`, then inspect the specific child PID from a separate session and
   verify no training subprocess remains. Do not kill by process name. Verify a completed job cannot
   be relabelled as cancelled.
6. Run two different jobs with unique names; verify independent state/logs. Verify duplicate names
   are rejected and that a cancelled/failed task is not treated as successful.

The package's Linux-hosted tests exercise the protocol mapping and mocked scheduler logic; they do
not prove native Windows SFTP, Task Scheduler privileges, Windows Job Object behavior or GPU access.
Treat the backend as a preview until these checks pass.
