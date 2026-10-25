import React, { useState } from "react";
import { api, CASE_ST, fmtDT, MODE, PRIO, SENT } from "../api";
import { useHash, useLoad } from "../main";
import { Chat } from "./Employees";

const REASON: Record<string, string> = {
  no_answer: "нет ответа в материалах", personal_data: "персональные сведения", feedback_signal: "сигнал обратной связи",
  human_request: "просит личного общения", service_error: "сбой автоматики", material_missing: "нет материала",
};

export function Cases() {
  const [status, setStatus] = useState("");
  const { data, error, reload } = useLoad<any[]>(`/api/cases?status=${status}`, [status], 5000);
  const [, go] = useHash();
  return (
    <>
      <h1>Обращения</h1>
      <p className="muted small">Единая очередь единственного HR. Сотрудник остаётся в своём чате с Корсиком; взяв обращение, вы отвечаете в тот же чат.</p>
      <div className="tabs">
        {[["", "Все"], ["new", "Новые"], ["in_progress", "В работе"], ["resolved", "Решённые"]].map(([k, v]) => (
          <a key={k} className={status === k ? "active" : ""} onClick={() => setStatus(k)}>{v}</a>))}
      </div>
      {error && <div className="err">{error}</div>}
      <div className="panel">
        <table>
          <thead><tr><th>#</th><th>Приоритет</th><th>Сотрудник</th><th>Вопрос / причина</th><th>Состояние</th><th>Диалог</th><th>Создано</th></tr></thead>
          <tbody>{(data || []).map((c) => (
            <tr key={c.id} className="click" onClick={() => go(`cases/${c.id}`)}>
              <td>{c.id}</td>
              <td><span className={`pill ${c.priority === "high" ? "bad" : c.priority === "medium" ? "warn" : ""}`}>{PRIO[c.priority]}</span>{c.needs_review && <div className="small muted">требует просмотра</div>}</td>
              <td>{c.full_name}<div className="small muted">{c.department}</div></td>
              <td>«{c.question.slice(0, 160)}»<div className="small muted">{REASON[c.reason]}</div></td>
              <td><span className="pill">{CASE_ST[c.status]}</span></td>
              <td><span className="pill">{MODE[c.dialog_mode]}</span></td>
              <td className="small">{fmtDT(c.created_at)}</td>
            </tr>))}
          </tbody>
        </table>
        {data && !data.length && <p className="muted">Обращений нет.</p>}
      </div>
    </>
  );
}

export function CaseView({ id }: { id: number }) {
  const { data, error, reload } = useLoad<any>(`/api/cases/${id}`, [id], 4000);
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const [, go] = useHash();
  if (error) return <div className="err">{error}</div>;
  if (!data) return null;
  const c = data.case, e = data.employee;
  const mine = e.dialog_mode === "hr_active";
  return (
    <>
      <a onClick={() => go("cases")}>← Обращения</a>
      <h1>Обращение #{c.id} <span className={`pill ${c.priority === "high" ? "bad" : c.priority === "medium" ? "warn" : ""}`}>{PRIO[c.priority]}</span> <span className="pill">{CASE_ST[c.status]}</span></h1>
      <div className="grid2">
        <div className="panel">
          <h3>Переписка (релевантная история)</h3>
          <Chat messages={data.history} />
          {err && <div className="err">{err}</div>}
          {mine ? (
            <>
              <textarea placeholder="Ответ уйдёт в тот же MAX-чат сотрудника. Текст отправляется без переписывания." value={text} onChange={(x) => setText(x.target.value)} />
              <div className="row">
                <button disabled={!text.trim()} onClick={() => api.post(`/api/employees/${e.id}/reply`, { text }).then(() => { setText(""); reload(); }).catch((x) => setErr(x.message))}>Отправить</button>
                <button className="sec" onClick={() => api.post(`/api/employees/${e.id}/release`, { resolve: true }).then(reload).catch((x) => setErr(x.message))}>Решено — вернуть диалог Корсику</button>
                <button className="sec" onClick={() => api.post(`/api/employees/${e.id}/release`, { resolve: false }).then(reload).catch((x) => setErr(x.message))}>Вернуть Корсику, не закрывая</button>
              </div>
              <p className="small muted">Пока диалог у вас, Корсик молчит у этого сотрудника: не отвечает сам, не шлёт опросы и рассылки.</p>
            </>
          ) : (
            <div className="row">
              {c.status !== "resolved" && <button onClick={() => api.post(`/api/cases/${c.id}/take`).then(reload).catch((x) => setErr(x.message))}>Взять диалог в работу</button>}
              {c.status !== "resolved" && <button className="sec" onClick={() => api.post(`/api/cases/${c.id}/resolve`).then(reload).catch((x) => setErr(x.message))}>Закрыть без ответа</button>}
            </div>
          )}
        </div>
        <div>
          <div className="panel">
            <h3>Суть</h3>
            <p>«{c.question}»</p>
            <p className="small">Причина: <b>{data.reason_label}</b> · тема: <b>{data.topic_label}</b>{c.needs_review && " · требует просмотра (анализ неполный)"}</p>
            <p className="small muted">Создано {fmtDT(c.created_at)}{c.taken_at && ` · взято ${fmtDT(c.taken_at)}`}{c.resolved_at && ` · решено ${fmtDT(c.resolved_at)}`}</p>
            {c.context && Object.keys(c.context).length > 0 && <pre className="wrap small muted">{JSON.stringify(c.context, null, 1)}</pre>}
          </div>
          <div className="panel">
            <h3>Сотрудник</h3>
            <p><a onClick={() => go(`employees/${e.id}`)}>{e.full_name}</a> · {e.position} · {e.department}</p>
            <p className="small">Этап адаптации: <b>{data.stage}</b> · выход {e.start_date || "—"} · теги {(e.tags || []).join(", ") || "—"} · обращение на «{e.address_form === "vy" ? "вы" : "ты"}»</p>
            <p className="small">Режим диалога: <b>{MODE[e.dialog_mode]}</b></p>
          </div>
          <div className="panel">
            <h3>Последний анализ</h3>
            {data.analyses.map((a: any) => (
              <div key={a.id} className="small" style={{ marginBottom: 6 }}>
                <span className="pill">{SENT[a.sentiment]}</span> <span className="pill">{PRIO[a.priority] || "—"}</span> {a.status === "rule_only" && <span className="pill warn">только правила (модель недоступна)</span>}
                <div>{a.summary}</div>
              </div>))}
            {!data.analyses.length && <p className="muted small">Нет данных анализа.</p>}
          </div>
        </div>
      </div>
    </>
  );
}
