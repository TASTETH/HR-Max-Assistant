import React, { useState } from "react";
import { api, fmtD, fmtDT } from "../api";
import { useLoad } from "../main";

const DST: Record<string, [string, string]> = {
  uploaded: ["в очереди", "warn"], processing: ["извлечение/OCR", "warn"], extracted: ["готов к публикации", "acc"],
  error: ["ошибка", "bad"], published: ["опубликован", "ok"], withdrawn: ["снят с публикации", ""], superseded: ["заменён новой версией", ""],
};

export function Knowledge() {
  const { data, error, reload } = useLoad<any>("/api/corpus", [], 5000);
  const [err, setErr] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [orgs, setOrgs] = useState("ООО «Группа ПОЛИПЛАСТИК»");
  const [tags, setTags] = useState("");
  const [ver, setVer] = useState("1");
  const [eff, setEff] = useState("");
  const [repl, setRepl] = useState("");
  const [preview, setPreview] = useState<any>(null);
  const [qtext, setQ] = useState("");
  const [hits, setHits] = useState<any>(null);
  const upload = () => {
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file); fd.append("title", title || file.name.replace(/\.[^.]+$/, ""));
    fd.append("organizations", orgs); fd.append("tags", tags); fd.append("version_label", ver);
    if (eff) fd.append("effective_from", eff);
    if (repl) fd.append("replaces_id", repl);
    api.upload("/api/corpus", fd).then(() => { setFile(null); setTitle(""); reload(); }).catch((x) => setErr(x.message));
  };
  const published = (data?.documents || []).filter((d: any) => d.status === "published");
  return (
    <>
      <h1>База знаний (регламенты)</h1>
      <p className="muted small">Загрузка → извлечение/OCR в фоне → проверка текста → публикация новой версии корпуса. Пока новая версия не собрана, бот отвечает по прежней; ошибка сборки рабочую базу не трогает. Снятый документ перестаёт использоваться сразу.</p>
      {(error || err) && <div className="err">{error || err}</div>}
      <div className="panel">
        <h3>Загрузить регламент</h3>
        <div className="grid3">
          <label className="f">Файл (PDF, PPTX, DOCX, TXT, скан)<input type="file" onChange={(x) => setFile(x.target.files?.[0] || null)} /></label>
          <label className="f">Название<input value={title} onChange={(x) => setTitle(x.target.value)} placeholder="по имени файла" /></label>
          <label className="f">Применимость: организации через «;» или * для всей группы<input value={orgs} onChange={(x) => setOrgs(x.target.value)} /></label>
          <label className="f">Теги площадок (необязательно, через запятую)<input value={tags} onChange={(x) => setTags(x.target.value)} placeholder="GTP" /></label>
          <label className="f">Версия<input value={ver} onChange={(x) => setVer(x.target.value)} /></label>
          <label className="f">Действует с<input type="date" value={eff} onChange={(x) => setEff(x.target.value)} /></label>
          <label className="f">Заменяет документ<select value={repl} onChange={(x) => setRepl(x.target.value)}><option value="">— новый документ —</option>{published.map((d: any) => <option key={d.id} value={d.id}>{d.title} (в. {d.version_label})</option>)}</select></label>
        </div>
        <br /><button disabled={!file} onClick={upload}>Загрузить и подготовить</button>
      </div>
      <div className="panel">
        <h3>Документы</h3>
        <table>
          <thead><tr><th>Документ</th><th>Применимость</th><th>Версия</th><th>Состояние</th><th></th></tr></thead>
          <tbody>{(data?.documents || []).map((d: any) => (
            <tr key={d.id}>
              <td><b>{d.title}</b><div className="small muted">{d.filename} · {d.pages_count} стр./слайдов</div></td>
              <td className="small">{d.organizations.join("; ")}{d.tags.length > 0 && <div>теги: {d.tags.join(", ")}</div>}</td>
              <td className="small">{d.version_label}{d.effective_from && <div>с {fmtD(d.effective_from)}</div>}</td>
              <td><span className={`pill ${DST[d.status][1]}`}>{DST[d.status][0]}</span>{d.error && <div className="small" style={{ color: "var(--warn)" }}>{d.error}</div>}{d.published_at && <div className="small muted">{fmtDT(d.published_at)}</div>}</td>
              <td className="row">
                {d.pages_count > 0 && <button className="small sec" onClick={() => api.get(`/api/corpus/${d.id}`).then(setPreview)}>Текст</button>}
                {["extracted", "error"].includes(d.status) && d.pages_count > 0 && <button className="small" onClick={() => api.post(`/api/corpus/${d.id}/publish`).then(reload).catch((x) => setErr(x.message))}>Опубликовать</button>}
                {d.status === "published" && <button className="small danger" onClick={() => confirm("Снять документ с публикации? Бот сразу перестанет на него ссылаться.") && api.post(`/api/corpus/${d.id}/withdraw`).then(reload)}>Снять</button>}
              </td>
            </tr>))}
          </tbody>
        </table>
      </div>
      {preview && (
        <div className="panel">
          <div className="row"><h3 style={{ flex: 1 }}>Извлечённый текст: {preview.title}</h3><button className="small sec" onClick={() => setPreview(null)}>Закрыть</button></div>
          {(preview.pages || []).map((p: any) => (
            <div key={p.number} style={{ borderTop: "1px solid var(--line)", padding: "8px 0" }}>
              <div className="small muted">стр./слайд {p.number} · {p.method} {p.notes}</div>
              <pre className="wrap small">{p.text || "— пусто —"}</pre>
            </div>))}
        </div>
      )}
      <div className="grid2">
        <div className="panel">
          <h3>Проверка поиска (без LLM)</h3>
          <div className="row"><input style={{ flex: 1 }} value={qtext} onChange={(x) => setQ(x.target.value)} placeholder="Можно ли дарить деньги контрагенту?" />
            <button className="sec" onClick={() => api.get(`/api/corpus/search?q=${encodeURIComponent(qtext)}`).then(setHits).catch((x) => setErr(x.message))}>Найти</button></div>
          {hits && <>
            <p className="small muted">Версия корпуса {hits.version_id} · смысловой поиск: {hits.dense_used ? "да" : `нет ${hits.dense_error || ""}`}</p>
            {hits.hits.map((h: any, i: number) => <div key={i} className="small" style={{ marginBottom: 8 }}><b>«{h.title}», {h.unit} {h.page}</b><div>{h.text.slice(0, 300)}…</div></div>)}
          </>}
        </div>
        <div className="panel">
          <h3>Версии корпуса</h3>
          <table><tbody>{(data?.versions || []).map((v: any) => (
            <tr key={v.id}><td>#{v.id}</td><td><span className={`pill ${v.status === "active" ? "ok" : v.status === "failed" ? "bad" : ""}`}>{v.status}</span></td>
              <td className="small">{v.chunks} фрагм. · {v.dense ? "BM25+векторы" : "только BM25"} · {v.reason}<div className="muted">{fmtDT(v.created_at)}</div>{v.error && <div style={{ color: "var(--bad)" }}>{v.error}</div>}</td></tr>))}
          </tbody></table>
        </div>
      </div>
    </>
  );
}
