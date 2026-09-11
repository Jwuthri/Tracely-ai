import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ConvNode } from "../../lib/api";
import { renderCell } from "./cells";
import { COLUMNS } from "./columns";

const col = COLUMNS.find((c) => c.key === "conversation")!;
const cell = (first_input: string) =>
  render(<>{renderCell(col, { level: "C", conv: { thread: "t1", first_input } as ConvNode, agentCount: 1 })}</>);

describe("conversation title cell", () => {
  it("keeps a title that fits as the link", () => {
    cell("Where is my order ORD-4471?");
    expect(screen.getByRole("link")).toHaveTextContent("Where is my order ORD-4471?");
  });

  it("shows a title too long for the column as the opening message's pill", () => {
    cell("create a sales agent for puravida https://www.puravida.com.br/whey?utm_source=google&utm_medium=cpc");
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByRole("button")).toHaveTextContent(/user\s*create a sales agent/i);
  });
});
