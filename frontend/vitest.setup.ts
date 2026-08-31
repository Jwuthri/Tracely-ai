import "@testing-library/jest-dom/vitest";

// jsdom ships no matchMedia, and every component that respects prefers-reduced-motion asks for
// it on mount. Answer "no preference" — the same thing a browser says by default.
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
}

// jsdom 29 leaves `localStorage` to Node's own global, which is undefined without
// --experimental-webstorage. Components (and tests seeding prefs) expect a real Storage.
// ponytail: Map-backed stub, drop it once jsdom ships its own again.
if (typeof window !== "undefined" && !window.localStorage) {
  const store = new Map<string, string>();
  const localStorage: Storage = {
    get length() { return store.size; },
    key: (i) => [...store.keys()][i] ?? null,
    getItem: (k) => store.get(String(k)) ?? null,
    setItem: (k, v) => void store.set(String(k), String(v)),
    removeItem: (k) => void store.delete(String(k)),
    clear: () => store.clear(),
  };
  Object.defineProperty(window, "localStorage", { value: localStorage, configurable: true });
  Object.defineProperty(globalThis, "localStorage", { value: localStorage, configurable: true });
}

// jsdom has no layout, so it ships no scrollIntoView — a no-op is the honest stand-in.
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
