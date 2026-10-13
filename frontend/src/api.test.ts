import { describe, expect, it } from "vitest";

import { Api, ApiError, sha256Hex, type FetchFn } from "./api";

interface Call {
  url: string;
  init: RequestInit;
}

function stubFetch(respond: (call: Call) => Response | Error): { fetchFn: FetchFn; calls: Call[] } {
  const calls: Call[] = [];
  const fetchFn: FetchFn = (url, init) => {
    const call = { url, init: init ?? {} };
    calls.push(call);
    const result = respond(call);
    return result instanceof Error ? Promise.reject(result) : Promise.resolve(result);
  };
  return { fetchFn, calls };
}

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("sha256Hex", () => {
  it("matches the known digest of 'test'", async () => {
    expect(await sha256Hex(new TextEncoder().encode("test"))).toBe(
      "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    );
  });
  it("hashes empty input", async () => {
    expect(await sha256Hex(new Uint8Array())).toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
  });
});

describe("Api", () => {
  it("sends the bearer token and a JSON body", async () => {
    const { fetchFn, calls } = stubFetch(() => json({ status: "consented" }));
    const api = new Api("", fetchFn);
    await api.recordConsent("sid", "tok", { consent_version: "v1" });
    const call = calls[0];
    expect(call?.url).toBe("/api/sessions/sid/consent");
    expect(call?.init.method).toBe("POST");
    const headers = call?.init.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer tok");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(call?.init.body).toBe('{"consent_version":"v1"}');
  });

  it("does not send a token on public endpoints", async () => {
    const { fetchFn, calls } = stubFetch(() => json({ name: "x" }));
    await new Api("", fetchFn).getStudy("phys 1");
    expect(calls[0]?.url).toBe("/api/studies/phys%201");
    expect((calls[0]?.init.headers as Record<string, string>)["Authorization"]).toBeUndefined();
  });

  it("puts raw bytes with the hash header", async () => {
    const { fetchFn, calls } = stubFetch(() => json({ stream: "events", seq: 3, size: 2 }));
    const bytes = new Uint8Array([1, 2]);
    const ack = await new Api("", fetchFn).putChunk("sid", "tok", "events", 3, bytes, "abc");
    expect(ack.seq).toBe(3);
    expect(calls[0]?.url).toBe("/api/sessions/sid/chunks/events/3");
    expect(calls[0]?.init.method).toBe("PUT");
    const headers = calls[0]?.init.headers as Record<string, string>;
    expect(headers["X-Chunk-SHA256"]).toBe("abc");
    expect(headers["Content-Type"]).toBe("application/octet-stream");
    expect(headers["Authorization"]).toBe("Bearer tok");
    expect(calls[0]?.init.body).toBe(bytes);
  });

  it("maps an error response to ApiError with status and detail", async () => {
    const { fetchFn } = stubFetch(() => json({ detail: "chunk differs" }, 409));
    const error = await new Api("", fetchFn)
      .putChunk("s", "t", "events", 0, new Uint8Array([1]), "h")
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).detail).toBe("chunk differs");
    expect((error as ApiError).retryable).toBe(false);
  });

  it("keeps a non-JSON error body as the detail", async () => {
    const { fetchFn } = stubFetch(() => new Response("bad gateway", { status: 502 }));
    const error = (await new Api("", fetchFn).getStudy("x").catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(502);
    expect(error.detail).toBe("bad gateway");
    expect(error.retryable).toBe(true);
  });

  it("maps a network failure to a retryable ApiError with status 0", async () => {
    const { fetchFn } = stubFetch(() => new TypeError("Failed to fetch"));
    const error = (await new Api("", fetchFn).getStudy("x").catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.retryable).toBe(true);
  });

  it("accepts the empty 204 of /api/client-errors", async () => {
    const { fetchFn, calls } = stubFetch(() => new Response(null, { status: 204 }));
    await new Api("", fetchFn).postClientError({ message: "boom" });
    expect(calls[0]?.url).toBe("/api/client-errors");
  });
});
