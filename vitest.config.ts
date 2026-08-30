import { defineConfig, configDefaults } from "vitest/config";

export default defineConfig({
  test: {
    // `tsc --build` also emits <name>.test.js into each package's dist/; only run
    // the TypeScript sources.
    exclude: [...configDefaults.exclude, "**/dist/**"],
  },
});
