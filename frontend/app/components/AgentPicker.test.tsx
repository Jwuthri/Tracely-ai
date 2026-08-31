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

const slugs = () =>
  Array.from(document.querySelectorAll("datalist option")).map((o) => (o as HTMLOptionElement).value);

describe("AgentPicker", () => {
  it("lists agents in natural order, digits numerically", () => {
    render(<AgentPicker agents={AGENTS} value="" onChange={() => {}} allLabel="All agents" />);
    expect(slugs()).toEqual(["agent_2", "agent_10", "Beta"]);
  });

  it("keeps the caller's order when sort is off", () => {
    render(<AgentPicker agents={AGENTS} value="" onChange={() => {}} sort={false} />);
    expect(slugs()).toEqual(["agent_10", "agent_2", "Beta"]);
  });

  it("commits only on an exact slug, not on every keystroke", async () => {
    const onChange = vi.fn();
    render(<AgentPicker agents={AGENTS} value="" onChange={onChange} allLabel="All agents" />);
    await userEvent.type(screen.getByRole("combobox"), "agent_2");
    expect(onChange.mock.calls).toEqual([["i2"]]); // "a", "ag", … matched nothing
  });

  it("stores the field the caller asked for", async () => {
    const onChange = vi.fn();
    render(<AgentPicker agents={AGENTS} value="" onChange={onChange} by="slug" allLabel="all" />);
    await userEvent.type(screen.getByRole("combobox"), "Beta");
    expect(onChange).toHaveBeenCalledWith("Beta");
  });

  it("clearing the box means all agents", async () => {
    const onChange = vi.fn();
    render(<AgentPicker agents={AGENTS} value="i2" onChange={onChange} allLabel="All agents" />);
    await userEvent.clear(screen.getByRole("combobox"));
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("cannot clear a required pick — half-typed text snaps back on blur", async () => {
    const onChange = vi.fn();
    render(<AgentPicker agents={AGENTS} value="i2" onChange={onChange} />);
    const box = screen.getByRole("combobox");
    await userEvent.clear(box);
    expect(onChange).not.toHaveBeenCalled();
    await userEvent.tab();
    expect(box).toHaveValue("agent_2");
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
    expect(screen.getByRole("combobox")).toHaveValue("agent_2");
    await userEvent.click(screen.getByText("reset"));
    expect(screen.getByRole("combobox")).toHaveValue("");
  });
});
