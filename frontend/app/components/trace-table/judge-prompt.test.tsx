import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { JudgePrompt } from "./cells";

// The judge prompt is fetched lazily from the eval recording; a step-level cell must ask for
// its own span, not the whole column.
describe("JudgePrompt", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  it("fetches on expand, scoped to the cell's span, and shows prompt + answer", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ trace_id: "eval-1", steps: [{ name: "TOOL refund", model: "gpt-x", input: "JUDGE PROMPT", output: "VERDICT" }] }),
    });
    render(<JudgePrompt subject="tr-1" level="step" name="tool_ok" step="TOOL refund" />);
    expect(fetchMock).not.toHaveBeenCalled(); // collapsed: nothing fetched

    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(screen.getByText("JUDGE PROMPT")).toBeInTheDocument());
    expect(screen.getByText("VERDICT")).toBeInTheDocument();
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("subject=tr-1");
    expect(url).toContain("level=step");
    expect(url).toContain("step=TOOL+refund");
    expect(screen.getByRole("link")).toHaveAttribute("href", "/traces/eval-1");
  });

  it("says a failed request failed instead of claiming there is no recording", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500, json: async () => ({ detail: "boom" }) });
    render(<JudgePrompt subject="tr-1" level="msg" name="on_topic" step="" />);
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(screen.getByText(/Couldn't load the prompt: boom/)).toBeInTheDocument());
  });
});
