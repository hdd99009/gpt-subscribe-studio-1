const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
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

test('helper receives the main client browser target and proxy unchanged', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gss-profile-'));
  const helper = path.join(dir, 'profile.cjs');
  fs.writeFileSync(helper, `let input='';process.stdin.on('data',x=>input+=x);process.stdin.on('end',()=>{
    process.stdout.write(JSON.stringify({status:200,body:input}));
  });`);
  try {
    const request = makePythonCurlFetch({ python: process.execPath, helper, timeoutMs: 2000,
      proxy: 'http://fixture.invalid:3010', impersonate: 'chrome150' });
    const sent = await (await request('https://upstream.invalid/', { headers: { 'user-agent': 'fixture-browser' } })).json();
    assert.equal(sent.impersonate, 'chrome150');
    assert.equal(sent.proxy, 'http://fixture.invalid:3010');
    assert.equal(sent.headers['user-agent'], 'fixture-browser');
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('Sentinel navigator high entropy values match the HTTP client hints', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../../packages/core/src/gss_core/sentinel_assets/sentinel_bootstrap.js'), 'utf8');
  const context = {
    __UA__: 'Mozilla/5.0 Chrome/150.0.7871.189 Safari/537.36', __PLATFORM__: 'Win32',
    __PLATFORM_LABEL__: 'Windows', __LANGUAGE__: 'en-PH', __CORES__: 8,
    __CLIENT_HINTS__: {
      'sec-ch-ua': '"Google Chrome";v="150", "Chromium";v="150", "Not(A:Brand";v="24"',
      'sec-ch-ua-platform': '"Windows"', 'sec-ch-ua-platform-version': '"10.0.0"',
      'sec-ch-ua-full-version-list': '"Google Chrome";v="150.0.7871.189", "Chromium";v="150.0.7871.189", "Not(A:Brand";v="24.0.0.0"',
      'sec-ch-ua-arch': '"x86"', 'sec-ch-ua-bitness': '"64"', 'sec-ch-ua-model': '""',
    },
    setTimeout, clearTimeout, setInterval, clearInterval, URL, TextEncoder, TextDecoder,
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  const values = await context.navigator.userAgentData.getHighEntropyValues();
  assert.equal(values.platform, 'Windows');
  assert.equal(values.uaFullVersion, '150.0.7871.189');
  assert.equal(values.platformVersion, '10.0.0');
  assert.equal(values.architecture, 'x86');
  assert.equal(values.bitness, '64');
  assert.equal(values.fullVersionList[0].version, '150.0.7871.189');
  assert.equal(values.brands[2].brand, 'Not(A:Brand');
  assert.equal(context.navigator.language, 'en-PH');
});
