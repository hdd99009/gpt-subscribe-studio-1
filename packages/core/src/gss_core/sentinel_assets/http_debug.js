"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

function headerList(headers) {
  if (!headers) return [];
  if (Array.isArray(headers)) return headers;
  const values = typeof headers.entries === "function" ? [...headers.entries()] : Object.entries(headers);
  return values.map(([name, value]) => ({ name: String(name), value: String(value) }));
}

function captureFetch(fetchImpl, context, proxy) {
  if (!context || !context.directory) return fetchImpl;
  return async function recordedFetch(url, opts = {}) {
    const id = randomUUID().replaceAll("-", "");
    const filename = path.join(context.directory, `exchange-${id}.json`);
    const started = performance.now();
    const requestHeaders = headerList(opts.headers);
    const body = opts.body == null ? "" : String(opts.body);
    const entry = {
      startedDateTime: new Date().toISOString(), time: 0,
      request: {
        method: opts.method || "GET", url: String(url), httpVersion: "",
        headers: requestHeaders, cookies: [],
        queryString: [...new URL(url).searchParams].map(([name, value]) => ({ name, value })),
        headersSize: -1, bodySize: Buffer.byteLength(body),
      },
      response: {
        status: 0, statusText: "", httpVersion: "", headers: [], cookies: [],
        content: { size: 0, mimeType: "", text: "" }, redirectURL: "", headersSize: -1, bodySize: 0,
      },
      cache: {}, timings: { send: 0, wait: 0, receive: 0 },
      _id: id, _parentRequestId: context.parentRequestId || "", _direction: "upstream",
      _transport: "sentinel-node", _proxy: proxy || "direct", _state: "pending",
    };
    if (body) {
      entry.request.postData = {
        mimeType: requestHeaders.find(h => h.name.toLowerCase() === "content-type")?.value || "",
        text: body,
      };
    }
    function save() {
      const temporary = `${filename}.${randomUUID()}.tmp`;
      try {
        fs.writeFileSync(temporary, JSON.stringify(entry), "utf8");
        fs.renameSync(temporary, filename);
      } catch (error) {
        process.stderr.write(`http_debug_write_failed type=${error.code || error.name}\n`);
      } finally {
        try { fs.unlinkSync(temporary); } catch (_) {}
      }
    }
    function finish(error) {
      entry.time = performance.now() - started;
      entry.timings.wait = entry.time;
      entry._state = error ? "error" : "complete";
      if (error) entry._error = { type: error.name || "Error", message: String(error.message || error) };
      save();
    }
    save();
    let response;
    try {
      response = await fetchImpl(url, opts);
    } catch (error) {
      finish(error);
      throw error;
    }
    entry.response.status = response.status;
    entry.response.statusText = response.statusText || "";
    entry.response.headers = headerList(response._debugHeaders || response.headers);
    entry.response._finalUrl = response.url || String(url);
    if (response._debugRequestHeaders) entry.request._transportHeaders = headerList(response._debugRequestHeaders);
    entry._state = "streaming";
    save();
    async function text() {
      try {
        const value = await response.text();
        entry.response.content = {
          size: Buffer.byteLength(value),
          mimeType: entry.response.headers.find(h => h.name.toLowerCase() === "content-type")?.value || "",
          text: value,
        };
        entry.response.bodySize = entry.response.content.size;
        finish();
        return value;
      } catch (error) {
        finish(error);
        throw error;
      }
    }
    return new Proxy(response, {
      get(target, name) {
        if (name === "text") return text;
        if (name === "json") return async () => JSON.parse(await text());
        const value = Reflect.get(target, name, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
  };
}

module.exports = { captureFetch };
