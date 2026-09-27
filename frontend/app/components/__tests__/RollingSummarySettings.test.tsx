import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RollingSummarySettings } from "@/app/components/trace-table/RollingSummarySettings";

const cfg = { config: { max_tokens: 16000, step_max_tokens: 512 }, defaults: { max_tokens: 16000, step_max_tokens: 512 }, custom: false };

describe("RollingSummarySettings", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        return new Response(JSON.stringify({ ...cfg, config: body, custom: true }), { status: 200 });
      }
      return new Response(JSON.stringify(cfg), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  it("loads the workspace budget and saves an edited one", async () => {
    render(<RollingSummarySettings />);
    fireEvent.click(screen.getByLabelText("Rolling summary settings"));
    const whole = (await screen.findByLabelText("Whole summary tokens")) as HTMLInputElement;
    await waitFor(() => expect(whole.value).toBe("16000"));
    fireEvent.click(screen.getByText("12k"));
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(screen.getByText(/Applies to summaries built from now on/)).toBeTruthy());
    const put = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    expect(JSON.parse(String((put![1] as RequestInit).body))).toEqual({ max_tokens: 12000, step_max_tokens: 512 });
  });
});
