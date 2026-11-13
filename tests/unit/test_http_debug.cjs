const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");

const { captureFetch } = require("../../packages/core/src/gss_core/sentinel_assets/http_debug.js");
const { boundedFetch, makePythonCurlFetch } = require("../../packages/core/src/gss_core/sentinel_assets/sentinel_transport.js");

async function withCapture(run) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "gss-http-debug-"));
  try {
    await run({ directory, parentRequestId: "fixture-parent" }, () =>
      fs.readdirSync(directory).filter(name => name.endsWith(".json")).map(name =>
        JSON.parse(fs.readFileSync(path.join(directory, name), "utf8"))));
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

async function withServer(handler, run) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try { await run(`http://127.0.0.1:${server.address().port}`); }
  finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
}

test("disabled Sentinel capture uses the original fetch function", () => {
  assert.equal(captureFetch(fetch, null, ""), fetch);
});

test("Sentinel keeps full challenge bodies, cookies and correlation", async () => {
  await withCapture(async (context, entries) => {
    await withServer((request, response) => {
      response.writeHead(403, { "Content-Type": "application/json", "Set-Cookie": "fixture=renewed" });
      response.end('{"challenge":"full-fixture-value"}');
    }, async url => {
      const request = captureFetch(boundedFetch(fetch, 1000), context, "fixture-proxy");
      const response = await request(`${url}/sentinel/req?flow=chatgpt_checkout`, {
        method: "POST", headers: { Cookie: "fixture=original", "Content-Type": "text/plain" },
        body: '{"p":"fixture-proof"}',
      });
      assert.equal(response.status, 403);
      assert.deepEqual(await response.json(), { challenge: "full-fixture-value" });
    });
    const [entry] = entries();
    assert.equal(entry._parentRequestId, "fixture-parent");
    assert.equal(entry._proxy, "fixture-proxy");
    assert.equal(entry.request.postData.text, '{"p":"fixture-proof"}');
    assert.equal(entry.response.content.text, '{"challenge":"full-fixture-value"}');
    assert.ok(entry.response.headers.some(header => header.name === "set-cookie"));
    assert.equal(entry._state, "complete");
  });
});

test("Sentinel timeouts are captured while preserving the transport exception", async () => {
  await withCapture(async (context, entries) => {
    await withServer((request, response) => { response.writeHead(200); response.write("{"); }, async url => {
      const request = captureFetch(boundedFetch(fetch, 100), context, "");
      const response = await request(url);
      await assert.rejects(response.text(), { code: "network_timeout" });
    });
    const [entry] = entries();
    assert.equal(entry.response.status, 200);
    assert.equal(entry._state, "error");
    assert.ok(entry._error.message.includes("timed out"));
  });
});

test("helper response headers are present in the exported Sentinel exchange", async () => {
  await withCapture(async (context, entries) => {
    const helper = path.join(context.directory, "helper.cjs");
    fs.writeFileSync(helper, `process.stdin.resume();process.stdin.on('end',()=>process.stdout.write(JSON.stringify({
      status:200,body:'{"ok":true}',headers:{'set-cookie':'fixture=new'},request_headers:{cookie:'fixture=old'}
    })));`);
    const request = captureFetch(makePythonCurlFetch({ python: process.execPath, helper, timeoutMs: 2000 }), context, "");
    const response = await request("https://upstream.invalid/sentinel");
    assert.deepEqual(await response.json(), { ok: true });
    const [entry] = entries();
    assert.deepEqual(entry.response.headers, [{ name: "set-cookie", value: "fixture=new" }]);
    assert.deepEqual(entry.request._transportHeaders, [{ name: "cookie", value: "fixture=old" }]);
  });
});

test("a capture filesystem failure preserves the upstream response", async () => {
  const request = captureFetch(async () => new Response("business-ok"), {
    directory: path.join(os.tmpdir(), "gss-nonexistent-parent", "child"), parentRequestId: "fixture",
  }, "");
  assert.equal(await (await request("https://upstream.invalid/")).text(), "business-ok");
});
