/** Official MAX Bridge; identity always comes from server-verified initData.
 * Docs: https://dev.max.ru/docs/webapps/bridge and /validation. */
export const MAX_BRIDGE_URL = "https://st.max.ru/js/max-web-app.js";
// Observed 2026-10-24 SDK SHA256: b8540b59e747944c22bbedf20e48cd3d1508bb97784dda86c38ab48835759b14.
// CDN omits CORS headers, so SRI+crossOrigin would block it. This is an official
// HTTPS script load, NOT runtime integrity pinning. CSP allows only st.max.ru.
type MaxBridge = {
  initData?: string; platform?: string; ready?: () => void;
  getViewportSize?: () => Promise<{ width: string | number; height: string | number }>;
};
declare global { interface Window { WebApp?: MaxBridge } }
export type MaxMiniAppContext = {
  detected: boolean; sdkAvailable: boolean; platform: string | null;
  viewport: { width: number; height: number; source: "MAX_SDK" | "VISUAL_VIEWPORT" | "WINDOW" };
  signedInitDataPresent: boolean;
};
export type MaxMiniAppSession = {
  user: { id: string; role: string; [key: string]: unknown };
  actor: { id: string; role: string; [key: string]: unknown };
  csrf_token: string; auth_method: "MAX_MINIAPP"; session_expires_at: string;
};
export class MaxMiniAppError extends Error {
  constructor(public code: string, message: string) { super(message); this.name = "MaxMiniAppError"; }
}
let capturedInitData: string | undefined;
let bridgeLoad: Promise<boolean> | undefined;
function launchParameters() { return new URLSearchParams(window.location.hash.replace(/^#/, "")); }
export function detectMaxMiniApp(): boolean {
  return Boolean(capturedInitData || window.WebApp?.initData || launchParameters().has("WebAppData"));
}
function captureInitData(): string {
  if (capturedInitData) return capturedInitData;
  const parameters = launchParameters();
  const values = parameters.getAll("WebAppData");
  if (values.length > 1) throw new MaxMiniAppError("max_init_data_invalid", "В запуске MAX повторяются параметры. Откройте приложение заново.");
  const raw = values[0] || window.WebApp?.initData || "";
  if (!raw || raw.length > 32768) throw new MaxMiniAppError("max_init_data_missing", "Откройте приложение через кнопку бота в MAX или используйте обычный вход.");
  if (values[0] && window.WebApp?.initData && values[0] !== window.WebApp.initData)
    throw new MaxMiniAppError("max_init_data_invalid", "Параметры запуска MAX не совпадают. Откройте приложение заново.");
  capturedInitData = raw; // Memory only: no localStorage, logs or unsafe fields.
  return raw;
}
function stripCredentialFragment() {
  const parameters = launchParameters();
  if (!parameters.has("WebAppData")) return;
  parameters.delete("WebAppData");
  const rest = parameters.toString();
  window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search + (rest ? `#${rest}` : ""));
}
function loadOfficialBridge(): Promise<boolean> {
  if (window.WebApp) return Promise.resolve(true);
  if (bridgeLoad) return bridgeLoad;
  bridgeLoad = new Promise((resolve) => {
    const script = document.createElement("script");
    script.src = MAX_BRIDGE_URL; script.async = true; script.referrerPolicy = "no-referrer";
    let done = false;
    const finish = () => {
      if (done) return;
      done = true; clearTimeout(timer); script.onload = script.onerror = null;
      resolve(Boolean(window.WebApp));
    };
    const timer = setTimeout(finish, 5000);
    script.onload = finish; script.onerror = finish;
    document.head.appendChild(script);
  });
  return bridgeLoad;
}
function dimension(value: unknown): number | null {
  if (typeof value !== "number" && (typeof value !== "string" || !/^\d+(?:\.\d+)?(?:px)?$/.test(value))) return null;
  const result = typeof value === "number" ? value : Number.parseFloat(value);
  return Number.isFinite(result) && result > 0 && result <= 20000 ? result : null;
}
function browserViewport(): MaxMiniAppContext["viewport"] {
  const visual = window.visualViewport;
  return { width: dimension(visual?.width) || dimension(window.innerWidth) || 360,
    height: dimension(visual?.height) || dimension(window.innerHeight) || 640,
    source: visual ? "VISUAL_VIEWPORT" : "WINDOW" };
}
function setViewport(viewport: MaxMiniAppContext["viewport"]) {
  document.documentElement.style.setProperty("--max-viewport-height", `${viewport.height}px`);
  document.documentElement.style.setProperty("--max-viewport-width", `${viewport.width}px`);
}
export async function initializeMaxMiniApp(): Promise<{ context: MaxMiniAppContext; cleanup: () => void }> {
  const detected = detectMaxMiniApp();
  if (detected) captureInitData();
  // SDK must see the original fragment before credentials are removed from it.
  const sdkAvailable = detected ? await loadOfficialBridge() : false;
  if (detected) stripCredentialFragment();
  const platform = window.WebApp?.platform;
  const context: MaxMiniAppContext = { detected, sdkAvailable,
    platform: platform && ["ios", "android", "desktop", "web"].includes(platform) ? platform : null,
    viewport: browserViewport(), signedInitDataPresent: Boolean(capturedInitData) };
  let active = true;
  const update = () => { if (active) { context.viewport = browserViewport(); setViewport(context.viewport); } };
  update();
  // No documented SDK safe-area API: use CSS environment values, not initData.
  for (const side of ["top", "right", "bottom", "left"])
    document.documentElement.style.setProperty(`--max-safe-area-${side}`, `env(safe-area-inset-${side}, 0px)`);
  window.addEventListener("resize", update);
  window.visualViewport?.addEventListener("resize", update);
  if (sdkAvailable) {
    try { window.WebApp?.ready?.(); } catch { /* Optional UI acknowledgement. */ }
    const sdk = window.WebApp;
    if (sdk?.getViewportSize) {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const size = await Promise.race([sdk.getViewportSize(), new Promise<null>((resolve) => { timer = setTimeout(() => resolve(null), 2000); })]);
        const width = dimension(size?.width), height = dimension(size?.height);
        if (active && width && height) { context.viewport = { width, height, source: "MAX_SDK" }; setViewport(context.viewport); }
      } catch { /* Unsupported client: browser viewport remains available. */ }
      finally { if (timer) clearTimeout(timer); }
    }
  }
  return { context, cleanup: () => { active = false; window.removeEventListener("resize", update); window.visualViewport?.removeEventListener("resize", update); } };
}
let authentication: Promise<MaxMiniAppSession> | undefined;
export function authenticateMaxMiniApp(): Promise<MaxMiniAppSession> {
  if (!authentication) authentication = exchangeMaxMiniApp().finally(() => { authentication = undefined; });
  return authentication;
}
async function exchangeMaxMiniApp(): Promise<MaxMiniAppSession> {
  const initData = captureInitData();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch("/api/v1/auth/max-miniapp", { method: "POST", credentials: "include", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ init_data: initData }), signal: controller.signal });
    const result = await response.json();
    if (!response.ok) throw new MaxMiniAppError(result?.detail?.code || "max_auth_failed", result?.detail?.message || "Не удалось войти через MAX. Закройте и снова откройте приложение через бота.");
    if (result.auth_method !== "MAX_MINIAPP" || !result.actor?.id || !result.csrf_token)
      throw new MaxMiniAppError("max_session_invalid", "Ответ входа некорректен. Закройте и снова откройте приложение через бота.");
    // Set-Cookie alone does not prove that an embedded webview accepted it.
    const check = await fetch("/api/v1/auth/session", { credentials: "include", cache: "no-store", signal: controller.signal });
    const current = await check.json();
    if (!check.ok || current.actor?.id !== result.actor.id || current.csrf_token !== result.csrf_token)
      throw new MaxMiniAppError("max_cookie_unavailable", "MAX не сохранил защищённую сессию. Разрешите cookie для приложения или откройте кабинет в браузере и войдите обычным способом.");
    return result as MaxMiniAppSession;
  } catch (error) {
    if (error instanceof MaxMiniAppError) throw error;
    throw new MaxMiniAppError("max_network_unavailable", "Не удалось связаться с кабинетом. Проверьте соединение и повторите вход.");
  } finally { clearTimeout(timer); }
}
