// The last events of the event log, one short line each, for the debug overlay (PLAN §12.1).

import type { EventRecord } from "../eventLog";

export const EVENT_TAIL_SIZE = 20;
/** Longest compact `data` JSON in a line; longer text is cut and ends with an ellipsis. */
export const EVENT_DATA_MAX_CHARS = 60;

/** `12.34 calib_dot_show {"phase":"calibration","id":3}`; empty data is left out. */
export function formatEventLine(
  record: EventRecord,
  maxChars: number = EVENT_DATA_MAX_CHARS,
): string {
  const head = `${(record.t / 1000).toFixed(2)} ${record.type}`;
  if (Object.keys(record.data).length === 0) return head;
  const json = JSON.stringify(record.data);
  const data = json.length > maxChars ? `${json.slice(0, maxChars - 1)}…` : json;
  return `${head} ${data}`;
}

/** Ring buffer of the newest `size` events. */
export class EventTail {
  private readonly records: EventRecord[] = [];

  constructor(private readonly size: number = EVENT_TAIL_SIZE) {}

  /** Use as (part of) the event sink. */
  push(record: EventRecord): void {
    this.records.push(record);
    if (this.records.length > this.size) this.records.shift();
  }

  /** Oldest first. */
  lines(): string[] {
    return this.records.map((r) => formatEventLine(r));
  }
}
