PRINCESS RESCUE 3D — SERVER MULTIPLAYER V3

V3 ADDS TWO PRODUCTION NETWORK LAYERS
=====================================

A) REDIS PERSISTENCE + RECONNECT AFTER SERVER RESTART
------------------------------------------------------
- Room state is serialized to Redis every ~250 ms.
- Critical operations (create/join/start/resume/disconnect) also persist immediately.
- Session tokens are indexed separately:
    princess-rescue:v3:session:<token> -> { room code, role }
- Room snapshots:
    princess-rescue:v3:room:<CODE>
- A browser refresh still resumes the same role via localStorage token.
- If the Node server restarts/redeploys:
    1. phones lose WebSocket,
    2. existing client auto-reconnect runs,
    3. client sends its session token,
    4. new Node process resolves token from Redis,
    5. room snapshot is lazily restored,
    6. running match stays PAUSED until both players reconnect,
    7. both clients receive resumePlay and continue from persisted state.
- Redis room/session TTL: 6 hours.
- Disconnected role reservation: 120 seconds.

IMPORTANT:
Redis must have persistence suitable for your host if you also want survival across
a Redis process restart. Managed Redis normally handles this according to its plan.

B) LAG COMPENSATION FOR COMBAT / HIT DETECTION
-----------------------------------------------
The server is still authoritative.

1. Action timestamp
   - Client already estimates server clock using ping.
   - Attack / Skill / Dash now sends:
       st = Date.now() + serverOffset

2. Server rewind
   - Server keeps ~1 second of Hero / Princess / Boss position history.
   - Action timestamp is untrusted and clamped.
   - Maximum rewind: 220 ms.

3. Projectile compensation
   - Attack starts from the attacker's historical position at action time.
   - Aim uses historical boss position.
   - Projectile is fast-forwarded by measured action delay, capped at 150 ms.
   - Segment-vs-circle collision is used during fast-forward so it cannot skip through boss.

4. Dash / Perfect Dash fairness
   - Enemy projectile collision is not applied immediately.
   - It enters a short hit-confirm queue: 90 ms.
   - If a Dash packet arrives during that window and its timestamp proves Dash started
     before the hit, the hit becomes PERFECT DASH instead of damage.
   - Dash iframe validation window: 340 ms.
   - This specifically reduces "I dashed on my screen but still got hit" under moderate ping.

5. Server-side swept collisions
   - Moving projectiles use segment-circle collision from previous -> current position.
   - This reduces tunneling when projectile speed is high or a tick is delayed.

PERSISTED GAME DETAILS
======================
Redis snapshot includes:
- room code
- Hero / Princess session tokens
- HP / stamina / food weapon
- positions / cooldowns / revive state
- boss HP / phase / current boss timers
- projectiles
- pickups
- Royal Trust
- pending hit confirmations
- scheduled boss tasks / delayed pickup spawns

Boss delayed attacks were changed from setTimeout-only logic to persisted scheduled tasks,
so a Node restart does not silently delete a pending Giấc Mơ Vỡ / Ba Giờ Sáng wave.

REDIS SETUP
===========
Set:
  REDIS_URL=redis://...
or:
  REDIS_URL=rediss://...   (TLS providers)

The app has a RAM-only fallback for local development, but restart persistence is disabled
when REDIS_URL is missing. /healthz reports whether Redis persistence is active.

RENDER / RAILWAY / FLY / VPS
==============================
Deploy the Node service and a managed Redis instance.

Set the Web Service environment variable:
  REDIS_URL=<connection string from your Redis provider>

Then deploy/redeploy normally.

The included render.yaml leaves REDIS_URL as a secret you must fill in.
The included Docker Compose is convenient for VPS/local testing.

LOCAL WITH DOCKER
=================
docker compose up --build

Open:
http://localhost:3000

LOCAL WITHOUT DOCKER
====================
1. Run Redis on localhost:6379.
2. npm install
3. set REDIS_URL=redis://localhost:6379
4. npm start

HEALTH
======
GET /healthz

Example:
{
  "ok": true,
  "rooms": 1,
  "redis": true,
  "persistence": "redis",
  "uptime": 123
}

FILES
=====
server.js
package.json
render.yaml
Dockerfile
docker-compose.yml
.env.example
public/index.html
README.txt


V4 — ADAPTIVE NETWORK LAB
=========================

ADAPTIVE INTERPOLATION
----------------------
Remote rendering no longer uses a fixed 100 ms buffer.

The client continuously estimates:
- RTT (round-trip ping)
- RTT jitter
- state/snapshot arrival jitter
- missing snapshot ratio from server snapshot sequence numbers

Policy:
- 80 ms  = GOOD network
  roughly RTT < 70 ms, jitter < 12 ms, loss < 1%
- 100 ms = NORMAL network
- 140 ms = ROUGH network
  used when RTT > 130 ms OR jitter > 28 ms OR loss > 3.5%

A candidate buffer must remain preferred for ~1.2 seconds before switching.
This prevents constant 80/100/140 oscillation when the network sits near a threshold.

NETWORK LAB
-----------
Open the deployed game with:
  ?nettest=1

Examples:
  https://YOUR-GAME.example/?nettest=1
  https://YOUR-GAME.example/?room=ABC123&join=1&nettest=1

The hidden lab provides:
- artificial state snapshot loss: 0 / 5 / 10 / 20 / 35%
- artificial inbound delay: 0 / 50 / 100 / 180 / 250 ms
- artificial delay jitter: ±0 / 15 / 40 / 80 ms
- forced 3-second socket outage
- forced 5-second socket outage
- refresh/session-resume test
- server-restart/Redis-resume arming test
- live RTT / RTT jitter / snapshot jitter / loss / active buffer

PACKET-LOSS SIMULATION
----------------------
WebSocket itself is reliable, so this harness deliberately drops selected incoming
STATE SNAPSHOTS at the application layer. Server snapshot messages now have a runtime
sequence number. The next delivered snapshot exposes the gap, allowing the same
adaptive buffer code to measure the simulated loss.

Delay/jitter simulation is applied to STATE and PONG processing. This allows RTT/jitter
and snapshot-jitter logic to react as if the connection had become worse.

RECONNECT HARNESS
-----------------
DROP 3s / DROP 5s:
- captures current room, role, boss HP and match state
- forcibly closes WebSocket
- blocks reconnect for the selected duration
- normal auto reconnect then resumes through the session token
- harness prints PASS/FAIL after resume

REFRESH TEST:
- stores expected room/role in sessionStorage
- reloads the page
- localStorage session token reconnects to the same server room
- harness prints PASS/FAIL after resume

ARM RESTART:
- records current state
- operator then restarts/redeploys Node server
- Redis V3 persistence restores room/session
- automatic reconnect resumes the room
- harness prints PASS/FAIL when resumed

SERVER STATE SEQUENCE
---------------------
Server adds a runtime snapshot sequence:
  { type:"state", seq:123, state:{...} }

The sequence is not persisted and may reset after server restart. Clients reset their
loss estimator on session resume, so a restart is not misclassified as massive packet loss.


V5 — PERFORMANCE / BOSS-SKILL HITCH FIX
=======================================

The main freeze was rendering-side, not network-side.

WHY V4 HITCHED WHEN A BOSS USED A SKILL
----------------------------------------
V4 created a new THREE.Geometry + Material + Mesh for each newly visible projectile.
A radial skill can add 20–42 projectiles at once. On mobile Safari this can trigger:
- many JS allocations in one frame
- material/shader setup
- WebGL object creation
- garbage collection shortly afterward

V4 also rebuilt temporary projectile Maps every render frame and touched the HTML HUD
at display refresh rate.

V5 FIXES
--------
1. PROJECTILES -> GPU INSTANCING
   - one InstancedMesh for normal food projectiles
   - one InstancedMesh for glass shards
   - one InstancedMesh for thought rings
   - capacity 128 visible projectiles
   - shared low-poly geometry
   - shared MeshBasicMaterial
   - per-instance color
   - no new projectile Geometry / Material / Mesh during combat
   - roughly 3 projectile draw calls even when a boss fires dozens of bullets

2. PICKUPS -> GPU INSTANCING
   - food pickups share one Octahedron geometry/material batch
   - no pickup object creation while rendering snapshots

3. PREWARM
   - projectile shaders/materials are compiled in setup3D()
   - first boss cast no longer has to initialize projectile rendering paths

4. LOWER PER-FRAME GARBAGE
   - projectile ID maps are cached per network snapshot
   - no new Map from both projectile arrays every animation frame

5. HUD THROTTLING
   - 3D remains full frame rate
   - HTML HP/Trust/Food HUD updates at 20 Hz
   - removes unnecessary style/text/layout work

6. SNAPSHOT RATE
   - server snapshots: 20 Hz -> 15 Hz
   - interpolation remains active, so remote movement stays smooth
   - ~25% less JSON parsing / state processing

7. DYNAMIC RESOLUTION
   - mobile starts around 1.10 DPR
   - desktop around 1.35 DPR
   - sustained slow frames reduce render scale down to ~0.82 on mobile
   - sustained fast frames slowly restore quality
   - changes are rate-limited to avoid framebuffer resize thrashing

8. MOBILE RENDERER
   - no antialias on mobile
   - shadow map disabled (the game had no actual cast-shadow pipeline)
   - simpler arena geometry
   - slightly lighter fog/lights

9. VĨNH DẠ DOM
   - 12 eye elements are created once at startup
   - skill activation only repositions/toggles them
   - no DOM-node burst during the cast

NETWORK LAB
-----------
Use ?nettest=1.
V5 adds:
- current frame time EMA
- dynamic render scale
- WebGL draw call count

This makes it easy to distinguish network jitter from actual GPU/main-thread stalls.


V6 — COMBAT FEEL
================

1. SERVER-SYNCHRONIZED BOSS TELEGRAPH
-------------------------------------
Every boss skill now creates a persisted cast envelope:
  {
    id,
    i,          // skill index
    startAt,
    impactAt,
    endAt
  }

The same cast is:
- broadcast as `bossCast`
- stored in room state / Redis
- included in snapshots

Both phones render the telegraph against the synchronized server clock.
If one client receives the event slightly later, it jumps to the correct cast progress
instead of starting its own independent timer.

Skill timing:
- Bóng Đêm Lan Ra: impact ~480 ms
- Giấc Mơ Vỡ: impact ~650 ms
- Ba Giờ Sáng: impact ~950 ms
- Vĩnh Dạ: first wave ~620 ms

Bóng Đêm's actual damaging pool now starts after the telegraph rather than immediately.

2. ATTACK / SKILL CLIENT-SIDE PROJECTILE PREDICTION
---------------------------------------------------
When Attack or Skill is pressed:
- local projectile(s) appear immediately
- no round-trip wait
- local predicted shot uses predicted player position and current displayed boss position
- one action ID (`aid`) is sent to server
- authoritative server projectile carries the same `aid`
- server returns `actionAck` with accepted/rejected state and projectile IDs
- predicted projectile is reconciled to server trajectory
- once the authoritative projectile appears in a snapshot, the prediction is removed

Prediction reuses the V5 InstancedMesh batch, so it does NOT bring back the
Geometry/Material allocation hitch.

3. ANTICIPATION / RELEASE / RECOVERY
------------------------------------
Hero and Princess root transforms now have lightweight attack poses:
- anticipation: slight squash / backward lean
- release: fast forward pop
- recovery: spring back to neutral
- Skill has a longer/stronger anticipation curve

Remote player's attack animation is triggered from an authoritative `actionAnim` event
using the server action timestamp.

No bones, new meshes or expensive tween objects are created during combat.

4. HIT-STOP
-----------
Server emits `combatHit` only after authoritative boss damage is confirmed.

Visual hit-stop:
- local confirmed hit: roughly 44–68 ms depending on damage
- teammate hit: ~30 ms
- freezes 3D visual update only
- server simulation/network continue normally
- after the micro-freeze, interpolation catches up
- adds tiny boss squash + camera kick + light flash

This gives attack weight without blocking the JavaScript main thread.

5. SERVER-DRIVEN TELEGRAPH SURVIVES REDIS / RECONNECT
-----------------------------------------------------
`activeCast` is persisted to Redis.
A reconnecting phone can reconstruct an in-progress telegraph from snapshot state.

6. V5 PERFORMANCE RETAINED
--------------------------
- projectile InstancedMesh
- shared geometry/materials
- prewarming
- 15 Hz server snapshots
- 20 Hz HUD
- dynamic resolution
- adaptive interpolation
- reconnect harness
- Redis persistence
- 220 ms combat rewind
- 90 ms hit-confirm grace


V7 — HEAVY COMBAT RENDERER
==========================

WHY
---
V5/V6 already removed per-projectile Mesh allocation, but enemy projectiles still occupied
separate InstancedMesh batches by visual type (shard / thought / generic).

V7 collapses ALL boss projectiles into one GPU draw path.

1. SINGLE BOSS PROJECTILE INSTANCEDMESH
---------------------------------------
All enemy projectiles use:
- one PlaneGeometry
- one RawShaderMaterial
- one InstancedMesh
- up to 128 visible instances

Per-instance attributes:
- aKind
- aColor
- aPulse
- instanceMatrix

`aKind` selects the projectile silhouette in the fragment shader:
- 0: glowing orb
- 1: dream/glass shard
- 2: thought ring

The quad is billboarded to the current camera using its instance transform.

Effect:
20, 40 or 80 mixed enemy bullets remain ONE enemy projectile draw call.

Player food projectiles remain a separate InstancedMesh because they use their own
prediction/reconciliation path and food colors.

2. SHADER VISUALS
-----------------
The fragment shader draws:
- soft orb radial falloff
- angular diamond/glass shard
- hollow thought ring
- cheap additive halo

No texture upload is required and no new Material/Geometry is created during combat.

3. AUTOMATIC BOSS OUTLINE CULLING
---------------------------------
Character outlines are now tagged when they are created.

Only the BOSS outline is disabled when combat becomes heavy:
- 16+ enemy bullets
- phase 3 with 10+ enemy bullets
- Vĩnh Dạ active

Hero and Princess outlines remain on.

Hysteresis:
- after the heavy load ends,
- enemy count must drop below 8,
- and stay quiet for ~650 ms,
- then boss outline turns back on.

This avoids repeatedly toggling outline every frame near the threshold.

4. NETWORK LAB
--------------
?nettest=1 now also displays:
- enemy batch count
- boss outline ON/OFF
- heavy-combat status

Example:
  enemy batch 42 · boss outline OFF · heavy YES

5. EVERYTHING FROM V6 IS RETAINED
---------------------------------
- server-clock boss telegraphs
- anticipation / release / recovery
- hit-stop
- client projectile prediction
- V5 dynamic resolution
- adaptive 80/100/140 ms interpolation
- Redis persistence
- server restart recovery
- lag compensation / rewind
- reconnect harness
