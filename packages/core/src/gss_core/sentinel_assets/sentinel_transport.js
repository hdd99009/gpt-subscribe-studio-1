"use strict";

// Transport deadlines and child cleanup only; no SDK or token logic.
const { spawn, execFile } = require("child_process");

function networkError(timeout) {
  const error = new Error(timeout ? "proxy request timed out" : "proxy transport failed");
  error.code = timeout ? "network_timeout" : "network_error";
  return error;
}

function killTree(child) {
  return new Promise((resolve) => {
    if (!child.pid || child.exitCode !== null || child.signalCode !== null) return resolve();
    if (process.platform === "win32") {
      execFile("taskkill", ["/PID", String(child.pid), "/T", "/F"],
        { windowsHide: true, timeout: 2000 }, () => { child.kill(); resolve(); });
    } else {
      child.kill("SIGKILL");
      resolve();
    }
  });
}

function makePythonCurlFetch({ python, helper, proxy, timeoutMs, onFailure = () => {} }) {
  return function pythonCurlFetch(url, opts = {}) {
    return new Promise((resolve, reject) => {
      const child = spawn(python, [helper], {
        stdio: ["pipe", "pipe", "pipe"], windowsHide: true,
      });
      const stdout = [], stderr = [];
      let settled = false, terminating = false;
      function fail(timeout) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        const error = networkError(timeout);
        onFailure(error.code);
        reject(error);
      }
      // Allow the helper to report its own curl timeout before forcibly stopping it.
      const timer = setTimeout(() => {
        terminating = true;
        killTree(child).finally(() => fail(true));
      }, timeoutMs + 2000);
      child.stdout.on("data", chunk => stdout.push(chunk));
      child.stderr.on("data", chunk => stderr.push(chunk));
      child.on("error", () => { if (!terminating) fail(false); });
      child.stdin.on("error", () => {
        if (!terminating) killTree(child).finally(() => fail(false));
      });
      child.on("close", code => {
        if (settled || terminating) return;
        if (code !== 0) {
          const trace = Buffer.concat(stderr).toString("utf8");
          fail(/curl:\s*\(28\)|exceptions\.Timeout/.test(trace));
          return;
        }
        try {
          const result = JSON.parse(Buffer.concat(stdout).toString("utf8"));
          settled = true;
          clearTimeout(timer);
          const status = Number(result.status || 0), body = String(result.body || "");
          resolve({ status, ok: status >= 200 && status < 300, headers: { get: () => null },
            text: async () => body, json: async () => JSON.parse(body) });
        } catch (_) { fail(false); }
      });
      child.stdin.end(JSON.stringify({ url: String(url), method: opts.method || "GET",
        headers: opts.headers || {}, body: opts.body == null ? null : String(opts.body),
        proxy, timeout: timeoutMs / 1000 }));
    });
  };
}

function boundedFetch(fetchImpl, timeoutMs, onFailure = () => {}) {
  return async function fetchWithDeadline(url, opts = {}) {
    const controller = new AbortController();
    let timedOut = false, reported = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    if (timer.unref) timer.unref();
    function failed() {
      clearTimeout(timer);
      const error = networkError(timedOut);
      if (!reported) { reported = true; onFailure(error.code); }
      return error;
    }
    try {
      const response = await fetchImpl(url, { ...opts, signal: controller.signal });
      // The deadline also covers reading a body after response headers arrive.
      return new Proxy(response, { get(target, name) {
        if (name === "text" || name === "json") return async () => {
          try { return await target[name](); }
          catch (error) {
            if (error instanceof SyntaxError) throw error;
            throw failed();
          }
          finally { clearTimeout(timer); }
        };
        const value = Reflect.get(target, name, target);
        return typeof value === "function" ? value.bind(target) : value;
      } });
    } catch (_) { throw failed(); }
  };
}

module.exports = { makePythonCurlFetch, boundedFetch };
