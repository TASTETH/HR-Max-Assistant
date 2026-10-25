import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { api, setCsrf } from "./api";
import { Employees, EmployeeCard } from "./pages/Employees";
import { Cases, CaseView } from "./pages/Cases";
import { Attention, Delivery, Notifications } from "./pages/Queues";
import { Knowledge } from "./pages/Knowledge";
import { Materials } from "./pages/Materials";
import { Analytics, Digests } from "./pages/Analytics";
import { Users, ManagerHome } from "./pages/Users";

export type User = { id: number; login: string; role: "hr" | "manager"; is_hr_head: boolean; display_name: string; max_linked: boolean };

export function useHash(): [string[], (h: string) => void] {
  const [h, setH] = useState(window.location.hash.slice(2));
  useEffect(() => {
    const f = () => setH(window.location.hash.slice(2));
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, []);
  return [h.split("/").filter(Boolean), (x: string) => (window.location.hash = "#/" + x)];
}

export function useLoad<T>(url: string | null, deps: unknown[] = [], pollMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const reload = useCallback(() => {
    if (!url) return;
    api.get(url).then((d) => { setData(d); setError(""); }).catch((e) => setError(e.message));
  }, [url]);
  useEffect(() => {
    reload();
    if (pollMs) {
      const t = setInterval(reload, pollMs);
      return () => clearInterval(t);
    }
  }, [reload, ...deps]);
  return { data, error, reload, setData };
}

function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  return (
    <div className="login panel">
      <h1>Корсик · вход</h1>
      <p className="muted small">Кабинет HR-службы и руководителей подразделений.</p>
      {err && <div className="err">{err}</div>}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          api.post("/api/login", { login, password })
            .then((r) => { setCsrf(r.csrf); onLogin(r.user); })
            .catch((x) => setErr(x.message));
        }}
      >
        <label className="f">Логин<input value={login} onChange={(e) => setLogin(e.target.value)} autoFocus /></label>
        <br />
        <label className="f">Пароль<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        <br />
        <button type="submit">Войти</button>
      </form>
    </div>
  );
}

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);
  const [route, go] = useHash();
  const [counts, setCounts] = useState({ cases: 0, notes: 0 });

  useEffect(() => {
    api.get("/api/me").then((r) => { setCsrf(r.csrf); setUser(r.user); }).catch(() => {}).finally(() => setChecked(true));
    const out = () => setUser(null);
    window.addEventListener("korsik:logout", out);
    return () => window.removeEventListener("korsik:logout", out);
  }, []);

  useEffect(() => {
    if (!user) return;
    const f = () => {
      if (user.role === "hr") api.get("/api/cases?status=new").then((c) => setCounts((x) => ({ ...x, cases: c.length }))).catch(() => {});
      api.get("/api/notifications").then((n) => setCounts((x) => ({ ...x, notes: n.filter((y: any) => !y.read_at).length }))).catch(() => {});
    };
    f();
    const t = setInterval(f, 8000);
    return () => clearInterval(t);
  }, [user]);

  if (!checked) return null;
  if (!user) return <Login onLogin={setUser} />;

  const page = route[0] || (user.role === "hr" ? "employees" : "team");
  const nav = (key: string, label: string, n = 0) => (
    <a className={page === key ? "active" : ""} onClick={() => go(key)}>
      <span>{label}</span>
      {n > 0 && <span className="badge-n">{n}</span>}
    </a>
  );
  let body: React.ReactNode = null;
  if (user.role === "manager") {
    body = page === "notifications" ? <Notifications /> : <ManagerHome />;
  } else if (page === "employees" && route[1]) body = <EmployeeCard id={Number(route[1])} tab={route[2] || "card"} />;
  else if (page === "employees") body = <Employees />;
  else if (page === "cases" && route[1]) body = <CaseView id={Number(route[1])} />;
  else if (page === "cases") body = <Cases />;
  else if (page === "attention") body = <Attention />;
  else if (page === "materials") body = <Materials />;
  else if (page === "knowledge") body = <Knowledge />;
  else if (page === "analytics") body = <Analytics />;
  else if (page === "digests") body = <Digests user={user} />;
  else if (page === "delivery") body = <Delivery />;
  else if (page === "users") body = <Users me={user} />;
  else if (page === "notifications") body = <Notifications />;

  return (
    <div className="layout">
      <nav className="side">
        <div className="brand">Корсик<small>{user.role === "hr" ? "кабинет HR-службы" : "кабинет руководителя"}</small></div>
        {user.role === "hr" ? (
          <>
            {nav("employees", "Сотрудники")}
            {nav("cases", "Обращения", counts.cases)}
            {nav("attention", "Сигналы опросов")}
            {nav("materials", "Сценарий и материалы")}
            {nav("knowledge", "База знаний")}
            {nav("analytics", "Аналитика")}
            {user.is_hr_head && nav("digests", "Недельный дайджест")}
            {nav("delivery", "Доставка")}
            {nav("users", "Руководители и MAX")}
            {nav("notifications", "Уведомления", counts.notes)}
          </>
        ) : (
          <>
            {nav("team", "Мои сотрудники")}
            {nav("notifications", "Уведомления", counts.notes)}
          </>
        )}
        <div className="bottom">
          {user.display_name}
          <br />
          <a onClick={() => api.post("/api/logout").finally(() => setUser(null))}>Выйти</a>
        </div>
      </nav>
      <main className="main">{body}</main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
