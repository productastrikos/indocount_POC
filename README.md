# Indocount Smart Factory Intelligence Platform (POC)

One integrated application that follows a pillow from raw-material purchase order to delivered truckload across Indocount's three US plants (Kernersville NC, Phoenix AZ, Columbus OH). All seven pages read and write one relational model. A simulation engine moves material through every stage, so an event in one stage changes the others.

> **All data is simulated.** Predictions (machine health, shipment risk) are illustrative models on simulated data, and detention charges are illustrative estimates ($350 per qualifying truck, configurable).

## Quick start

### Docker (PostgreSQL)
```bash
docker compose up --build
# UI  http://localhost:3000      API docs  http://localhost:8000/docs
```
On its first start the backend runs the Alembic migrations, seeds the demo data and starts the simulation. Seeding takes about 30 seconds.

### Local development (SQLite)
```bash
# backend
cd backend
python -m venv .venv && .venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000

# frontend (repo root, second terminal)
npm install
npm start                                               # http://localhost:3000 (proxies /api to :8000)
```

### Single-port mode
After `npm run build`, the backend also serves the built UI, so `uvicorn app.main:app --port 8000` gives the whole app at http://localhost:8000.

### Demo accounts
| User | Password | Role |
|---|---|---|
| `admin` | `admin123` | Administrator: everything, including simulation controls |
| `exec` | `exec123` | Executive (read-only) |
| `plant` | `plant123` | Plant Manager |
| `maint` | `maint123` | Maintenance Manager |
| `quality` | `quality123` | Quality Supervisor (carton overrides) |
| `warehouse` | `wh123` | Warehouse Operator |
| `procure` | `proc123` | Procurement Officer |
| `logistics` | `log123` | Logistics Coordinator |

The login screen lists them; click one to sign in.

## What each page does

| Page | Operational workspace |
|---|---|
| **Executive Overview** | 10 calculated KPIs with drill-downs (output vs target, efficiency vs previous shift, OEE = A×P×Q, downtime by cause, workforce utilization, labour cost per pillow incl. shift-2 differential, packaging accuracy, inventory availability, shipment readiness, potential detention). Also: the **Digital Twin** entry (live US network map), **S!AP advisories**, hourly actual vs target, factory comparison, lifecycle stage health, and a ranked *Requires Attention* list that links to the affected record. |
| **Procurement & Inbound** | Purchase orders (create/update), inbound shipments (Scheduled → In Transit → Arrived → Receiving → Received/Discrepancy), and goods receipt with scan simulation. A goods receipt records shortages and damage separately and updates stock only when confirmed. Also: material days-of-cover, supplier performance, and AI reorder and expedite recommendations. |
| **Production & Manufacturing** | Process pipeline (Raw Material Prep → Mixing → Flapper → Bun Formation → Filling → Sewing → Transfer) with live bottleneck detection. Also: a 3D **digital twin** of the pillow plant with every stage highlighted, CCTV with vision analytics, 8 lines per plant, production orders, hourly monitoring, machine health (0–100 score, anomaly detection, maintenance prediction, work orders), **predictive maintenance** analytics, workforce productivity and labour cost, changeovers (3-min standard), and the **Optimize Production Plan** scheduler with before/after comparison and accept/reject. |
| **Quality & Packaging** | Packaging workflow; packing stations that read the SKU spec, count pillows and block wrong counts until rework or a supervisor override with a reason. Also: simulated cameras (OpenCV counting on generated frames, labelled SIMULATED), defect history and quality analytics. |
| **Warehouse & Inventory** | Zone/bay/rack/level layout with occupancy, finished-goods receiving by carton/pallet scan, and put-away (failed cartons are refused). Also: allocation with shortages linked to production orders, picking, and workload/workforce planning. |
| **Dispatch & Delivery** | Order readiness (production → quality → inventory → allocation → picking), truck and dock scheduling with conflict prevention, shipment risk prediction with actions (prioritize, reallocate, expedite picking, reschedule truck), detention monitoring, and loading, departure and delivery confirmation. |
| **Predictive Maintenance** | Reliability view of every production machine (fibre openers, flapper units, Garnett and blow-line motors, filling heads; 96 across the three plants). *Failure prediction & plan*: machines at risk, expected failures, pillows at risk and avoidable loss, a risk matrix (7-day failure probability vs pillows lost), remaining useful life with P10–P90 bands, a 14-day failure forecast, MTBF/MTTR, the recommended maintenance plan with one-click scheduling, fill-weight drift, energy anomalies, spare-parts demand and technician workload. *Machine health & work orders*: health scores, telemetry vs baseline, anomaly detection and work orders per machine. The same views are also tabs on the Production page. |
| **AI Command Center** | Natural-language questions answered by data tools over the same database, with charts, tables, recommended actions and links to records. Uses Claude when configured; otherwise a local engine answers from the same tools. |

## Digital twin
Open it from the **Digital twin** card on the Executive Overview (deep link: `/?twin=us`, or `/?twin=KNV|PHX|CMH` for a plant).

* **US network map.** The three plants on a real US map (us-atlas, Albers projection) with live status, output vs target, lines running, RFID badges at station/away, top alert, and the trucks currently on the road to customer DCs (destinations illustrative). Click a plant to open its twin.
* **Facility twin (Three.js).** A top view of the plant, with a 3D orbit view as an option. Each of the 8 lines has its stations in process order: fibre opener → flapper → Garnett carding or blow unit → filling head → sewing tables (2 sewers) → bagging & compression press → conveyor to carton packing. Downstream are the packing cells with CV cameras, the QA hold cage, palletizing, finished-goods racks (real slot occupancy by zone/bay/rack/level), staging lanes and docks with live trucks. The receiving and raw-material stacks, break room, canteen, restrooms, lockers, supervisor desk, maintenance crib and material store complete the floor plan.
* **Pillows at every step.** Each line shows pillows filled, sewn and bagged today and the work-in-process waiting between stations. The pillow-flow strip covers the whole plant: receiving → fibre prep → filling → sewing → bagging → carton packing → QA hold → palletizing → FG racks → dispatch. Each step shows the operators working it (at station / away), its availability, and pillows per worker-hour.
* **RFID workforce tracking.** Every employee has a passive UHF badge (simulated). Zone readers at stations and doors write badge intervals (`rfid_intervals`): *Station*, *Away* (unscheduled) or *Break* (scheduled breaks and lunch, never counted as time away). The twin shows each person on the floor coloured by state, with a pulsing ring when someone has been away for more than 10 minutes. The *Workforce · RFID* panel shows time at station, time away by zone (restroom, material store, out of coverage, break overruns, …), the hourly trend, role comparison, and a searchable worker list. Each worker's drawer has their shift timeline, effectiveness (availability × output vs standard), longest trips, and a comparison with peers and previous days. Maintenance technicians are dispatched to broken machines, and their badges follow them.
* **Production digital twin.** A live card on the Production page (beside the S!AP advisories) shows the plant model with its stage chips; *Open full twin* opens Production → *Digital twin* on the same plant and stage. The full tab is a walk-through 3D model of the selected plant, built from the manufacturing-canvas reference supplied for this project (a biscuit-factory twin, kept outside the repo) and re-tooled for pillows. One pillow line (bale feeder, fibre opener and flapper, fibre bin and ducts, Garnett card or blow unit, filling head, two sewing tables, bagging and compression press, outfeed conveyor, CV packing station) is replicated across the plant's 8 lines, inside an expanded 264 × 128 m hall with receiving, QA cage, palletizing, high-bay racks (real occupancy), 4 docks with live trucks, utilities, maintenance crib and amenities. The 11 stages are numbered in the scene and in the stage strip; selecting one highlights that stage on every line, flies the camera there and opens its live panel (output, machines, RFID staffing, alerts, S!AP actions). Also: Overview/Front/Side/Top cameras, display toggles, and a process walkthrough that carries a token from fibre bale to truck. Code: `src/pages/production/FactoryTwinTab.js` and `factoryTwin/` (scene, stage catalogue, styles).
* **Causality.** Time away is not decoration. A sewer away from the table reduces that line's sewing capacity in the engine. Operators away grow the WIP buffers in front of their station. Per-person output is credited only for time at the station.

## S!AP advisories and predictive maintenance
The **S!AP advisories** panel (sparkles button in the header, the Executive Overview card and the twin's S!AP tab) ranks predictive, prescriptive advisories. Each comes with evidence, a confidence figure and an action.

* **Machine-repair prediction** (`services/predictive.py`). A Weibull reliability model per machine type, with a proportional-hazards adjustment for current condition (health score, vibration/temperature vs baseline, wear). It is blended with the vibration-trend time to the alarm threshold, taking whichever is more conservative. It gives failure probability within 24 h, 72 h and 7 days, remaining useful life with a P10–P90 band, and the likely failure mode (bearing wear, belt slip, pneumatic seal leak, impeller imbalance, …). It also estimates the impact on pillows and orders, and recommends a maintenance window: the 23:00–06:00 non-production window loses no pillows. The comparison with running to failure is quantified, and **Schedule** creates the predictive work order.
* **Related predictions:** fill-weight / under-fill risk from filling-head pressure, energy anomalies, spare-parts demand, maintenance-technician capacity, and RFID workforce patterns (people away now, chronic time away, material-store trips, break overruns).
* **Predictive Maintenance module** (sidebar, also the Production → *Predictive maintenance* tab): risk matrix, 14-day failure and downtime forecast, remaining-life ranking, MTBF/MTTR, maintenance plan and per-machine probability curves, plus machine health and work orders.

All models run on simulated telemetry and are labelled illustrative.

## CCTV · Vision AI
Plant-floor footage (four phone clips from a pillow plant) is turned into 20 CCTV camera feeds, one for every CV camera in the data model plus fibre-opening and carding cameras, each with computer-vision analytics drawn over the video.

* **Where it appears:** Quality → Cameras (carton-packing and inspection cameras, replacing the generated frames), Production → *CCTV · Vision AI* (every line camera, filterable by process step), Warehouse → *Staging CCTV*, Dispatch → *Dispatch lane CCTV*, and the facility twin's *CCTV* tab. Any tile opens a full-screen viewer with layer toggles, an activity timeline you can seek, and the analytics panel.
* **What the overlay shows:** people tracked with a box, skeleton and activity (handling, lifting, walking, idle with a running timer). Station workers are named by fusing the track with the RFID badge read at that station (no facial recognition). Pillows are tracked with motion trails and counted into each carton opening (+1 / +2-pack, carton progress, *carton complete*). Carton print and labels are read by OCR and matched to the customer (e.g. "Dept. #22 · Bedding · SERTA BED PILLOWS" with an RFID mark → Walmart; the Beautyrest BLACK club display pallet → Costco), together with the matching open order from the live data. Also: conveyor belt speed from optical flow, pillows counted across a line, idle and left-station alerts, an occupancy heatmap, and the camera OSD with plant time.
* **Replicated feeds:** `tools/cctv/render.py` cuts each clip into camera segments, stabilises them (fixed cameras are registered to a reference frame with ORB + RANSAC; walking shots become smooth PTZ tours), re-frames them per camera (crop, mounting angle, roll, wide-angle lens), applies that camera's sensor look (white balance, contrast, saturation, noise, vignette, sharpening) and encodes a seamless 15 fps H.264 loop. The camera-to-clip mapping, zones and label rules are in `tools/cctv/feeds.py`.
* **Analysis:** `tools/cctv/analyze.py` runs on every rendered feed. People: YOLO11m + ByteTrack with YOLO11s-pose keypoints matched to the tracks on packing cameras, and YOLO11s-pose tracking directly (one pass, keypoints on every box) on line and area cameras; fragments of a person lost behind a carton are re-joined per station. Activity: a keypoint/motion classifier. Pillows: MOG2 motion × colour segmentation with blob tracking on fixed cameras, YOLOE open-vocabulary detection (pillows, cartons, pallets) on PTZ tours. Placements: on the club display a pillow track that descends into the opening; on retail cartons, where the pile behind is just as white, the packer's wrists lifting above the rim with something white and plunging into the carton, merged with descending blob tracks. Labels: RapidOCR (PP-OCR) fuzzy-matched to the label vocabulary. Conveyors: Farnebäck optical flow for belt speed and a virtual line sensor (flow at the count line) for pillows passing. Results go to `public/cctv/<id>.json` and the catalogue to `backend/app/cv/cctv_catalog.json`; the browser replays them in sync with the video, the way an edge device would stream them. `GET /api/cctv/feeds` and `/api/cctv/feeds/{id}` add the live context (station, line, RFID badges at the station, customer order, today's counts).
* **Regenerating:** the CV toolchain is kept out of the app runtime. Create `.cvenv` (Python 3.12) with `torch`/`torchvision` (CPU), `ultralytics`, `imageio-ffmpeg`, `rapidocr-onnxruntime` and `lap`, put the four source clips (kept out of git; file names in `tools/cctv/feeds.py`) in the repo root, then run `.cvenv/Scripts/python tools/cctv/render.py` and, from `.cvenv/models`, `../Scripts/python ../../tools/cctv/analyze.py` (optionally with feed ids).

The footage is recorded, not live; the overlays are real model output on that footage. Customer mappings for the carton labels are configurable in `tools/cctv/feeds.py`.

## Cross-stage behaviour
* **Material shortage.** A delayed shipment lowers days of cover and raises a procurement alert. Production orders get a projected stock-out window and later completion estimates, at-risk shipments appear in Dispatch, and management gets one consolidated alert. Lines stop when stock runs out and restart after a goods receipt.
* **Machine breakdown.** Health drops to Critical, the line stops and a corrective work order opens. Completion estimates for the line's orders move out, and shipment readiness and risk are recalculated. If nobody intervenes, a degrading machine (e.g. M-102) can fail on its own.
* **Packaging mismatch.** The carton is blocked and gets a HOLD id, no carton id. A defect record is created and the carton cannot be put away. Readiness waits for rework, override or rejection; rejected pillows go back to production.
* **Warehouse delay.** Picking falls behind, the waiting truck accrues waiting time and the estimated detention charge, and the system recommends expediting picking or rescheduling.

## Simulation
Administrators use the **Sim** control in the header to start, pause, step, reset, change speed (1/5/15/30 simulated minutes per 3-second tick), and trigger scenarios: machine failure, production delay, material shortage, packaging mismatch, workforce productivity decline and truck delay. Scenarios target the factory selected in the header.

The demo day starts at 11:00. Production between 06:00 and 11:00 has already been run by the engine, so today's history is consistent. Shift rules: 8.5 h scheduled presence, 30-min unpaid lunch, two paid 10-min breaks, and +$1/h on shift 2. Non-production hours (23:00–06:00) are skipped.

## Architecture
```
React (CRA, Tailwind, UI kit, Chart.js, Three.js twin) ──REST /api──▶ FastAPI ──SQLAlchemy──▶ PostgreSQL / SQLite
        ▲                                         │   Alembic migrations, JWT + roles
        └────────────── WebSocket /ws ◀───────────┤   simulation engine (background thread)
                                                  ├─ services: planning/optimizer, readiness, inventory, quality,
                                                  │            workforce, machine health, metrics, alerts, insights,
                                                  │            predictive (S!AP), twin (+ simulation/rfid.py), cctv
                                                  ├─ ai/: data tools + Claude tool use (anthropic SDK) / local engine
                                                  ├─ cv/: inference interface, OpenCV counter, simulated fallback,
                                                  │       CCTV feed catalogue (written by tools/cctv)
                                                  └─ integrations/: SAP B1, HRMS, IoT/PLC, CCTV, barcode/RFID adapters
```
* `backend/app/models.py`: one relational model (38 tables) shared by every stage. Migration `0002` adds `rfid_intervals` and `line_wip`.
* `backend/app/simulation/rfid.py`: badge simulation (zones, trips, breaks, overruns, late arrivals, technician dispatch). `services/twin.py` is the twin read model; `services/predictive.py` holds the reliability models and S!AP advisories.
* `backend/app/simulation/engine.py`: each tick moves inbound shipments, stock, lines, packing, pallets, put-away, allocation, picking, trucks and delivery, then recomputes projections and alerts.
* `backend/app/services/planning.py`: schedule projection on the shift calendar, plus the optimizer (earliest-due-date construction with changeover-aware line choice, then relocation local search on weighted tardiness, changeovers and completion time).
* `backend/app/services/cctv.py`: CCTV feed catalogue plus live context (RFID identities at the station, the customer's next open order, today's carton counts). `tools/cctv/` (render, analyze, feed definitions) is the offline CV toolchain; it is not imported by the app.
* Frontend: the existing dashboard template's design system (`src/theme`, `src/components/ui`) with the seven pages in `src/pages`; `src/components/cctv` holds the CCTV player, wall and full-screen viewer.

### Stack note
The brief lists TypeScript, shadcn/ui and Recharts. To build on the UI template already in this repository, the frontend keeps that template's stack: React 18 (JavaScript), Tailwind tokens, the template's component kit and Chart.js theming. The backend follows the brief: FastAPI, SQLAlchemy, PostgreSQL, Alembic, WebSockets, pandas/NumPy, OpenCV and the Anthropic SDK.

## AI configuration
```
LLM_PROVIDER=anthropic   ANTHROPIC_API_KEY=...   LLM_MODEL=claude-opus-5-5
```
With `anthropic`, Claude chooses among 18 data tools (`backend/app/ai/tools.py`) and answers only from their results. Server-side refusal fallback is enabled. If the API is unavailable, the local engine answers instead and says so. The default is `mock`, the local engine, which needs no network.

## Tests
`backend/tests/` holds 26 tests:

* `test_acceptance.py` runs the eight acceptance scenarios end to end through the REST API, plus a smoke test of every page endpoint and a live-speed (1-minute tick) packing check.
* `test_llm_provider.py` exercises the Claude tool loop and its fallback with a scripted client, so it makes no network calls.
* `test_predictive.py` covers the S!AP contract, the M-102 bearing prediction and scheduling.
* `test_twin.py` covers the network map, pillow-flow invariants (filled ≥ sewn ≥ bagged = line output), breaks never counted as time away, and technician dispatch to a breakdown.
* `test_cctv.py` checks that every CV camera has a feed, that the Walmart packing feed reads its label by OCR and resolves the customer's order and the packers' RFID badges, and that the published video, poster and analytics match the catalogue (frame counts, placements on packing cameras).
* `test_numbering.py` guards order numbering against collisions with seeded records.

The suite passes on SQLite and on PostgreSQL (`PG_TEST_URL`). Set `TEST_DB_PATH` to run two suites side by side. The acceptance scenarios:

1. Goods receipt with a shortage (10,000 → 9,800 kg) updates stock and production material availability.
2. Production output flows into packing and inspection.
3. An under-packed carton is blocked, alerted and refused by the warehouse; an override needs a supervisor and a reason.
4. An approved carton is palletised, scanned and put away to a location.
5. Allocation and picking raise readiness; loading, departure and delivery complete the order.
6. A machine breakdown moves completion estimates and shipment readiness for ORD-501.
7. A truck delay produces waiting time and a $350 estimated detention.
8. AI answers match the records shown in the application.

```bash
cd backend && .venv/Scripts/python -m pytest tests -q
```

## Integration layer
`backend/app/integrations/registry.py` defines adapters with simulated modes for SAP Business One (Service Layer), HRMS, IoT/PLC (OPC UA/MQTT), CCTV (RTSP) and barcode/RFID. Setting the matching environment variable (`SAP_B1_URL`, `HRMS_URL`, `IOT_BROKER_URL`, `CV_VIDEO_SOURCE`) switches an adapter to live mode. No credentials are assumed. Facial recognition is not used: workforce activity is attributed by employee ID from production events, and location only by RFID zone reads.
