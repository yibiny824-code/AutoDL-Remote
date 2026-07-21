import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, realpath, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import readline from "node:readline";
import test from "node:test";

const repoRoot = path.resolve(import.meta.dirname, "..");
const serverPath = path.join(
  repoRoot,
  "plugins",
  "autodl-remote",
  "mcp",
  "server.mjs",
);

class McpClient {
  constructor(child) {
    this.child = child;
    this.nextId = 1;
    this.pending = new Map();
    this.stderr = "";

    readline.createInterface({ input: child.stdout }).on("line", (line) => {
      const message = JSON.parse(line);
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
      else pending.resolve(message.result);
    });
    child.stderr.on("data", (chunk) => {
      this.stderr += chunk;
    });
    child.on("exit", (code) => {
      for (const pending of this.pending.values()) {
        pending.reject(
          new Error(`MCP server exited with ${code}: ${this.stderr.trim()}`),
        );
      }
      this.pending.clear();
    });
  }

  request(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.child.stdin.write(
        `${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`,
      );
    });
  }

  close() {
    this.child.stdin.end();
  }
}

async function createRecordingCli(tempDir) {
  const fakeCli = path.join(tempDir, "autodl-remote-recording");
  await writeFile(
    fakeCli,
    [
      "#!/usr/bin/env node",
      "console.log(JSON.stringify({ args: process.argv.slice(2), cwd: process.cwd() }));",
      "",
    ].join("\n"),
  );
  await chmod(fakeCli, 0o755);
  return fakeCli;
}

function startClient(fakeCli, env = {}) {
  const child = spawn(process.execPath, [serverPath], {
    cwd: repoRoot,
    env: { ...process.env, AUTODL_REMOTE_MCP_CLI: fakeCli, ...env },
    stdio: ["pipe", "pipe", "pipe"],
  });
  return new McpClient(child);
}

async function call(client, name, args) {
  const result = await client.request("tools/call", {
    name,
    arguments: args,
  });
  assert.equal(result.isError, false);
  return JSON.parse(result.structuredContent.stdout);
}

test("MCP exposes account list through the existing CLI", async (t) => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "autodl-mcp-"));
  t.after(async () => rm(tempDir, { recursive: true, force: true }));

  const fakeCli = path.join(tempDir, "autodl-remote");
  await writeFile(
    fakeCli,
    "#!/usr/bin/env bash\nset -euo pipefail\nprintf '* gpu-a\\n  gpu-b\\n'\n",
  );
  await chmod(fakeCli, 0o755);

  const child = spawn(process.execPath, [serverPath], {
    cwd: repoRoot,
    env: { ...process.env, AUTODL_REMOTE_MCP_CLI: fakeCli },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const client = new McpClient(child);
  t.after(() => client.close());

  const initialized = await client.request("initialize", {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "test", version: "1.0.0" },
  });
  assert.equal(initialized.serverInfo.version, "0.9.0");

  const listed = await client.request("tools/list");
  assert.ok(listed.tools.some((tool) => tool.name === "autodl_account"));

  const called = await client.request("tools/call", {
    name: "autodl_account",
    arguments: { action: "list" },
  });
  assert.equal(called.isError, false);
  assert.equal(called.structuredContent.ok, true);
  assert.equal(called.structuredContent.command, "account list");
  assert.match(called.structuredContent.stdout, /gpu-a/);
});

test("MCP maps experiment, fleet, tmux, dashboard, and shutdown workflows", async (t) => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "autodl-mcp-workflows-"));
  t.after(async () => rm(tempDir, { recursive: true, force: true }));
  const projectRoot = path.join(tempDir, "project");
  await mkdir(projectRoot);
  const client = startClient(await createRecordingCli(tempDir));
  t.after(() => client.close());

  const run = await call(client, "autodl_run", {
    action: "submit",
    projectRoot,
    argv: ["python", "train.py"],
    syncUp: ["src", "configs/base.yaml"],
    tmux: true,
    name: "baseline-a",
    model: "qwen",
    tags: ["baseline"],
  });
  assert.deepEqual(run.args, [
    "run",
    "submit",
    "--sync-up",
    "src",
    "--sync-up",
    "configs/base.yaml",
    "--tmux",
    "--name",
    "baseline-a",
    "--model",
    "qwen",
    "--tag",
    "baseline",
    "--",
    "python",
    "train.py",
  ]);

  const jobTail = await call(client, "autodl_job", {
    action: "tail",
    projectRoot,
    name: "baseline-a",
    lines: 40,
  });
  assert.deepEqual(jobTail.args, ["job", "tail", "--lines", "40", "baseline-a"]);

  const fleet = await call(client, "autodl_fleet", {
    action: "add",
    projectRoot,
    fleet: "lab",
    device: "gpu-1",
    account: "gpu-a",
    remote: "/root/workspace/a",
    tags: "a100,80g",
  });
  assert.deepEqual(fleet.args, [
    "fleet",
    "add",
    "lab",
    "gpu-1",
    "--account",
    "gpu-a",
    "--remote",
    "/root/workspace/a",
    "--tags",
    "a100,80g",
  ]);

  const captured = await call(client, "autodl_tmux", {
    action: "capture",
    projectRoot,
    session: "baseline-a",
    lines: 25,
  });
  assert.deepEqual(captured.args, [
    "tmux",
    "capture",
    "--lines",
    "25",
    "--",
    "baseline-a",
  ]);

  const dashboard = await call(client, "autodl_dashboard", {
    projectRoot,
    fleet: "lab",
    output: "reports/fleet.html",
    lines: 60,
  });
  assert.deepEqual(dashboard.args, [
    "dashboard",
    "--fleet",
    "lab",
    "--output",
    "reports/fleet.html",
    "--lines",
    "60",
  ]);

  const shutdown = await call(client, "autodl_shutdown", {
    projectRoot,
    confirm: "shutdown",
    waitSeconds: 3,
  });
  assert.deepEqual(shutdown.args, ["shutdown", "--wait", "3"]);
});

test("MCP requires explicit confirmation for destructive operations", async (t) => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "autodl-mcp-safety-"));
  t.after(async () => rm(tempDir, { recursive: true, force: true }));
  const projectRoot = path.join(tempDir, "project");
  await mkdir(projectRoot);
  const client = startClient(await createRecordingCli(tempDir));
  t.after(() => client.close());

  await assert.rejects(
    client.request("tools/call", {
      name: "autodl_job",
      arguments: { action: "stop", projectRoot, name: "train-a", confirm: "train-b" },
    }),
    /exactly match name/,
  );
  await assert.rejects(
    client.request("tools/call", {
      name: "autodl_exec",
      arguments: {
        projectRoot,
        argv: ["shutdown"],
        allowDangerous: true,
      },
    }),
    /confirmDangerous=allow-dangerous/,
  );
  await assert.rejects(
    client.request("tools/call", {
      name: "autodl_transfer",
      arguments: {
        action: "upload",
        projectRoot,
        localPath: "src",
        delete: true,
      },
    }),
    /confirmDelete=delete-remote-extraneous/,
  );
  await assert.rejects(
    client.request("tools/call", {
      name: "autodl_file",
      arguments: { action: "read", projectRoot },
    }),
    /path is required/,
  );
  await assert.rejects(
    client.request("tools/call", {
      name: "autodl_tmux",
      arguments: { action: "install", projectRoot },
    }),
    /confirm=install-tmux/,
  );
  await assert.rejects(
    client.request("tools/call", {
      name: "autodl_shutdown",
      arguments: { projectRoot, confirm: "yes" },
    }),
    /confirm=shutdown/,
  );
});

test("MCP keeps declared file operands inside the selected project", async (t) => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "autodl-mcp-boundary-"));
  t.after(async () => rm(tempDir, { recursive: true, force: true }));
  const projectRoot = path.join(tempDir, "project");
  await mkdir(projectRoot);
  const outside = path.join(tempDir, "outside.txt");
  await writeFile(outside, "secret");
  await symlink(outside, path.join(projectRoot, "outside-link"));
  const client = startClient(await createRecordingCli(tempDir));
  t.after(() => client.close());

  const rejectedCalls = [
    ["autodl_file", { action: "read", projectRoot, path: "/etc/passwd" }],
    ["autodl_file", { action: "tail", projectRoot, path: "../outside.txt" }],
    ["autodl_transfer", { action: "upload", projectRoot, localPath: "/etc/passwd" }],
    ["autodl_transfer", { action: "upload", projectRoot, localPath: "outside-link" }],
    ["autodl_transfer", { action: "download", projectRoot, remotePath: "runs/a", localPath: "../outside.txt" }],
    ["autodl_transfer", { action: "download", projectRoot, remotePath: "/etc/passwd", localPath: "copy" }],
    ["autodl_exec", { projectRoot, cwd: "../outside", argv: ["pwd"] }],
    ["autodl_run", { action: "submit", projectRoot, syncUp: ["../outside.txt"], argv: ["true"] }],
    ["autodl_dashboard", { projectRoot, output: "../outside.html" }],
  ];
  for (const [name, arguments_] of rejectedCalls) {
    await assert.rejects(
      client.request("tools/call", { name, arguments: arguments_ }),
      /project-relative|escape projectRoot/,
    );
  }
});

test("MCP rejects ambiguous names and negotiates a supported protocol version", async (t) => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "autodl-mcp-canonical-"));
  t.after(async () => rm(tempDir, { recursive: true, force: true }));
  const projectRoot = path.join(tempDir, "project");
  await mkdir(projectRoot);
  const client = startClient(await createRecordingCli(tempDir));
  t.after(() => client.close());

  await assert.rejects(
    client.request("tools/call", {
      name: "autodl_job",
      arguments: { action: "stop", projectRoot, name: "train prod", confirm: "train prod" },
    }),
    /canonical name/,
  );
  const initialized = await client.request("initialize", {
    protocolVersion: "2099-01-01",
    capabilities: {},
    clientInfo: { name: "future", version: "1" },
  });
  assert.equal(initialized.protocolVersion, "2025-06-18");
});

test("MCP returns bounded structured output for CLI failures", async (t) => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "autodl-mcp-output-"));
  t.after(async () => rm(tempDir, { recursive: true, force: true }));
  const projectRoot = path.join(tempDir, "project");
  await mkdir(projectRoot);
  const fakeCli = path.join(tempDir, "autodl-remote-failing");
  await writeFile(
    fakeCli,
    [
      "#!/usr/bin/env node",
      "process.stdout.write('A'.repeat(100));",
      "process.stderr.write('B'.repeat(100));",
      "process.exitCode = 7;",
      "",
    ].join("\n"),
  );
  await chmod(fakeCli, 0o755);
  const client = startClient(fakeCli, { AUTODL_REMOTE_MCP_MAX_OUTPUT_BYTES: "16" });
  t.after(() => client.close());

  const result = await client.request("tools/call", {
    name: "autodl_doctor",
    arguments: { projectRoot },
  });
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.ok, false);
  assert.equal(result.structuredContent.exitCode, 7);
  assert.equal(Buffer.byteLength(result.structuredContent.stdout), 16);
  assert.equal(Buffer.byteLength(result.structuredContent.stderr), 16);
  assert.equal(result.structuredContent.stdoutTruncated, true);
  assert.equal(result.structuredContent.stderrTruncated, true);
});

test("MCP drains an in-flight CLI call after stdin reaches EOF", async (t) => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "autodl-mcp-eof-"));
  t.after(async () => rm(tempDir, { recursive: true, force: true }));
  const fakeCli = path.join(tempDir, "autodl-remote-slow");
  await writeFile(
    fakeCli,
    [
      "#!/usr/bin/env node",
      "setTimeout(() => console.log('drained'), 100);",
      "",
    ].join("\n"),
  );
  await chmod(fakeCli, 0o755);

  const child = spawn(process.execPath, [serverPath], {
    cwd: repoRoot,
    env: { ...process.env, AUTODL_REMOTE_MCP_CLI: fakeCli },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const lines = [];
  readline.createInterface({ input: child.stdout }).on("line", (line) => {
    lines.push(JSON.parse(line));
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  child.stdin.end(
    `${JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "autodl_account", arguments: { action: "list" } },
    })}\n`,
  );
  const exitCode = await new Promise((resolve) => child.on("exit", resolve));
  assert.equal(exitCode, 0, stderr);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].result.structuredContent.ok, true);
  assert.match(lines[0].result.structuredContent.stdout, /drained/);
});

test("MCP exits cleanly when the client closes its response stream", async (t) => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "autodl-mcp-epipe-"));
  t.after(async () => rm(tempDir, { recursive: true, force: true }));
  const child = spawn(process.execPath, [serverPath], {
    cwd: repoRoot,
    env: {
      ...process.env,
      AUTODL_REMOTE_MCP_CLI: await createRecordingCli(tempDir),
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  child.stdout.destroy();
  child.stdin.end(
    `${JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "autodl_account", arguments: { action: "list" } },
    })}\n`,
  );
  const exitCode = await new Promise((resolve) => child.on("exit", resolve));
  assert.equal(exitCode, 0, stderr);
  assert.doesNotMatch(stderr, /EPIPE/);
});

test("MCP standardizes common project, file, transfer, and execution commands", async (t) => {
  const tempDir = await mkdtemp(path.join(os.tmpdir(), "autodl-mcp-map-"));
  t.after(async () => rm(tempDir, { recursive: true, force: true }));
  const projectRoot = path.join(tempDir, "project");
  await mkdir(projectRoot);
  const client = startClient(await createRecordingCli(tempDir));
  t.after(() => client.close());

  const listed = await client.request("tools/list");
  assert.deepEqual(
    listed.tools.map((tool) => tool.name),
    [
      "autodl_account",
      "autodl_bind",
      "autodl_doctor",
      "autodl_model_dir",
      "autodl_file",
      "autodl_transfer",
      "autodl_exec",
      "autodl_job",
      "autodl_run",
      "autodl_fleet",
      "autodl_tmux",
      "autodl_dashboard",
      "autodl_shutdown",
    ],
  );

  const bound = await call(client, "autodl_bind", {
    projectRoot,
    account: "gpu-a",
    remote: "/root/workspace/demo",
    force: true,
  });
  assert.deepEqual(bound.args, [
    "bind",
    "--account",
    "gpu-a",
    "--remote",
    "/root/workspace/demo",
    "--force",
  ]);
  assert.equal(bound.cwd, await realpath(projectRoot));

  const read = await call(client, "autodl_file", {
    action: "read",
    projectRoot,
    path: "logs/train.log",
    lines: 80,
    account: "gpu-a",
  });
  assert.deepEqual(read.args, [
    "cat",
    "--account",
    "gpu-a",
    "--lines",
    "80",
    "--",
    "logs/train.log",
  ]);

  const uploaded = await call(client, "autodl_transfer", {
    action: "upload",
    projectRoot,
    localPath: "src",
    remotePath: "src",
    dryRun: true,
    delete: true,
    confirmDelete: "delete-remote-extraneous",
  });
  assert.deepEqual(uploaded.args, [
    "put",
    "--dry-run",
    "--delete",
    "--",
    "src",
    "src",
  ]);

  const executed = await call(client, "autodl_exec", {
    projectRoot,
    argv: ["bash", "-lc", "python train.py | tee train.log"],
    mode: "tmux",
    name: "train-a",
    tags: ["baseline", "a100"],
    outputs: ["runs/train-a"],
  });
  assert.deepEqual(executed.args, [
    "exec",
    "--tmux",
    "--name",
    "train-a",
    "--tag",
    "baseline",
    "--tag",
    "a100",
    "--output",
    "runs/train-a",
    "--",
    "bash",
    "-lc",
    "python train.py | tee train.log",
  ]);
});
