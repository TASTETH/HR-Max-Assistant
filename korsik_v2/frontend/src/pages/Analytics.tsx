import React, { useState } from "react";
import { api, fmtD, fmtDT, PRIO, SENT } from "../api";
import type { User } from "../main";
import { useLoad } from "../main";

function Bars({ items }: { items: { label: string; value: number; sub?: string }[] }) {
  const max = Math.max(1, ...items.map((i) => i.value));
  return (
    <div>{items.map((i) => (
      <div key={i.label} className="row" style={{ marginBottom: 6, flexWrap: "nowrap" }}>
        <span style={{ width: 190 }} className="small">{i.label}</span>
        <div className="barwrap"><div className="bar" style={{ width: `${(100 * i.value) / max}%` }} /></div>
        <span style={{ width: 110 }} className="small">{i.value}{i.sub ? ` · ${i.sub}` : ""}</span>
      </div>))}
      {!items.length && <p className="muted small">Нет данных за период.</p>}
    </div>
  );
}

export function Analytics() {
  const [days, setDays] = useState(30);
  const [dep, setDep] = useState("");
  const [tag, setTag] = useState("");
  const { data, error } = useLoad<any>(`/api/analytics?days=${days}&department=${encodeURIComponent(dep)}&tag=${tag}`, [], 30000);
  if (error) return <div className="err">{error}</div>;
  if (!data) return null;
  const c = data.current, p = data.previous;
  const delta = (a: number, b: number) => (b === undefined ? "" : a > b ? ` ▲ было ${b}` : a < b ? ` ▼ было ${b}` : " = как раньше");
  const sentItems = ["positive", "neutral", "negative", "unknown"].filter((k) => c.sentiment[k]).map((k) => ({ label: SENT[k], value: c.sentiment[k].items, sub: `${c.sentiment[k].employees} чел.` }));
  const open = c.cases.filter((x: any) => x.status !== "resolved");
  return (
    <>
      <h1>Аналитика адаптации</h1>
      <div className="panel row">
        <label className="f">Период<select value={days} onChange={(x) => setDays(Number(x.target.value))}><option value={7}>7 дней</option><option value={30}>30 дней</option><option value={90}>90 дней</option></select></label>
        <label className="f">Подразделение<select value={dep} onChange={(x) => setDep(x.target.value)}><option value="">все</option>{data.departments.map((d: string) => <option key={d}>{d}</option>)}</select></label>
        <label className="f">Тег<select value={tag} onChange={(x) => setTag(x.target.value)}><option value="">все</option><option>GrPP</option><option>GTP</option><option>Luzhniki</option></select></label>
        <span className="small muted">Сравнение — с предыдущим таким же периодом. Сообщения, ответы и сотрудники считаются отдельно.</span>
      </div>
      <div className="grid4" style={{ marginBottom: 16 }}>
        <div className="kpi"><b>{c.coverage.in_adaptation}</b><span>в адаптации · всего {c.coverage.employees}, MAX {c.coverage.linked_max}</span></div>
        <div className="kpi"><b>{c.coverage.respondents}</b><span>сотрудников ответили/писали{delta(c.coverage.respondents, p.coverage.respondents)}</span></div>
        <div className="kpi"><b>{c.survey_answers.answers}</b><span>ответов на опросы · {c.survey_answers.employees} чел.</span></div>
        <div className="kpi"><b>{c.messages.messages}</b><span>сообщений сотрудников · {c.messages.employees} чел.</span></div>
        <div className="kpi"><b>{c.surveys.completed}</b><span>опросов завершено{delta(c.surveys.completed, p.surveys.completed)}</span></div>
        <div className="kpi"><b>{c.surveys.open}</b><span>опросов ждут ответа</span></div>
        <div className="kpi"><b>{c.surveys.overdue}</b><span>без ответа после 2 напоминаний</span></div>
        <div className="kpi"><b>{open.reduce((s: number, x: any) => s + x.n, 0)}</b><span>открытых обращений</span></div>
      </div>
      <div className="grid2">
        <div className="panel"><h3>Этапы адаптации</h3><Bars items={Object.entries(c.stages).map(([k, v]) => ({ label: k, value: v as number }))} /></div>
        <div className="panel"><h3>Тональность (проанализированные ответы и сообщения)</h3><Bars items={sentItems} />
          <p className="small muted">«Не определено» — низкая оценка без комментария или недоступная модель; не превращается в негатив.</p></div>
        <div className="panel"><h3>Темы трудностей</h3><Bars items={c.topics.map((t: any) => ({ label: t.label, value: t.employees, sub: `${t.mentions} упом.` }))} /></div>
        <div className="panel"><h3>Динамика по неделям</h3>
          <table><thead><tr><th>Неделя с</th><th>позитив</th><th>нейтр.</th><th>негатив</th><th>не опр.</th></tr></thead>
            <tbody>{data.weekly.map((w: any) => <tr key={w.week_start}><td>{fmtD(w.week_start)}</td><td>{w.positive || 0}</td><td>{w.neutral || 0}</td><td>{w.negative || 0}</td><td>{w.unknown || 0}</td></tr>)}</tbody></table>
        </div>
      </div>
      <div className="panel"><h3>Высокоприоритетные сигналы</h3>
        {c.high_signals.map((s: any, i: number) => <div key={i} className="small" style={{ marginBottom: 6 }}><b>{s.employee}</b> ({s.department || "—"}) · {fmtDT(s.at)}<div>{s.summary}</div></div>)}
        {!c.high_signals.length && <p className="muted small">Нет.</p>}
      </div>
    </>
  );
}

export function Digests({ user }: { user: User }) {
  const { data, error, reload } = useLoad<any[]>("/api/digests");
  const [msg, setMsg] = useState("");
  return (
    <>
      <h1>Дайджест «Настроения новичков»</h1>
      <p className="muted small">Формируется автоматически еженедельно (по умолчанию понедельник 09:00 МСК) за прошлую неделю; доставляется в MAX, если ваш аккаунт MAX привязан. Числа считаются кодом из БД.</p>
      {error && <div className="err">{error}</div>}
      <div className="row"><button className="sec" onClick={() => api.post("/api/digests/run").then((r) => { setMsg(r.created ? "Сформирован" : "За прошлую неделю уже есть"); reload(); })}>Сформировать за прошлую неделю</button><span className="small">{msg}</span></div>
      <br />
      {(data || []).map((d) => (
        <div className="panel" key={d.id}>
          <h3>{fmtD(d.period_start)} — {fmtD(d.period_end)}</h3>
          <pre className="wrap">{d.text}</pre>
          {d.data.topics.length > 0 && <><h3>Темы</h3><Bars items={d.data.topics.map((t: any) => ({ label: t.label, value: t.employees, sub: `${t.mentions} упом.` }))} /></>}
          <p className="small muted">Создан {fmtDT(d.created_at)}</p>
        </div>))}
      {data && !data.length && <p className="muted">Дайджестов пока нет.</p>}
    </>
  );
}
