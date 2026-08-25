/* Pixel-art office furniture + characters, all inline SVG (no assets, no licenses).
   Everything uses shape-rendering: crispEdges so rectangles stay pixel-sharp at any scale.
   Shared by the Fleet office (OfficeStage) and the marketing FleetPeek — keep the APIs stable. */

const crisp = { shapeRendering: "crispEdges" as const };

const SKIN = "hsl(28 56% 74%)";
const SKIN_SHADE = "hsl(25 46% 63%)";

/** A little person. Hue drives shirt + hair + hairstyle so every agent is recognizable at a
 *  glance; the same hue always renders the same character. */
export function PixelPerson({
  hue,
  size = 44,
  walking = false,
  working = false,
  facing = 1,
  dim = false,
  hat = false,
}: {
  hue: number;
  size?: number;
  walking?: boolean;
  working?: boolean;
  facing?: 1 | -1;
  dim?: boolean;
  /** A cap: the customer, not staff. */
  hat?: boolean;
}) {
  const shirt = `hsl(${hue} 62% 52%)`;
  const shirtShade = `hsl(${hue} 62% 40%)`;
  const shirtLight = `hsl(${hue} 70% 62%)`;
  const hair = `hsl(${(hue + 160) % 360} 38% 26%)`;
  const hairLight = `hsl(${(hue + 160) % 360} 38% 34%)`;
  const pants = `hsl(${(hue + 40) % 360} 16% 21%)`;
  const style = Math.floor(hue / 90) % 4; // deterministic hairstyle per hue
  return (
    <div
      className={walking ? "fleet-walk" : working ? "fleet-type" : "fleet-breathe"}
      style={{ width: size, height: size * 1.18, transform: `scaleX(${facing})`, filter: dim ? "grayscale(0.9) brightness(0.7)" : undefined }}
    >
      <svg viewBox="0 0 36 43" width="100%" height="100%" style={crisp} aria-hidden>
        {/* ── head ── */}
        <rect x="10" y="4" width="16" height="10" fill={SKIN} />
        <rect x="10" y="12" width="16" height="2" fill={SKIN_SHADE} />
        {/* ears */}
        <rect x="8" y="8" width="2" height="4" fill={SKIN} />
        <rect x="26" y="8" width="2" height="4" fill={SKIN_SHADE} />
        {/* hair — four styles so a team isn't a row of clones */}
        {style === 0 && (
          <g>
            <rect x="10" y="0" width="16" height="5" fill={hair} />
            <rect x="10" y="0" width="16" height="2" fill={hairLight} />
            <rect x="8" y="2" width="4" height="6" fill={hair} />
          </g>
        )}
        {style === 1 && (
          <g>
            <rect x="10" y="0" width="16" height="4" fill={hair} />
            <rect x="10" y="0" width="7" height="2" fill={hairLight} />
            <rect x="21" y="3" width="5" height="3" fill={hair} />
            <rect x="24" y="2" width="4" height="7" fill={hair} />
          </g>
        )}
        {style === 2 && (
          <g>
            <rect x="11" y="1" width="3" height="4" fill={hair} />
            <rect x="15" y="0" width="3" height="5" fill={hairLight} />
            <rect x="19" y="1" width="3" height="4" fill={hair} />
            <rect x="23" y="0" width="3" height="5" fill={hairLight} />
            <rect x="10" y="3" width="16" height="3" fill={hair} />
          </g>
        )}
        {style === 3 && (
          <g>
            <rect x="10" y="0" width="16" height="5" fill={hair} />
            <rect x="12" y="0" width="12" height="2" fill={hairLight} />
            <rect x="8" y="2" width="4" height="12" fill={hair} />
            <rect x="24" y="2" width="4" height="12" fill={hair} />
          </g>
        )}
        {/* cap (over any hair) */}
        {hat && (
          <g>
            <rect x="8" y="0" width="20" height="5" fill="#e8b04b" />
            <rect x="8" y="0" width="20" height="2" fill="#f3c76e" />
            <rect x="22" y="4" width="10" height="2" fill="#b8843a" />
          </g>
        )}
        {/* eyes + glint + brows, with a blinking lid over them */}
        <rect x="14" y="8" width="3" height="3" fill="#151a24" />
        <rect x="21" y="8" width="3" height="3" fill="#151a24" />
        <rect x="15" y="8" width="1" height="1" fill="#cfd8ea" />
        <rect x="22" y="8" width="1" height="1" fill="#cfd8ea" />
        {!hat && <rect x="14" y="6" width="3" height="1" fill={hair} />}
        {!hat && <rect x="21" y="6" width="3" height="1" fill={hair} />}
        <rect className="fleet-lid fleet-blink" x="13" y="8" width="12" height="3" fill={SKIN}
          style={{ animationDelay: `${(hue % 23) / 6}s` }} />
        {/* mouth */}
        <rect x="17" y="12" width="3" height="1" fill={SKIN_SHADE} />
        {/* ── body ── */}
        <rect x="8" y="15" width="20" height="13" rx="1" fill={shirt} />
        <rect x="8" y="15" width="20" height="2" fill={shirtLight} />
        <rect x="24" y="15" width="4" height="13" fill={shirtShade} />
        <rect x="8" y="24" width="20" height="4" fill={shirtShade} />
        {/* collar */}
        <rect x="14" y="15" width="8" height="2" fill={shirtShade} />
        {/* ── arms (sleeve + hand) ── */}
        <g className="fleet-arm-l">
          <rect x="4" y="16" width="4" height="7" fill={shirt} />
          <rect x="4" y="21" width="4" height="2" fill={shirtShade} />
          <rect x="4" y="23" width="4" height="3" fill={SKIN} />
        </g>
        <g className="fleet-arm-r">
          <rect x="28" y="16" width="4" height="7" fill={shirtShade} />
          <rect x="28" y="21" width="4" height="2" fill={shirtShade} />
          <rect x="28" y="23" width="4" height="3" fill={SKIN_SHADE} />
        </g>
        {/* ── legs (pants + shoes) ── */}
        <g className="fleet-leg-l">
          <rect x="11" y="28" width="5" height="11" fill={pants} />
          <rect x="10" y="39" width="6" height="3" fill="#10141d" />
          <rect x="10" y="39" width="6" height="1" fill="#2a3348" />
        </g>
        <g className="fleet-leg-r">
          <rect x="20" y="28" width="5" height="11" fill={pants} />
          <rect x="20" y="28" width="5" height="11" fill="rgba(0,0,0,0.18)" />
          <rect x="20" y="39" width="6" height="3" fill="#10141d" />
          <rect x="20" y="39" width="6" height="1" fill="#2a3348" />
        </g>
      </svg>
    </div>
  );
}

/** A desk with a monitor whose screen lights up in the owner's hue while they work. */
export function Desk({ hue, on, name }: { hue: number; on: boolean; name: string }) {
  const glow = `hsl(${hue} 80% 62%)`;
  return (
    <div className="flex flex-col items-center">
      <svg viewBox="0 0 92 50" width="100%" style={{ ...crisp, ["--glow" as string]: glow }} aria-hidden>
        {/* monitor */}
        <g className={on ? "fleet-monitor-on" : undefined}>
          <rect x="28" y="0" width="34" height="22" rx="2" fill="#10151f" stroke="#2a3348" strokeWidth="1.4" />
          <rect x="31" y="3" width="28" height="16" fill={on ? `hsl(${hue} 80% 60% / 0.28)` : "#182031"} className={on ? "fleet-screen" : undefined} />
          {/* code lines on the lit screen */}
          {on && (
            <g className="fleet-screen">
              <rect x="33" y="5" width="14" height="2" fill={glow} opacity="0.9" />
              <rect x="33" y="9" width="22" height="2" fill={glow} opacity="0.55" />
              <rect x="36" y="13" width="12" height="2" fill={glow} opacity="0.7" />
              <rect x="33" y="17" width="8" height="1" fill={glow} opacity="0.4" />
            </g>
          )}
        </g>
        {/* stand */}
        <rect x="42" y="22" width="6" height="3" fill="#2a3348" />
        <rect x="37" y="25" width="16" height="2" fill="#232c42" />
        {/* desktop with grain */}
        <rect x="2" y="27" width="88" height="7" rx="2" fill="#8a5a33" />
        <rect x="2" y="27" width="88" height="2" fill="#9c6a3e" />
        <rect x="12" y="30" width="20" height="1" fill="#7a4e2b" />
        <rect x="58" y="29" width="16" height="1" fill="#7a4e2b" />
        <rect x="2" y="32" width="88" height="3" fill="#6d4526" />
        {/* keyboard + papers + mug */}
        <rect x="36" y="28" width="18" height="3" rx="1" fill="#39435c" />
        <rect x="38" y="29" width="14" height="1" fill="#4c5876" />
        <rect x="10" y="27.5" width="11" height="4" fill="#d8dce8" />
        <rect x="11" y="28.5" width="8" height="1" fill="#9aa3b6" />
        <rect x="11" y="30" width="6" height="1" fill="#9aa3b6" />
        <rect x="70" y="23" width="7" height="6" rx="1" fill={`hsl(${hue} 60% 55%)`} />
        <rect x="77" y="24" width="2" height="3" fill={`hsl(${hue} 60% 45%)`} />
        {on && (
          <g className="fleet-steam">
            <rect x="72" y="19" width="2" height="3" fill="#9aa3b6" opacity="0.5" />
            <rect x="75" y="17" width="1" height="3" fill="#9aa3b6" opacity="0.35" />
          </g>
        )}
        {/* legs */}
        <rect x="6" y="35" width="5" height="15" fill="#5d3a1f" />
        <rect x="6" y="35" width="2" height="15" fill="#6d4526" />
        <rect x="81" y="35" width="5" height="15" fill="#5d3a1f" />
        <rect x="81" y="35" width="2" height="15" fill="#6d4526" />
      </svg>
      <span className="mt-0.5 rounded-sm border border-white/5 bg-ink-950/85 px-1.5 py-px font-mono text-[9px] text-fg-muted">
        {name}
      </span>
    </div>
  );
}

/** The skill library: one book spine per skill; the active one slides out and glows. */
export function Bookshelf({ skills, active, onPick }: {
  skills: string[]; active: string; onPick?: (name: string) => void;
}) {
  const spineHue = (s: string) => {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h % 360;
  };
  const shelves: string[][] = [[], [], []];
  skills.slice(0, 12).forEach((s, i) => shelves[i % 3].push(s));
  return (
    <div className="flex flex-col items-center">
      <svg viewBox="0 0 74 110" width="100%" style={crisp} aria-hidden={onPick ? undefined : true}>
        {/* carcass with a lit top edge */}
        <rect x="0" y="0" width="74" height="110" rx="2" fill="#4a2f18" />
        <rect x="0" y="0" width="74" height="3" fill="#5f3d20" />
        <rect x="0" y="107" width="74" height="3" fill="#331f0e" />
        <rect x="4" y="4" width="66" height="102" fill="#2c1b0d" />
        <rect x="4" y="4" width="2" height="102" fill="#22150a" />
        {shelves.map((row, si) => (
          <g key={si}>
            <rect x="4" y={36 + si * 32} width="66" height="4" fill="#5d3a1f" />
            <rect x="4" y={36 + si * 32} width="66" height="1" fill="#7a4e2b" />
            {row.map((s, bi) => {
              const isActive = s === active;
              const h = spineHue(s);
              return (
                <g key={s} role={onPick ? "button" : undefined} onClick={() => onPick?.(s)}
                  className={onPick ? "cursor-pointer hover:brightness-150" : undefined}>
                  <title>{s}</title>
                  <g className={isActive ? "fleet-book" : undefined}>
                    <rect
                      x={8 + bi * 13}
                      y={isActive ? 8 + si * 32 : 12 + si * 32}
                      width="10"
                      height="24"
                      rx="1"
                      fill={`hsl(${h} 55% ${isActive ? 62 : 45}%)`}
                    />
                    <rect x={8 + bi * 13} y={isActive ? 8 + si * 32 : 12 + si * 32} width="10" height="3"
                      fill={`hsl(${h} 55% ${isActive ? 74 : 56}%)`} />
                    <rect x={10 + bi * 13} y={isActive ? 18 + si * 32 : 22 + si * 32} width="6" height="2"
                      fill={`hsl(${h} 40% ${isActive ? 82 : 62}%)`} opacity="0.8" />
                  </g>
                </g>
              );
            })}
          </g>
        ))}
      </svg>
      <span className="mt-1 font-mono text-[9px] uppercase tracking-[0.2em] text-fg-faint">library</span>
    </div>
  );
}

/** The tool wall: a rack with one LED slot per tool. Running one lights it up; tools that
 *  ran keep a warm LED, declared-but-never-run ones stay visibly dark — the wall must not
 *  present the catalog as activity. */
export function ToolsRack({ tools, active, onPick }: {
  tools: { name: string; used: boolean }[]; active: string; onPick?: (name: string) => void;
}) {
  return (
    <div className="flex flex-col items-center">
      <svg viewBox="0 0 74 110" width="100%" style={crisp} aria-hidden={onPick ? undefined : true}>
        <rect x="0" y="0" width="74" height="110" rx="2" fill="#1c2434" />
        <rect x="0" y="0" width="74" height="2" fill="#2a3348" />
        <rect x="4" y="4" width="66" height="102" fill="#12192a" />
        {/* rack rails */}
        <rect x="6" y="4" width="2" height="102" fill="#1c2434" />
        <rect x="66" y="4" width="2" height="102" fill="#1c2434" />
        {tools.slice(0, 8).map((t, i) => {
          const isActive = t.name === active;
          const led = isActive ? "#34d399" : t.used ? "#1f6f52" : "#26314a";
          const label = isActive ? "#7df0ff" : t.used ? "#39435c" : "#232c42";
          return (
            <g key={t.name} opacity={t.used || isActive ? 1 : 0.55}
              role={onPick ? "button" : undefined} onClick={() => onPick?.(t.name)}
              className={onPick ? "cursor-pointer hover:brightness-150" : undefined}>
              <title>{t.name}</title>
              <rect x="8" y={9 + i * 12.5} width="58" height="9" rx="1.5" fill={isActive ? "#20304e" : "#182236"} />
              <rect x="8" y={9 + i * 12.5} width="58" height="1" fill={isActive ? "#31486e" : "#20293d"} />
              <circle cx="14" cy={13.5 + i * 12.5} r="2.6" fill={led} className={isActive ? "fleet-led" : undefined} />
              {isActive && <circle cx="14" cy={13.5 + i * 12.5} r="4.4" fill="none" stroke="#34d399" strokeWidth="0.8" opacity="0.5" />}
              <rect x="20" y={11.5 + i * 12.5} width={Math.min(40, t.name.length * 3.4)} height="4" rx="1" fill={label} />
            </g>
          );
        })}
      </svg>
      <span className="mt-1 font-mono text-[9px] uppercase tracking-[0.2em] text-fg-faint">tools</span>
    </div>
  );
}

export function CoffeeMachine() {
  return (
    <svg viewBox="0 0 40 52" width="100%" style={crisp} aria-hidden>
      <rect x="4" y="6" width="32" height="42" rx="2" fill="#31405e" />
      <rect x="4" y="6" width="32" height="3" fill="#41547a" />
      <rect x="4" y="45" width="32" height="3" fill="#232c42" />
      <rect x="8" y="10" width="24" height="10" fill="#0f1523" />
      <rect x="10" y="12" width="12" height="4" fill="#34d399" opacity="0.8" className="fleet-led" />
      <rect x="26" y="12" width="4" height="4" fill="#fb7185" opacity="0.6" />
      <rect x="14" y="24" width="12" height="10" fill="#0f1523" />
      <rect className="fleet-drip" x="19" y="24" width="2" height="3" fill="#b8793f" />
      <rect x="17" y="30" width="6" height="6" fill="#b8793f" />
      <rect x="17" y="30" width="6" height="1" fill="#d09055" />
      <g className="fleet-steam">
        <rect x="18" y="20" width="2" height="4" fill="#9aa3b6" opacity="0.5" />
        <rect x="21" y="18" width="2" height="4" fill="#9aa3b6" opacity="0.35" />
      </g>
    </svg>
  );
}

export function Plant() {
  return (
    <svg viewBox="0 0 30 40" width="100%" style={crisp} aria-hidden>
      <g className="fleet-sway">
        <rect x="6" y="2" width="6" height="14" fill="#2f9e5f" />
        <rect x="14" y="0" width="6" height="16" fill="#37b56d" />
        <rect x="15" y="0" width="2" height="6" fill="#4fcf85" />
        <rect x="20" y="4" width="5" height="12" fill="#2f9e5f" />
        <rect x="8" y="16" width="14" height="4" fill="#26824e" />
      </g>
      <rect x="7" y="20" width="16" height="12" fill="#8a5a33" />
      <rect x="7" y="20" width="16" height="2" fill="#9c6a3e" />
      <rect x="9" y="32" width="12" height="3" fill="#6d4526" />
    </svg>
  );
}

export function OfficeDoor() {
  return (
    <div className="flex flex-col items-center">
      <span className="mb-0.5 rounded-sm bg-fail/20 px-1.5 font-mono text-[8px] tracking-[0.25em] text-fail shadow-[0_0_8px_rgba(251,113,133,0.35)]">EXIT</span>
      <svg viewBox="0 0 34 52" width="100%" style={crisp} aria-hidden>
        <rect x="0" y="0" width="34" height="52" fill="#3a2c1c" />
        <rect x="3" y="3" width="28" height="49" fill="#241a10" />
        {/* panels + window slit */}
        <rect x="6" y="6" width="22" height="14" fill="#171009" />
        <rect x="8" y="8" width="18" height="4" fill="#3d4d79" opacity="0.55" />
        <rect x="6" y="24" width="22" height="18" fill="#1c1309" />
        <rect x="7" y="25" width="20" height="1" fill="#312312" />
        <circle cx="26" cy="30" r="2" fill="#c9a227" />
        <rect x="25" y="31" width="2" height="3" fill="#a3831f" />
      </svg>
    </div>
  );
}

/** The reception counter: the customer waits on the right side, the supervisor takes the
 *  question from the left. Vertical bar with a service bell. */
export function ReceptionCounter() {
  return (
    <div className="flex flex-col items-center">
      <svg viewBox="0 0 24 80" width="100%" style={crisp} aria-hidden>
        <rect x="4" y="0" width="16" height="76" rx="2" fill="#8a5a33" />
        <rect x="4" y="0" width="16" height="3" fill="#9c6a3e" />
        <rect x="6" y="4" width="12" height="70" fill="#6d4526" />
        <rect x="6" y="4" width="2" height="70" fill="#5d3a1f" />
        {/* service bell + papers on the counter top */}
        <rect x="9" y="12" width="6" height="4" fill="#c9a227" />
        <rect x="11" y="10" width="2" height="2" fill="#e8c96a" />
        <rect x="8" y="30" width="8" height="6" fill="#d8dce8" />
        <rect x="9" y="32" width="6" height="1" fill="#9aa3b6" />
        <rect x="8" y="52" width="8" height="5" fill="#b0b8ca" />
      </svg>
      <span className="mt-0.5 font-mono text-[8px] uppercase tracking-[0.2em] text-fg-faint">reception</span>
    </div>
  );
}

/** The break-room pong table. `playing` rallies the ball between the paddles. */
export function PingPongTable({ playing }: { playing: boolean }) {
  return (
    <svg viewBox="0 0 96 44" width="100%" style={crisp} aria-hidden>
      {/* table top */}
      <rect x="2" y="6" width="92" height="26" rx="2" fill="#1f7a4d" />
      <rect x="2" y="6" width="92" height="3" fill="#279159" />
      <rect x="4" y="8" width="88" height="22" fill="none" stroke="#d8f5e6" strokeWidth="1" opacity="0.7" />
      {/* net */}
      <rect x="46" y="2" width="4" height="32" fill="#d8dce8" />
      <rect x="46" y="2" width="4" height="2" fill="#f4f6fb" />
      {/* paddles resting at the ends */}
      <rect x="6" y="15" width="4" height="8" fill="#c0392b" />
      <rect x="86" y="15" width="4" height="8" fill="#2b6cc0" />
      {/* the ball — rallies while two agents play, rests on the line otherwise */}
      <circle cx={playing ? 0 : 48} cy="14" r="2.4" fill="#f4f6fb"
        className={playing ? "fleet-pong-ball" : undefined} />
      {/* legs */}
      <rect x="8" y="32" width="5" height="11" fill="#14532f" />
      <rect x="83" y="32" width="5" height="11" fill="#14532f" />
    </svg>
  );
}

/* ── wall dressing (Fleet office only) ────────────────────────────────────── */

/** A night window: stars, moon, a sleeping city skyline. */
export function WallWindow({ moon = false }: { moon?: boolean }) {
  return (
    <svg viewBox="0 0 64 46" width="100%" style={crisp} aria-hidden>
      <rect x="0" y="0" width="64" height="46" rx="2" fill="#181022" />
      <rect x="3" y="3" width="58" height="40" fill="#101b33" />
      <rect x="3" y="3" width="58" height="14" fill="#0c1526" />
      {/* stars */}
      <rect className="fleet-twinkle" x="10" y="7" width="1.5" height="1.5" fill="#cfd8ea" />
      <rect className="fleet-twinkle" x="22" y="12" width="1" height="1" fill="#9fb4d8" style={{ animationDelay: "0.9s" }} />
      <rect className="fleet-twinkle" x="38" y="6" width="1.5" height="1.5" fill="#cfd8ea" style={{ animationDelay: "1.7s" }} />
      <rect className="fleet-twinkle" x="50" y="14" width="1" height="1" fill="#9fb4d8" style={{ animationDelay: "0.4s" }} />
      <rect className="fleet-twinkle" x="30" y="18" width="1" height="1" fill="#7d8fb3" style={{ animationDelay: "2.3s" }} />
      {moon && (
        <g>
          <circle cx="47" cy="12" r="5" fill="#f5edd8" />
          <circle cx="45" cy="11" r="1.4" fill="#ddd2b8" />
          <circle cx="49" cy="14" r="1" fill="#ddd2b8" />
        </g>
      )}
      {/* skyline with a few windows still lit */}
      <rect x="3" y="30" width="10" height="13" fill="#0a0f1c" />
      <rect x="13" y="26" width="8" height="17" fill="#0d1322" />
      <rect x="24" y="32" width="12" height="11" fill="#0a0f1c" />
      <rect x="38" y="28" width="9" height="15" fill="#0d1322" />
      <rect x="49" y="33" width="12" height="10" fill="#0a0f1c" />
      <rect x="15" y="29" width="1.5" height="1.5" fill="#e8b04b" opacity="0.8" />
      <rect x="41" y="31" width="1.5" height="1.5" fill="#e8b04b" opacity="0.6" />
      <rect className="fleet-twinkle" x="27" y="35" width="1.5" height="1.5" fill="#e8b04b" opacity="0.7" style={{ animationDelay: "1.2s" }} />
      {/* frame cross */}
      <rect x="30" y="3" width="3" height="40" fill="#181022" />
      <rect x="3" y="21" width="58" height="3" fill="#181022" />
      {/* sill */}
      <rect x="0" y="43" width="64" height="3" fill="#241a30" />
    </svg>
  );
}

/** A ticking wall clock — the office is alive even when nobody moves. */
export function WallClock() {
  return (
    <svg viewBox="0 0 22 22" width="100%" aria-hidden>
      <circle cx="11" cy="11" r="10" fill="#0f1523" stroke="#2a3348" strokeWidth="1.4" />
      <circle cx="11" cy="11" r="8.2" fill="#e8ecf5" />
      <rect x="10.6" y="3.4" width="0.8" height="1.8" fill="#39435c" />
      <rect x="10.6" y="16.8" width="0.8" height="1.8" fill="#39435c" />
      <rect x="3.4" y="10.6" width="1.8" height="0.8" fill="#39435c" />
      <rect x="16.8" y="10.6" width="1.8" height="0.8" fill="#39435c" />
      <g className="fleet-hand-hr" style={{ transformOrigin: "11px 11px" }}>
        <rect x="10.5" y="6.6" width="1" height="4.8" fill="#151a24" />
      </g>
      <g className="fleet-hand-min" style={{ transformOrigin: "11px 11px" }}>
        <rect x="10.7" y="4.8" width="0.6" height="6.6" fill="#e11d48" />
      </g>
      <circle cx="11" cy="11" r="0.9" fill="#151a24" />
    </svg>
  );
}

/** A framed poster: a tiny chart — this office ships dashboards, after all. */
export function WallPoster() {
  return (
    <svg viewBox="0 0 30 24" width="100%" style={crisp} aria-hidden>
      <rect x="0" y="0" width="30" height="24" rx="1" fill="#181022" />
      <rect x="2" y="2" width="26" height="20" fill="#1c2434" />
      <rect x="5" y="14" width="3" height="5" fill="#34d399" />
      <rect x="10" y="10" width="3" height="9" fill="#7aa2ff" />
      <rect x="15" y="12" width="3" height="7" fill="#f472b6" />
      <rect x="20" y="6" width="3" height="13" fill="#34d399" />
      <rect x="4" y="4" width="10" height="1" fill="#39435c" />
    </svg>
  );
}
