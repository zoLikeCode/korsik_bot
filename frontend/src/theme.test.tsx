import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  createThemeStore,
  ThemeToggle,
  THEME_STORAGE_KEY,
  type Theme,
  type ThemeEnvironment,
} from "./theme";

function environment(saved: string | null = null, dark = false) {
  let systemDark = dark;
  const systemListeners = new Set<() => void>();
  const storageListeners = new Set<(value: string | null) => void>();
  const apply = vi.fn();
  const writePreference = vi.fn();
  const env: ThemeEnvironment = {
    readPreference: () => saved,
    writePreference,
    prefersDark: () => systemDark,
    apply,
    onSystemChange: (listener) => {
      systemListeners.add(listener);
      return () => {
        systemListeners.delete(listener);
      };
    },
    onStorageChange: (listener) => {
      storageListeners.add(listener);
      return () => {
        storageListeners.delete(listener);
      };
    },
  };
  return {
    env,
    apply,
    writePreference,
    system: (value: boolean) => {
      systemDark = value;
      systemListeners.forEach((fn) => fn());
    },
    storage: (value: string | null) =>
      storageListeners.forEach((fn) => fn(value)),
    listenerCount: () => systemListeners.size + storageListeners.size,
  };
}
afterEach(() => vi.unstubAllGlobals());

describe("theme preference", () => {
  it("uses a valid saved choice before the OS preference and ignores invalid saved values", () => {
    expect(THEME_STORAGE_KEY).toBe("korsik-theme");
    expect(createThemeStore(environment("light", true).env).getSnapshot()).toBe(
      "light",
    );
    expect(createThemeStore(environment("dark", false).env).getSnapshot()).toBe(
      "dark",
    );
    expect(
      createThemeStore(environment("invalid", true).env).getSnapshot(),
    ).toBe("dark");
    expect(createThemeStore(environment(null, false).env).getSnapshot()).toBe(
      "light",
    );
  });

  it("follows OS changes until a deliberate choice, persists the toggle and releases listeners", () => {
    const e = environment();
    const store = createThemeStore(e.env),
      listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    expect(e.apply).toHaveBeenLastCalledWith("light");
    e.system(true);
    expect(store.getSnapshot()).toBe("dark");
    expect(listener).toHaveBeenCalledOnce();
    store.toggleTheme();
    expect(store.getSnapshot()).toBe("light");
    expect(e.writePreference).toHaveBeenCalledExactlyOnceWith("light");
    e.system(false);
    e.system(true);
    expect(store.getSnapshot()).toBe("light");
    expect(e.listenerCount()).toBe(2);
    unsubscribe();
    expect(e.listenerCount()).toBe(0);
  });

  it("synchronizes another tab without writing back and returns to system preference after storage is cleared", () => {
    const e = environment("light", true),
      store = createThemeStore(e.env);
    const unsubscribe = store.subscribe(() => {});
    e.storage("dark");
    expect(store.getSnapshot()).toBe("dark");
    e.storage("light");
    expect(store.getSnapshot()).toBe("light");
    e.storage(null);
    expect(store.getSnapshot()).toBe("dark");
    e.system(false);
    expect(store.getSnapshot()).toBe("light");
    expect(e.writePreference).not.toHaveBeenCalled();
    unsubscribe();
  });

  it("keeps a usable explicit choice when storage cannot be written", () => {
    const e = environment(null, true);
    e.env.writePreference = () => {
      throw new Error("Storage denied");
    };
    const store = createThemeStore(e.env),
      unsubscribe = store.subscribe(() => {});
    expect(() => store.toggleTheme()).not.toThrow();
    expect(store.getSnapshot()).toBe("light");
    e.system(false);
    e.system(true);
    expect(store.getSnapshot()).toBe("light");
    expect(e.apply).toHaveBeenLastCalledWith("light");
    unsubscribe();
  });

  it("applies the HTML theme and native control color scheme even when a webview denies localStorage access", () => {
    const media = Object.assign(new EventTarget(), { matches: true });
    const win = Object.assign(new EventTarget(), { matchMedia: () => media });
    Object.defineProperty(win, "localStorage", {
      get: () => {
        throw new Error("Blocked");
      },
    });
    const root = {
      dataset: {} as Record<string, string>,
      style: { colorScheme: "" },
    };
    vi.stubGlobal("window", win);
    vi.stubGlobal("document", { documentElement: root });
    const store = createThemeStore(),
      unsubscribe = store.subscribe(() => {});
    expect(root.dataset.theme).toBe("dark");
    expect(root.style.colorScheme).toBe("dark");
    store.toggleTheme();
    expect(root.dataset.theme).toBe("light");
    expect(root.style.colorScheme).toBe("light");
    unsubscribe();
  });

  it("labels the next action accessibly while showing the current pressed state", () => {
    for (const theme of ["light", "dark"] as Theme[]) {
      const html = renderToStaticMarkup(
        <ThemeToggle theme={theme} onToggle={() => {}} />,
      );
      expect(html).toContain('type="button"');
      expect(html).toContain(`aria-pressed="${theme === "dark"}"`);
      expect(html).toContain(
        `aria-label="Включить ${theme === "dark" ? "светлую" : "тёмную"} тему"`,
      );
      expect(html).toContain(theme === "dark" ? "Светлая" : "Тёмная");
      expect(html).toContain('aria-hidden="true"');
    }
  });
});

it("keeps the dark semantic foregrounds above WCAG AA normal-text contrast on work surfaces", async () => {
  // Node is available in Vitest; production browser types deliberately omit its APIs.
  const builtin = "node:fs";
  const fs: { readFileSync: (path: URL, encoding: "utf8") => string } =
    await import(/* @vite-ignore */ builtin);
  const themeCss = fs.readFileSync(
    new URL("./theme.css", import.meta.url),
    "utf8",
  );
  const variable = (name: string) => {
    const match = themeCss.match(
      new RegExp(`--theme-${name}:\\s*(#[0-9a-f]{6})`, "i"),
    );
    if (!match) throw new Error("Missing palette color " + name);
    return match[1];
  };
  const luminance = (hex: string) => {
    const rgb = [1, 3, 5]
      .map((offset) => parseInt(hex.slice(offset, offset + 2), 16) / 255)
      .map((value) =>
        value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
      );
    return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
  };
  const contrast = (fg: string, bg: string) => {
    const values = [luminance(fg), luminance(bg)].sort((a, b) => a - b);
    return (values[1] + 0.05) / (values[0] + 0.05);
  };
  for (const foreground of ["ink", "muted", "accent"])
    for (const background of ["bg", "surface", "raised", "soft"])
      expect(
        contrast(variable(foreground), variable(background)),
      ).toBeGreaterThanOrEqual(4.5);
  expect(contrast(variable("danger"), "#432820")).toBeGreaterThanOrEqual(4.5);
  expect(contrast(variable("warning"), "#473b22")).toBeGreaterThanOrEqual(4.5);
});
