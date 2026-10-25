import React, { useState } from "react";
import { api, fmtDT } from "../api";
import { useLoad } from "../main";

export function Materials() {
  const { data, error, reload } = useLoad<any>("/api/scenario");
  const [err, setErr] = useState("");
  const [form, setForm] = useState<{ key: string; kind: string; tags: string; url: string; body: string; file: File | null }>({ key: "", kind: "file", tags: "", url: "", body: "", file: null });
  if (error) return <div className="err">{error}</div>;
  if (!data) return null;
  const send = () => {
    const fd = new FormData();
    fd.append("key", form.key); fd.append("kind", form.kind); fd.append("tags", form.tags);
    if (form.url) fd.append("url", form.url);
    if (form.body) fd.append("body", form.body);
    if (form.file) fd.append("file", form.file);
    api.upload("/api/materials", fd).then(() => { setForm({ ...form, key: "", file: null, url: "", body: "" }); reload(); }).catch((x) => setErr(x.message));
  };
  const anchor = (a: any) => ({
    workday: `${a.n}-й рабочий день`, before_start: `за ${a.days} дн. до выхода`, days: `через ${a.days} дн.`,
    months: `через ${a.months} мес.${a.days ? ` ${a.days} дн.` : ""}`, probation_end: "последний день ИС",
  } as any)[a.type] + (a.time ? ` в ${a.time}` : "");
  return (
    <>
      <h1>Сценарий и материалы</h1>
      <p className="small muted">Сценарий собран один раз из «{data.source.filename}», лист {data.source.sheet} ({data.code}). Каждое событие хранит строки Excel.</p>
      <div className="panel">
        <h3>Материалы сценария</h3>
        <p className="small muted">В XLSX есть только названия картинок и указания «приложить». Пока слот пуст, бот не отправляет файл и показывает пропуск в событии.</p>
        {err && <div className="err">{err}</div>}
        <table>
          <thead><tr><th>Слот</th><th>Строки</th><th>Загружено</th></tr></thead>
          <tbody>{data.materials.map((m: any) => (
            <tr key={m.key}>
              <td><b>{m.title}</b><div className="small muted">{m.key}{m.personal && " · персональный, загружается в карточке сотрудника"}</div></td>
              <td className="small">{m.rows}</td>
              <td>{m.uploaded.length ? m.uploaded.map((u: any) => (
                <div key={u.id} className="small row"><span className="pill ok">{u.kind}</span> {u.filename || u.url || "текст"} {u.tags.length ? `(${u.tags.join(", ")})` : "(все теги)"}
                  <a onClick={() => api.del(`/api/materials/${u.id}`).then(reload)}>убрать</a></div>))
                : m.personal ? <span className="small muted">—</span> : <span className="pill warn">нет</span>}</td>
            </tr>))}
          </tbody>
        </table>
        <h3>Добавить материал</h3>
        <div className="grid3">
          <label className="f">Слот<select value={form.key} onChange={(x) => setForm({ ...form, key: x.target.value })}><option value="">—</option>{data.materials.filter((m: any) => !m.personal).map((m: any) => <option key={m.key} value={m.key}>{m.title}</option>)}</select></label>
          <label className="f">Тип<select value={form.kind} onChange={(x) => setForm({ ...form, kind: x.target.value })}><option value="file">файл / изображение</option><option value="link">ссылка</option><option value="text">текст</option></select></label>
          <label className="f">Теги (пусто = для всех; GrPP, GTP, Luzhniki)<input value={form.tags} onChange={(x) => setForm({ ...form, tags: x.target.value })} /></label>
          {form.kind === "file" && <label className="f">Файл<input type="file" onChange={(x) => setForm({ ...form, file: x.target.files?.[0] || null })} /></label>}
          {form.kind === "link" && <label className="f">Ссылка<input value={form.url} onChange={(x) => setForm({ ...form, url: x.target.value })} /></label>}
          {form.kind === "text" && <label className="f">Текст<textarea value={form.body} onChange={(x) => setForm({ ...form, body: x.target.value })} /></label>}
        </div>
        <br /><button disabled={!form.key} onClick={send}>Добавить</button>
      </div>
      <div className="grid2">
        <div className="panel">
          <h3>Таймлайн сценария</h3>
          <table><tbody>{data.steps.map((s: any) => (
            <tr key={s.key}><td className="small">{anchor(s.anchor)}</td><td>{s.title}<div className="small muted">строки {s.rows}{s.tags && ` · ${s.tags.join("/")}`}{s.work_format && ` · ${s.work_format === "office" ? "офис" : s.work_format}`}</div>
              {s.needs_permission && <div className="small" style={{ color: "var(--warn)" }}>{s.permission_note}</div>}</td></tr>))}
          </tbody></table>
        </div>
        <div className="panel">
          <h3>Неоднозначности исходного XLSX и принятые рабочие значения</h3>
          <ol className="small">{data.ambiguities.map((a: string, i: number) => <li key={i} style={{ marginBottom: 6 }}>{a}</li>)}</ol>
        </div>
      </div>
    </>
  );
}
