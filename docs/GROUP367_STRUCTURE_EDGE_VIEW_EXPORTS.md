# group367 - Vercel build fix: structureView edge/calibration exports

Vercel `tsc` failed (12 errors) because `components/StructureEdgeCard.tsx` imports `calibrationView`, `edgeView`, `pctText` and
`StructureEdgeDoc` from `structureView.ts`, which did not export them (the other 8 errors were `implicit any` knock-ons).

## Change (frontend only)
- `src/structureView.ts`: added `StructureEdgeDoc`, `pctText()`, `edgeView()`, `calibrationView()` (+ row/view types). They read the
  real `GET /structure/edge[/{mode}]` shape (`stats` from `observation.aggregate`, `calibration` from `observation.calibrate`,
  `recent`). Defensive: null / partial / garbage input never throws and gives an honest "not enough data" view.
- `src/structureView.test.mjs`: new assertions for the three helpers (`node --experimental-strip-types src/structureView.test.mjs`).

## Verified
- Node test file passes. `tsc --strict` over the card, `StructureGateCard.tsx` and `structureView.ts` is clean (React types stubbed:
  `node_modules` not installed in the sandbox). Full `npm run build` NOT run; run `scripts/docker_frontend_check.sh`.

## Not changed
`StructureEdgeCard` is not imported by any tab yet and the frontend has no loader for `/structure/edge`; wiring it in is a separate step.
