import { afterEach, describe, expect, it, vi } from "vitest";

function browser(hash = "") {
  const styles = new Map<string,string>();
  const visual = Object.assign(new EventTarget(), { width: 380, height: 700 });
  const win: any = Object.assign(new EventTarget(), {
    location: { hash, pathname: "/dashboard", search: "" },
    innerWidth: 400, innerHeight: 800, visualViewport: visual,
    history: { state: null, replaceState: vi.fn((_s: unknown, _t: string, url: string) => {
      win.location.hash = url.includes("#") ? "#" + url.split("#")[1] : "";
    }) },
  });
  const append = vi.fn();
  const doc = {
    documentElement: { style: { setProperty: (key: string, value: string) => styles.set(key,value) } },
    createElement: vi.fn(() => ({})), head: { appendChild: append },
  };
  vi.stubGlobal("window",win);vi.stubGlobal("document",doc);
  return { win,doc,append,styles,visual };
}
const session = {
  actor: { id: "server-employee",role:"EMPLOYEE" }, user: { id:"server-employee",role:"EMPLOYEE" },
  csrf_token:"server-csrf",auth_method:"MAX_MINIAPP",session_expires_at:"2026-10-24T12:00:00Z",
};
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); vi.resetModules(); });

describe("MAX launch bridge boundary", () => {
  it("ordinary browser does not load MAX or invent a signed identity", async () => {
    const b=browser();const bridge=await import("./maxMiniApp");
    const initialized=await bridge.initializeMaxMiniApp();
    expect(initialized.context.detected).toBe(false);expect(b.append).not.toHaveBeenCalled();
    expect(initialized.context.viewport.source).toBe("VISUAL_VIEWPORT");
    expect(b.styles.get("--max-safe-area-top")).toBe("env(safe-area-inset-top, 0px)");
    await expect(bridge.authenticateMaxMiniApp()).rejects.toMatchObject({code:"max_init_data_missing"});
    initialized.cleanup();
    b.visual.height=500;b.visual.dispatchEvent(new Event("resize"));
    expect(b.styles.get("--max-viewport-height")).toBe("700px");
  });
  it("uses only raw signed initData and official viewport, strips it from history", async () => {
    const raw="user=%7B%22id%22%3A123%7D&hash=signed-fixture";
    const b=browser("#WebAppData="+encodeURIComponent(raw)+"&WebAppPlatform=web");
    b.win.WebApp={initData:raw,platform:"web",ready:vi.fn(),getViewportSize:async()=>({width:"390px",height:"750"})};
    const bridge=await import("./maxMiniApp");const initialized=await bridge.initializeMaxMiniApp();
    expect(initialized.context.viewport).toEqual({width:390,height:750,source:"MAX_SDK"});
    expect(initialized.context.signedInitDataPresent).toBe(true);expect(b.win.WebApp.ready).toHaveBeenCalledOnce();
    expect(b.win.location.hash).not.toContain("WebAppData");expect(b.win.location.hash).toContain("WebAppPlatform");
    const fetch=vi.fn(async(_url:string,_options?:RequestInit)=>new Response(JSON.stringify(session),{headers:{"Content-Type":"application/json"}}));
    vi.stubGlobal("fetch",fetch);
    await bridge.authenticateMaxMiniApp();
    expect(JSON.parse(fetch.mock.calls[0][1]?.body as string)).toEqual({init_data:raw});
    initialized.cleanup();
  });
  it("loads only official HTTPS SDK when actually launched and uses browser dimensions on unsupported clients", async () => {
    const b=browser("#WebAppData="+encodeURIComponent("user=signed&hash=fixture"));
    b.append.mockImplementation((script:any)=>{
      expect(script.src).toBe("https://st.max.ru/js/max-web-app.js");
      expect(script.crossOrigin).toBeUndefined();expect(script.integrity).toBeUndefined();
      b.win.WebApp={getViewportSize:async()=>({width:"100%;color:red",height:"-5"})};script.onload();
    });
    const bridge=await import("./maxMiniApp");const initialized=await bridge.initializeMaxMiniApp();
    expect(initialized.context.sdkAvailable).toBe(true);expect(initialized.context.viewport.source).toBe("VISUAL_VIEWPORT");
    expect(b.styles.get("--max-viewport-width")).toBe("380px");initialized.cleanup();
  });
  it("initDataUnsafe and query uid/role do not authenticate", async () => {
    const b=browser();b.win.location.search="?user_id=123&role=HR_ADMIN";
    b.win.WebApp={initDataUnsafe:{user:{id:123,role:"HR_ADMIN"}}};
    const bridge=await import("./maxMiniApp");expect(bridge.detectMaxMiniApp()).toBe(false);
    await expect(bridge.authenticateMaxMiniApp()).rejects.toMatchObject({code:"max_init_data_missing"});
  });
  it("rejects duplicate outer launch parameters before SDK or exchange", async () => {
    const b=browser("#WebAppData=first-signed-string&WebAppData=second-signed-string");
    const bridge=await import("./maxMiniApp");
    await expect(bridge.initializeMaxMiniApp()).rejects.toMatchObject({code:"max_init_data_invalid"});
    expect(b.append).not.toHaveBeenCalled();
  });
  it("does not treat a successful exchange as accepted cookies if browser retains another account", async () => {
    const b=browser();b.win.WebApp={initData:"signed-server-fixture"};
    let requests=0;
    vi.stubGlobal("fetch",vi.fn(async()=>new Response(JSON.stringify(++requests===1?session:{actor:{id:"old-hr"},csrf_token:"old-csrf"}),{headers:{"Content-Type":"application/json"}})));
    const bridge=await import("./maxMiniApp");
    await expect(bridge.authenticateMaxMiniApp()).rejects.toMatchObject({code:"max_cookie_unavailable"});
    expect(requests).toBe(2);
  });
  it("deduplicates concurrent StrictMode exchanges and confirms actor/CSRF on session read", async () => {
    const b=browser();b.win.WebApp={initData:"signed-server-fixture"};
    const fetch=vi.fn(async(_url:string,_options?:RequestInit)=>new Response(JSON.stringify(session),{headers:{"Content-Type":"application/json"}}));
    vi.stubGlobal("fetch",fetch);const bridge=await import("./maxMiniApp");
    const first=bridge.authenticateMaxMiniApp(),second=bridge.authenticateMaxMiniApp();
    expect(first).toBe(second);expect((await first).actor.role).toBe("EMPLOYEE");await second;
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/auth/max-miniapp");
    expect(fetch.mock.calls[0][1]?.credentials).toBe("include");
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/auth/session");
  });
});
