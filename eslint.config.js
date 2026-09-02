import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["**/dist/**", "**/.next/**", "**/node_modules/**", "**/*.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    // decisions.md, "`packages/api` is a library, not a server": it imports no
    // `next/*` module. Request primitives are read by a `packages/web` adapter
    // and passed in as strings. Enforced, not just documented.
    files: ["packages/api/**/*.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "next",
              message:
                "packages/api must not import next/*. Read request primitives in a packages/web adapter and pass them in as strings.",
            },
          ],
          patterns: [
            {
              group: ["next/*"],
              message:
                "packages/api must not import next/*. Read request primitives in a packages/web adapter and pass them in as strings.",
            },
          ],
        },
      ],
    },
  },
);
