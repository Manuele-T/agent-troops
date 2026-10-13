import { useEffect, useRef } from "react";
import { Application, Assets, Container, Graphics, Sprite, Text, Texture, type Ticker } from "pixi.js";
import { visibleState, type AgentView, type Boundaries } from "../fleet";

// The shared sprites.png sheet is gone. Robots are now 8 individual PNGs in
// public/sprites/ — filenames confirmed against the actual directory listing
// (see DISCOVERY.md), not guessed. Each agent gets one whole file as a plain
// static sprite; there is no more row/column slicing.
const ROBOT_SPRITE_FILES = [
  "1FFCE6E6-665D-4354-95F3-9E1BA1695C89.PNG",
  "294C7FAB-56E2-4795-8B2B-BC00E3CD0626.PNG",
  "48DDFC06-67AC-4DF8-84CE-8C9ACF3645EE.PNG",
  "60C29F10-4A3C-496D-AB0C-00F32F91F671.PNG",
  "718142AD-AB68-4060-BB39-C78C4C15D596.PNG",
  "7AAD85A4-F28D-48AD-8758-FF903FBBEB5F.PNG",
  "D1F43FE4-EA2C-4FE5-AD9A-302E55DE63E0.PNG",
  "E8464DE4-763D-4ACE-A26D-D21E7DFB6D83.PNG",
];
const ROBOT_SPRITE_URLS = ROBOT_SPRITE_FILES.map((f) => `/sprites/${f}`);
const BACKGROUND_URL = "/sprites/background.jpeg";

// The source PNGs are full-size renders (1024x1536), not 16x32 pixel-art
// cells, so the old fixed SCALE=3 multiplier no longer means anything —
// instead every sprite is scaled to a consistent on-floor display height.
const DISPLAY_HEIGHT = 96;

// Step 15: brief tint flash on a state change, so a transition reads instead
// of an instant colour snap — decays from FLASH_COLOR to the new target tint.
const FLASH_COLOR = 0xffffff;
const FLASH_DURATION_MS = 220;
// Step 15: agents stand at a fixed spot now (no wandering) — a slow tint pulse
// toward GLOW_PEAK_COLOR is the "still alive and working" signal that the old
// walk-cycle used to provide.
const GLOW_PERIOD_MS = 1100;
const GLOW_PEAK_COLOR = 0xbfe4ff;
const GLOW_STRENGTH = 0.55;
// Step 14: overlay dim amount when no agent is working — a plain semi-
// transparent rectangle above the whole stage (no Filter needed), eased in/out.
const DIM_ALPHA = 0.35;
const DIM_EASE = 0.05;

// Step 16: the desks are gone from the background — agents now stand in the
// open center floor, 2 rows of 5 (up to 10). Fractions of canvas
// width/height, measured directly against the real background.jpeg (footprint
// boxes checked clear of every piece of furniture on all sides), not
// eyeballed. Filled left-to-right, top row first (index 0-4), then bottom row
// (5-9). See DISCOVERY.md for the measurement method.
const SEAT_FRACTIONS: { x: number; y: number }[] = [
  { x: 0.27, y: 0.3875 },
  { x: 0.39, y: 0.3875 },
  { x: 0.51, y: 0.3875 },
  { x: 0.63, y: 0.3875 },
  { x: 0.75, y: 0.3875 },
  { x: 0.27, y: 0.5875 },
  { x: 0.39, y: 0.5875 },
  { x: 0.51, y: 0.5875 },
  { x: 0.63, y: 0.5875 },
  { x: 0.75, y: 0.5875 },
];

function computeSeatPositions(app: Application): { x: number; y: number }[] {
  return SEAT_FRACTIONS.map((s) => ({ x: s.x * app.screen.width, y: s.y * app.screen.height }));
}

const TINT: Record<string, number> = {
  idle: 0xaaaaaa,
  working: 0x4488ff,
  blocked: 0xff4444,
  done: 0x44ff88,
};

function lerpColor(a: number, b: number, t: number): number {
  const ar = (a >> 16) & 0xff;
  const ag = (a >> 8) & 0xff;
  const ab = a & 0xff;
  const br = (b >> 16) & 0xff;
  const bg = (b >> 8) & 0xff;
  const bb = b & 0xff;
  const r = Math.round(ar + (br - ar) * t);
  const g = Math.round(ag + (bg - ag) * t);
  const bl = Math.round(ab + (bb - ab) * t);
  return (r << 16) | (g << 8) | bl;
}

interface SpriteEntry {
  id: string;
  container: Container;
  sprite: Sprite;
  highlight: Graphics;
  label: Text;
  labelPlate: Graphics;
  // Trifecta-leg chips under the name (P/U/E, plus G when approval-gated). Rebuilt when boundaries change.
  badges: Container | null;
  badgeKey: string;
  // Step 17: which of the 8 robot PNGs this agent renders as — assigned once,
  // the first time the agent is seen, from a Map keyed by agent id so it can
  // never change across re-renders/re-syncs (stable per agent id, per the
  // brief). Cycles through the 8 files in first-seen order.
  spriteIndex: number;
  // Step 16: which of the 10 fixed floor spots (2 rows of 5) this agent
  // stands at — assigned by position in the (alphabetically-sorted, see
  // fleet.ts) agent list whenever membership changes, clamped to the last
  // spot if there are more than 10 agents. Re-resolved to a pixel position
  // every tick via computeSeatPositions() so a canvas resize keeps everyone
  // aligned with the (stretched-to-fill) background — never eased/animated,
  // so agents are never seen moving between spots.
  seatIndex: number;
  state: string;
  // tint-flash-on-change / working-glow-pulse bookkeeping.
  lastTargetTint: number;
  flashElapsed: number;
  glowPhase: number;
}

const LEG_CHIPS = [
  { key: "private_data", letter: "P", color: 0x5b9dff },
  { key: "untrusted_content", letter: "U", color: 0xffb04a },
  { key: "exfiltration", letter: "E", color: 0xff5a5a },
];

// P/U/E chips: filled = this role's tools grant that leg of the lethal trifecta. G = human-approval gate.
function buildBadges(b: Boundaries, label: Text): Container {
  const c = new Container();
  const items = [
    ...LEG_CHIPS.map((x) => ({ letter: x.letter, color: x.color, on: b.legs.includes(x.key) })),
    ...(b.gated ? [{ letter: "G", color: 0x44ff88, on: true }] : []),
  ];
  const size = 14;
  const gap = 3;
  const total = items.length * size + (items.length - 1) * gap;
  items.forEach((it, i) => {
    const g = new Graphics().roundRect(0, 0, size, size, 3);
    if (it.on) g.fill({ color: it.color });
    else g.fill({ color: 0x000000, alpha: 0.35 }).stroke({ color: it.color, alpha: 0.4, width: 1 });
    g.x = -total / 2 + i * (size + gap);
    g.y = label.y + label.height + 8;
    const tx = new Text({
      text: it.letter,
      style: { fontFamily: "system-ui, sans-serif", fontSize: 9, fontWeight: "700", fill: it.on ? 0x111111 : it.color },
    });
    tx.anchor.set(0.5);
    tx.x = g.x + size / 2;
    tx.y = g.y + size / 2 + 0.5;
    tx.alpha = it.on ? 1 : 0.55;
    c.addChild(g, tx);
  });
  return c;
}

// Single shared PixiJS Application for the whole room (never one per agent —
// see PLAN's pitfall). Room.tsx renders this instead of one Character node per
// agent; fleet.ts's state model and every CSS state-* class are untouched —
// this component only ever reads visibleState(agent, now) and applies a tint.
export default function Character({
  agents,
  selectedId,
  onSelect,
}: {
  agents: AgentView[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const appRef = useRef<Application | null>(null);
  const texturesRef = useRef<Texture[] | null>(null);
  const entriesRef = useRef<Map<string, SpriteEntry>>(new Map());
  const spriteAssignmentRef = useRef<Map<string, number>>(new Map());
  const nextSpriteIndexRef = useRef(0);
  const selectedIdRef = useRef(selectedId);
  const onSelectRef = useRef(onSelect);
  const membershipRef = useRef<string>("");
  const agentsRef = useRef(agents);
  const dimOverlayRef = useRef<Graphics | null>(null);
  agentsRef.current = agents;
  selectedIdRef.current = selectedId;
  onSelectRef.current = onSelect;

  const syncEntries = (app: Application, textures: Texture[], currentAgents: AgentView[]) => {
    const entries = entriesRef.current;
    const currentIds = new Set(currentAgents.map((a) => a.id));

    for (const [id, entry] of entries) {
      if (!currentIds.has(id)) {
        entry.container.destroy({ children: true });
        entries.delete(id);
      }
    }

    const membershipKey = currentAgents
      .map((a) => a.id)
      .sort()
      .join(",");
    const membershipChanged = membershipKey !== membershipRef.current;
    membershipRef.current = membershipKey;

    currentAgents.forEach((agent) => {
      let entry = entries.get(agent.id);
      if (!entry) {
        let spriteIndex = spriteAssignmentRef.current.get(agent.id);
        if (spriteIndex === undefined) {
          spriteIndex = nextSpriteIndexRef.current % textures.length;
          nextSpriteIndexRef.current += 1;
          spriteAssignmentRef.current.set(agent.id, spriteIndex);
        }
        const container = new Container();
        const highlight = new Graphics().ellipse(0, -3, 18, 7).fill({ color: 0xffffff, alpha: 0.3 });
        const texture = textures[spriteIndex];
        const sprite = new Sprite(texture);
        sprite.anchor.set(0.5, 1);
        const scale = DISPLAY_HEIGHT / texture.height;
        sprite.scale.set(scale);
        sprite.interactive = true;
        sprite.cursor = "pointer";
        sprite.on("pointertap", () => onSelectRef.current(agent.id));
        // GUI polish: profile name label below the sprite, centered on its x —
        // a plain Text child of the container so it stays pinned under the
        // sprite at its floor spot.
        const label = new Text({
          text: agent.name,
          style: { fontFamily: "system-ui, sans-serif", fontSize: 11, fill: 0xffffff, align: "center" },
        });
        label.anchor.set(0.5, 0);
        label.y = 4;
        // Readability plate behind the name — the office background is light
        // in places, so plain white text can wash out. A rounded, semi-
        // transparent black rect sized to the label's own (stable, per-agent)
        // bounds, added BEFORE the label so it renders underneath. It's a
        // child of the same container as the label/sprite, so it inherits the
        // container's transform automatically every frame — no per-tick x/y
        // copy needed, and it can never lag behind the label it's paired with.
        const platePad = 4;
        const labelPlate = new Graphics()
          .roundRect(
            -(label.width / 2 + platePad),
            label.y - platePad,
            label.width + platePad * 2,
            label.height + platePad * 2,
            4,
          )
          .fill({ color: 0x000000, alpha: 0.6 });
        container.addChild(highlight, sprite, labelPlate, label);
        app.stage.addChild(container);
        // Keep the dim overlay (added once at init) on top of every new sprite.
        if (dimOverlayRef.current) {
          app.stage.setChildIndex(dimOverlayRef.current, app.stage.children.length - 1);
        }
        entry = {
          id: agent.id,
          container,
          sprite,
          highlight,
          label,
          labelPlate,
          badges: null,
          badgeKey: "",
          spriteIndex,
          seatIndex: 0, // real slot assigned below once membership is known
          state: "idle",
          lastTargetTint: TINT.idle,
          flashElapsed: FLASH_DURATION_MS,
          glowPhase: Math.random() * GLOW_PERIOD_MS, // desync the working-glow pulse between agents
        };
        entries.set(agent.id, entry);
      }
      entry.state = visibleState(agent, Date.now());
      const badgeKey = agent.boundaries ? `${agent.boundaries.legs.join(",")}|${agent.boundaries.gated}` : "";
      if (badgeKey !== entry.badgeKey) {
        entry.badgeKey = badgeKey;
        if (entry.badges) {
          entry.container.removeChild(entry.badges);
          entry.badges.destroy({ children: true });
          entry.badges = null;
        }
        if (agent.boundaries) {
          entry.badges = buildBadges(agent.boundaries, entry.label);
          entry.container.addChild(entry.badges);
        }
      }
      if (entry.label.text !== agent.name) {
        // Name changed (membership churn reusing an id is not expected, but
        // cheap to handle) — resize the plate to match the new bounds.
        entry.label.text = agent.name;
        const platePad = 4;
        entry.labelPlate
          .clear()
          .roundRect(
            -(entry.label.width / 2 + platePad),
            entry.label.y - platePad,
            entry.label.width + platePad * 2,
            entry.label.height + platePad * 2,
            4,
          )
          .fill({ color: 0x000000, alpha: 0.6 });
      }
    });

    if (membershipChanged) {
      // Step 16: fixed floor spots, filled in agent-list order (fleet.ts
      // sorts by profile id) — up to the 10 real spots (2 rows of 5) in the
      // open center floor; any overflow beyond 10 stacks on the last spot
      // rather than being dropped.
      currentAgents.forEach((agent, i) => {
        const entry = entries.get(agent.id);
        if (!entry) return;
        entry.seatIndex = Math.min(i, SEAT_FRACTIONS.length - 1);
      });
    }
  };

  // Step 15: position ONLY — isolated from updateVisual() per the isolation
  // note this replaces (agents now stand at a fixed floor spot instead of
  // wandering). Re-resolves the spot's pixel position from screen fractions
  // every tick (cheap — at most 10 entries) so a canvas resize keeps everyone
  // aligned with the (stretched-to-fill) background; there is no velocity,
  // easing, or target-seeking left, so the sprite is never seen moving.
  const updatePosition = (entry: SpriteEntry, seats: { x: number; y: number }[]) => {
    const seat = seats[entry.seatIndex] ?? seats[seats.length - 1];
    entry.container.x = seat.x;
    entry.container.y = seat.y;
  };

  const updateVisual = (entry: SpriteEntry, ticker: Ticker) => {
    entry.highlight.visible = entry.id === selectedIdRef.current;
    // Step 17: no more per-state frame swap — each agent is a single static
    // PIXI.Sprite now. State is conveyed by tint only.

    const targetTint = TINT[entry.state] ?? TINT.idle;
    if (targetTint !== entry.lastTargetTint) {
      entry.lastTargetTint = targetTint;
      entry.flashElapsed = 0;
    }
    if (entry.flashElapsed < FLASH_DURATION_MS) {
      entry.sprite.tint = lerpColor(FLASH_COLOR, targetTint, entry.flashElapsed / FLASH_DURATION_MS);
      entry.flashElapsed += ticker.deltaMS;
    } else if (entry.state === "working") {
      // Step 15: replaces the old walk-cycle as the "actively working"
      // signal — a slow glow pulse toward a brighter blue and back.
      entry.glowPhase += ticker.deltaMS;
      const pulse = (Math.sin((entry.glowPhase / GLOW_PERIOD_MS) * Math.PI * 2) + 1) / 2;
      entry.sprite.tint = lerpColor(targetTint, GLOW_PEAK_COLOR, pulse * GLOW_STRENGTH);
    } else {
      entry.glowPhase = 0;
      entry.sprite.tint = targetTint;
    }
  };

  // Mount once: create the ONE shared PixiJS Application, load assets, start
  // the ticker. Destroyed on unmount — never recreated on agents/selection
  // changes (see PLAN's pitfall: single shared Application, not one per agent).
  useEffect(() => {
    let cancelled = false;
    const app = new Application();
    appRef.current = app;

    // React 18 StrictMode double-invokes effects in dev (mount -> cleanup ->
    // mount) specifically to surface bugs like this one: calling
    // app.destroy() while app.init() is still in flight throws
    // ("this._cancelResize is not a function") because PixiJS's resize
    // plugin hasn't finished wiring itself up yet. `ready` lets the cleanup
    // defer destruction until init has genuinely settled, no matter how far
    // setup got — this is the ONLY place the app is ever destroyed.
    const ready = (async () => {
      const host = hostRef.current;
      if (!host) return;
      await app.init({ resizeTo: host, backgroundAlpha: 0, antialias: false });
      if (cancelled || !hostRef.current) return;
      hostRef.current.appendChild(app.canvas);

      const [bgTexture] = await Promise.all([
        Assets.load(BACKGROUND_URL),
        // Pre-load every robot PNG so its real dimensions are known before
        // first paint (Texture.from() on an unloaded url would otherwise
        // resolve to a 1x1 placeholder and break the display-height scale
        // computed below), then hand back a PIXI.Texture per file via
        // Texture.from(), per the brief.
        Assets.load(ROBOT_SPRITE_URLS),
      ]);
      if (cancelled) return;

      const bg = new Sprite(bgTexture);
      bg.width = app.screen.width;
      bg.height = app.screen.height;
      app.stage.addChild(bg);

      // Step 14: dim overlay — a plain semi-transparent rectangle above the
      // whole stage (no Filter API needed), non-interactive so clicks still
      // reach the sprites underneath. Alpha eased toward DIM_ALPHA whenever
      // no agent is working, and toward 0 the instant any agent is.
      const dimOverlay = new Graphics();
      dimOverlay.alpha = 0;
      app.stage.addChild(dimOverlay);
      dimOverlayRef.current = dimOverlay;

      const textures = ROBOT_SPRITE_URLS.map((url) => Texture.from(url));
      texturesRef.current = textures;
      syncEntries(app, textures, agentsRef.current);

      app.ticker.add((ticker) => {
        // Cheap resize-safety net: bg.width/height were set once at load time;
        // keep them matching the canvas if the room container is resized.
        if (bg.width !== app.screen.width || bg.height !== app.screen.height) {
          bg.width = app.screen.width;
          bg.height = app.screen.height;
        }
        const seats = computeSeatPositions(app);
        let anyWorking = false;
        for (const entry of entriesRef.current.values()) {
          updatePosition(entry, seats);
          updateVisual(entry, ticker);
          if (entry.state === "working") anyWorking = true;
        }
        dimOverlay
          .clear()
          .rect(0, 0, app.screen.width, app.screen.height)
          .fill({ color: 0x000000, alpha: 1 });
        const targetAlpha = anyWorking ? 0 : DIM_ALPHA;
        dimOverlay.alpha += (targetAlpha - dimOverlay.alpha) * DIM_EASE;
      });
    })();

    return () => {
      cancelled = true;
      entriesRef.current.clear();
      texturesRef.current = null;
      appRef.current = null;
      dimOverlayRef.current = null;
      ready
        .then(() => app.destroy(true, { children: true, texture: true }))
        .catch(() => {
          /* init itself failed (e.g. host unmounted before first paint) — nothing to destroy */
        });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-sync sprites whenever the agent list/state changes. Skipped while
  // assets are still loading — the init effect's own syncEntries call picks
  // up agentsRef.current (always fresh) once loading finishes.
  useEffect(() => {
    const app = appRef.current;
    const textures = texturesRef.current;
    if (!app || !textures) return;
    syncEntries(app, textures, agents);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents]);

  return (
    <div className="room" ref={hostRef}>
      {agents.length === 0 && (
        <p className="empty">No agents yet — drive the board to bring the floor to life.</p>
      )}
    </div>
  );
}
