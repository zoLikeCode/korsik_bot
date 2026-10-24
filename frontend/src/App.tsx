import {
  createContext,
  useContext,
  useEffect,
  useState,
  lazy,
  Suspense,
  Component,
  type ReactNode,
} from "react";
import {
  NavLink,
  Route,
  Routes,
  Navigate,
  useLocation,
} from "react-router-dom";
import {
  LayoutDashboard,
  Users,
  Workflow,
  BookOpen,
  MessageCircle,
  ClipboardList,
  LogOut,
  Menu,
  X,
  ChartNoAxesCombined,
  ChevronRight,
  Link2,
  Network,
  UserRound,
  UserCog,
  Send,
} from "lucide-react";
import { api, ApiError, errorText } from "./api";
import { ErrorBox, Loading, PageTitle, Modal } from "./ui";
import { useTheme, ThemeToggle, type Theme } from "./theme";
import {
  initializeMaxMiniApp,
  authenticateMaxMiniApp,
  detectMaxMiniApp,
  type MaxMiniAppContext,
} from "./maxMiniApp";
import "./AppChrome.css";
import DialogNavBadge from "./DialogNavBadge";
import CredentialsForm from "./CredentialsForm";
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Employees = lazy(() => import("./pages/Employees"));
const Organization = lazy(() => import("./pages/Organization"));
const Chat = lazy(() => import("./pages/Chat"));
const Scenarios = lazy(() => import("./pages/Scenarios"));
const Knowledge = lazy(() => import("./pages/Knowledge"));
const Surveys = lazy(() => import("./pages/Surveys"));
const PrincipalBindings = lazy(() => import("./pages/PrincipalBindings"));
const Profile = lazy(() => import("./pages/Profile"));
const HrAccounts = lazy(() => import("./pages/HrAccounts"));
const Broadcasts = lazy(() => import("./pages/Broadcasts"));

export type Actor = {
  id: string;
  email: string;
  name: string;
  role: string;
  scope: any;
  employee_id?: string;
  credentials_change_required?: boolean;
  hidden_setup?: boolean;
  login?: string;
};
const SessionContext = createContext<Actor | null>(null);
export function useActor() {
  return useContext(SessionContext)!;
}
const roles: Record<string, string> = {
  HR_ADMIN: "Администратор HR",
  HR_SPECIALIST: "HR-менеджер",
  HEAD_TALENT: "Руководитель по подбору персонала",
  MANAGER: "Руководитель подразделения",
  EMPLOYEE: "Сотрудник",
  PLATFORM_ADMIN: "Администратор системы",
};
const icons = {
  dashboard: LayoutDashboard,
  employees: Users,
  scenarios: Workflow,
  knowledge: BookOpen,
  chat: MessageCircle,
  surveys: ClipboardList,
  analytics: ChartNoAxesCombined,
  organization: Network,
  profile: UserRound,
  hrAccounts: UserCog,
  broadcasts: Send,
};
export function canManagePersonalMax(role: string): boolean {
  return ["HR_ADMIN", "HR_SPECIALIST", "MANAGER", "HEAD_TALENT"].includes(role);
}
export type AppPage = { to: string; title: string; icon: keyof typeof icons };
export function pagesForActor(actor: Pick<Actor, "role" | "scope">): AppPage[] {
  if (actor.role === "EMPLOYEE")
    return [
      { to: "/profile", title: "Мой профиль", icon: "profile" },
      { to: "/chat", title: "Диалог", icon: "chat" },
      { to: "/surveys", title: "Опросы", icon: "surveys" },
      { to: "/organization", title: "Оргструктура", icon: "organization" },
    ];
  if (actor.role === "MANAGER")
    return [
      { to: "/profile", title: "Профиль и команда", icon: "profile" },
      { to: "/dashboard", title: "Обзор", icon: "dashboard" },
      { to: "/surveys", title: "Опросы", icon: "surveys" },
      { to: "/organization", title: "Оргструктура", icon: "organization" },
    ];
  const author = ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(
    actor.role,
  );
  if (!author) return [];
  return [
    { to: "/dashboard", title: "Обзор", icon: "dashboard" },
    { to: "/employees", title: "Сотрудники", icon: "employees" },
    ...(["HR_ADMIN", "HEAD_TALENT"].includes(actor.role)
      ? [
          {
            to: "/hr-accounts",
            title: "HR-менеджеры",
            icon: "hrAccounts" as const,
          },
        ]
      : []),
    { to: "/organization", title: "Оргструктура", icon: "organization" },
    ...(author
      ? [
          {
            to: "/scenarios",
            title: "Конструктор сценариев",
            icon: "scenarios" as const,
          },
          {
            to: "/knowledge",
            title: "База знаний",
            icon: "knowledge" as const,
          },
        ]
      : []),
    { to: "/surveys", title: "Опросы", icon: "surveys" },
    ...(author
      ? [{ to: "/chat", title: "Диалоги", icon: "chat" as const }]
      : []),
    { to: "/broadcasts", title: "Рассылки", icon: "broadcasts" },
    { to: "/analytics", title: "Аналитика", icon: "analytics" },
  ];
}
export function homeForActor(actor: Pick<Actor, "role" | "scope">) {
  return pagesForActor(actor)[0]?.to || "/access";
}
function Login({
  onLogin,
  miniApp,
  onRetry,
  startupError,
  theme,
  toggleTheme,
}: {
  onLogin: (u: Actor) => void;
  miniApp: boolean;
  onRetry: () => void;
  startupError: string;
  theme: Theme;
  toggleTheme: () => void;
}) {
  const [email, setEmail] = useState(""),
    [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    document.title = "Вход";
  }, []);
  return (
    <main className="login-page compact-login">
      <div className="login-theme">
        <ThemeToggle theme={theme} onToggle={toggleTheme} />
      </div>
      <section className="login-form-inner panel">
        <div className="brand">
          <img className="brand-logo" src="/brand/korsik.png" alt="Корсик" />
        </div>
        <h1>Вход</h1>
        <ErrorBox error={startupError || error} />
        {miniApp ? (
          <>
            <p>
              Вход выполняется через подтверждённый аккаунт MAX. Если аккаунт
              ещё не подключён, откройте персональное приглашение и подключите
              аккаунт.
            </p>
            <button className="button primary full" onClick={onRetry}>
              Повторить вход через MAX
            </button>
          </>
        ) : (
          <form
            onSubmit={async (event) => {
              event.preventDefault();
              setError("");
              setBusy(true);
              try {
                const r = await api.post("/auth/login", { email, password });
                onLogin(r.data.user || r.data.actor);
              } catch (e) {
                setError(errorText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            <label className="field">
              <span>Логин</span>
              <input
                type="text"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <label className="field">
              <span>Пароль</span>
              <input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            <button className="button primary full" disabled={busy}>
              {busy ? "Входим…" : "Войти"}
            </button>
          </form>
        )}
      </section>
    </main>
  );
}
class ProductBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <section className="panel">
        <h2>Не удалось показать страницу</h2>
        <button
          className="button primary"
          onClick={() => window.location.reload()}
        >
          Обновить страницу
        </button>
      </section>
    ) : (
      this.props.children
    );
  }
}
export function MobileNavigationTrigger({
  open,
  onToggle,
}: {
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className="icon-button app-menu-trigger"
      aria-label={open ? "Закрыть меню" : "Открыть меню"}
      aria-controls="app-sidebar"
      aria-expanded={open}
      onClick={onToggle}
    >
      {open ? <X aria-hidden="true" /> : <Menu aria-hidden="true" />}
    </button>
  );
}
export default function App() {
  const { theme, toggleTheme } = useTheme();
  const [actor, setActor] = useState<Actor | null>(null),
    [loading, setLoading] = useState(true);
  const [mobile, setMobile] = useState(false),
    [context, setContext] = useState<MaxMiniAppContext | null>(null);
  const [miniAppDetected, setMiniAppDetected] = useState(false);
  const [personalMaxOpen, setPersonalMaxOpen] = useState(false);
  const [credentialsOpen, setCredentialsOpen] = useState(false);
  const [startupError, setStartupError] = useState(""),
    [retry, setRetry] = useState(0);
  const location = useLocation();
  useEffect(() => {
    if (!mobile) return;
    const sidebar = document.getElementById("app-sidebar");
    sidebar?.querySelector<HTMLButtonElement>(".sidebar-close")?.focus();
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobile(false);
      if (event.key === "Tab") {
        const targets = sidebar?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex="0"]',
        );
        if (!targets?.length) return;
        const first = targets[0],
          last = targets[targets.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("keydown", close);
      document.querySelector<HTMLButtonElement>(".app-menu-trigger")?.focus();
    };
  }, [mobile]);
  useEffect(() => {
    setPersonalMaxOpen(false);
  }, [actor?.id]);
  useEffect(() => {
    let active = true;
    let cleanup: (() => void) | undefined;
    let signedMax = false;
    setLoading(true);
    setStartupError("");
    (async () => {
      try {
        signedMax = detectMaxMiniApp();
        if (active) setMiniAppDetected(signedMax);
        const initialized = await initializeMaxMiniApp();
        if (!active) {
          initialized.cleanup();
          return;
        }
        cleanup = initialized.cleanup;
        setContext(initialized.context);
        signedMax =
          initialized.context.detected &&
          initialized.context.signedInitDataPresent;
        const result =
          initialized.context.detected &&
          initialized.context.signedInitDataPresent
            ? await authenticateMaxMiniApp()
            : (await api.get("/auth/session")).data;
        if (active) setActor(result.user || result.actor);
      } catch (e) {
        if (active) {
          setActor(null);
          setStartupError(
            e instanceof ApiError && e.status === 401 && !signedMax
              ? ""
              : errorText(e),
          );
        }
      } finally {
        if (active) setLoading(false);
      }
    })();
    const expired = () => setActor(null);
    window.addEventListener("session-expired", expired);
    return () => {
      active = false;
      cleanup?.();
      window.removeEventListener("session-expired", expired);
    };
  }, [retry]);
  const pages = actor ? pagesForActor(actor) : [];
  const current = pages.find((page) => page.to === location.pathname);
  useEffect(() => {
    setMobile(false);
    document.title = current?.title || (actor ? "Доступ" : "Вход");
  }, [location.pathname, current?.title, actor?.id]);
  if (loading)
    return (
      <div className="app-loading">
        <Loading />
      </div>
    );
  if (!actor)
    return (
      <Login
        onLogin={(user) => {
          setStartupError("");
          setActor(user);
        }}
        miniApp={miniAppDetected || context?.detected === true}
        onRetry={() => setRetry((v) => v + 1)}
        startupError={startupError}
        theme={theme}
        toggleTheme={toggleTheme}
      />
    );
  if (actor.credentials_change_required)
    return (
      <main className="login-page compact-login">
        <section className="panel login-form-inner">
          <img className="brand-logo" src="/brand/korsik.png" alt="Корсик" />
          <h1>Настройка аккаунта</h1>
          <p>Задайте свой логин и новый пароль перед началом работы.</p>
          <CredentialsForm actor={actor} onDone={setActor} />
        </section>
      </main>
    );
  const home = homeForActor(actor);
  return (
    <SessionContext.Provider value={actor}>
      <div className={`app-shell ${context?.detected ? "max-miniapp" : ""}`}>
        <aside
          id="app-sidebar"
          className={`sidebar ${mobile ? "visible" : ""}`}
          aria-label="Навигация приложения"
        >
          <div className="sidebar-brand-row">
            <NavLink className="brand" to={home}>
              <img
                className="brand-logo"
                src="/brand/korsik.png"
                alt="Корсик"
              />
            </NavLink>
            <button
              type="button"
              className="icon-button sidebar-close"
              aria-label="Закрыть меню"
              onClick={() => setMobile(false)}
            >
              <X aria-hidden="true" />
            </button>
          </div>
          <nav aria-label="Разделы">
            {pages.map((page) => {
              const Icon = icons[page.icon];
              return (
                <NavLink
                  key={page.to}
                  to={page.to}
                  onClick={() => setMobile(false)}
                  className={({ isActive }) =>
                    isActive ? "nav-link active" : "nav-link"
                  }
                >
                  <Icon size={19} />
                  {page.title}
                  {page.to === "/chat" &&
                    ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(
                      actor.role,
                    ) && <DialogNavBadge />}
                  <ChevronRight className="nav-arrow" size={14} />
                </NavLink>
              );
            })}
          </nav>
          <div className="sidebar-foot">
            <ThemeToggle
              theme={theme}
              onToggle={toggleTheme}
              className="sidebar-theme-toggle"
            />
            {canManagePersonalMax(actor.role) && (
              <button
                className="button secondary full personal-max-button"
                onClick={() => {
                  setMobile(false);
                  setPersonalMaxOpen(true);
                }}
              >
                <Link2 size={17} />
                Аккаунт MAX
              </button>
            )}
            {actor.hidden_setup && (
              <button
                className="button secondary full"
                onClick={() => {
                  setMobile(false);
                  setCredentialsOpen(true);
                }}
              >
                Логин и пароль
              </button>
            )}
            <div className="profile">
              <div className="avatar">
                {actor.name
                  ?.split(" ")
                  .slice(0, 2)
                  .map((x) => x[0])
                  .join("") || "К"}
              </div>
              <div>
                <strong>{actor.name}</strong>
                <small>{roles[actor.role] || "Нет доступной роли"}</small>
              </div>
              <button
                className="icon-button"
                aria-label="Выйти"
                title="Выйти"
                onClick={async () => {
                  try {
                    await api.post("/auth/logout");
                    setActor(null);
                  } catch (e) {
                    setStartupError(errorText(e));
                  }
                }}
              >
                <LogOut size={17} />
              </button>
            </div>
          </div>
        </aside>
        {mobile && (
          <button
            className="sidebar-backdrop"
            aria-label="Закрыть меню"
            onClick={() => setMobile(false)}
          />
        )}
        <div className="main-shell">
          <MobileNavigationTrigger
            open={mobile}
            onToggle={() => setMobile(!mobile)}
          />
          <main className="main-content">
            <ErrorBox error={startupError} />
            <ProductBoundary key={`${actor.id}:${location.pathname}`}>
              <Suspense fallback={<Loading />}>
                <Routes>
                  {pages.map((page) => (
                    <Route
                      key={page.to}
                      path={page.to}
                      element={
                        page.to === "/profile" ? (
                          <Profile />
                        ) : page.to === "/hr-accounts" ? (
                          <HrAccounts />
                        ) : page.to === "/broadcasts" ? (
                          <Broadcasts />
                        ) : page.to === "/dashboard" ? (
                          <Dashboard />
                        ) : page.to === "/analytics" ? (
                          <Dashboard analytics />
                        ) : page.to === "/employees" ? (
                          <Employees />
                        ) : page.to === "/organization" ? (
                          <Organization />
                        ) : page.to === "/scenarios" ? (
                          <Scenarios />
                        ) : page.to === "/knowledge" ? (
                          <Knowledge />
                        ) : page.to === "/surveys" ? (
                          <Surveys />
                        ) : (
                          <Chat />
                        )
                      }
                    />
                  ))}
                  <Route
                    path="*"
                    element={
                      pages.length ? (
                        <Navigate to={home} replace />
                      ) : (
                        <>
                          <PageTitle title="Доступ" />
                          <section className="panel">
                            Для этой учётной записи нет доступных разделов
                            приложения.
                          </section>
                        </>
                      )
                    }
                  />
                </Routes>
              </Suspense>
            </ProductBoundary>
          </main>
        </div>
        {personalMaxOpen && canManagePersonalMax(actor.role) && (
          <Modal title="Аккаунт MAX" onClose={() => setPersonalMaxOpen(false)}>
            <Suspense fallback={<Loading />}>
              <PrincipalBindings />
            </Suspense>
          </Modal>
        )}
      </div>
      {credentialsOpen && (
        <Modal title="Логин и пароль" onClose={() => setCredentialsOpen(false)}>
          <CredentialsForm
            actor={actor}
            onDone={(user) => {
              setActor(user);
              setCredentialsOpen(false);
            }}
          />
        </Modal>
      )}
    </SessionContext.Provider>
  );
}
