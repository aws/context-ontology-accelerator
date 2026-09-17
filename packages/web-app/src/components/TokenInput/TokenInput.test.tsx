// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { TokenInput } from "./TokenInput";

describe("TokenInput", () => {
  it("renders existing values as chips", () => {
    render(<TokenInput value={["orders", "customers"]} onChange={vi.fn()} />);
    expect(screen.getByText("orders")).toBeInTheDocument();
    expect(screen.getByText("customers")).toBeInTheDocument();
  });

  it("adds a value when the add button is clicked", () => {
    const onChange = vi.fn();
    render(
      <TokenInput value={[]} onChange={onChange} ariaLabel="Table allowlist" />,
    );
    fireEvent.change(screen.getByLabelText("Table allowlist"), {
      target: { value: "orders" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add/i }));
    expect(onChange).toHaveBeenCalledWith(["orders"]);
  });

  it("adds a value when Enter is pressed", () => {
    const onChange = vi.fn();
    render(
      <TokenInput value={["orders"]} onChange={onChange} ariaLabel="Tables" />,
    );
    const input = screen.getByLabelText("Tables");
    fireEvent.change(input, { target: { value: "products" } });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    expect(onChange).toHaveBeenCalledWith(["orders", "products"]);
  });

  it("trims whitespace before adding", () => {
    const onChange = vi.fn();
    render(<TokenInput value={[]} onChange={onChange} ariaLabel="Tables" />);
    fireEvent.change(screen.getByLabelText("Tables"), {
      target: { value: "  orders  " },
    });
    fireEvent.click(screen.getByRole("button", { name: /add/i }));
    expect(onChange).toHaveBeenCalledWith(["orders"]);
  });

  it("ignores duplicate values", () => {
    const onChange = vi.fn();
    render(
      <TokenInput value={["orders"]} onChange={onChange} ariaLabel="Tables" />,
    );
    fireEvent.change(screen.getByLabelText("Tables"), {
      target: { value: "orders" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add/i }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does not add a blank value", () => {
    const onChange = vi.fn();
    render(<TokenInput value={[]} onChange={onChange} ariaLabel="Tables" />);
    fireEvent.change(screen.getByLabelText("Tables"), {
      target: { value: "   " },
    });
    // Add button is disabled for blank input; Enter is a no-op.
    fireEvent.keyDown(screen.getByLabelText("Tables"), { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("removes a value when its chip is dismissed", () => {
    const onChange = vi.fn();
    render(
      <TokenInput
        value={["orders", "customers"]}
        onChange={onChange}
        ariaLabel="Tables"
      />,
    );
    fireEvent.click(screen.getByLabelText("Remove orders"));
    expect(onChange).toHaveBeenCalledWith(["customers"]);
  });

  it("rejects a value that fails validation and shows the error", () => {
    const onChange = vi.fn();
    render(
      <TokenInput
        value={[]}
        onChange={onChange}
        ariaLabel="Tables"
        validate={(c) => (c.includes(" ") ? "No spaces allowed" : null)}
      />,
    );
    fireEvent.change(screen.getByLabelText("Tables"), {
      target: { value: "bad name" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add/i }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText("No spaces allowed")).toBeInTheDocument();
  });

  // Committing several entries at once. Before this was supported, pasting a
  // comma-separated list produced ONE token containing the commas, which passed
  // validation and silently gave the caller a nonsense value.
  describe("committing several entries at once", () => {
    /** Type `value` into the input and commit it with the add button. */
    const commit = (value: string) => {
      fireEvent.change(screen.getByLabelText("Tables"), { target: { value } });
      fireEvent.click(screen.getByRole("button", { name: /add/i }));
    };

    it.each([
      ["commas", "orders, customers, refunds"],
      ["tabs", "orders\tcustomers\trefunds"],
      ["mixed separators", "orders,\ncustomers\t refunds"],
    ])("splits on %s", (_name, input) => {
      const onChange = vi.fn();
      render(<TokenInput value={[]} onChange={onChange} ariaLabel="Tables" />);
      commit(input);
      expect(onChange).toHaveBeenCalledWith(["orders", "customers", "refunds"]);
    });

    // A single-line <input> runs the HTML value sanitization algorithm, which
    // strips CR and LF. A newline-separated list therefore arrives with its
    // separators already gone — there is nothing left to split on, and no code
    // in this component can recover the boundaries. Pinned so nobody "fixes"
    // the regex expecting it to help: only a <textarea> can accept such a paste.
    it("cannot recover entries from a newline-separated list", () => {
      const onChange = vi.fn();
      render(<TokenInput value={[]} onChange={onChange} ariaLabel="Tables" />);
      commit("orders\ncustomers\nrefunds");
      expect(onChange).toHaveBeenCalledWith(["orderscustomersrefunds"]);
    });

    it("collapses separator runs rather than adding blank entries", () => {
      const onChange = vi.fn();
      render(<TokenInput value={[]} onChange={onChange} ariaLabel="Tables" />);
      commit("orders,,  ,customers,");
      expect(onChange).toHaveBeenCalledWith(["orders", "customers"]);
    });

    it("appends to existing values instead of replacing them", () => {
      const onChange = vi.fn();
      render(
        <TokenInput
          value={["orders"]}
          onChange={onChange}
          ariaLabel="Tables"
        />,
      );
      commit("customers, refunds");
      expect(onChange).toHaveBeenCalledWith(["orders", "customers", "refunds"]);
    });

    it("adds a value repeated within one paste only once", () => {
      const onChange = vi.fn();
      render(<TokenInput value={[]} onChange={onChange} ariaLabel="Tables" />);
      commit("orders, orders");
      expect(onChange).toHaveBeenCalledWith(["orders"]);
    });

    it("passes the running list to validate, so the cap spans the paste", () => {
      const onChange = vi.fn();
      // Stands in for MAX_VOCABULARY_ENTRIES: refuse anything past two entries.
      render(
        <TokenInput
          value={[]}
          onChange={onChange}
          ariaLabel="Tables"
          validate={(_c, existing) =>
            existing.length >= 2 ? "At most 2 allowed" : null
          }
        />,
      );
      commit("a, b, c, d");
      expect(onChange).toHaveBeenCalledWith(["a", "b"]);
      expect(screen.getByText(/2 skipped/)).toBeInTheDocument();
    });

    it("keeps the valid entries and reports the rejected ones", () => {
      const onChange = vi.fn();
      render(
        <TokenInput
          value={[]}
          onChange={onChange}
          ariaLabel="Tables"
          itemLabelPlural="tables"
          validate={(c) => (c.includes(" ") ? `"${c}" has a space` : null)}
        />,
      );
      commit("orders, bad name, customers");
      expect(onChange).toHaveBeenCalledWith(["orders", "customers"]);
      expect(screen.getByText("Added 2 tables.")).toBeInTheDocument();
      expect(screen.getByText(/1 skipped/)).toBeInTheDocument();
      expect(screen.getByText(/has a space/)).toBeInTheDocument();
    });

    it("reports one identical reason once rather than per entry", () => {
      render(
        <TokenInput
          value={[]}
          onChange={vi.fn()}
          ariaLabel="Tables"
          validate={() => "Not allowed"}
        />,
      );
      commit("a, b, c");
      expect(screen.getByText("3 skipped. Not allowed")).toBeInTheDocument();
    });

    it("clears the input after a multi-entry commit", () => {
      render(<TokenInput value={[]} onChange={vi.fn()} ariaLabel="Tables" />);
      commit("orders, customers");
      expect(screen.getByLabelText("Tables")).toHaveValue("");
    });

    it("retains a single rejected entry so it can be corrected", () => {
      render(
        <TokenInput
          value={[]}
          onChange={vi.fn()}
          ariaLabel="Tables"
          validate={() => "Not allowed"}
        />,
      );
      commit("bad");
      // Only the plain error, no batch summary, and the text stays put.
      expect(screen.getByLabelText("Tables")).toHaveValue("bad");
      expect(screen.getByText("Not allowed")).toBeInTheDocument();
      expect(screen.queryByText(/skipped/)).not.toBeInTheDocument();
    });

    it("does not announce a count when only one entry survives", () => {
      const onChange = vi.fn();
      render(
        <TokenInput
          value={[]}
          onChange={onChange}
          ariaLabel="Tables"
          itemLabelPlural="tables"
          validate={(c) => (c === "b" ? "no" : null)}
        />,
      );
      commit("a, b");
      expect(onChange).toHaveBeenCalledWith(["a"]);
      expect(screen.queryByText(/^Added/)).not.toBeInTheDocument();
    });
  });
});
