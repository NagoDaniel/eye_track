import { describe, expect, it } from "vitest";

import protocol from "../../shared/protocol.json";
import {
  BROWSER_REQUIREMENTS,
  type ChunkAck,
  type ClientErrorRequest,
  type CompleteRequest,
  type CompleteResponse,
  type ConsentRequest,
  type ConsentResponse,
  type SessionCreateRequest,
  type SessionCreateResponse,
  type StudyConfig,
  type StudyInfo,
} from "./config";
import { EVENT_TYPES, type EventRecord, STREAMS } from "./eventLog";

describe("names match shared/protocol.json (the Python side has the same test)", () => {
  it("streams", () => expect([...STREAMS]).toEqual(protocol.streams));
  it("event types", () => expect([...EVENT_TYPES]).toEqual(protocol.event_types));
  it("browser requirements", () =>
    expect([...BROWSER_REQUIREMENTS]).toEqual(protocol.browser_requirements));
});

// The shared example payloads must be assignable to the TypeScript types. Plain JSON imports
// widen string literals to `string`, so the few literal-union fields are narrowed by hand; a
// missing or misnamed field is still a compile error (`npm run typecheck`).
describe("example payloads fit the TypeScript types", () => {
  const ex = protocol.examples;

  it("study config and session response", () => {
    const c = ex.session_create_response.config;
    const config: StudyConfig = {
      ...c,
      calibration_gate: {
        ...c.calibration_gate,
        on_fail: c.calibration_gate.on_fail as StudyConfig["calibration_gate"]["on_fail"],
      },
    };
    const response: SessionCreateResponse = { ...ex.session_create_response, config };
    expect(response.videos[0]?.id).toBe("1");
    expect(config.retention.raw_video_days_after_processing).toBeNull();
  });

  it("the other payloads", () => {
    const info: StudyInfo = { ...ex.study_info, mode: ex.study_info.mode as StudyInfo["mode"] };
    const request: SessionCreateRequest = ex.session_create_request;
    const consent: ConsentRequest = ex.consent_request;
    const consented: ConsentResponse = {
      status: ex.consent_response.status as ConsentResponse["status"],
    };
    const ack: ChunkAck = { ...ex.chunk_ack, stream: ex.chunk_ack.stream as ChunkAck["stream"] };
    const complete: CompleteRequest = ex.complete_request;
    const missing: CompleteResponse = {
      status: ex.complete_response_missing.status as CompleteResponse["status"],
      missing: ex.complete_response_missing.missing,
    };
    const error: ClientErrorRequest = ex.client_error_request;
    const event: EventRecord = {
      ...ex.event_record,
      type: ex.event_record.type as EventRecord["type"],
    };
    expect(info.code).toBe(request.study_code);
    expect(consent.consent_version).toBe("v1");
    expect(consented.status).toBe("consented");
    expect(ack.seq).toBe(0);
    expect(complete.last_seq.events).toBe(3);
    expect(missing.missing.events).toEqual([1]);
    expect(error.session_id).toBe(ex.session_create_response.session_id);
    expect(EVENT_TYPES).toContain(event.type);
  });
});
