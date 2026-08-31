/**
 * A minimal RFC 6902 JSON Patch generator, hand-written so core carries no
 * dependency for it. Used to fill the `changes` field on `listing.updated`
 * (spec §3.3, §4.1) and, later, `tenant.settings_changed`.
 *
 * Pure. Given a `from` and a `to` value it returns the operations that turn one
 * into the other. Scope is deliberately narrow - it diffs the shapes core
 * actually emits (plain objects, arrays, JSON primitives):
 *
 *   - plain objects are walked key by key: a key only in `to` is an `add`, a
 *     key only in `from` is a `remove`, a key in both recurses.
 *   - arrays and primitives are compared by value; any difference is one
 *     `replace` of the whole value at that path. We never emit index-level
 *     array ops - a wholesale replace is valid RFC 6902 and keeps the output
 *     predictable for the string arrays (`categories`) this diffs.
 *
 * Keys are visited in sorted order so the operation list is deterministic.
 */

export type JsonPatchOp =
  | { readonly op: "add"; readonly path: string; readonly value: unknown }
  | { readonly op: "remove"; readonly path: string }
  | { readonly op: "replace"; readonly path: string; readonly value: unknown };

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** RFC 6901: `~` -> `~0`, `/` -> `~1`, in that order. */
function escapeToken(token: string): string {
  return token.replace(/~/g, "~0").replace(/\//g, "~1");
}

/** Structural equality for JSON values (objects, arrays, primitives). */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;

  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, i) => deepEqual(item, b[i]));
  }

  if (isPlainObject(a) && isPlainObject(b)) {
    const aKeys = Object.keys(a);
    const bKeys = Object.keys(b);
    return (
      aKeys.length === bKeys.length &&
      aKeys.every(
        (k) =>
          Object.prototype.hasOwnProperty.call(b, k) && deepEqual(a[k], b[k]),
      )
    );
  }

  return false;
}

/**
 * The RFC 6902 patch that transforms `from` into `to`. Empty when they are
 * already structurally equal. `path` is the JSON Pointer prefix for recursion
 * and defaults to the document root.
 */
export function jsonPatch(
  from: unknown,
  to: unknown,
  path = "",
): JsonPatchOp[] {
  if (deepEqual(from, to)) return [];

  if (isPlainObject(from) && isPlainObject(to)) {
    const ops: JsonPatchOp[] = [];
    const keys = [
      ...new Set([...Object.keys(from), ...Object.keys(to)]),
    ].sort();

    for (const key of keys) {
      const childPath = `${path}/${escapeToken(key)}`;
      const inFrom = Object.prototype.hasOwnProperty.call(from, key);
      const inTo = Object.prototype.hasOwnProperty.call(to, key);

      if (inFrom && !inTo) {
        ops.push({ op: "remove", path: childPath });
      } else if (!inFrom && inTo) {
        ops.push({ op: "add", path: childPath, value: to[key] });
      } else {
        ops.push(...jsonPatch(from[key], to[key], childPath));
      }
    }

    return ops;
  }

  // Arrays, primitives, or a type change between the two sides.
  return [{ op: "replace", path, value: to }];
}
