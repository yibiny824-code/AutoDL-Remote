// Windows routing for the upstream MCP server. Linux invocation is unchanged.
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const controller = path.join(path.dirname(fileURLToPath(import.meta.url)), 'controller.py');

export function isWindowsProject(cwd) {
  let dir = path.resolve(cwd);
  for (;;) {
    // Windows wins for same-directory conflict so the Python controller can fail closed.
    if (existsSync(path.join(dir, '.autodl-remote-windows.json'))) return true;
    if (existsSync(path.join(dir, '.autodl-remote.conf'))) return false;
    const parent = path.dirname(dir);
    if (parent === dir) return false;
    dir = parent;
  }
}

export function selectBackend(args, cwd, original) {
  const explicit = args[0] === 'windows';
  if (explicit || (args[0] !== 'account' && isWindowsProject(cwd))) {
    return {
      executable: process.env.AUTODL_REMOTE_WINDOWS_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'),
      args: [controller, ...(explicit ? args.slice(1) : args)],
    };
  }
  return { executable: original, args };
}
