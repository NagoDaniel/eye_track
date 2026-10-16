import { describe, expect, it } from "vitest";

import landmarkSubset from "../../shared/landmark_subset.json";
import protocol from "../../shared/protocol.json";
import {
  BROWSER_REQUIREMENTS,
  PARTICIPANT_CODE_PATTERN,
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
import {
  CALIBRATION_POINT_COUNT,
  FEATURE_MATRIX_SIZE,
  INNER_POINT_LOW,
  LANDMARK_COUNT,
  VALIDATION_POINT_COUNT,
} from "./constants";
import {
  type EventDataMap,
  type EventRecord,
  EVENT_TYPES,
  GATE_BLOCKS,
  GATE_REASONS,
  SKIP_TARGETS,
  STREAMS,
} from "./eventLog";
import type { FeatureRecord, StimulusFrameRecord } from "./records";

describe("names match shared/protocol.json (the Python side has the same test)", () => {
  it("streams", () => expect([...STREAMS]).toEqual(protocol.streams));
  it("event types", () => expect([...EVENT_TYPES]).toEqual(protocol.event_types));
  it("calibration and validation point layout", () => {
    expect(CALIBRATION_POINT_COUNT).toBe(protocol.calibration_point_count);
    expect(VALIDATION_POINT_COUNT).toBe(protocol.validation_point_count);
    expect(INNER_POINT_LOW).toBe(protocol.inner_point_low);
  });
  it("participant code pattern", () =>
    expect(PARTICIPANT_CODE_PATTERN.source).toBe(protocol.participant_code_pattern));
  it("browser requirements", () =>
    expect([...BROWSER_REQUIREMENTS]).toEqual(protocol.browser_requirements));
  it("feature record sizes", () => {
    expect(LANDMARK_COUNT).toBe(protocol.landmark_count);
    expect(FEATURE_MATRIX_SIZE).toBe(protocol.feature_matrix_size);
  });
  it("values of enum-like event data fields", () => {
    expect([...GATE_BLOCKS]).toEqual(protocol.event_data_values.gate_blocks);
    expect([...GATE_REASONS]).toEqual(protocol.event_data_values.gate_reasons);
    expect([...SKIP_TARGETS]).toEqual(protocol.event_data_values.skip_targets);
  });
});

describe("shared/landmark_subset.json", () => {
  const indices = Object.values(landmarkSubset.groups).flat();
  it("holds LANDMARK_COUNT distinct indices of the mesh", () => {
    expect(indices).toHaveLength(LANDMARK_COUNT);
    expect(new Set(indices).size).toBe(indices.length);
    expect(Math.max(...indices)).toBeLessThan(landmarkSubset.mesh_size);
    expect(Math.min(...indices)).toBeGreaterThanOrEqual(0);
  });
  it("includes both iris rings", () => {
    for (let i = 468; i <= 477; i++) expect(indices).toContain(i);
  });
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
    const debugRequest: SessionCreateRequest = { ...request, debug: true };
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
    expect(debugRequest.debug).toBe(true);
  });

  it("feature and stimulus-frame records", () => {
    const withFace = ex.feature_record as FeatureRecord;
    const noFace: FeatureRecord = ex.feature_record_no_face;
    const unmeasured = ex.feature_record_unmeasured_brightness as FeatureRecord;
    expect(unmeasured.face && unmeasured.brightness === null).toBe(true);
    const frame: StimulusFrameRecord = ex.stimulus_frame_record;
    expect(withFace.lm).toHaveLength(LANDMARK_COUNT);
    expect(withFace.matrix).toHaveLength(FEATURE_MATRIX_SIZE);
    expect(noFace.face).toBe(false);
    expect(noFace.lm).toBeNull();
    expect(frame.presented_frames).toBe(36);
  });

  it("event data of the typed events", () => {
    const data = ex.event_data;
    const gate: EventDataMap["calib_gate_result"] = {
      ...data.calib_gate_result,
      block: data.calib_gate_result.block as EventDataMap["calib_gate_result"]["block"],
      reason: data.calib_gate_result.reason as EventDataMap["calib_gate_result"]["reason"],
    };
    const recorderStart: EventDataMap["recorder_start"] = data.recorder_start;
    const dataAvailable: EventDataMap["recorder_dataavailable"] = data.recorder_dataavailable;
    const recorderStop: EventDataMap["recorder_stop"] = data.recorder_stop;
    const regained: EventDataMap["face_regained"] = data.face_regained;
    const skip: EventDataMap["debug_skip"] = {
      target: data.debug_skip.target as EventDataMap["debug_skip"]["target"],
    };
    expect(Object.keys(data).sort()).toEqual(
      [
        "calib_gate_result",
        "debug_skip",
        "face_lost",
        "face_regained",
        "recorder_dataavailable",
        "recorder_start",
        "recorder_stop",
      ].sort(),
    );
    expect(gate.point_errors).toHaveLength(5);
    expect(gate.valid_frames_per_dot).toHaveLength(CALIBRATION_POINT_COUNT);
    expect(recorderStart.mime_type).toContain("webm");
    expect(dataAvailable.seq).toBe(3);
    expect(recorderStop.chunks).toBe(14);
    expect(regained.lost_ms).toBeGreaterThan(0);
    expect(skip.target).toBe("calibration");
  });
});
