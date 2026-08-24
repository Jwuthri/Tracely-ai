"use client";

/* POC: the Fleet office rendered with three.js instead of DOM sprites.
   The pose engine is untouched — layoutOffice/poseAt from office.ts drive WHERE everyone is
   and what floats over their head; this file only replaces the presentation: a voxel room,
   an orbitable camera, real lights and shadows. Labels and speech bubbles stay real DOM via
   CSS2DRenderer so text keeps wrapping, ellipsis and theme tokens for free. */

import clsx from "clsx";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";
import { layoutOffice, narrate, poseAt, type Bubble, type Pose } from "./office";
import { fmtMs, isCustomer, OFFICE_PACING, orderActors, realMsAt, toPlayEvents, type ReplayActor, type ReplayEvent } from "./timeline";
import { usePlayClock } from "./useClock";

type Payload = { actors: ReplayActor[]; events: ReplayEvent[]; durationMs: number };

const SPEEDS = [0.5, 1, 2, 4];

const hueOf = (id: string) => {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return h % 360;
};

/* office % → world units. The floor is ~22 wide and ~13 deep, origin at room center. */
const WX = (x: number) => (x - 50) * 0.2;
const WZ = (y: number) => (y - 56) * 0.155;

const col = (h: number, s: number, l: number) => new THREE.Color().setHSL(h / 360, s / 100, l / 100);

/* ── mesh factories (all boxes — voxel look, cheap, no assets) ── */

function box(w: number, h: number, d: number, color: THREE.ColorRepresentation, opts?: { emissive?: THREE.ColorRepresentation; emissiveIntensity?: number }) {
  const m = new THREE.Mesh(
    new THREE.BoxGeometry(w, h, d),
    new THREE.MeshStandardMaterial({ color, roughness: 0.85, metalness: 0.05, emissive: opts?.emissive ?? 0x000000, emissiveIntensity: opts?.emissiveIntensity ?? 1 }),
  );
  m.castShadow = true;
  m.receiveShadow = true;
  return m;
}

/** A voxel minifig. Returns the group plus the bits the animation loop touches. */
function makePerson(hue: number, scale: number, hat: boolean) {
  const g = new THREE.Group();
  const shirt = col(hue, 62, 52);
  const shirtDark = col(hue, 62, 40);
  const hair = col((hue + 160) % 360, 38, 28);
  const skin = col(28, 56, 74);
  const pants = col((hue + 40) % 360, 16, 22);

  const legL = box(0.16, 0.42, 0.2, pants); legL.position.set(-0.11, 0.21, 0);
  const legR = box(0.16, 0.42, 0.2, pants); legR.position.set(0.11, 0.21, 0);
  const torso = box(0.56, 0.5, 0.3, shirt); torso.position.y = 0.67;
  const belt = box(0.56, 0.08, 0.31, shirtDark); belt.position.y = 0.45;
  const armL = box(0.14, 0.42, 0.18, shirt); armL.position.set(-0.36, 0.68, 0);
  const armR = box(0.14, 0.42, 0.18, shirtDark); armR.position.set(0.36, 0.68, 0);
  const head = box(0.4, 0.36, 0.34, skin); head.position.y = 1.12;
  const hairCap = box(0.44, 0.14, 0.38, hair); hairCap.position.y = 1.32;
  const eyeL = box(0.05, 0.06, 0.02, 0x151a24); eyeL.position.set(-0.09, 1.13, 0.18);
  const eyeR = box(0.05, 0.06, 0.02, 0x151a24); eyeR.position.set(0.09, 1.13, 0.18);
  g.add(legL, legR, torso, belt, armL, armR, head, hairCap, eyeL, eyeR);
  if (hat) {
    const cap = box(0.46, 0.1, 0.4, 0xe8b04b); cap.position.y = 1.36;
    const brim = box(0.44, 0.05, 0.2, 0xb8843a); brim.position.set(0, 1.32, 0.28);
    g.add(cap, brim);
  }
  g.scale.setScalar(scale);
  return { group: g, legL, legR, armL, armR };
}

function makeDesk(hue: number) {
  const g = new THREE.Group();
  const top = box(2.4, 0.12, 1.05, 0x8a5a33); top.position.y = 0.92;
  const panelL = box(0.1, 0.92, 0.95, 0x6d4526); panelL.position.set(-1.1, 0.46, 0);
  const panelR = box(0.1, 0.92, 0.95, 0x6d4526); panelR.position.set(1.1, 0.46, 0);
  const foot = box(0.5, 0.05, 0.32, 0x2a3348); foot.position.set(0, 1.0, -0.2);
  const neck = box(0.1, 0.16, 0.1, 0x2a3348); neck.position.set(0, 1.08, -0.2);
  const bezel = box(1.05, 0.68, 0.07, 0x10151f); bezel.position.set(0, 1.5, -0.2);
  const screen = new THREE.Mesh(
    new THREE.PlaneGeometry(0.92, 0.55),
    new THREE.MeshStandardMaterial({ color: 0x182031, emissive: col(hue, 80, 55), emissiveIntensity: 0, roughness: 0.4 }),
  );
  screen.position.set(0, 1.5, -0.16);
  const keyboard = box(0.7, 0.04, 0.24, 0x39435c); keyboard.position.set(0, 1.0, 0.18);
  const mug = box(0.14, 0.14, 0.14, col(hue, 60, 55)); mug.position.set(0.85, 1.05, 0.1);
  g.add(top, panelL, panelR, foot, neck, bezel, screen, keyboard, mug);
  return { group: g, screen: screen.material as THREE.MeshStandardMaterial };
}

function starTexture(moon: boolean) {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const ctx = c.getContext("2d")!;
  const grad = ctx.createLinearGradient(0, 0, 0, 128);
  grad.addColorStop(0, "#0c1526");
  grad.addColorStop(1, "#14224a");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 128, 128);
  ctx.fillStyle = "#cfd8ea";
  for (let i = 0; i < 22; i++) ctx.fillRect((i * 47) % 128, (i * 31) % 90, 2, 2);
  if (moon) {
    ctx.fillStyle = "#f5edd8";
    ctx.beginPath(); ctx.arc(92, 30, 13, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#ddd2b8";
    ctx.beginPath(); ctx.arc(87, 27, 3.4, 0, Math.PI * 2); ctx.fill();
  }
  // skyline
  ctx.fillStyle = "#0a0f1c";
  for (let i = 0; i < 7; i++) ctx.fillRect(i * 19, 96 + (i % 3) * 8, 16, 40);
  ctx.fillStyle = "#e8b04b";
  ctx.fillRect(24, 104, 2, 2); ctx.fillRect(80, 110, 2, 2);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function floorTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const ctx = c.getContext("2d")!;
  ctx.fillStyle = "#251d33"; ctx.fillRect(0, 0, 128, 128);
  ctx.fillStyle = "#211a2e"; ctx.fillRect(0, 0, 64, 64); ctx.fillRect(64, 64, 64, 64);
  ctx.strokeStyle = "rgba(0,0,0,0.25)"; ctx.strokeRect(0, 0, 64, 64); ctx.strokeRect(64, 64, 64, 64);
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.repeat.set(9, 6);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

/* ── DOM label helpers (CSS2D) ── */

const BUBBLE_CLS: Record<Bubble["type"], string> = {
  speech: "max-w-[210px] rounded-lg border border-line-bright bg-[#f4f6fb] px-2.5 py-1.5 text-[11px] font-medium leading-snug text-ink-900 shadow-[3px_3px_0_rgba(10,7,18,0.45)]",
  thought: "max-w-[190px] rounded-[14px] border border-t_think/40 bg-ink-800/95 px-2.5 py-1.5 font-mono text-[10px] leading-snug text-t_think shadow-[2px_2px_0_rgba(10,7,18,0.4)]",
  chip: "max-w-[200px] rounded-md border border-t_tool/50 bg-ink-900/90 px-2 py-0.5 font-mono text-[10px] text-t_tool shadow-[2px_2px_0_rgba(10,7,18,0.4)]",
  error: "grid h-6 w-6 place-items-center rounded-full bg-fail font-mono text-[13px] font-bold text-ink-950 shadow-[0_0_14px_rgba(251,113,133,0.8)]",
};

function bubbleText(b: Bubble): string {
  if (b.type === "error") return "!";
  if (b.type === "chip") return `${b.icon === "skill" ? "◈" : b.icon === "call" ? "→" : "⚙"} ${b.text}`;
  return b.text;
}

export function OfficeStage3D({ threadId }: { threadId: string }) {
  const [data, setData] = useState<Payload | null>(null);
  const [failed, setFailed] = useState(false);
  const mountRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    fetch(`/api/session-replay?thread=${encodeURIComponent(threadId)}`, { cache: "no-store" })
      .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then((d) => alive && setData({ actors: d.actors ?? [], events: d.events ?? [], durationMs: d.duration_ms ?? 0 }))
      .catch(() => { if (alive) { setFailed(true); setData({ actors: [], events: [], durationMs: 0 }); } });
    return () => { alive = false; };
  }, [threadId]);

  const { events, total } = useMemo(() => toPlayEvents(data?.events ?? [], OFFICE_PACING), [data]);
  const actors = useMemo(() => orderActors(data?.actors ?? []), [data]);
  const layout = useMemo(() => layoutOffice(actors), [actors]);
  const clock = usePlayClock(total, 0.5);
  const { t } = clock;

  const nameOf = useMemo(() => {
    const m = new Map(actors.map((a) => [a.id, a.name]));
    return (id: string) => m.get(id) ?? id;
  }, [actors]);

  const poses = useMemo(() => {
    const out = new Map<string, Pose>();
    actors.forEach((a, i) => out.set(a.id, poseAt(a, events, t, layout, i)));
    return out;
  }, [actors, events, t, layout]);
  const sign = narrate(events, t, nameOf);

  /* refs the RAF loop reads without re-running React */
  const rigs = useRef(new Map<string, {
    person: ReturnType<typeof makePerson>;
    target: THREE.Vector3;
    bubbleEl: HTMLDivElement;
    nameEl: HTMLDivElement;
    working: boolean;
  }>());
  const screens = useRef(new Map<string, THREE.MeshStandardMaterial>());
  const signEl = useRef<HTMLDivElement | null>(null);

  /* ── build the scene once per conversation ── */
  useEffect(() => {
    const mount = mountRef.current;
    if (!mount || !data || !actors.length) return;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x191322);
    scene.fog = new THREE.FogExp2(0x191322, 0.016);

    const camera = new THREE.PerspectiveCamera(45, 16 / 10, 0.1, 100);
    camera.position.set(0, 8.5, 12.5);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    mount.appendChild(renderer.domElement);

    const labels = new CSS2DRenderer();
    labels.domElement.style.position = "absolute";
    labels.domElement.style.inset = "0";
    labels.domElement.style.pointerEvents = "none";
    mount.appendChild(labels.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(0, 1, -0.5);
    controls.enableDamping = true;
    controls.minDistance = 4;
    controls.maxDistance = 26;
    controls.maxPolarAngle = Math.PI * 0.49;

    /* room */
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(23, 14),
      new THREE.MeshStandardMaterial({ map: floorTexture(), roughness: 0.92 }),
    );
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    scene.add(floor);

    const rug = new THREE.Mesh(
      new THREE.PlaneGeometry(11, 4.6),
      new THREE.MeshStandardMaterial({ color: 0x2d2340, roughness: 1 }),
    );
    rug.rotation.x = -Math.PI / 2;
    rug.position.set(0, 0.012, WZ(46));
    rug.receiveShadow = true;
    scene.add(rug);

    const wallMat = new THREE.MeshStandardMaterial({ color: 0x2a2138, roughness: 0.95 });
    const back = new THREE.Mesh(new THREE.PlaneGeometry(23, 6), wallMat);
    back.position.set(0, 3, -7);
    back.receiveShadow = true;
    const left = new THREE.Mesh(new THREE.PlaneGeometry(14, 6), wallMat);
    left.rotation.y = Math.PI / 2;
    left.position.set(-11.5, 3, 0);
    const right = new THREE.Mesh(new THREE.PlaneGeometry(14, 6), wallMat);
    right.rotation.y = -Math.PI / 2;
    right.position.set(11.5, 3, 0);
    scene.add(back, left, right);

    /* windows + door on the back wall */
    [{ x: -6.2, moon: false }, { x: -2.6, moon: true }].forEach(({ x, moon }) => {
      const frame = box(2.5, 1.9, 0.1, 0x181022);
      frame.position.set(x, 3.4, -6.96);
      const glass = new THREE.Mesh(
        new THREE.PlaneGeometry(2.2, 1.6),
        new THREE.MeshBasicMaterial({ map: starTexture(moon) }),
      );
      glass.position.set(x, 3.4, -6.89);
      scene.add(frame, glass);
    });
    const doorX = WX(88);
    const door = box(1.5, 3.1, 0.12, 0x241a10);
    door.position.set(doorX, 1.55, -6.93);
    const doorKnob = box(0.1, 0.1, 0.08, 0xc9a227);
    doorKnob.position.set(doorX + 0.55, 1.5, -6.85);
    const exitEl = document.createElement("div");
    exitEl.className = "rounded-sm bg-fail/20 px-1.5 font-mono text-[9px] tracking-[0.25em] text-fail";
    exitEl.textContent = "EXIT";
    const exitLabel = new CSS2DObject(exitEl);
    exitLabel.position.set(doorX, 3.5, -6.9);
    scene.add(door, doorKnob, exitLabel);

    /* LED sign */
    const signDiv = document.createElement("div");
    signDiv.className = "max-w-[420px] truncate rounded border border-[#181022] bg-[#0a0f14]/95 px-3 py-1 text-center font-mono text-[11px] tracking-wider text-[#57e39a]";
    signDiv.style.textShadow = "0 0 6px rgba(87,227,154,0.6)";
    const signObj = new CSS2DObject(signDiv);
    signObj.position.set(0, 4.6, -6.9);
    scene.add(signObj);
    signEl.current = signDiv;

    /* library + tool wall, simplified */
    const shelf = box(0.5, 2.6, 2.6, 0x4a2f18);
    shelf.position.set(-10.6, 1.3, WZ(52));
    scene.add(shelf);
    for (let i = 0; i < 8; i++) {
      const b = box(0.3, 0.5, 0.24, col((i * 77) % 360, 55, 45));
      b.position.set(-10.35, 0.6 + Math.floor(i / 4) * 0.85, WZ(52) - 1 + (i % 4) * 0.62);
      scene.add(b);
    }
    const rack = box(0.5, 2.6, 2.6, 0x1c2434);
    rack.position.set(10.6, 1.3, WZ(52));
    scene.add(rack);
    for (let i = 0; i < 6; i++) {
      const led = new THREE.Mesh(
        new THREE.SphereGeometry(0.07, 8, 8),
        new THREE.MeshBasicMaterial({ color: i < 3 ? 0x34d399 : 0x26314a }),
      );
      led.position.set(10.3, 0.7 + i * 0.35, WZ(52) - 0.9);
      scene.add(led);
    }

    /* plants */
    [[-9.8, 5.4], [9.6, 5.6], [-4.5, -5.9]].forEach(([px, pz]) => {
      const pot = box(0.42, 0.4, 0.42, 0x8a5a33); pot.position.set(px, 0.2, pz);
      const bush = new THREE.Mesh(new THREE.ConeGeometry(0.42, 1.1, 6), new THREE.MeshStandardMaterial({ color: 0x2f9e5f, roughness: 0.9 }));
      bush.castShadow = true;
      bush.position.set(px, 0.95, pz);
      scene.add(pot, bush);
    });

    /* lights */
    scene.add(new THREE.AmbientLight(0x8878b8, 0.9));
    scene.add(new THREE.HemisphereLight(0x6c5a9e, 0x2a1f3d, 0.5));
    const moonlight = new THREE.DirectionalLight(0x9fb4d8, 1.4);
    moonlight.position.set(-7, 9, -3);
    moonlight.castShadow = true;
    moonlight.shadow.mapSize.set(1024, 1024);
    moonlight.shadow.camera.left = -12; moonlight.shadow.camera.right = 12;
    moonlight.shadow.camera.top = 10; moonlight.shadow.camera.bottom = -10;
    scene.add(moonlight);
    const warm = new THREE.PointLight(0xffc98a, 110, 0, 2);
    warm.position.set(0, 5.2, 1.5);
    scene.add(warm);
    const fill = new THREE.PointLight(0x7df0ff, 40, 0, 2);
    fill.position.set(6, 4.5, 4.5);
    scene.add(fill);

    /* desks + people */
    const staff = actors.filter((a) => !isCustomer(a));
    for (const a of staff) {
      const d = layout.desks[a.id];
      if (!d) continue;
      const { group, screen } = makeDesk(hueOf(a.id));
      group.position.set(WX(d.x), 0, WZ(d.y) + 0.62);
      scene.add(group);
      screens.current.set(a.id, screen);
      const nameEl = document.createElement("div");
      nameEl.className = "rounded-sm border border-white/5 bg-ink-950/85 px-1.5 font-mono text-[9px] text-fg-muted";
      nameEl.textContent = a.name;
      const nameObj = new CSS2DObject(nameEl);
      nameObj.position.set(0, 0.4, 0.75);
      group.add(nameObj);
    }
    for (const a of actors) {
      const guest = isCustomer(a);
      const person = makePerson(guest ? 42 : hueOf(a.id), guest ? 1.15 : a.depth ? 1.1 : 1.3, guest);
      const p0 = poseAt(a, events, 0, layout, 0);
      person.group.position.set(WX(p0.x), 0, WZ(p0.y));
      scene.add(person.group);

      const holder = document.createElement("div");
      holder.className = "flex flex-col items-center gap-1";
      const bubbleEl = document.createElement("div");
      const nameEl = document.createElement("div");
      nameEl.className = clsx("rounded-sm border px-1.5 font-mono text-[9px]",
        guest ? "border-warn/25 bg-ink-950/85 text-warn" : "border-white/5 bg-ink-950/80 text-fg-faint");
      nameEl.textContent = a.name;
      holder.append(bubbleEl, nameEl);
      const label = new CSS2DObject(holder);
      label.position.set(0, 2.05, 0);
      person.group.add(label);

      rigs.current.set(a.id, { person, target: person.group.position.clone(), bubbleEl, nameEl, working: false });
    }

    /* render loop */
    const clockThree = new THREE.Clock();
    let raf = 0;
    const tick = () => {
      const dt = Math.min(clockThree.getDelta(), 0.1);
      const now = clockThree.elapsedTime;
      controls.update();
      for (const [id, rig] of rigs.current) {
        const g = rig.person.group;
        const dist = g.position.distanceTo(rig.target);
        if (dist > 0.02) {
          const step = Math.min(dist, 3.4 * dt);
          const dir = rig.target.clone().sub(g.position).normalize();
          g.position.addScaledVector(dir, step);
          g.rotation.y = THREE.MathUtils.damp(g.rotation.y, Math.atan2(dir.x, dir.z), 8, dt);
          g.position.y = Math.abs(Math.sin(now * 10)) * 0.08; // walk bob
          rig.person.armL.rotation.x = Math.sin(now * 10) * 0.6;
          rig.person.armR.rotation.x = -Math.sin(now * 10) * 0.6;
        } else {
          g.position.y = 0;
          g.rotation.y = THREE.MathUtils.damp(g.rotation.y, 0, 4, dt);
          if (rig.working) { // typing
            rig.person.armL.rotation.x = -0.9 + Math.sin(now * 14) * 0.12;
            rig.person.armR.rotation.x = -0.9 - Math.sin(now * 14) * 0.12;
          } else { // breathe
            rig.person.armL.rotation.x = THREE.MathUtils.damp(rig.person.armL.rotation.x, 0, 6, dt);
            rig.person.armR.rotation.x = THREE.MathUtils.damp(rig.person.armR.rotation.x, 0, 6, dt);
            g.scale.y = g.scale.x * (1 + Math.sin(now * 2.4 + g.position.x) * 0.008);
          }
        }
        const screen = screens.current.get(id);
        if (screen) screen.emissiveIntensity = THREE.MathUtils.damp(screen.emissiveIntensity, rig.working ? 1.6 : 0, 6, dt);
      }
      renderer.render(scene, camera);
      labels.render(scene, camera);
      raf = requestAnimationFrame(tick);
    };

    const resize = () => {
      const w = mount.clientWidth, h = mount.clientHeight;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
      labels.setSize(w, h);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(mount);
    raf = requestAnimationFrame(tick);

    const rigsMap = rigs.current;
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      controls.dispose();
      scene.traverse((o) => {
        if (o instanceof THREE.Mesh) {
          o.geometry.dispose();
          (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m.dispose());
        }
      });
      renderer.dispose();
      mount.removeChild(renderer.domElement);
      mount.removeChild(labels.domElement);
      rigsMap.clear();
      screens.current.clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, actors, layout]);

  /* ── feed the current poses into the scene (targets + DOM labels) ── */
  useEffect(() => {
    for (const [id, rig] of rigs.current) {
      const pose = poses.get(id);
      if (!pose) continue;
      rig.target.set(WX(pose.x), 0, WZ(pose.y));
      rig.working = pose.working && pose.at === "desk";
      const b = pose.bubble;
      if (b) {
        rig.bubbleEl.className = clsx(BUBBLE_CLS[b.type], b.faded ? "opacity-60" : "fleet-pop");
        rig.bubbleEl.textContent = bubbleText(b);
      } else {
        rig.bubbleEl.className = "";
        rig.bubbleEl.textContent = "";
      }
    }
    if (signEl.current) signEl.current.textContent = sign;
  }, [poses, sign]);

  if (!data) {
    return <div className="card grid h-[480px] place-items-center font-mono text-[12px] text-fg-muted">opening the office…</div>;
  }
  if (!events.length) {
    return (
      <div className="card grid h-[320px] place-items-center text-center">
        <div>
          <p className="text-[15px] font-semibold">{failed ? "Couldn't load the conversation" : "The office is empty"}</p>
          <p className="mt-1 text-[13px] text-fg-muted">
            {failed ? "The replay endpoint answered with an error — try reloading." : "This conversation has no spans to act out."}
          </p>
        </div>
      </div>
    );
  }

  const done = total > 0 && t >= total;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button onClick={clock.toggle} className="btn-primary !py-1.5">
          {done ? "↺ replay" : clock.playing ? "❚❚ pause" : "▶ play"}
        </button>
        <button onClick={clock.restart} className="btn-ghost">↺ restart</button>
        <div className="flex gap-1">
          {SPEEDS.map((s) => (
            <button key={s} onClick={() => clock.setSpeed(s)}
              className={clsx("rounded-md border px-2 py-1 font-mono text-[11px] transition-colors",
                clock.speed === s ? "border-signal/40 bg-signal/15 text-signal" : "border-line text-fg-muted hover:text-fg")}>
              {s}×
            </button>
          ))}
        </div>
        <span className="font-mono text-[10px] text-fg-faint">drag to orbit · scroll to zoom</span>
        <span className="ml-auto font-mono text-[11px] text-fg-faint"
          title="Real trace time — the play clock squeezes pauses and long calls">
          {fmtMs(realMsAt(events, t))} / {fmtMs(data.durationMs)}
        </span>
      </div>

      <div data-theme="dark" ref={mountRef}
        className="relative aspect-[16/10] select-none overflow-hidden rounded-2xl border border-line shadow-panel" />

      <input type="range" min={0} max={total} value={Math.min(t, total)}
        onChange={(e) => clock.seek(Number(e.target.value))}
        className="w-full accent-[#7df0ff]" aria-label="scrub the replay" />

      <p className="text-[12px] text-fg-muted">
        Three.js proof of concept — same conversation, same pose engine, a real camera. The DOM version stays at{" "}
        <Link href={`/sessions/${encodeURIComponent(threadId)}/fleet`} className="text-signal hover:underline">⌂ Fleet</Link>.
      </p>
    </div>
  );
}
