import { describe, expect, it } from "vitest";
import { dur, modelCost, tokens, totalCost, ttft, usd } from "@/app/components/ops-format";

const model = (o: Partial<Parameters<typeof modelCost>[0]>) =>
  ({ model: "", calls: 0, p50: 0, p95: 0, ttft_p50: 0, errors: 0, tokens: 0,
     input_tokens: 0, output_tokens: 0, cost: 0, ...o }) as Parameters<typeof modelCost>[0];

describe("dur", () => {
  it("picks the unit and never shows a measured 0 as missing data", () => {
    expect(dur(0)).toBe("<1ms");
    expect(dur(16)).toBe("16ms");
    expect(dur(1000)).toBe("1.00s");
    expect(dur(31_400)).toBe("31.4s");
    expect(dur(90_000)).toBe("1.5m");
  });

  it("distinguishes an unreported TTFT from a fast one", () => {
    expect(ttft(0)).toBe("—");
    expect(ttft(120)).toBe("120ms");
  });
});

describe("usd", () => {
  it("drops precision as the number grows — no '$15680.2080'", () => {
    expect(usd(2_400_000)).toBe("$2.40M");
    expect(usd(15_680.208)).toBe("$15,680");
    expect(usd(126.6985)).toBe("$126.70");
    expect(usd(0.127)).toBe("$0.13");
  });

  it("keeps a real sub-cent amount distinct from zero", () => {
    expect(usd(0)).toBe("$0");
    expect(usd(0.0042)).toBe("<$0.01");
  });
});

describe("tokens", () => {
  it("scales to B so a big window reads as a number, not a typo", () => {
    expect(tokens(8_310_800_000)).toBe("8.3B");
    expect(tokens(1_234_567)).toBe("1.2M");
    expect(tokens(12_500)).toBe("12.5k");
    expect(tokens(999)).toBe("999");
  });
});

describe("modelCost", () => {
  it("prefers the SDK-reported cost when there is one", () => {
    expect(modelCost(model({ model: "gpt-4o", cost: 0.25, input_tokens: 1e6 }))).toBe(0.25);
  });

  it("prices from the input/output split when cost is absent", () => {
    // gpt-4o list: $2.50 / 1M in, $10 / 1M out.
    expect(modelCost(model({ model: "gpt-4o", input_tokens: 1_000_000, output_tokens: 100_000 }))).toBeCloseTo(3.5, 10);
  });

  it("charges nothing for a model outside the rate table rather than guessing", () => {
    expect(modelCost(model({ model: "acme-internal-v3", input_tokens: 1e6 }))).toBe(0);
  });

  it("still prices a namespaced/suffixed id — rateFor matches on substring", () => {
    // "openrouter/meta-llama/llama-3.1-70b-instruct" must not fall through to $0.
    expect(modelCost(model({ model: "meta-llama/llama-3.1-70b", input_tokens: 1_000_000 }))).toBeCloseTo(0.9, 10);
  });
});

describe("totalCost", () => {
  const models = [
    model({ model: "gpt-4o", input_tokens: 1_000_000, output_tokens: 100_000 }),
    model({ model: "claude-3-5-sonnet", input_tokens: 1_000_000 }), // $3 / 1M in
  ];

  it("falls back to the sum of per-model list price when nothing reported cost", () => {
    expect(totalCost({ summary: { cost: 0 } as never, models })).toBeCloseTo(6.5, 10);
  });

  it("uses the reported window cost when there is one", () => {
    expect(totalCost({ summary: { cost: 9.99 } as never, models })).toBe(9.99);
  });
});
