// @ts-check
import * as path from "node:path";
import { loadEnvFile } from "node:process";

// The repo keeps a single .env at its root (see .env.example). Next only
// auto-loads .env from the package directory, so pull the root file in here.
// Anchored to this file, not the cwd. Mirrors packages/db/src/migrate.ts.
try {
  loadEnvFile(path.resolve(import.meta.dirname, "../../.env"));
} catch {
  // No repo-root .env - rely on the ambient environment.
}

// Kept out of the server bundle and required at runtime instead:
//   - pg           : optional native bindings, dynamic requires
//   - @osds/db     : its entrypoint runs `import.meta.dirname` at import time
//                    (migration loader), which webpack cannot evaluate
const EXTERNAL = new Set(["pg", "@osds/db"]);

/** @type {import('next').NextConfig} */
const nextConfig = {
  serverExternalPackages: [...EXTERNAL],
  webpack: (config, { isServer }) => {
    if (isServer) {
      // `serverExternalPackages` doesn't reliably externalise a pnpm
      // `workspace:*` symlink (it resolves to in-repo source), so force it.
      const externals = Array.isArray(config.externals)
        ? config.externals
        : [config.externals].filter(Boolean);
      config.externals = [
        /** @param {{ request?: string }} ctx */
        ({ request }, callback) => {
          if (request && EXTERNAL.has(request)) {
            callback(null, `commonjs ${request}`);
            return;
          }
          callback();
        },
        ...externals,
      ];
    }
    return config;
  },
};

export default nextConfig;
