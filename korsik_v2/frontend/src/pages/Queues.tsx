import React from "react";
import { api, DELIV, fmtDT, PRIO, SENT } from "../api";
import { useHash, useLoad } from "../main";

export function Attention() {
  const { data, error } = useLoad<any[]>("/api/attention", [], 15000);
  const [, go] = useHash();
  return (
    <>
      <h1>Сигналы опросов</h1>
      <p className="muted small">Оценки ниже 8, ответы «нет», проблемные варианты и комментарии с приоритетом. Низкая оценка без комментария — «не определено», а не эмоция.</p>
      {error && <div className="err">{error}</div>}
      <div className="panel">
        <table>
          <thead><tr><th>Сотрудник</th><th>Вопрос</th><th>Ответ</th><th>Анализ</th><th>Когда</th></tr></thead>
          <tbody>{(data || []).map((a) => (
            <tr key={a.id} className="click" onClick={() => go(`employees/${a.employee_id}/surveys`)}>
              <td>{a.full_name}</td>
              <td className="small">{a.question_text}</td>
              <td><b>{a.score ?? a.option_labels.join(", ")}</b>{(a.comment || a.text) && <div className="small">«{a.comment || a.text}»</div>}</td>
              <td className="small">{a.sentiment ? <><span className="pill">{SENT[a.sentiment]}</span> {a.priority && <span className={`pill ${a.priority === "high" ? "bad" : a.priority === "medium" ? "warn" : ""}`}>{PRIO[a.priority]}</span>}<div className="muted">{a.summary}</div></> : "—"}</td>
              <td className="small">{fmtDT(a.updated_at)}</td>
            </tr>))}
          </tbody>
        </table>
        {data && !data.length && <p className="muted">Сигналов нет.</p>}
      </div>
    </>
  );
}

export function Delivery() {
  const { data, error, reload } = useLoad<any[]>("/api/delivery", [], 10000);
  return (
    <>
      <h1>Доставка сообщений</h1>
      <p className="muted small">«Неизвестно» — запрос ушёл в MAX, но подтверждения нет (тайм-аут): сообщение могло дойти. Автоматически не переотправляется; решение — за вами.</p>
      {error && <div className="err">{error}</div>}
      <div className="panel">
        <table>
          <thead><tr><th>#</th><th>Кому</th><th>Текст</th><th>Состояние</th><th></th></tr></thead>
          <tbody>{(data || []).map((o) => (
            <tr key={o.id}>
              <td>{o.id}</td><td>{o.full_name || "служебное"}</td><td className="small">{o.text}</td>
              <td><span className={`pill ${o.status === "failed" ? "bad" : "warn"}`}>{DELIV[o.status]}</span><div className="small muted">{o.last_error} · попыток {o.attempts}</div></td>
              <td className="row">
                <button className="small" onClick={() => api.post(`/api/delivery/${o.id}/retry`).then(reload)}>Отправить повторно</button>
                <button className="small sec" onClick={() => api.post(`/api/delivery/${o.id}/dismiss`).then(reload)}>Скрыть</button>
              </td>
            </tr>))}
          </tbody>
        </table>
        {data && !data.length && <p className="muted">Проблем доставки нет.</p>}
      </div>
    </>
  );
}

export function Notifications() {
  const { data, reload } = useLoad<any[]>("/api/notifications", [], 8000);
  const [, go] = useHash();
  const ch: Record<string, string> = { pending: "ожидает", queued: "в очереди MAX", sent: "доставлено в MAX", failed: "ошибка MAX", no_channel: "только в кабинете (MAX не привязан)" };
  return (
    <>
      <h1>Уведомления</h1>
      <div className="panel">
        <table><tbody>{(data || []).map((n) => (
          <tr key={n.id} style={{ fontWeight: n.read_at ? 400 : 600 }}>
            <td>{n.title}<div className="small" style={{ fontWeight: 400, whiteSpace: "pre-wrap" }}>{n.body}</div></td>
            <td className="small">{fmtDT(n.created_at)}<div className="muted">{ch[n.channel_status]}</div></td>
            <td className="row">
              {n.case_id && <button className="small sec" onClick={() => go(`cases/${n.case_id}`)}>Открыть</button>}
              {!n.read_at && <button className="small sec" onClick={() => api.post(`/api/notifications/${n.id}/read`).then(reload)}>Прочитано</button>}
            </td>
          </tr>))}
        </tbody></table>
        {data && !data.length && <p className="muted">Уведомлений нет.</p>}
      </div>
    </>
  );
}
