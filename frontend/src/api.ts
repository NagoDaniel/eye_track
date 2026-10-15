// Client for the participant API (PLAN §6). Errors are `{"detail": ...}` with the HTTP status.

import type {
  ChunkAck,
  ClientErrorRequest,
  CompleteRequest,
  CompleteResponse,
  ConsentRequest,
  ConsentResponse,
  SessionCreateRequest,
  SessionCreateResponse,
  StudyInfo,
} from "./config";
import type { Stream } from "./eventLog";

/** A failed request. `status` 0 means the request never got an answer (network error). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(`API error ${status}: ${describeDetail(detail)}`);
    this.name = "ApiError";
  }

  /** 5xx and network errors are retried; everything else is a loud failure (PLAN §5.8). */
  get retryable(): boolean {
    return this.status === 0 || this.status >= 500;
  }
}

function describeDetail(detail: unknown): string {
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

/** Lowercase hex sha256 of `bytes` (needs a secure context). */
export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

export class Api {
  constructor(
    private readonly baseUrl = "",
    private readonly fetchFn: FetchFn = (input, init) => fetch(input, init),
  ) {}

  getStudy(code: string): Promise<StudyInfo> {
    return this.json("GET", `/api/studies/${encodeURIComponent(code)}`);
  }

  createSession(request: SessionCreateRequest): Promise<SessionCreateResponse> {
    return this.json("POST", "/api/sessions", { body: request });
  }

  recordConsent(
    sessionId: string,
    token: string,
    request: ConsentRequest,
  ): Promise<ConsentResponse> {
    return this.json("POST", `/api/sessions/${sessionId}/consent`, { body: request, token });
  }

  putChunk(
    sessionId: string,
    token: string,
    stream: Stream,
    seq: number,
    bytes: Uint8Array,
    sha256: string,
  ): Promise<ChunkAck> {
    return this.json("PUT", `/api/sessions/${sessionId}/chunks/${stream}/${seq}`, {
      token,
      raw: bytes,
      headers: { "X-Chunk-SHA256": sha256 },
    });
  }

  complete(sessionId: string, token: string, request: CompleteRequest): Promise<CompleteResponse> {
    return this.json("POST", `/api/sessions/${sessionId}/complete`, { body: request, token });
  }

  /** POST /api/client-errors (no auth, answers 204). */
  async postClientError(request: ClientErrorRequest): Promise<void> {
    await this.request("POST", "/api/client-errors", { body: request });
  }

  private async json<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
    const response = await this.request(method, path, options);
    return (await response.json()) as T;
  }

  private async request(method: string, path: string, options: RequestOptions): Promise<Response> {
    const headers: Record<string, string> = { ...options.headers };
    if (options.token !== undefined) headers["Authorization"] = `Bearer ${options.token}`;
    let body: BodyInit | undefined;
    if (options.raw !== undefined) {
      headers["Content-Type"] = "application/octet-stream";
      body = options.raw as BufferSource;
    } else if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.body);
    }
    let response: Response;
    try {
      response = await this.fetchFn(this.baseUrl + path, { method, headers, body });
    } catch (e) {
      throw new ApiError(0, e instanceof Error ? e.message : String(e));
    }
    if (!response.ok) throw new ApiError(response.status, await readDetail(response));
    return response;
  }
}

interface RequestOptions {
  body?: unknown;
  raw?: Uint8Array;
  token?: string;
  headers?: Record<string, string>;
}

async function readDetail(response: Response): Promise<unknown> {
  const text = await response.text();
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    return parsed.detail ?? parsed;
  } catch {
    return text || response.statusText;
  }
}
