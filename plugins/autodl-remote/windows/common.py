"""Shared path/serialization helpers. Python 3.9+, stdlib only."""
import base64
import json
import ntpath
import re
import subprocess

_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)


def component(value):
    if (not value or value in ('.', '..') or value[-1] in '. ' or
            re.search(r'[<>:"|?*\x00-\x1f/\\]', value) or _RESERVED.match(value)):
        raise ValueError(f'Invalid Windows path component: {value!r}')
    return value


def relative(value, *, internal=False):
    value = str(value).replace('\\', '/')
    if value == '.':
        return '.'
    if not value or value.startswith('/') or ntpath.splitdrive(value)[0]:
        raise ValueError('Expected a project-relative Windows path')
    parts = value.split('/')
    for part in parts:
        component(part)
    if not internal and parts[0].lower() in ('.autodl-remote', '.autodl-remote-windows.json', '.git'):
        raise ValueError('Reserved project path')
    return '/'.join(parts)


def windows_root(value):
    value = str(value).replace('\\', '/').rstrip('/')
    if not re.match(r'^[A-Za-z]:/', value):
        raise ValueError('Use an absolute drive-letter path, for example D:/GPUProjects/demo')
    parts = value[3:].split('/')
    for part in parts:
        component(part)
    return value[0].upper() + value[1:]


def job_name(value):
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}', value):
        raise ValueError('Invalid job name (1-80 safe ASCII characters required)')
    return component(value)


def encode(value):
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode('utf-8')).decode('ascii')


def decode(value):
    return json.loads(base64.b64decode(value, validate=True).decode('utf-8'))


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def ps_command(script):
    prefix = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $OutputEncoding=[Console]::OutputEncoding; "
    script = prefix + 'try { ' + script + ' } catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }'
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return 'powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + encoded


def native_command(argv):
    return subprocess.list2cmdline([str(arg) for arg in argv])
