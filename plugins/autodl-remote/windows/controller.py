"""Native Windows remote backend controller, callable from Linux/macOS/Windows."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from common import encode, job_name, ps_command, ps_quote, relative, windows_root

MARKER = '.autodl-remote-windows.json'
HERE = Path(__file__).resolve().parent
FILES = ('worker.py', 'common.py')
VERSION = 'win-preview-0.1.0'


def find_binding(start=None):
    here = Path(start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        win = directory / MARKER
        linux = directory / '.autodl-remote.conf'
        if win.exists() and linux.exists():
            raise ValueError('Conflicting Linux and Windows project bindings')
        if win.is_file():
            if os.environ.get('AUTODL_REMOTE_CONFIG'):
                raise ValueError('Unset AUTODL_REMOTE_CONFIG before using the Windows binding')
            return win
        if linux.is_file():
            return None
    return None


def absolute_local(root, value, *, must_exist=False):
    p = root
    rel = relative(value)
    if rel != '.':
        for part in rel.split('/'):
            p = p / part
            try:
                info = p.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError(f'Local symlink/reparse point is not supported: {p}')
    if os.path.commonpath([str(root), str(p.resolve())]) != str(root):
        raise ValueError('Local path escapes project root')
    if must_exist and not p.exists():
        raise ValueError(f'Local path does not exist: {p}')
    return p


def ssh_target(value):
    # SSH config aliases, DNS names, IPv4 and optional user. Deliberately excludes options/IPv6 for v1.
    if not re.fullmatch(r'(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise ValueError('Use a safe SSH host alias or user@host')
    return value


def batch_quote(path):
    path = str(path).replace('\\', '/')
    if any(ch in path for ch in '\r\n"[]*?\x00'):
        raise ValueError('SFTP paths containing quotes, glob characters or control characters are unsupported')
    return '"' + path + '"'


def sftp_remote(root, rel):
    # Native Win32-OpenSSH SFTP absolute drive paths are /D:/... .
    return '/' + root + ('/' + rel if rel != '.' else '')


def script_version():
    h = hashlib.sha256()
    for name in FILES:
        h.update(name.encode()); h.update((HERE / name).read_bytes())
    return h.hexdigest()[:20]


def write_new(path, data):
    with path.open('x', encoding='utf-8') as out:
        json.dump(data, out, indent=2, ensure_ascii=False); out.write('\n')


def call(args, *, data=None, capture=False):
    kwargs = {'check': True}
    if data is not None:
        kwargs['input'] = data.encode('utf-8')
    if capture:
        kwargs['stdout'] = subprocess.PIPE
    result = subprocess.run(args, **kwargs)
    return result.stdout.decode('utf-8-sig', errors='strict').strip() if capture else ''


class Client:
    def __init__(self, binding):
        self.project = binding.parent.resolve()
        self.config = json.loads(binding.read_text(encoding='utf-8-sig'))
        if self.config.get('backend') != 'windows':
            raise ValueError('Invalid Windows binding')
        self.root = windows_root(self.config['root'])
        self.target = ssh_target(self.config['target'])
        self.port = int(self.config.get('port', 22))
        if not 1 <= self.port <= 65535:
            raise ValueError('Invalid SSH port')
        self.key = self.config.get('key')
        self.python = self.config['python']
        if not re.match(r'^[A-Za-z]:[\\/]', self.python) or not self.python.lower().endswith('.exe'):
            raise ValueError('Configure an absolute remote python.exe path')
        self.python = windows_root(self.python)
        self.worker_dir = '.autodl-remote/windows/worker-' + script_version()
        self.worker = self.root + '/' + self.worker_dir + '/worker.py'

    def transport(self, sftp=False):
        binary = shutil.which('sftp' if sftp else 'ssh')
        if binary is None:
            raise RuntimeError('OpenSSH client executable not found')
        args = [binary, '-P' if sftp else '-p', str(self.port),
                '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=30',
                '-o', 'ServerAliveCountMax=3']
        if self.key:
            key = str(Path(self.key).expanduser().resolve(strict=True))
            args += ['-i', key, '-o', 'IdentitiesOnly=yes']
        if sftp:
            args += ['-b', '-']
        else:
            args += ['-T']
        return args + [self.target]

    def ps(self, script, capture=False):
        command = ps_command(script)
        if len(command) > 7500:
            raise ValueError('Encoded command exceeds safe Windows CMD length; use an uploaded script instead')
        return call(self.transport() + [command], capture=capture)

    def sftp(self, commands):
        call(self.transport(sftp=True), data='\n'.join(commands) + '\n')

    def request(self, op, *, capture=False, **payload):
        req = encode({'op': op, 'root': self.root, **payload})
        # All user argv is base64 JSON, decoded by Python, never by cmd.exe/PowerShell.
        script = f"& {ps_quote(self.python)} -E -s -B -X utf8 {ps_quote(self.worker)} {ps_quote(req)}; exit $LASTEXITCODE"
        return self.ps(script, capture=capture)

    def request_json(self, op, **payload):
        return json.loads(self.request(op, capture=True, **payload))

    def doctor(self):
        script = f"""
        'host='+$env:COMPUTERNAME
        'remote_root='+{ps_quote(self.root)}
        if (-not (Test-Path -LiteralPath {ps_quote(self.root)} -PathType Container)) {{ throw 'Remote project root is missing' }}
        if (-not (Test-Path -LiteralPath {ps_quote(self.python)} -PathType Leaf)) {{ throw 'Configured python.exe is missing' }}
        & {ps_quote(self.python)} --version
        if ($LASTEXITCODE -ne 0) {{ throw 'Python failed' }}
        if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {{ nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv,noheader; if ($LASTEXITCODE -ne 0) {{ throw 'nvidia-smi failed' }} }} else {{ 'gpu=unavailable' }}
        & {ps_quote(self.python)} -c 'import torch; print(torch.cuda.is_available())'
        if ($LASTEXITCODE -ne 0) {{ 'pytorch=unavailable' }}
        'task_scheduler='+[string](Get-Service Schedule).Status
        'worker_installed='+[string](Test-Path -LiteralPath {ps_quote(self.worker)} -PathType Leaf)
        """
        self.ps(script)

    def init(self):
        # Validate all existing ancestors (including the project root) before creating anything.
        root = self.root
        directory = root + '/' + self.worker_dir
        script = f"""
        $root=Get-Item -LiteralPath {ps_quote(root)} -ErrorAction Stop
        if (-not $root.PSIsContainer) {{ throw 'Project root must be an existing directory' }}
        $cur=$root
        while ($null -ne $cur) {{ if ($cur.Attributes -band [IO.FileAttributes]::ReparsePoint) {{ throw 'Root contains a reparse point' }}; $cur=$cur.Parent }}
        $p=$root.FullName
        foreach ($part in @('.autodl-remote','windows',{ps_quote(self.worker_dir.split('/')[-1])})) {{
          $p=Join-Path $p $part
          if (-not (Test-Path -LiteralPath $p)) {{ [IO.Directory]::CreateDirectory($p) | Out-Null }}
          $item=Get-Item -LiteralPath $p -ErrorAction Stop
          if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {{ throw 'Worker path is not a regular directory' }}
        }}
        """
        self.ps(script)
        for name in FILES:
            dest = directory + '/' + name
            expected = hashlib.sha256((HERE / name).read_bytes()).hexdigest()
            check = f"$p={ps_quote(dest)}; if (Test-Path -LiteralPath $p) {{ $i=Get-Item -LiteralPath $p; if ($i.PSIsContainer -or ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)) {{ throw 'Invalid worker destination' }}; (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() }} else {{ 'missing' }}"
            result = self.ps(check, capture=True)
            if result != expected:
                if result != 'missing':
                    raise RuntimeError('Existing versioned worker has an unexpected hash; refusing overwrite')
                self.sftp(['put ' + batch_quote(HERE / name) + ' ' + batch_quote('/' + dest)])
                if self.ps(check, capture=True) != expected:
                    raise RuntimeError('Worker upload hash mismatch')
        print('worker=' + self.worker)

    def upload(self, local, remote=None, dry=False):
        source = absolute_local(self.project, local, must_exist=True)
        rel = relative(remote or local)
        if source == self.project or rel == '.':
            raise ValueError('Select explicit project files/directories, not the whole project root')
        entries = []
        def walk(p, dst):
            absolute_local(self.project, p.relative_to(self.project).as_posix(), must_exist=True)
            relative(dst)
            if p.is_dir():
                entries.append(('dir', p, dst))
                for child in sorted(p.iterdir()):
                    walk(child, dst + '/' + child.name)
            elif p.is_file():
                entries.append(('file', p, dst))
            else:
                raise ValueError(f'Unsupported local entry: {p}')
            if len(entries) > 5000:
                raise ValueError('Transfer exceeds 5000 entries; narrow selection')
        walk(source, rel)
        for kind, p, dst in entries:
            if kind == 'file':
                batch_quote(p); batch_quote(sftp_remote(self.root, dst))
        if dry:
            print(json.dumps([{'type': kind, 'local': str(p), 'remote': dst} for kind,p,dst in entries], ensure_ascii=False)); return
        for kind, p, dst in entries:
            if kind == 'dir':
                self.request('mkdir', path=dst)
            else:
                self.request('prepare', path=dst)
                self.sftp(['put ' + batch_quote(p) + ' ' + batch_quote(sftp_remote(self.root, dst))])

    def download(self, remote, local=None, dry=False, overwrite=False):
        rel = relative(remote)
        dest = relative(local or remote)
        if rel == '.' or dest == '.':
            raise ValueError('Select explicit project files/directories')
        manifest = self.request_json('manifest', path=rel, depth=100, limit=5000)
        if not isinstance(manifest, list) or not manifest:
            raise ValueError('Empty/invalid remote manifest')
        entries = []
        for entry in manifest:
            rp = relative(entry['path'])
            if rp != rel and not rp.startswith(rel + '/'):
                raise ValueError('Manifest path escaped requested subtree')
            lp = absolute_local(self.project, dest + rp[len(rel):])
            if entry['type'] == 'file':
                if lp.exists() and (not overwrite or not lp.is_file()):
                    raise ValueError(f'Local destination already exists: {lp}; use --overwrite for files')
                batch_quote(lp); batch_quote(sftp_remote(self.root, rp))
            elif entry['type'] != 'dir' or (lp.exists() and not lp.is_dir()):
                raise ValueError('Invalid local directory destination')
            entries.append((entry['type'], rp, lp))
        if dry:
            print(json.dumps([{'type': k,'remote':r,'local':str(p)} for k,r,p in entries], ensure_ascii=False)); return
        for kind, rp, lp in entries:
            if kind == 'dir':
                lp.mkdir(parents=True, exist_ok=True)
            else:
                lp.parent.mkdir(parents=True, exist_ok=True)
                self.sftp(['get ' + batch_quote(sftp_remote(self.root, rp)) + ' ' + batch_quote(lp)])


def parser():
    p = argparse.ArgumentParser(prog='autodl-remote windows', description='Windows native backend (preview)')
    sub = p.add_subparsers(dest='command', required=True)
    b = sub.add_parser('bind'); b.add_argument('--target', required=True); b.add_argument('--remote', required=True)
    b.add_argument('--python', required=True); b.add_argument('--port', type=int, default=22); b.add_argument('--key')
    for cmd in ('doctor','init','version'):
        sub.add_parser(cmd)
    m = sub.add_parser('model-dir'); m.add_argument('--mkdir', action='store_true')
    def exec_args(q):
        q.add_argument('--detach', action='store_true'); q.add_argument('--cwd', default='.')
        q.add_argument('--name'); q.add_argument('--model'); q.add_argument('--tag', action='append', default=[])
        q.add_argument('--stage'); q.add_argument('--purpose'); q.add_argument('--output', action='append', default=[])
        q.add_argument('--allow-dangerous', action='store_true'); q.add_argument('argv', nargs=argparse.REMAINDER)
    exec_args(sub.add_parser('exec'))
    for cmd in ('put','sync-up','get','sync-down'):
        q = sub.add_parser(cmd); q.add_argument('--dry-run', action='store_true'); q.add_argument('--overwrite', action='store_true'); q.add_argument('paths', nargs='+')
    for cmd in ('ls','tree','cat','tail'):
        q = sub.add_parser(cmd); q.add_argument('--lines', type=int, default=200 if cmd=='cat' else 120)
        q.add_argument('--depth', type=int, default=3); q.add_argument('--limit', type=int, default=500); q.add_argument('path', nargs='?', default='.')
    def job_sub(q, run=False):
        a=q.add_subparsers(dest='action', required=True)
        a.add_parser('list')
        for name in ('status','stop','tail'):
            x=a.add_parser(name); x.add_argument('name'); x.add_argument('--lines', type=int, default=120)
        if run:
            s=a.add_parser('submit'); s.add_argument('--sync-up', action='append', default=[]); exec_args(s)
            n=a.add_parser('note'); n.add_argument('name'); n.add_argument('--model'); n.add_argument('--tag', action='append',default=[]); n.add_argument('--stage'); n.add_argument('--purpose'); n.add_argument('--output',action='append',default=[])
    job_sub(sub.add_parser('job')); job_sub(sub.add_parser('run'), True)
    return p


def metadata(args):
    return {k:getattr(args,k) for k in ('model','tag','stage','purpose','output') if getattr(args,k,None)}


def main(argv=None):
    args=parser().parse_args(argv)
    cmd=args.command
    if cmd=='version':
        print(VERSION); return 0
    if cmd=='bind':
        root=Path.cwd().resolve()
        if (root/'.autodl-remote.conf').exists():
            raise ValueError('A Linux binding already exists; use a separate project workspace')
        target=ssh_target(args.target); remote=windows_root(args.remote); python=windows_root(args.python)
        if not python.lower().endswith('.exe') or not 1<=args.port<=65535:
            raise ValueError('Invalid remote python.exe or port')
        key=str(Path(args.key).expanduser().resolve(strict=True)) if args.key else None
        write_new(root/MARKER, {'backend':'windows','target':target,'port':args.port,'root':remote,'python':python,'key':key})
        print('Bound '+str(root)+' to '+target+':'+remote); return 0
    binding=find_binding()
    if not binding:
        raise ValueError('No Windows binding; run windows bind first')
    client=Client(binding)
    if cmd in ('doctor','init'):
        getattr(client,cmd)()
    elif cmd=='model-dir':
        if args.mkdir: client.request('mkdir',path='models')
        print(client.root+'/models')
    elif cmd=='exec' or (cmd=='run' and args.action=='submit'):
        argv=list(args.argv)
        if argv[:1]==['--']: argv=argv[1:]
        if not argv: raise ValueError('Command argv required after --')
        if args.name: job_name(args.name)
        cwd=relative(args.cwd)
        for upload in getattr(args,'sync_up',[]): client.upload(upload)
        client.request('submit' if args.detach or cmd=='run' else 'exec',argv=argv,cwd=cwd,name=args.name,allow=args.allow_dangerous,metadata=metadata(args))
    elif cmd in ('put','sync-up','get','sync-down'):
        if len(args.paths)>2: raise ValueError('Expected one source and optional destination')
        if cmd in ('put','sync-up'):
            client.upload(*args.paths,dry=args.dry_run)
        else:
            client.download(*args.paths,dry=args.dry_run,overwrite=args.overwrite)
    elif cmd in ('ls','tree','cat','tail'):
        if not 0<=args.depth<=20 or not 1<=args.limit<=5000 or not 0<=args.lines<=5000:
            raise ValueError('Invalid depth/limit/lines')
        client.request(cmd,path=relative(args.path),lines=args.lines,depth=args.depth if cmd=='tree' else 1,limit=args.limit)
    elif cmd in ('job','run'):
        if args.action=='list': client.request('jobs')
        elif args.action=='note': client.request('note',name=job_name(args.name),metadata=metadata(args))
        else:
            name=job_name(args.name)
            if args.action=='tail' and not 0<=args.lines<=5000: raise ValueError('Invalid line count')
            client.request('job_tail' if args.action=='tail' else args.action,name=name,lines=args.lines)
    else:
        raise ValueError('Unsupported command')
    return 0


if __name__=='__main__':
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        print(f'Transport/remote command failed (exit {e.returncode})',file=sys.stderr)
        sys.exit(e.returncode or 1)
    except (ValueError, RuntimeError, OSError) as e:
        print(f'autodl-remote windows: {e}',file=sys.stderr); sys.exit(2)
