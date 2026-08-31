import { describe, it, expect } from "vitest";
import { jsonPatch, deepEqual } from "./json-patch.js";

describe("jsonPatch", () => {
  it("is empty for structurally equal values", () => {
    expect(jsonPatch({ a: 1, b: [1, 2] }, { a: 1, b: [1, 2] })).toEqual([]);
  });

  it("replaces a changed primitive at its pointer path", () => {
    expect(jsonPatch({ a: 1, b: 2 }, { a: 1, b: 3 })).toEqual([
      { op: "replace", path: "/b", value: 3 },
    ]);
  });

  it("adds a key present only in the target", () => {
    expect(jsonPatch({ a: 1 }, { a: 1, b: 2 })).toEqual([
      { op: "add", path: "/b", value: 2 },
    ]);
  });

  it("removes a key present only in the source", () => {
    expect(jsonPatch({ a: 1, b: 2 }, { a: 1 })).toEqual([
      { op: "remove", path: "/b" },
    ]);
  });

  it("recurses into nested objects", () => {
    expect(
      jsonPatch(
        { loc: { city: "A", zip: "1" } },
        { loc: { city: "B", zip: "1" } },
      ),
    ).toEqual([{ op: "replace", path: "/loc/city", value: "B" }]);
  });

  it("replaces an array wholesale rather than by index", () => {
    expect(jsonPatch({ tags: ["a", "b"] }, { tags: ["a", "c", "d"] })).toEqual([
      { op: "replace", path: "/tags", value: ["a", "c", "d"] },
    ]);
  });

  it("emits operations in sorted key order", () => {
    const ops = jsonPatch({ z: 1, a: 1 }, { z: 2, a: 2 });
    expect(ops.map((o) => o.path)).toEqual(["/a", "/z"]);
  });

  it("escapes '/' and '~' in keys per RFC 6901", () => {
    expect(jsonPatch({ "a/b": 1, "c~d": 1 }, { "a/b": 2, "c~d": 2 })).toEqual([
      { op: "replace", path: "/a~1b", value: 2 },
      { op: "replace", path: "/c~0d", value: 2 },
    ]);
  });

  it("treats null as a value, not an absent key", () => {
    expect(jsonPatch({ a: "x" }, { a: null })).toEqual([
      { op: "replace", path: "/a", value: null },
    ]);
  });
});

describe("deepEqual", () => {
  it("compares nested objects and arrays by value", () => {
    expect(deepEqual({ a: [1, { b: 2 }] }, { a: [1, { b: 2 }] })).toBe(true);
    expect(deepEqual({ a: [1, { b: 2 }] }, { a: [1, { b: 3 }] })).toBe(false);
  });

  it("distinguishes null from an object", () => {
    expect(deepEqual(null, {})).toBe(false);
  });
});
