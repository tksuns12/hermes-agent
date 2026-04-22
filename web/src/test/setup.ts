import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

if (typeof window === "undefined" || typeof document === "undefined") {
  throw new Error(
    "Vitest frontend setup requires jsdom globals (window/document). Check vite test environment.",
  );
}

const vitestGlobals = globalThis as typeof globalThis & {
  describe?: unknown;
  it?: unknown;
  expect?: unknown;
};

if (
  typeof vitestGlobals.describe !== "function" ||
  typeof vitestGlobals.it !== "function" ||
  typeof vitestGlobals.expect !== "function"
) {
  throw new Error(
    "Vitest globals are unavailable. Check test.globals and setupFiles wiring.",
  );
}

if (typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string): MediaQueryList => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

afterEach(() => {
  cleanup();
});
