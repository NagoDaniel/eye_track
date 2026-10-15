// Video preload (PLAN §5.2 step 3): fetch every playlist MP4 completely into a Blob, check its sha256,
// and keep an object URL so playback never waits on the network.

import { sha256Hex, type FetchFn } from "./api";
import type { VideoRef } from "./config";

export const VIDEO_MIME_TYPE = "video/mp4";

/** The downloaded bytes do not match the sha256 the server gave for the video. */
export class IntegrityError extends Error {
  constructor(
    readonly videoId: string,
    readonly expected: string,
    readonly actual: string,
  ) {
    super(`Video ${videoId} failed the integrity check (expected ${expected}, got ${actual})`);
    this.name = "IntegrityError";
  }
}

export interface PreloadDeps {
  fetchFn?: FetchFn;
  createObjectUrl?: (blob: Blob) => string;
}

/** Overall progress, 0..1: the mean over videos of received / total (a video of unknown size counts 0 until done). */
export function overallProgress(fractions: readonly number[]): number {
  if (fractions.length === 0) return 1;
  return fractions.reduce((sum, f) => sum + f, 0) / fractions.length;
}

async function download(
  video: VideoRef,
  fetchFn: FetchFn,
  onFraction: (fraction: number) => void,
): Promise<Blob> {
  const response = await fetchFn(video.url);
  if (!response.ok) throw new Error(`Video ${video.id} could not be downloaded (HTTP ${response.status})`);
  const total = Number(response.headers.get("Content-Length") ?? 0);
  const parts: Uint8Array[] = [];
  if (response.body === null) {
    parts.push(new Uint8Array(await response.arrayBuffer()));
  } else {
    const reader = response.body.getReader();
    let received = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value);
      received += value.byteLength;
      if (total > 0) onFraction(Math.min(received / total, 1));
    }
  }
  return new Blob(parts as BlobPart[], { type: VIDEO_MIME_TYPE });
}

/** Resolves with `video id -> object URL`. Throws IntegrityError on a hash mismatch. */
export async function preloadVideos(
  videos: readonly VideoRef[],
  onProgress: (fraction: number) => void,
  deps: PreloadDeps = {},
): Promise<Map<string, string>> {
  const fetchFn = deps.fetchFn ?? ((input, init) => fetch(input, init));
  const createObjectUrl = deps.createObjectUrl ?? ((blob: Blob) => URL.createObjectURL(blob));
  const fractions = videos.map(() => 0);
  const urls = new Map<string, string>();
  onProgress(overallProgress(fractions));
  for (const [i, video] of videos.entries()) {
    const blob = await download(video, fetchFn, (f) => {
      fractions[i] = f;
      onProgress(overallProgress(fractions));
    });
    const actual = await sha256Hex(new Uint8Array(await blob.arrayBuffer()));
    if (actual !== video.sha256.toLowerCase()) throw new IntegrityError(video.id, video.sha256, actual);
    fractions[i] = 1;
    onProgress(overallProgress(fractions));
    urls.set(video.id, createObjectUrl(blob));
  }
  return urls;
}
