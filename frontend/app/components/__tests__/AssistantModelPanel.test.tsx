import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AssistantModelPanel } from "@/app/components/AssistantModelPanel";

const models = [
  { id: "anthropic/claude-sonnet-5.5", label: "Anthropic Claude Sonnet 5.5" },
  { id: "anthropic/claude-opus-5.5", label: "Anthropic Claude Opus 5.5" },
];
const base = { model: "anthropic/claude-sonnet-5.5", default: "anthropic/claude-sonnet-5.5", custom: false, models };

describe("AssistantModelPanel", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const { model } = JSON.parse(String(init.body));
        return new Response(JSON.stringify({ ...base, model: model ?? base.default, custom: !!model }), { status: 200 });
      }
      return new Response(JSON.stringify(base), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  it("shows the server default and pins a picked model", async () => {
    render(<AssistantModelPanel />);
    const select = (await screen.findByLabelText("Assistant model")) as HTMLSelectElement;
    expect(select.value).toBe("");
    expect(screen.getByText("Default (Anthropic Claude Sonnet 5.5)")).toBeTruthy();

    fireEvent.change(select, { target: { value: "anthropic/claude-opus-5.5" } });
    await waitFor(() => expect(screen.getByText(/The next message uses it/)).toBeTruthy());
    const put = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    expect(JSON.parse(String((put![1] as RequestInit).body))).toEqual({ model: "anthropic/claude-opus-5.5" });
    expect(select.value).toBe("anthropic/claude-opus-5.5");

    // "Default" sends null, which clears the workspace's pin
    fireEvent.change(select, { target: { value: "" } });
    await waitFor(() => expect(select.value).toBe(""));
    const last = fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "PUT").at(-1);
    expect(JSON.parse(String((last![1] as RequestInit).body))).toEqual({ model: null });
  });
});
