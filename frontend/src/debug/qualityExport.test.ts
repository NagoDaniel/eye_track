import { describe, expect, it } from "vitest";

import protocol from "../../../shared/protocol.json";
import {
  QUALITY_KEYS,
  QUALITY_SLIDERS,
  type QualityThresholds,
  isValidQuality,
  qualityToJson,
} from "./qualityExport";

// protocol.json does not hold the extra keys yet (only the two face-ratio warnings); they are
// filled in here so the export tests run on a full set of thresholds.
const EXTRA: Omit<QualityThresholds, "min_face_ratio_warn" | "max_face_ratio_warn"> = {
  brightness_min: 60,
  brightness_max: 200,
  face_ratio_min: 0.1,
  face_ratio_max: 0.25,
  max_yaw_deg: 25,
  max_pitch_deg: 20,
};
const defaults: QualityThresholds = { ...EXTRA, ...protocol.study_config_default.quality };

function parseBlock(json: string): QualityThresholds {
  const parsed = JSON.parse(`{${json}}`) as { quality: Partial<QualityThresholds> };
  return { ...defaults, ...parsed.quality };
}

describe("QUALITY_SLIDERS", () => {
  it("has one slider per key, and each key is in the protocol default quality block", () => {
    expect(QUALITY_SLIDERS.map((s) => s.key).sort()).toEqual([...QUALITY_KEYS].sort());
    // Once shared/protocol.json lists all keys, this becomes an equality check.
    for (const key of Object.keys(protocol.study_config_default.quality)) {
      expect(QUALITY_SLIDERS.map((s) => s.key)).toContain(key);
    }
  });

  it("has sane ranges and the protocol defaults lie inside them", () => {
    for (const s of QUALITY_SLIDERS) {
      expect(s.min).toBeLessThan(s.max);
      expect(s.step).toBeGreaterThan(0);
      expect(defaults[s.key]).toBeGreaterThanOrEqual(s.min);
      expect(defaults[s.key]).toBeLessThanOrEqual(s.max);
    }
  });
});

describe("isValidQuality", () => {
  it("accepts the defaults", () => expect(isValidQuality(defaults)).toBe(true));

  it("rejects each lower bound at or above its upper bound", () => {
    expect(isValidQuality({ ...defaults, brightness_min: 200 })).toBe(false);
    expect(isValidQuality({ ...defaults, brightness_min: 201 })).toBe(false);
    expect(isValidQuality({ ...defaults, face_ratio_min: 0.25 })).toBe(false);
    expect(isValidQuality({ ...defaults, face_ratio_max: 0.05 })).toBe(false);
    expect(isValidQuality({ ...defaults, min_face_ratio_warn: 1.25 })).toBe(false);
    expect(isValidQuality({ ...defaults, max_face_ratio_warn: 0.5 })).toBe(false);
  });
});

describe("qualityToJson", () => {
  it("is an empty block when nothing differs", () => {
    expect(qualityToJson(defaults, defaults)).toBe('"quality": {}');
  });

  it("lists only the keys that differ from the defaults", () => {
    const json = qualityToJson({ ...defaults, brightness_min: 80, max_yaw_deg: 30 }, defaults);
    expect(json).toBe('"quality": {\n  "brightness_min": 80,\n  "max_yaw_deg": 30\n}');
  });

  it("round-trips: the printed block parses to a config the backend rules accept", () => {
    const changed = { ...defaults, brightness_min: 90, face_ratio_max: 0.3, max_pitch_deg: 15 };
    const back = parseBlock(qualityToJson(changed, defaults));
    expect(back).toEqual(changed);
    expect(isValidQuality(back)).toBe(true);
    for (const key of QUALITY_KEYS) expect(back[key]).toBeGreaterThan(0);
  });
});
