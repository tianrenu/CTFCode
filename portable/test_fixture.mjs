// 真实V2服务的回归夹具：独立XDG、随机认证、仅本机监听、Windows作业兜底。
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import net from "node:net";
import { spawn } from "node:child_process";
import { once } from "node:events";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

export const root = path.dirname(fileURLToPath(import.meta.url));
export const cli = process.env.CTFCODE_V2_CLI;
export const python = process.env.CTFCODE_PYTHON;
export const skip = !cli || !python ? "需显式指定CTFCODE_V2_CLI和CTFCODE_PYTHON；不使用全局/桌面CLI替代" : false;

export async function port() {
  const socket = net.createServer();
  socket.listen(0, "127.0.0.1");
  await once(socket, "listening");
  const value = socket.address().port;
  await new Promise((resolve) => socket.close(resolve));
  return value;
}

export async function fixture(t, opts = {}) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "CTFCode V2 真服务 "));
  const a = path.join(dir, "题目 A");
  const b = path.join(dir, "题目 B");
  await Promise.all([fs.mkdir(a), fs.mkdir(b), fs.writeFile(path.join(dir, "fixture.marker"), "synthetic API test")]);
  const token = crypto.randomBytes(24).toString("base64url");
  const headers = { Authorization: "Basic " + Buffer.from("opencode:" + token).toString("base64") };
  const value = await port();
  const child = spawn(python, ["-I", "-X", "utf8", "-u", path.join(root, "v2_fixture.py"), "--cli", cli,
    "--root", dir, "--port", String(value), ...(opts.model ? ["--model-endpoint", opts.model, "--context", opts.context ?? "32"] : []),
    ...(opts.tokenizer === false ? ["--missing-tokenizer"] : []), ...(opts.controller ? ["--controller"] : [])],
    { windowsHide: true, stdio: ["pipe", "pipe", "pipe"], env: { ...process.env, CTFCODE_FIXTURE_KEY: token } });
  let err = "";
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk; });
  child.stderr.on("data", (chunk) => { err += chunk; });
  t.after(async () => {
    child.stdin.end();
    if (child.exitCode === null) await Promise.race([once(child, "exit"),
      new Promise((resolve) => setTimeout(resolve, 10000).unref())]);
    if (child.exitCode === null) child.kill();
  });
  const url = "http://127.0.0.1:" + value;
  const calls = [];
  async function api(route, body) {
    calls.push(route);
    const res = await fetch(url + route, { method: body === undefined ? "GET" : "POST",
      headers: { ...headers, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(30000) });
    const text = await res.text();
    const json = text ? JSON.parse(text) : undefined;
    if (!res.ok) throw new Error(JSON.stringify({ status: res.status, route, json }));
    return json?.data ?? json;
  }
  const deadline = Date.now() + 45000;
  while (true) {
    if (child.exitCode !== null) throw new Error("服务已退出：" + err);
    const ready = await fetch(url + "/api/session/active", { headers, signal: AbortSignal.timeout(1000) }).then((res) => res.ok, () => false);
    if (ready) break;
    if (Date.now() > deadline) throw new Error("服务启动超时：" + err + "；日志：" + dir);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  const client = { session: { create: (value) => api("/api/session", value),
    get: ({ sessionID }) => api("/api/session/" + sessionID), active: () => api("/api/session/active"),
  }, experimental: { session: { export: ({ sessionID }) => api("/api/experimental/session/" + sessionID + "/export") } } };
  const metadata = JSON.parse(output.trim());
  return { dir, a, b, api, client, calls, controller: metadata.controller, validator: metadata.validator };
}
