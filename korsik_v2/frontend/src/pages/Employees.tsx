import React, { useState } from "react";
import { api, AUTHOR, CASE_ST, DELIV, EMP_ST, EV_ST, fmtD, fmtDT, MODE, PRIO, SENT } from "../api";
import { useHash, useLoad } from "../main";

const TAGS = ["GrPP", "GTP", "Luzhniki"];
const FIELD_LABEL: Record<string, string> = {
  full_name: "ФИО", short_name: "Обращение (имя)", phone: "Телефон", email: "E-mail", organization: "Организация",
  department: "Подразделение", position: "Должность", work_format: "Формат работы", tags: "Теги сценария",
  start_date: "Дата выхода", timezone: "Часовой пояс", probation_end: "Окончание испытательного срока",
  address_form: "«Ты/вы»", manager_user_id: "Руководитель", it_access_confirmed: "IT-доступы подтверждены",
};

export function Employees() {
  const [qs, setQs] = useState("");
  const [status, setStatus] = useState("");
  const [tag, setTag] = useState("");
  const { data, error, reload } = useLoad<any[]>(`/api/employees?q=${encodeURIComponent(qs)}&status=${status}&tag=${tag}`, [], 15000);
  const [, go] = useHash();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [err, setErr] = useState("");
  return (
    <>
      <h1>Сотрудники</h1>
      <div className="panel row">
        <input placeholder="Поиск: ФИО, подразделение, должность" value={qs} onChange={(e) => setQs(e.target.value)} style={{ minWidth: 300 }} />
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Все состояния</option>
          {Object.entries(EMP_ST).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select value={tag} onChange={(e) => setTag(e.target.value)}>
          <option value="">Все теги</option>
          {TAGS.map((t) => <option key={t}>{t}</option>)}
        </select>
        <span style={{ flex: 1 }} />
        <button onClick={() => setCreating(!creating)}>+ Новый сотрудник</button>
      </div>
      {creating && (
        <div className="panel">
          <h3>Черновик карточки</h3>
          <p className="muted small">Ответственный HR назначается автоматически. Остальные поля можно заполнить вручную или из загруженных документов.</p>
          {err && <div className="err">{err}</div>}
          <div className="row">
            <input placeholder="ФИО" value={name} onChange={(e) => setName(e.target.value)} style={{ minWidth: 320 }} />
            <button disabled={!name.trim()} onClick={() => {
              const parts = name.trim().split(/\s+/);
              api.post("/api/employees", { full_name: name.trim(), short_name: parts[1] || parts[0], organization: "ООО «Группа ПОЛИПЛАСТИК»" })
                .then((r) => go(`employees/${r.id}`)).catch((x) => setErr(x.message));
            }}>Создать</button>
          </div>
        </div>
      )}
      {error && <div className="err">{error}</div>}
      <div className="panel">
        <table>
          <thead><tr><th>Сотрудник</th><th>Подразделение / руководитель</th><th>Тег</th><th>Выход</th><th>Этап</th><th>Прогресс</th><th>Диалог</th><th>MAX</th></tr></thead>
          <tbody>
            {(data || []).map((e) => (
              <tr key={e.id} className="click" onClick={() => go(`employees/${e.id}`)}>
                <td><b>{e.full_name || "—"}</b><br /><span className="muted small">{e.position}</span></td>
                <td>{e.department || "—"}<br /><span className="muted small">{e.manager_name || "руководитель не указан"}</span></td>
                <td>{(e.tags || []).join(", ") || "—"}</td>
                <td>{fmtD(e.start_date)}</td>
                <td><span className={`pill ${e.status === "active" ? "acc" : ""}`}>{EMP_ST[e.status]}</span><br /><span className="small muted">{e.stage}</span></td>
                <td className="small">{e.progress.events_done}/{e.progress.events_total} событий<br />опросы {e.progress.surveys_completed} ✓ {e.progress.surveys_overdue ? <span className="pill bad">просрочено {e.progress.surveys_overdue}</span> : null}</td>
                <td><span className={`pill ${e.dialog_mode === "auto" ? "" : e.dialog_mode === "hr_active" ? "ok" : "warn"}`}>{MODE[e.dialog_mode]}</span>{e.open_cases > 0 && <><br /><span className="small">обращений: {e.open_cases}</span></>}</td>
                <td>{e.max_linked ? <span className="pill ok">привязан</span> : <span className="pill">нет</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && !data.length && <p className="muted">Пока нет сотрудников.</p>}
      </div>
    </>
  );
}

export function EmployeeCard({ id, tab }: { id: number; tab: string }) {
  const { data, error, reload } = useLoad<any>(`/api/employees/${id}`, [id], 6000);
  const [, go] = useHash();
  if (error) return <div className="err">{error}</div>;
  if (!data) return null;
  const e = data.employee;
  const tabs: [string, string][] = [["card", "Карточка и документы"], ["invite", "Приглашение MAX"], ["events", "Сценарий"],
    ["surveys", "Опросы"], ["chat", "Переписка"], ["materials", "Материалы"]];
  return (
    <>
      <div className="row" style={{ marginBottom: 8 }}>
        <a onClick={() => go("employees")}>← Сотрудники</a>
      </div>
      <h1>{e.full_name || "Без имени"} <span className={`pill ${e.status === "active" ? "acc" : ""}`}>{EMP_ST[e.status]}</span>{" "}
        <span className={`pill ${e.dialog_mode === "auto" ? "" : "warn"}`}>{MODE[e.dialog_mode]}</span></h1>
      <div className="tabs">
        {tabs.map(([k, v]) => <a key={k} className={tab === k ? "active" : ""} onClick={() => go(`employees/${id}/${k}`)}>{v}</a>)}
      </div>
      {tab === "card" && <CardTab data={data} reload={reload} />}
      {tab === "invite" && <InviteTab data={data} reload={reload} />}
      {tab === "events" && <EventsTab data={data} reload={reload} />}
      {tab === "surveys" && <SurveysTab data={data} />}
      {tab === "chat" && <ChatTab data={data} reload={reload} />}
      {tab === "materials" && <PersonalMaterials data={data} reload={reload} />}
    </>
  );
}

function CardTab({ data, reload }: { data: any; reload: () => void }) {
  const e = data.employee;
  const [form, setForm] = useState<any>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const managers = useLoad<any[]>("/api/users").data?.filter((u) => u.role === "manager" && u.active) || [];
  const f = form || e;
  const set = (k: string, v: any) => setForm({ ...f, [k]: v });
  const save = () => {
    const body: any = {};
    for (const k of Object.keys(FIELD_LABEL)) if (form && form[k] !== e[k]) body[k] = form[k] === "" && ["start_date", "probation_end", "manager_user_id", "address_form"].includes(k) ? null : form[k];
    api.patch(`/api/employees/${e.id}`, body).then((r) => { setForm(null); setMsg(r.rescheduled ? `Сохранено, пересчитано событий: ${r.rescheduled}` : "Сохранено"); reload(); }).catch((x) => setErr(x.message));
  };
  const input = (k: string, type = "text") => (
    <label className="f">{FIELD_LABEL[k]}<input type={type} value={f[k] ?? ""} onChange={(x) => set(k, x.target.value)} /></label>
  );
  const pending = data.suggestions.filter((s: any) => s.status === "proposed");
  return (
    <div className="grid2">
      <div>
        <div className="panel">
          <h3>Подтвержденные данные карточки</h3>
          {err && <div className="err">{err}</div>}
          {msg && <div className="info">{msg}</div>}
          <div className="grid2">
            {input("full_name")}{input("short_name")}{input("phone")}{input("email")}
            {input("organization")}{input("department")}{input("position")}
            <label className="f">Формат работы<select value={f.work_format} onChange={(x) => set("work_format", x.target.value)}><option value="office">офис</option><option value="production">производство</option></select></label>
            {input("start_date", "date")}{input("probation_end", "date")}{input("timezone")}
            <label className="f">«Ты/вы»<select value={f.address_form || ""} onChange={(x) => set("address_form", x.target.value)}><option value="">ещё не выбрано</option><option value="ty">на «ты»</option><option value="vy">на «вы»</option></select></label>
            <label className="f">Руководитель<select value={f.manager_user_id || ""} onChange={(x) => set("manager_user_id", x.target.value ? Number(x.target.value) : null)}><option value="">не указан</option>{managers.map((m) => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label>
            <label className="f">Теги сценария<div className="row">{TAGS.map((t) => (
              <label key={t} className="small"><input type="checkbox" checked={(f.tags || []).includes(t)} onChange={(x) => set("tags", x.target.checked ? [...(f.tags || []), t] : (f.tags || []).filter((y: string) => y !== t))} /> {t}</label>))}</div></label>
            <label className="f">IT-доступы<label className="small"><input type="checkbox" checked={!!f.it_access_confirmed} onChange={(x) => set("it_access_confirmed", x.target.checked)} /> подтверждены IT/руководителем</label></label>
          </div>
          <p className="small muted">Окончание ИС по умолчанию: {fmtD(e.effective_probation_end)}. Ответственный HR назначается сервером.</p>
          <div className="row">
            <button disabled={!form} onClick={save}>Сохранить</button>
            {e.status === "draft" && <button className="sec" onClick={() => api.post(`/api/employees/${e.id}/activate`).then((r) => { setMsg(`Сопровождение запущено: событий ${r.created}, прошедших пропущено ${r.skipped_past}`); reload(); }).catch((x) => setErr(x.message))}>Запустить сопровождение</button>}
            {e.status === "active" && <button className="sec" onClick={() => api.post(`/api/employees/${e.id}/status/paused`).then(reload)}>Приостановить</button>}
            {e.status === "paused" && <button className="sec" onClick={() => api.post(`/api/employees/${e.id}/status/active`).then(reload)}>Возобновить</button>}
            {e.status !== "deactivated" && <button className="danger" onClick={() => confirm("Деактивировать карточку? Очередные сообщения отправляться не будут.") && api.post(`/api/employees/${e.id}/status/deactivated`).then(reload)}>Деактивировать</button>}
          </div>
          {e.status === "draft" && data.activation_missing.length > 0 && <div className="note">Для запуска сценария не хватает: {data.activation_missing.join(", ")}</div>}
        </div>
        {(e.intro_text || e.intro_photo) && (
          <div className="panel">
            <h3>Описание для представления коллегам (строка 34)</h3>
            <pre className="wrap">{e.intro_text || "—"}</pre>
            <p className="small">Фото: {e.intro_photo ? "получено" : "нет"} · {e.intro_approved_at ? `согласовано ${fmtDT(e.intro_approved_at)}` : "не согласовано"}</p>
            {!e.intro_approved_at && <button className="small" onClick={() => api.post(`/api/employees/${e.id}/intro/approve`).then(reload)}>Согласовать представление</button>}
          </div>
        )}
      </div>
      <div>
        <div className="panel">
          <h3>Документы сотрудника (приватно)</h3>
          <p className="small muted">PDF, DOCX, сканы JPG/PNG/TIFF до 20 МБ. Не попадают в базу знаний; доступны только HR.</p>
          <input type="file" onChange={(x) => {
            const file = x.target.files?.[0];
            if (!file) return;
            const fd = new FormData();
            fd.append("file", file);
            api.upload(`/api/employees/${e.id}/documents`, fd).then(() => { x.target.value = ""; reload(); }).catch((y) => setErr(y.message));
          }} />
          <table>
            <tbody>
              {data.documents.map((d: any) => (
                <tr key={d.id}>
                  <td><a href={`/api/documents/${d.id}/file`}>{d.filename}</a></td>
                  <td><span className={`pill ${d.status === "done" ? "ok" : d.status === "error" ? "bad" : "warn"}`}>{{ uploaded: "в очереди", processing: "распознаётся", done: "обработан", error: "ошибка" }[d.status as string]}</span> <span className="small muted">{d.error}</span></td>
                  <td className="small muted">{fmtDT(d.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="panel">
          <h3>Предложенные поля {pending.length > 0 && <span className="pill warn">{pending.length} на проверке</span>}</h3>
          <p className="small muted">Извлечены из документов. Становятся рабочими только после подтверждения.</p>
          <table>
            <tbody>
              {data.suggestions.map((s: any) => (
                <tr key={s.id}>
                  <td><b>{FIELD_LABEL[s.field] || s.field}</b><br />{s.value}</td>
                  <td className="small muted">стр. {s.page}: «{s.quote}» <br />({s.method === "llm" ? "модель" : "шаблон"})</td>
                  <td>{s.status === "proposed" ? (
                    <div className="row">
                      <button className="small" onClick={() => api.post(`/api/suggestions/${s.id}/accept`).then(reload)}>Принять</button>
                      <button className="small sec" onClick={() => api.post(`/api/suggestions/${s.id}/reject`).then(reload)}>Отклонить</button>
                    </div>) : <span className={`pill ${s.status === "accepted" ? "ok" : ""}`}>{s.status === "accepted" ? "принято" : "отклонено"}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!data.suggestions.length && <p className="muted small">Пока нет предложений.</p>}
        </div>
      </div>
    </div>
  );
}

function InviteTab({ data, reload }: { data: any; reload: () => void }) {
  const e = data.employee;
  const [link, setLink] = useState("");
  const [err, setErr] = useState("");
  return (
    <div className="panel">
      <h3>Приглашение в MAX</h3>
      <p>Статус привязки: {e.max_linked ? <span className="pill ok">привязан {fmtDT(e.linked_at)}</span> : <span className="pill">не привязан</span>}
        {" "}· преонбординг: <span className="pill">{{ none: "не начат", running: "идёт", done: "пройден" }[e.preonboarding_state as string]}</span></p>
      <p className="small muted">Ссылка персональная, действует 14 дней, привязывается к одному аккаунту MAX. Передайте её сотруднику сами. Новая ссылка отзывает прежнюю неиспользованную.</p>
      {err && <div className="err">{err}</div>}
      <button onClick={() => api.post(`/api/employees/${e.id}/invite`).then((r) => { setLink(r.link); reload(); }).catch((x) => setErr(x.message))}>Создать ссылку-приглашение</button>
      {link && <><p className="small">Скопируйте сейчас — повторно ссылка не показывается:</p><div className="linkbox">{link}</div></>}
      <h3>История приглашений</h3>
      <table><tbody>{data.invites.map((i: any) => (
        <tr key={i.id}><td>#{i.id}</td><td className="small">создано {fmtDT(i.created_at)}, до {fmtDT(i.expires_at)}</td>
          <td>{i.used_at ? <span className="pill ok">использовано {fmtDT(i.used_at)}</span> : i.revoked_at ? <span className="pill">отозвано</span> : <span className="pill acc">активно</span>}</td>
          <td>{!i.used_at && !i.revoked_at && <button className="small sec" onClick={() => api.post(`/api/invites/${i.id}/revoke`).then(reload)}>Отозвать</button>}</td></tr>))}
      </tbody></table>
    </div>
  );
}

function EventsTab({ data, reload }: { data: any; reload: () => void }) {
  const e = data.employee;
  const [survey, setSurvey] = useState("check2w");
  const [msg, setMsg] = useState("");
  const [edit, setEdit] = useState<{ id: number; v: string } | null>(null);
  const p = data.progress;
  return (
    <>
      <div className="grid4" style={{ marginBottom: 16 }}>
        <div className="kpi"><b>{p.events_done}/{p.events_total}</b><span>событий исполнено</span></div>
        <div className="kpi"><b>{p.waiting_reply}</b><span>ждут ответа сотрудника</span></div>
        <div className="kpi"><b>{p.surveys_completed}</b><span>опросов завершено</span></div>
        <div className="kpi"><b>{p.surveys_overdue}</b><span>опросов без ответа после 2 напоминаний</span></div>
      </div>
      <div className="panel row">
        <b>Тестовый опрос</b><span className="small muted">(не сдвигает расписание; уходит через 45 с)</span>
        <select value={survey} onChange={(x) => setSurvey(x.target.value)}>
          <option value="day3">3-й рабочий день</option><option value="check2w">Чек через 2 недели</option>
          <option value="two_months_grpp">2 месяца (GrPP/Luzhniki)</option><option value="two_months_gtp">2 месяца (GTP)</option>
          <option value="final">Итоги ИС</option>
        </select>
        <button className="sec" disabled={!e.max_linked} onClick={() => api.post(`/api/employees/${e.id}/test-survey`, { survey, seconds: 45 }).then((r) => { setMsg(`Поставлен на ${fmtDT(r.due_at)}`); reload(); }).catch((x) => setMsg(x.message))}>Поставить</button>
        {e.manager_user_id && <button className="sec" onClick={() => api.post(`/api/employees/${e.id}/meeting-request`).then(() => setMsg("Руководителю отправлено уведомление о встрече"))}>Попросить руководителя о встрече</button>}
        {msg && <span className="small">{msg}</span>}
      </div>
      <div className="panel">
        <table>
          <thead><tr><th>Когда</th><th>Событие</th><th>Строки XLSX</th><th>Состояние</th><th></th></tr></thead>
          <tbody>
            {data.events.map((ev: any) => (
              <tr key={ev.id}>
                <td className="small">{edit?.id === ev.id ? (
                  <span className="row"><input type="datetime-local" value={edit!.v} onChange={(x) => setEdit({ id: ev.id, v: x.target.value })} />
                    <button className="small" onClick={() => api.patch(`/api/events/${ev.id}`, { due_at: new Date(edit!.v).toISOString() }).then(() => { setEdit(null); reload(); })}>OK</button></span>
                ) : fmtDT(ev.due_at)}</td>
                <td>{ev.title}{ev.is_test && <span className="pill acc"> тест</span>}
                  {ev.warnings?.length > 0 && <div className="small" style={{ color: "var(--warn)" }}>{ev.warnings.join("; ")}</div>}
                  {ev.last_error && !ev.warnings?.length && <div className="small" style={{ color: "var(--warn)" }}>{ev.last_error}</div>}</td>
                <td className="small muted">{ev.source_rows}</td>
                <td>
                  <span className={`pill ${["sent", "done"].includes(ev.status) ? "ok" : ev.status === "failed" ? "bad" : ev.status === "waiting_reply" ? "warn" : ""}`}>{EV_ST[ev.status]}</span>
                  {ev.status === "scheduled" && ev.needs_permission && !ev.permitted_at && <> <span className="pill warn">ждёт разрешения HR</span></>}
                  {ev.permitted_at && <> <span className="pill ok">разрешено</span></>}
                  {ev.disabled && <> <span className="pill">отключено</span></>}
                  {ev.skip_reason && <div className="small muted">{{ late_start: "прошло до запуска/привязки", stale: "просрочено", paused_during_hr: "пришлось на разговор с HR" }[ev.skip_reason as string] || ev.skip_reason}</div>}
                </td>
                <td className="row">
                  {ev.status === "scheduled" && ev.needs_permission && !ev.permitted_at && <button className="small" onClick={() => api.post(`/api/events/${ev.id}/permit`).then(reload)}>Разрешить</button>}
                  {ev.status === "scheduled" && <button className="small sec" onClick={() => setEdit({ id: ev.id, v: ev.due_at.slice(0, 16) })}>Время</button>}
                  {ev.status === "scheduled" && <button className="small sec" onClick={() => api.patch(`/api/events/${ev.id}`, { disabled: !ev.disabled }).then(reload)}>{ev.disabled ? "Включить" : "Отключить"}</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!data.events.length && <p className="muted">События появятся после запуска сопровождения (нужны дата выхода, организация, тег, часовой пояс).</p>}
      </div>
    </>
  );
}

function SurveysTab({ data }: { data: any }) {
  const byRun: Record<number, any[]> = {};
  data.answers.forEach((a: any) => (byRun[a.run_id] = [...(byRun[a.run_id] || []), a]));
  const an: Record<number, any> = {};
  data.analyses.filter((x: any) => x.source_type === "survey_answer").forEach((x: any) => (an[x.source_id] = x));
  return (
    <>
      {data.surveys.map((r: any) => (
        <div className="panel" key={r.id}>
          <h3>{r.title} <span className={`pill ${r.status === "completed" ? "ok" : r.status === "active" ? "warn" : ""}`}>{{ queued: "в очереди", active: "идёт", completed: "завершён", expired: "истёк", cancelled: "отменён" }[r.status as string]}</span>
            {r.survey_key.includes("#test") && <span className="pill acc"> тест</span>}</h3>
          <p className="small muted">Начат: {fmtDT(r.started_at)} · напоминаний: {r.reminders_sent}/2 · завершён: {fmtDT(r.completed_at)}</p>
          <table><tbody>
            {(byRun[r.id] || []).map((a: any) => (
              <tr key={a.id}>
                <td style={{ width: "45%" }}>{a.question_text}</td>
                <td><b>{a.score ?? a.option_labels.join(", ")}</b>{a.text && <div>«{a.text}»</div>}{a.comment && <div className="small">Комментарий: «{a.comment}»</div>}</td>
                <td className="small">{an[a.id] ? <><span className={`pill ${an[a.id].sentiment === "negative" ? "bad" : an[a.id].sentiment === "positive" ? "ok" : ""}`}>{SENT[an[a.id].sentiment]}</span> {an[a.id].priority && <span className="pill">{PRIO[an[a.id].priority]}</span>}<div className="muted">{an[a.id].summary}</div></> : null}</td>
              </tr>))}
          </tbody></table>
        </div>
      ))}
      {!data.surveys.length && <p className="muted">Опросов пока не было.</p>}
    </>
  );
}

export function ChatTab({ data, reload }: { data: any; reload: () => void }) {
  const e = data.employee;
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const open = data.cases.filter((c: any) => c.status !== "resolved");
  return (
    <div className="grid2">
      <div className="panel">
        <h3>Переписка в MAX</h3>
        <Chat messages={data.messages} />
        {e.dialog_mode === "hr_active" ? (
          <>
            {err && <div className="err">{err}</div>}
            <textarea placeholder="Сообщение уйдёт в тот же чат от имени Корсика (автор — HR сохраняется внутри)" value={text} onChange={(x) => setText(x.target.value)} />
            <div className="row">
              <button disabled={!text.trim()} onClick={() => api.post(`/api/employees/${e.id}/reply`, { text }).then(() => { setText(""); reload(); }).catch((x) => setErr(x.message))}>Отправить</button>
              <button className="sec" onClick={() => api.post(`/api/employees/${e.id}/release`, { resolve: true }).then(reload)}>Завершить участие и вернуть Корсику</button>
              <button className="sec" onClick={() => api.post(`/api/employees/${e.id}/release`, { resolve: false }).then(reload)}>Вернуть Корсику, обращение оставить открытым</button>
            </div>
          </>
        ) : <p className="small muted">Чтобы ответить, возьмите обращение в работу (справа или в разделе «Обращения»).</p>}
      </div>
      <div className="panel">
        <h3>Обращения сотрудника</h3>
        <CaseList cases={data.cases} reload={reload} />
        {!open.length && <p className="small muted">Открытых обращений нет.</p>}
      </div>
    </div>
  );
}

export function CaseList({ cases, reload }: { cases: any[]; reload: () => void }) {
  const [, go] = useHash();
  return (
    <table><tbody>{cases.map((c) => (
      <tr key={c.id}>
        <td><a onClick={() => go(`cases/${c.id}`)}>#{c.id}</a> <span className={`pill ${c.priority === "high" ? "bad" : c.priority === "medium" ? "warn" : ""}`}>{PRIO[c.priority]}</span> <span className="pill">{CASE_ST[c.status]}</span>
          <div className="small">«{c.question.slice(0, 140)}»</div></td>
        <td>{c.status === "new" && <button className="small" onClick={() => api.post(`/api/cases/${c.id}/take`).then(reload)}>Взять в работу</button>}</td>
      </tr>))}
    </tbody></table>
  );
}

export function Chat({ messages }: { messages: any[] }) {
  return (
    <div className="chat">
      {messages.map((m) => (
        <div key={m.id} className={`msg ${m.direction === "in" ? "in" : m.author_kind === "hr" ? "hr" : "out"}`}>
          {m.text || <i className="muted">[вложение]</i>}
          <div className="meta">{AUTHOR[m.author_kind] || m.author_kind}{m.author ? ` (${m.author})` : ""} · {fmtDT(m.created_at)}
            {m.direction === "out" && m.delivery && <> · <span className={m.delivery === "sent" ? "" : "pill warn"}>{DELIV[m.delivery]}</span></>}
            {m.case_id && <> · обращение #{m.case_id}</>}</div>
        </div>
      ))}
      {!messages.length && <p className="muted">Сообщений пока нет.</p>}
    </div>
  );
}

function PersonalMaterials({ data, reload }: { data: any; reload: () => void }) {
  const e = data.employee;
  const [err, setErr] = useState("");
  return (
    <div className="panel">
      <h3>Персональный адаптационный план (строки 35–36)</h3>
      <p className="small muted">Отдельный материал руководителя. Бот доставит его в 3-й рабочий день после разрешения HR. Содержание не генерируется.</p>
      {err && <div className="err">{err}</div>}
      <input type="file" onChange={(x) => {
        const file = x.target.files?.[0];
        if (!file) return;
        const fd = new FormData();
        fd.append("key", "adaptation_plan");
        fd.append("kind", "file");
        fd.append("employee_id", String(e.id));
        fd.append("title", "Адаптационный план");
        fd.append("file", file);
        api.upload("/api/materials", fd).then(reload).catch((y) => setErr(y.message));
      }} />
      <table><tbody>{data.materials.map((m: any) => (
        <tr key={m.id}><td>{m.title}</td><td>{m.filename || m.url}</td><td className="small muted">{fmtDT(m.created_at)}</td>
          <td><button className="small danger" onClick={() => api.del(`/api/materials/${m.id}`).then(reload)}>Убрать</button></td></tr>))}
      </tbody></table>
      {!data.materials.length && <p className="note">План не прикреплён — событие «Адаптационный план» будет ждать.</p>}
    </div>
  );
}
