"use client";

import { useEffect, useRef, useState } from "react";

/** The shared play clock for the Replay and Fleet views. Advances every animation frame but
 *  COMMITS at ~25fps — a React commit re-renders the whole scene, and 60fps of that is pure
 *  waste when the stage reads identically. */
export function usePlayClock(total: number, rate = 1) {
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [speed, setSpeed] = useState(1);
  const raf = useRef<number | null>(null);
  const last = useRef<number>(0);

  useEffect(() => {
    if (!playing || !total) return;
    const COMMIT_MS = 40;
    let pending = 0;
    const tick = (now: number) => {
      // Clamp the frame delta: rAF stops in a hidden tab, and the first frame back would
      // otherwise carry the entire hidden duration — teleporting the play head to the end.
      const dt = Math.min(last.current ? now - last.current : 16, 100);
      last.current = now;
      pending += dt * speed * rate;
      if (pending >= COMMIT_MS) {
        const step = pending;
        pending = 0;
        setT((prev) => {
          const next = prev + step;
          if (next >= total) {
            setPlaying(false);
            return total;
          }
          return next;
        });
      }
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => {
      if (raf.current) cancelAnimationFrame(raf.current);
      last.current = 0;
    };
  }, [playing, speed, total, rate]);

  const restart = () => {
    setT(0);
    setPlaying(true);
  };
  const seek = (v: number) => {
    setT(v);
    setPlaying(false);
  };
  const toggle = () => (t >= total ? restart() : setPlaying((p) => !p));

  return { t, playing, speed, setSpeed, setT, setPlaying, restart, seek, toggle };
}

/** True for ~one walk's worth of time after a character's stand point moved — drives the
 *  stepping-legs sprite. Shared by the Fleet office and the landing-page peek. */
export function useWalking(x: number, y: number) {
  const [walking, setWalking] = useState(false);
  const prev = useRef<{ x: number; y: number } | null>(null);
  useEffect(() => {
    const moved = prev.current && (Math.abs(prev.current.x - x) > 1.5 || Math.abs(prev.current.y - y) > 1.5);
    prev.current = { x, y };
    if (!moved) return;
    setWalking(true);
    const id = setTimeout(() => setWalking(false), 800);
    return () => clearTimeout(id);
  }, [x, y]);
  return walking;
}
