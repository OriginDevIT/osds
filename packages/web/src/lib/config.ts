import { normalizeHost } from "@osds/api";

/**
 * Deployment topology read once, at module load.
 *
 * `OSDS_CONSOLE_HOST` is the hostname the installation console is served at -
 * where the first-run wizard runs and where an operator accepts an invitation
 * to a directory they have no session on yet (decisions.md, "Admin surfaces").
 * It resolves to no tenant. It is topology, not a wizard setting, precisely
 * because it is where the wizard runs; there is no in-app screen that could set
 * it. So a deployment that has not set it is misconfigured, and this throws
 * rather than guessing.
 */
const raw = process.env.OSDS_CONSOLE_HOST;
if (raw === undefined || raw.trim() === "") {
  throw new Error(
    "OSDS_CONSOLE_HOST is not set. It is the hostname the installation console " +
      "is served at (deployment topology, not a wizard setting). Set it in the " +
      "environment - see .env.example.",
  );
}

/** The console hostname, normalized the same way request hosts are. */
export const CONSOLE_HOST: string = normalizeHost(raw);
