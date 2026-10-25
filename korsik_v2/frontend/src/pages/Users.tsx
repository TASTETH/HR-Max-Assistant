import React, { useState } from "react";
import { api, EMP_ST, fmtD, fmtDT } from "../api";
import type { User } from "../main";
import { useLoad } from "../main";

export function Users({ me }: { me: User }) {
  const { data, error, reload } = useLoad<any[]>("/api/users");
  const [f, setF] = useState({ login: "", password: "", display_name: "" });
  const [err, setErr] = useState("");
  const [link, setLink] = useState<{ id: number; link: string } | null>(null);
  return (
    <>
      <h1>Руководители и каналы MAX</h1>
      {(error || err) && <div className="err">{error || err}</div>}
      <div className="panel">
        <table>
          <thead><tr><th>Пользователь</th><th>Роль</th><th>Подчинённых</th><th>MAX для уведомлений</th><th></th></tr></thead>
          <tbody>{(data || []).map((u) => (
            <tr key={u.id}>
              <td><b>{u.display_name}</b><div className="small muted">{u.login}{!u.active && " · отключён"}</div></td>
              <td>{u.role === "hr" ? `HR${u.is_hr_head ? " + руководитель HR-направления" : ""}` : "руководитель подразделения"}</td>
              <td>{u.role === "manager" ? u.team : "—"}</td>
              <td>{u.max_linked ? <span className="pill ok">привязан</span> : <span className="pill">не привязан</span>}
                {link?.id === u.id && <div className="linkbox small">{link!.link}</div>}</td>
              <td className="row">
                <button className="small sec" onClick={() => api.post(`/api/users/${u.id}/invite`).then((r) => setLink({ id: u.id, link: r.link }))}>Ссылка для привязки MAX</button>
                {u.role === "manager" && <button className="small sec" onClick={() => api.post(`/api/users/${u.id}/active/${!u.active}`).then(reload)}>{u.active ? "Отключить" : "Включить"}</button>}
              </td>
            </tr>))}
          </tbody>
        </table>
        <p className="small muted">Привязка MAX нужна, чтобы служебные уведомления (новые обращения, встречи, дайджест) приходили в MAX. Без неё они видны только в кабинете — это показано в статусе уведомления.</p>
      </div>
      <div className="panel">
        <h3>Новый руководитель подразделения</h3>
        <p className="small muted">Видит только явно связанных с ним сотрудников: этап и прогресс адаптации, ближайшие события, уведомления о встречах. Не видит документы, переписку и тексты жалоб.</p>
        <div className="grid3">
          <label className="f">Логин<input value={f.login} onChange={(x) => setF({ ...f, login: x.target.value })} /></label>
          <label className="f">ФИО<input value={f.display_name} onChange={(x) => setF({ ...f, display_name: x.target.value })} /></label>
          <label className="f">Пароль (≥10 символов)<input type="password" value={f.password} onChange={(x) => setF({ ...f, password: x.target.value })} /></label>
        </div><br />
        <button onClick={() => api.post("/api/users", f).then(() => { setF({ login: "", password: "", display_name: "" }); reload(); }).catch((x) => setErr(x.message))}>Создать</button>
      </div>
    </>
  );
}

export function ManagerHome() {
  const { data, error, reload } = useLoad<any>("/api/manager/team", [], 15000);
  if (error) return <div className="err">{error}</div>;
  if (!data) return null;
  return (
    <>
      <h1>Адаптация моих сотрудников</h1>
      {data.notifications.filter((n: any) => !n.read_at).map((n: any) => (
        <div key={n.id} className="note"><b>{n.title}</b><div>{n.body}</div>
          <a className="small" onClick={() => api.post(`/api/notifications/${n.id}/read`).then(reload)}>Отметить прочитанным</a></div>))}
      <div className="panel">
        <table>
          <thead><tr><th>Сотрудник</th><th>Выход / конец ИС</th><th>Этап</th><th>Прогресс</th><th>Ближайшее</th></tr></thead>
          <tbody>{data.team.map((e: any) => (
            <tr key={e.id}>
              <td><b>{e.full_name}</b><div className="small muted">{e.position} · {e.department}</div></td>
              <td className="small">{fmtD(e.start_date)}<br />{fmtD(e.probation_end)}</td>
              <td><span className="pill acc">{e.stage}</span><div className="small muted">{EMP_ST[e.status]}{!e.max_linked && " · бот ещё не подключён"}</div></td>
              <td className="small">{e.progress.events_done}/{e.progress.events_total} этапов · опросов пройдено {e.progress.surveys_completed}{e.progress.surveys_overdue > 0 && <div className="pill warn">опрос без ответа</div>}</td>
              <td className="small">{e.next.map((n: any, i: number) => <div key={i}>{fmtDT(n.due_at)} — {n.title}</div>)}</td>
            </tr>))}
          </tbody>
        </table>
        {!data.team.length && <p className="muted">За вами пока не закреплены новые сотрудники.</p>}
      </div>
    </>
  );
}
