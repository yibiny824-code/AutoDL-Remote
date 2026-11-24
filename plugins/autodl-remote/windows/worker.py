"""Project-scoped Windows worker: no daemon, no Codex, no pip packages."""
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from common import decode, job_name, native_command, ps_command, ps_quote, relative

FINAL = {'exited', 'failed', 'cancelled', 'submission_failed'}


def now():
    return datetime.now(timezone.utc).isoformat()


def emit(value):
    print(json.dumps(value, ensure_ascii=False))


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic_json(path, data):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def reject_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError(f'Reparse point/symlink is not supported: {path}')


def root_path(value):
    root = Path(value)
    if not root.is_absolute() or not root.is_dir() or root == Path(root.anchor):
        raise ValueError('Project root must be an existing non-drive-root directory')
    for part in reversed((root, *root.parents)):
        reject_link(part)
    return root.resolve(strict=True)


def under(root, value, *, internal=False):
    rel = relative(value, internal=internal)
    p = root
    if rel != '.':
        for part in rel.split('/'):
            p = p / part
            reject_link(p)
    if os.path.commonpath([str(root), str(p.resolve())]) != str(root):
        raise ValueError('Path escapes project root')
    return p


def internal(root, value):
    return under(root, '.autodl-remote/windows/' + value, internal=True)


def ps(script, *, capture=True):
    # The command contains only fixed syntax and literal-quoted data; no shell=True.
    encoded = ps_command(script).split()[-1]
    result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                             '-EncodedCommand', encoded], capture_output=capture,
                            text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'PowerShell failed').strip())
    return (result.stdout or '').strip()


def assign_kill_on_close_job():
    """Assign this wrapper to a non-breakaway Job Object BEFORE spawning a child.
    The wrapper owns the non-inheritable handle. Closing it kills remaining members.
    Failure is fatal; we never silently fall back to a detached unmanaged process.
    """
    from ctypes import wintypes as W
    class BASIC(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                    ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', W.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', W.DWORD), ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', W.DWORD), ('SchedulingClass', W.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(key, ctypes.c_ulonglong) for key in
                    ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                     'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
    class EXT(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, W.LPCWSTR]
    k.CreateJobObjectW.restype = W.HANDLE
    k.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
    k.SetInformationJobObject.restype = W.BOOL
    k.GetCurrentProcess.restype = W.HANDLE
    k.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    k.AssignProcessToJobObject.restype = W.BOOL
    k.CloseHandle.argtypes = [W.HANDLE]
    handle = k.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = EXT()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error(); k.CloseHandle(handle); raise ctypes.WinError(error)
    if not k.AssignProcessToJobObject(handle, k.GetCurrentProcess()):
        error = ctypes.get_last_error(); k.CloseHandle(handle); raise ctypes.WinError(error)
    # Do not close before process exit. OS closes the handle at wrapper termination.
    return handle


def resolve_argv(argv, allow=False):
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or '\x00' in x for x in argv):
        raise ValueError('Invalid command argv')
    executable = argv[0].lower().replace('\\', '/')
    if executable in ('python', 'python.exe', 'python3', 'python3.exe') or os.path.normcase(argv[0]) == os.path.normcase(sys.executable):
        return [sys.executable, *argv[1:]]
    if executable in ('nvidia-smi', 'nvidia-smi.exe'):
        return argv
    if not allow:
        raise ValueError('This backend permits the configured Python and nvidia-smi by default. Other executables require --allow-dangerous.')
    return argv


def job_directory(root, name):
    return internal(root, 'runs/' + job_name(name))


def job_state(directory):
    return read_json(directory / 'state.json') if (directory / 'state.json').is_file() else {'status': 'unknown'}


def task_status(task):
    if not re.fullmatch(r'AutoDLRemote-[0-9a-f]{32}', task):
        raise ValueError('Invalid managed task name')
    return ps(f"$t=Get-ScheduledTask -TaskPath '\\' -TaskName {ps_quote(task)} -ErrorAction SilentlyContinue; if ($null -eq $t) {{ 'Missing' }} else {{ [string]$t.State }}")


def job_details(root, name):
    directory = job_directory(root, name)
    spec = read_json(directory / 'spec.json')
    state = job_state(directory)
    if state.get('status') not in FINAL:
        task = task_status(spec['task'])
        state = dict(state, task_state=task)
        if state.get('status') == 'running' and task not in ('Running', 'Queued'):
            state['status'] = 'lost'
        if state.get('status') == 'queued' and task == 'Missing':
            state['status'] = 'lost'
    return dict(state, name=name, task=spec['task'], log=str(directory / 'stdout.log'),
                backend='task_scheduler', argv=spec['argv'], metadata=spec.get('metadata', {}))


def submit(root, request):
    name = job_name(request.get('name') or 'job-' + uuid.uuid4().hex[:12])
    cwd = under(root, request.get('cwd', '.'))
    if not cwd.is_dir():
        raise ValueError('Remote cwd does not exist')
    argv = resolve_argv(request['argv'], request.get('allow', False))
    directory = job_directory(root, name)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(exist_ok=False)  # Atomic claim; no --replace in v1.
    task = 'AutoDLRemote-' + uuid.uuid4().hex
    spec = {'name': name, 'task': task, 'root': str(root), 'cwd': str(cwd), 'argv': argv,
            'metadata': request.get('metadata', {})}
    spec_path = directory / 'spec.json'
    atomic_json(spec_path, spec)
    atomic_json(directory / 'state.json', {'status': 'queued', 'submitted_at': now()})
    worker = str(Path(__file__).resolve())
    argument = native_command(['-E', '-s', '-B', '-X', 'utf8', worker, '--job-run', str(spec_path)])
    script = f"""
    $principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType S4U -RunLevel Limited
    $action=New-ScheduledTaskAction -Execute {ps_quote(sys.executable)} -Argument {ps_quote(argument)} -WorkingDirectory {ps_quote(str(cwd))}
    $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskPath '\\' -TaskName {ps_quote(task)} -Action $action -Principal $principal -Settings $settings | Out-Null
    Start-ScheduledTask -TaskPath '\\' -TaskName {ps_quote(task)}
    """
    try:
        ps(script)
    except Exception as error:
        atomic_json(directory / 'state.json', {'status': 'submission_failed', 'finished_at': now(), 'error': str(error)})
        raise
    emit({'status': 'submitted', 'name': name, 'task': task, 'log': str(directory / 'stdout.log')})


def run_job(spec_path):
    spec_path = Path(spec_path)
    spec = read_json(spec_path)
    root = root_path(spec['root'])
    directory = job_directory(root, spec['name'])
    if spec_path.resolve() != (directory / 'spec.json').resolve():
        raise ValueError('Spec path mismatch')
    state_path = directory / 'state.json'
    started = now()
    try:
        # The handle remains open until this wrapper exits (including forced stop).
        job_handle = assign_kill_on_close_job()
        atomic_json(state_path, {'status': 'running', 'pid': os.getpid(), 'started_at': started})
        with (directory / 'stdout.log').open('wb') as log:
            rc = subprocess.call(spec['argv'], cwd=spec['cwd'], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, shell=False,
                                 env={**os.environ, 'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'})
        atomic_json(state_path, {'status': 'exited' if rc == 0 else 'failed', 'exit_code': rc,
                                 'pid': os.getpid(), 'started_at': started, 'finished_at': now()})
        return rc
    except Exception as error:
        atomic_json(state_path, {'status': 'failed', 'exit_code': 1, 'error': str(error),
                                 'started_at': started, 'finished_at': now()})
        return 1


def stop_job(root, name):
    directory = job_directory(root, name)
    details = job_details(root, name)
    if details['status'] in FINAL:
        emit(details); return
    task = details['task']
    if details.get('task_state') not in ('Running', 'Queued'):
        raise RuntimeError('Managed task is not running/queued; refusing to mark it cancelled')
    ps(f"Stop-ScheduledTask -TaskPath '\\' -TaskName {ps_quote(task)}")
    deadline = time.monotonic() + 20
    while True:
        task_state = task_status(task)
        if task_state in ('Ready', 'Disabled'):
            break
        if task_state not in ('Running', 'Queued') or time.monotonic() >= deadline:
            raise RuntimeError('Task stop could not be verified')
        time.sleep(.5)
    spec_path = str(directory / 'spec.json')
    # Verify the task-specific wrapper is gone, without killing an arbitrary PID.
    check = f"""
    $spec={ps_quote(spec_path)}
    $running=@(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {{
      $_.CommandLine -and $_.CommandLine.Contains('--job-run') -and
      $_.CommandLine.IndexOf($spec,[StringComparison]::OrdinalIgnoreCase) -ge 0
    }})
    if ($running.Count -gt 0) {{ throw 'Job wrapper remains alive' }}
    'stopped'
    """
    if ps(check) != 'stopped':
        raise RuntimeError('Job wrapper termination was not verified')
    state = job_state(directory)
    if state.get('status') not in FINAL:
        atomic_json(directory / 'state.json', dict(state, status='cancelled', finished_at=now()))
    emit(job_details(root, name))


def bounded_text(path, lines, tail=False):
    if not isinstance(lines, int) or not 0 <= lines <= 5000:
        raise ValueError('lines must be between 0 and 5000')
    if lines == 0:
        return
    cap = 65536
    with path.open('rb') as f:
        if tail:
            f.seek(max(0, path.stat().st_size - cap))
        data = f.read(cap).decode('utf-8', errors='replace').splitlines()
    print('\n'.join(data[-lines:] if tail else data[:lines]))


def manifest(root, path, depth=20, limit=5000, complete=False):
    # No traversal through reparse points. Directory transfer fails on any such entry.
    entries = []
    def walk(p, level):
        if len(entries) >= limit:
            raise ValueError('File listing limit exceeded; narrow the requested path')
        reject_link(p)
        rel = p.relative_to(root).as_posix()
        if rel != '.':
            relative(rel)
        kind = 'dir' if p.is_dir() else 'file' if p.is_file() else 'other'
        if kind == 'other':
            raise ValueError(f'Unsupported file type: {p}')
        entries.append({'path': rel, 'type': kind, 'size': p.stat().st_size if kind == 'file' else 0})
        if kind == 'dir' and complete and level >= depth and any(p.iterdir()):
            raise ValueError('Directory nesting exceeds transfer limit')
        if kind == 'dir' and level < depth:
            for child in sorted(p.iterdir(), key=lambda p:p.name.lower()):
                if p == root and child.name.lower() in ('.autodl-remote', '.git', '.autodl-remote-windows.json'):
                    continue
                walk(child, level + 1)
    walk(path, 0)
    return entries


def dispatch(request):
    root = root_path(request['root'])
    op = request['op']
    if op == 'exec':
        argv = resolve_argv(request['argv'], request.get('allow', False))
        cwd = under(root, request.get('cwd', '.'))
        return subprocess.call(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, shell=False)
    if op == 'submit':
        submit(root, request)
    elif op == 'status':
        emit(job_details(root, request['name']))
    elif op == 'stop':
        stop_job(root, request['name'])
    elif op == 'jobs':
        runs = internal(root, 'runs')
        records = []
        if runs.is_dir():
            for directory in sorted(runs.iterdir()):
                reject_link(directory)
                if (directory / 'spec.json').is_file():
                    try:
                        records.append(job_details(root, directory.name))
                    except Exception as e:
                        records.append({'name': directory.name, 'status': 'unknown', 'error': str(e)})
        emit(records)
    elif op == 'job_tail':
        bounded_text(job_directory(root, request['name']) / 'stdout.log', request['lines'], True)
    elif op == 'note':
        spec_path = job_directory(root, request['name']) / 'spec.json'
        spec = read_json(spec_path)
        spec.setdefault('metadata', {}).update(request['metadata'])
        atomic_json(spec_path, spec)
        emit(spec['metadata'])
    elif op == 'mkdir':
        under(root, request['path']).mkdir(parents=True, exist_ok=True)
    elif op == 'prepare':
        p = under(root, request['path'])
        if p.exists() and not p.is_file():
            raise ValueError('Upload destination is not a regular file')
        p.parent.mkdir(parents=True, exist_ok=True)
    elif op in ('ls', 'tree', 'manifest'):
        p = under(root, request.get('path', '.'))
        emit(manifest(root, p, request.get('depth', 1), request.get('limit', 5000), complete=(op == 'manifest')))
    elif op in ('cat', 'tail'):
        bounded_text(under(root, request['path']), request['lines'], op == 'tail')
    else:
        raise ValueError(f'Unsupported operation: {op}')
    return 0


if __name__ == '__main__':
    try:
        if os.name != 'nt':
            raise RuntimeError('Worker requires native Windows')
        if len(sys.argv) == 3 and sys.argv[1] == '--job-run':
            sys.exit(run_job(sys.argv[2]))
        if len(sys.argv) != 2:
            raise ValueError('Expected one encoded request')
        sys.exit(dispatch(decode(sys.argv[1])))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
