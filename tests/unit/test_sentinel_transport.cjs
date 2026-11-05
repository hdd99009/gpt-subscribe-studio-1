const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { makePythonCurlFetch, boundedFetch } = require('../../packages/core/src/gss_core/sentinel_assets/sentinel_transport.js');

async function withServer(handler, run) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { await run(`http://127.0.0.1:${server.address().port}`); }
  finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
}

test('HTTP timeout covers stalled headers', async () => {
  await withServer(() => {}, async url => {
    const failures = [];
    const request = boundedFetch(fetch, 150, code => failures.push(code));
    await assert.rejects(request(url), { code: 'network_timeout' });
    assert.deepEqual(failures, ['network_timeout']);
  });
});

test('HTTP timeout covers a stalled response body', async () => {
  await withServer((req, res) => { res.writeHead(200); res.write('{'); }, async url => {
    const response = await boundedFetch(fetch, 150)(url);
    assert.equal(response.status, 200);
    await assert.rejects(response.json(), { code: 'network_timeout' });
  });
});

test('HTTP success and upstream rejection keep their actual statuses and bodies', async () => {
  await withServer((req, res) => { res.writeHead(403); res.end('{"denied":true}'); }, async url => {
    const response = await boundedFetch(fetch, 1000)(url);
    assert.equal(response.status, 403);
    assert.equal(response.ok, false);
    assert.deepEqual(await response.json(), { denied: true });
  });
});

test('invalid upstream JSON is not reported as a network failure', async () => {
  await withServer((req, res) => res.end('invalid-json'), async url => {
    const failures = [];
    const response = await boundedFetch(fetch, 1000, code => failures.push(code))(url);
    await assert.rejects(response.json(), SyntaxError);
    assert.deepEqual(failures, []);
  });
});

test('helper timeout kills its process and descendants on Windows', { timeout: 10000 }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gss-helper-'));
  const helper = path.join(dir, 'hang.cjs');
  const beat = path.join(dir, 'heartbeat');
  const heartbeatCode = `setInterval(()=>require('node:fs').appendFileSync(${JSON.stringify(beat)},'x'),50)`;
  fs.writeFileSync(helper, process.platform === 'win32' ? `
    const {spawn}=require('node:child_process');
    const child=spawn(process.execPath,['-e',${JSON.stringify(heartbeatCode)}],{stdio:'ignore',windowsHide:true});
    setInterval(()=>{},1000);
  ` : heartbeatCode);
  try {
    const request = makePythonCurlFetch({ python: process.execPath, helper, timeoutMs: 100,
      proxy: 'socks5h://private:secret@invalid', onFailure: () => {} });
    await assert.rejects(request('https://public.invalid'), { code: 'network_timeout' });
    // Windows venv launchers also create a descendant. Verify it stopped writing.
    const before = fs.statSync(beat).size;
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal(fs.statSync(beat).size, before);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('helper curl timeout is classified without leaking traceback credentials', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gss-helper-'));
  const helper = path.join(dir, 'error.cjs');
  fs.writeFileSync(helper, `process.stderr.write('curl_cffi.requests.exceptions.Timeout: curl: (28) socks5h://private:secret@invalid');process.exit(1);`);
  try {
    const request = makePythonCurlFetch({ python: process.execPath, helper, timeoutMs: 1000 });
    await assert.rejects(request('https://public.invalid'), error =>
      error.code === 'network_timeout' && !error.message.includes('secret'));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
