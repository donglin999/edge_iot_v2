# XIU-108 Playwright e2e suite

Acceptance harness for the MQTT migration's frontend slice (Phase 2 P5.2).

## Layout

| Path | What it is |
|---|---|
| `playwright.config.ts` (in `frontend/`) | Project config; outputs land in `frontend/e2e/.artifacts/`. |
| `specs/fleet.spec.ts` | `/fleet` dual-edge online → broker stop → offline → restart → online. |
| `specs/data.spec.ts` | `/data` history fetch keeps working under MQTT-form transport. |
| `specs/alarms.spec.ts` | `/alarms` MQTT `alarm_event` surfaces in the list. |
| `specs/configuration.spec.ts` | `/import` Excel → validate → apply → new `ConfigVersion`. |
| `helpers/` | Shared API + docker helpers used by the specs. |
| `fixtures/` | Test inputs (e.g. a known-good xlsx). |

## Run

The suite assumes the docker stacks for the center and two edges are
already up and reachable. Drive everything end-to-end through:

```sh
./scripts/qa_xiu108_playwright.sh
```

That script:

1. `docker compose -f docker-compose.center.yml up -d --build` — center
   stack (django + redis + mosquitto + frontend).
2. Brings up 2 edge stacks under compose project names
   `xiu108-qa-xiu108-a` / `xiu108-qa-xiu108-b` (skipped if their
   `EDGE_A_TOKEN` / `EDGE_B_TOKEN` env vars aren't exported, e.g. when
   the operator brought them up manually with a different naming).
3. Waits for both edges to flip to `online`.
4. Runs `npx playwright test --project=chromium`.
5. Copies screenshots + html-report + JSON results under
   `/tmp/mqtt-evidence/playwright-xiu108/`.

## Run manually (already-up stack)

```sh
cd frontend
E2E_EDGE_A=qa-xiu108-a E2E_EDGE_B=qa-xiu108-b \
  npx playwright test --project=chromium
```

Override the UI/API endpoints with `E2E_BASE_URL` / `E2E_API_URL`.

## Skip behaviour

Each spec runs a preflight against `/api/fleet/edges/`. If the expected
edges aren't registered or online, the spec calls `test.skip()` with a
clear message — it does NOT fail. That way the harness is safe to wire
into CI before the operator has finished the docker-side bring-up.

## Evidence

After a green run, the following land under
`/tmp/mqtt-evidence/playwright-xiu108/`:

```
screenshots/
  fleet-01-both-online.png
  fleet-02-both-offline.png
  fleet-03-recovered.png
  data-01-history-fetched.png
  alarms-01-mqtt-row.png
  configuration-01-validated.png
  configuration-02-applied.png
html-report/index.html
results.json
```

The 2-up demo before/after comparison images (fleet + alarms) are the
existing `*-before.png` (captured against the WS-form baseline by the
M7 acceptance run) compared side-by-side to `fleet-01-both-online.png`
and `alarms-01-mqtt-row.png` from this suite. Once captured, drop them
into `/tmp/mqtt-evidence/comparison/` for the demo deck.
