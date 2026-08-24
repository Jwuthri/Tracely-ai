import { describe, expect, it } from "vitest";

import { EvaluatorActionError, deleteColumnError } from "./evaluators";

// Deleting a metric column is refused for a machine credential (an ingest key has no role, by
// design — a key leaked from CI must not be able to delete what people built). The banner has to
// say that, not echo the API's "insufficient role", which reads as the product being broken.
describe("deleteColumnError", () => {
  it("explains a refusal and what the user needs instead", () => {
    const msg = deleteColumnError(new EvaluatorActionError("insufficient role", 403), "Answer quality");
    expect(msg).toContain("Answer quality");
    expect(msg).toContain("signed-in");
    expect(msg).not.toContain("insufficient role");
  });

  it("tells an expired session to sign in again", () => {
    const msg = deleteColumnError(new EvaluatorActionError("unauthorized", 401), "Run outcome");
    expect(msg).toMatch(/expired/i);
    expect(msg).toContain("Run outcome");
  });

  it("treats a missing column as someone else's delete, not an error to fear", () => {
    const msg = deleteColumnError(new EvaluatorActionError("evaluator not found", 404), "Tool success");
    expect(msg).toMatch(/no longer exists/i);
  });

  it("falls back to the underlying message for anything else", () => {
    expect(deleteColumnError(new EvaluatorActionError("boom", 500), "X")).toContain("boom");
    expect(deleteColumnError(new Error("network down"), "X")).toContain("network down");
    expect(deleteColumnError("not an error", "X")).toContain("unknown error");
  });
});
