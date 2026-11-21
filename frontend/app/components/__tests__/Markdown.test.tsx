import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Markdown } from "../Markdown";

const html = (md: string) => renderToStaticMarkup(<Markdown content={md} />);

describe("Markdown tables", () => {
  it("renders a pipe table as a table, header and links included", () => {
    const out = html("Your audits:\n| Audit | Rate |\n|---|---|\n| [Form check](/audits/1) | **0%** |\n| Other | none |\n\nDone.");
    expect(out).toContain("<th");
    expect(out.match(/<tr>/g)).toHaveLength(3);
    expect(out).toContain('href="/audits/1"');
    expect(out).toContain("<strong");
    expect(out).not.toContain("---");
    expect(out).toContain("Your audits:");
    expect(out).toContain("Done.");
  });

  it("does not hang on a stray pipe line", () => {
    expect(html("| not a table")).toContain("not a table");
  });
});
