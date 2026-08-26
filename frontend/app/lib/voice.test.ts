// The PCM16 codec both voice transports depend on: a capture buffer must survive the
// round-trip to base64 and back, and clipping must clamp instead of wrapping.
import { describe, expect, it } from "vitest";
import { float32ToPcm16Base64, pcm16Base64ToFloat32 } from "./voice";

describe("pcm16 codec", () => {
  it("round-trips a buffer within quantization error", () => {
    const input = new Float32Array([0, 0.5, -0.5, 0.999, -1]);
    const out = pcm16Base64ToFloat32(float32ToPcm16Base64(input));
    expect(out.length).toBe(input.length);
    for (let i = 0; i < input.length; i++) expect(out[i]).toBeCloseTo(input[i], 3);
  });

  it("clamps out-of-range samples instead of wrapping", () => {
    const out = pcm16Base64ToFloat32(float32ToPcm16Base64(new Float32Array([2, -2])));
    expect(out[0]).toBeCloseTo(1, 3);
    expect(out[1]).toBeCloseTo(-1, 3);
  });

  it("handles an empty buffer", () => {
    expect(pcm16Base64ToFloat32(float32ToPcm16Base64(new Float32Array(0))).length).toBe(0);
  });
});
