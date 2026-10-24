import { useSyncExternalStore } from "react";
import { Moon, Sun } from "lucide-react";

export type Theme = "light" | "dark";
export const THEME_STORAGE_KEY = "korsik-theme";

export interface ThemeEnvironment {
  readPreference: () => string | null;
  writePreference: (theme: Theme) => void;
  prefersDark: () => boolean;
  apply: (theme: Theme) => void;
  onSystemChange: (listener: () => void) => () => void;
  onStorageChange: (listener: (value: string | null) => void) => () => void;
}

const asTheme = (value: string | null): Theme | null =>
  value === "dark" || value === "light" ? value : null;

function browserEnvironment(): ThemeEnvironment {
  let storage: Storage | null = null;
  let media: MediaQueryList | null = null;
  try {
    if (typeof window !== "undefined") storage = window.localStorage;
  } catch {
    /* A webview can deny storage while the current tab still works. */
  }
  try {
    if (typeof window !== "undefined" && window.matchMedia)
      media = window.matchMedia("(prefers-color-scheme: dark)");
  } catch {
    /* Use the light fallback on unsupported clients. */
  }
  return {
    readPreference: () => storage?.getItem(THEME_STORAGE_KEY) ?? null,
    writePreference: (theme) => storage?.setItem(THEME_STORAGE_KEY, theme),
    prefersDark: () => media?.matches ?? false,
    apply: (theme) => {
      if (typeof document !== "undefined") {
        document.documentElement.dataset.theme = theme;
        document.documentElement.style.colorScheme = theme;
      }
    },
    onSystemChange: (listener) => {
      if (!media) return () => {};
      if (typeof media.addEventListener === "function") {
        media.addEventListener("change", listener);
        return () => media?.removeEventListener("change", listener);
      }
      if (typeof media.addListener === "function") {
        media.addListener(listener);
        return () => media?.removeListener(listener);
      }
      return () => {};
    },
    onStorageChange: (listener) => {
      if (typeof window === "undefined") return () => {};
      const handler = (event: StorageEvent) => {
        if (
          (event.key === THEME_STORAGE_KEY || event.key === null) &&
          (!storage || !event.storageArea || event.storageArea === storage)
        )
          listener(event.newValue);
      };
      window.addEventListener("storage", handler);
      return () => window.removeEventListener("storage", handler);
    },
  };
}

export function createThemeStore(
  environment: ThemeEnvironment = browserEnvironment(),
) {
  let preference: Theme | null = null;
  try {
    preference = asTheme(environment.readPreference());
  } catch {
    /* Storage denied. */
  }
  let theme: Theme =
    preference ?? (environment.prefersDark() ? "dark" : "light");
  const listeners = new Set<() => void>();
  let stopSystem: (() => void) | undefined;
  let stopStorage: (() => void) | undefined;
  const update = (next: Theme) => {
    environment.apply(next);
    if (next === theme) return;
    theme = next;
    listeners.forEach((listener) => listener());
  };
  const setTheme = (next: Theme) => {
    preference = next;
    try {
      environment.writePreference(next);
    } catch {
      /* Keep the choice in this tab. */
    }
    update(next);
  };
  return {
    getSnapshot: () => theme,
    getServerSnapshot: (): Theme => "light",
    setTheme,
    toggleTheme: () => setTheme(theme === "dark" ? "light" : "dark"),
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      if (listeners.size === 1) {
        update(preference ?? (environment.prefersDark() ? "dark" : "light"));
        stopSystem = environment.onSystemChange(() => {
          if (preference === null)
            update(environment.prefersDark() ? "dark" : "light");
        });
        stopStorage = environment.onStorageChange((value) => {
          preference = asTheme(value);
          update(preference ?? (environment.prefersDark() ? "dark" : "light"));
        });
      }
      return () => {
        listeners.delete(listener);
        if (!listeners.size) {
          stopSystem?.();
          stopStorage?.();
          stopSystem = stopStorage = undefined;
        }
      };
    },
  };
}

let sharedStore: ReturnType<typeof createThemeStore> | undefined;
export function useTheme() {
  const store = (sharedStore ??= createThemeStore());
  const theme = useSyncExternalStore(
    store.subscribe,
    store.getSnapshot,
    store.getServerSnapshot,
  );
  return { theme, toggleTheme: store.toggleTheme, setTheme: store.setTheme };
}

export function ThemeToggle({
  theme,
  onToggle,
  className = "",
}: {
  theme: Theme;
  onToggle: () => void;
  className?: string;
}) {
  const dark = theme === "dark";
  const label = dark ? "Включить светлую тему" : "Включить тёмную тему";
  return (
    <button
      type="button"
      className={`theme-toggle ${className}`.trim()}
      onClick={onToggle}
      aria-label={label}
      title={label}
      aria-pressed={dark}
    >
      {dark ? (
        <Sun size={18} aria-hidden="true" />
      ) : (
        <Moon size={18} aria-hidden="true" />
      )}
      <span>{dark ? "Светлая" : "Тёмная"}</span>
    </button>
  );
}
