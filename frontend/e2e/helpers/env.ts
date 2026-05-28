/**
 * Shared env defaults for the XIU-108 Playwright suite.
 */

export const API_URL =
  process.env.E2E_API_URL?.replace(/\/$/, '') ?? 'http://localhost:8000';

export const EDGE_A = process.env.E2E_EDGE_A ?? 'qa-xiu108-a';
export const EDGE_B = process.env.E2E_EDGE_B ?? 'qa-xiu108-b';

export const BROKER_CONTAINER =
  process.env.E2E_BROKER_CONTAINER ?? 'center-mosquitto';
export const EDGE_A_CONTAINER =
  process.env.E2E_EDGE_A_CONTAINER ?? `edge-agent-${EDGE_A}`;
export const EDGE_B_CONTAINER =
  process.env.E2E_EDGE_B_CONTAINER ?? `edge-agent-${EDGE_B}`;

/** Where the run helper drops per-spec evidence under /tmp. */
export const EVIDENCE_DIR =
  process.env.E2E_EVIDENCE_DIR ?? '/tmp/mqtt-evidence';
