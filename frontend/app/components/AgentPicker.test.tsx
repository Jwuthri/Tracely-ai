import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { AgentPicker } from "./AgentPicker";

const AGENTS = [
  { id: "i10", slug: "agent_10", display_name: "Ten" },
  { id: "i2", slug: "agent_2", display_name: "Two" },
  { id: "i1", slug: "Beta", display_name: "Beta" },
];

const box = () => screen.getByRole("combobox");
const rows = () => screen.getAllByRole("option").map((o) => o.textContent?.replace(/^✓/, "").trim());

describe("AgentPicker", () => {
  it("lists agents in natural order, digits numerically", async () => {
    render(<AgentPicker agents={AGENTS} value="" onChange={() => {}} allLabel="All agents" />);
    await userEvent.click(box());
    expect(rows()).toEqual(["All agents", "agent_2", "agent_10", "Beta"]);
  });

  it("keeps the caller's order when sort is off", async () => {
    render(<AgentPicker agents={AGENTS} value="" onChange={() => {}} sort={false} />);
    await userEvent.click(box());
    expect(rows()).toEqual(["agent_10", "agent_2", "Beta"]);
  });

  it("filters on a substring, not just a prefix", async () => {
    render(<AgentPicker agents={AGENTS} value="" onChange={() => {}} allLabel="All agents" />);
    await userEvent.type(box(), "et");
    expect(rows()).toEqual(["Beta"]); // the "all agents" row drops out of a search
  });

  it("typing filters but never commits — only a click does", async () => {
    const onChange = vi.fn();
    render(<AgentPicker agents={AGENTS} value="" onChange={onChange} allLabel="All agents" />);
    await userEvent.type(box(), "agent_2");
    expect(onChange).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("option", { name: /agent_2/ }));
    expect(onChange).toHaveBeenCalledWith("i2");
  });

  it("stores the field the caller asked for", async () => {
    const onChange = vi.fn();
    render(<AgentPicker agents={AGENTS} value="" onChange={onChange} by="slug" allLabel="all" />);
    await userEvent.click(box());
    await userEvent.click(screen.getByRole("option", { name: /Beta/ }));
    expect(onChange).toHaveBeenCalledWith("Beta");
  });

  it("arrow keys + Enter pick a row", async () => {
    const onChange = vi.fn();
    render(<AgentPicker agents={AGENTS} value="" onChange={onChange} />);
    await userEvent.click(box());
    await userEvent.keyboard("{ArrowDown}{Enter}"); // agent_2 → agent_10
    expect(onChange).toHaveBeenCalledWith("i10");
  });

  it("offers 'all agents' only where the caller allows an empty value", async () => {
    const { unmount } = render(<AgentPicker agents={AGENTS} value="i2" onChange={() => {}} />);
    await userEvent.click(box());
    expect(rows()).not.toContain("All agents");
    unmount();

    const onChange = vi.fn();
    render(<AgentPicker agents={AGENTS} value="i2" onChange={onChange} allLabel="All agents" />);
    await userEvent.click(box());
    await userEvent.click(screen.getByRole("option", { name: "All agents" }));
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("a half-typed search never survives the panel closing", async () => {
    render(<AgentPicker agents={AGENTS} value="i2" onChange={() => {}} />);
    await userEvent.type(box(), "Bet");
    await userEvent.keyboard("{Escape}");
    expect(box()).toHaveValue("agent_2");
  });

  it("follows the value when it changes from outside", async () => {
    function Harness() {
      const [v, setV] = useState("i2");
      return (
        <>
          <button onClick={() => setV("")}>reset</button>
          <AgentPicker agents={AGENTS} value={v} onChange={setV} allLabel="All agents" />
        </>
      );
    }
    render(<Harness />);
    expect(box()).toHaveValue("agent_2");
    await userEvent.click(screen.getByText("reset"));
    expect(box()).toHaveValue("");
  });
});
