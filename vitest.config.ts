import { fileURLToPath } from "node:url";
import { defineConfig, configDefaults } from "vitest/config";

const fromRoot = (p: string): string => fileURLToPath(new URL(p, import.meta.url));

export default defineConfig({
  test: {
    // `tsc --build` also emits <name>.test.js into each package's dist/; only run
    // the TypeScript sources.
    exclude: [...configDefaults.exclude, "**/dist/**"],
    // DB-backed suites create a scratch database and run every migration in a hook.
    hookTimeout: 60_000,
    testTimeout: 20_000,
  },
  resolve: {
    // Resolve workspace packages to their source, so tests run against current
    // code with no build step.
    alias: {
      "@osds/adapter-kit": fromRoot("./packages/adapter-kit/src/index.ts"),
      "@osds/core": fromRoot("./packages/core/src/index.ts"),
      "@osds/db": fromRoot("./packages/db/src/index.ts"),
    },
    // pnpm nests a `kysely` symlink under every package; without this the SSR
    // resolver can load two copies, and `sql` from one hands the other's Kysely
    // a no-op executor ("this query cannot be compiled to SQL").
    dedupe: ["kysely", "pg"],
  },
});
