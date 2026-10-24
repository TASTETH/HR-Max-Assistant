"""Offline review surface and hash-bound corrections."""
import html
import json
from pathlib import Path

from ocr_core import tidy


def apply_reviews(segments, path):
    if not path:
        return
    payload = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if payload.get('schema_version') != 1 or not isinstance(payload.get('decisions'), list):
        raise ValueError('Некорректный формат файла проверки')
    lookup = {s['segment_id']: s for s in segments}
    used = set()
    for d in payload['decisions']:
        ident = d['segment_id']
        if ident in used or ident not in lookup:
            raise ValueError(f'Неизвестный или повторный segment_id: {ident}')
        used.add(ident)
        seg = lookup[ident]
        if d.get('source_sha256') != seg['source_sha256'] or d.get('content_fingerprint') != seg['content_fingerprint']:
            raise ValueError(f'Устаревшая проверка для {seg["document"]}, {seg["location"]}. Создай её из нового review.html.')
        decision = d.get('decision')
        if decision not in ('approve', 'exclude'):
            raise ValueError('decision должен быть approve или exclude')
        text = tidy(d.get('text', seg['text']))
        if decision == 'approve' and (not text or seg['service_content']):
            raise ValueError('Нельзя утвердить пустой текст или служебный слайд; используй exclude')
        seg['review'] = {'decision': decision, 'note': d.get('note', ''),
                         'original_reasons': seg['review_reasons'][:], 'text_changed': text != seg['text']}
        seg['text'] = text
        seg['char_count'] = len(text)
        seg['needs_review'] = False
        seg['status'] = 'reviewed' if decision == 'approve' else 'excluded'


def write_review(path, segments, report):
    data = json.dumps(segments, ensure_ascii=False).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    template = r'''<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ПОЛИПЛАСТИК — проверка извлечения</title>
<style>
body{font:16px/1.5 system-ui;background:#eef3f7;color:#17314a;margin:0}header{background:#112e46;color:white;padding:22px 4vw;position:sticky;top:0;z-index:2}h1{font-size:22px;margin:0}main{max-width:1500px;margin:24px auto;padding:0 20px}button,select,input{font:inherit;padding:8px;border:1px solid #a9bac7;border-radius:5px}button{background:#136c8e;color:#fff;cursor:pointer}nav{display:flex;gap:10px;margin:12px 0;flex-wrap:wrap}.card{background:#fff;border:1px solid #d1dce5;border-radius:10px;padding:18px;margin:18px 0}.columns{display:grid;grid-template-columns:1fr 1fr;gap:18px}textarea{width:100%;box-sizing:border-box;min-height:350px;resize:vertical;font:15px/1.5 ui-monospace,monospace}img{max-width:100%;max-height:800px;object-fit:contain;border:1px solid #d8e1e9;background:#fff}.badge{color:#9a3300}.ok{color:#11613c}pre{white-space:pre-wrap;word-break:break-word;font:14px/1.5 ui-monospace,monospace}.hint{font-size:14px;color:#4a6070}details{margin:12px 0}label{display:block}input.note{width:95%}@media(max-width:850px){.columns{grid-template-columns:1fr}}
</style>
<header><h1>ПОЛИПЛАСТИК · контроль текста перед эмбеддингами</h1><div id="summary"></div>
<nav><select id="filter"><option value="review">Требуют проверки</option><option value="all">Все страницы и слайды</option><option value="excluded">Исключённые</option></select><button id="save">Скачать решения → reviews.json</button><label>Загрузить решения <input type="file" id="import" accept=".json"></label></nav></header>
<main><p>Сверь текст со сканом. Для слайдов также открой исходную презентацию: здесь показаны извлечённые изображения, а не рендер слайда. Заголовки и SmartArt находятся в текстовых блоках.</p><p class="hint">Оценка Tesseract — эвристика, не вероятность правильности. Все исправления сохраняются только при нажатии «Скачать решения». После этого запусти ingest.py с --reviews. Заглушку iSpring исключаем: вопросов теста в ней нет.</p><div id="cards"></div></main>
<script id="source-data" type="application/json">__DATA__</script>
<script>
const segments=JSON.parse(document.querySelector('#source-data').textContent), decisions=new Map();
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const reasons={low_ocr_confidence:'Низкая оценка OCR',many_uncertain_words:'Много неуверенно распознанных слов',small_text_in_screenshot:'Мелкий текст скриншота',ocr_attempts_disagree:'Режимы OCR дали разные результаты',suspicious_character_mix:'Подозрительное соотношение символов',empty_ocr:'OCR не извлёк текст',very_little_page_text:'На странице извлечено мало текста',image_ocr_error:'Ошибка OCR изображения',mixed_pdf_layout:'Смешанный текст и изображения PDF',document_image_requires_verification:'Документ/интерфейс внутри картинки: сверь и убери элементы интерфейса',handwritten_approval_details:'Рукописные реквизиты утверждения: обязательная сверка',unreadable_image_or_illustration:'Крупное изображение почти без извлечённого текста'};
function render(){
 const filter=document.querySelector('#filter').value;const items=segments.filter(s=>filter==='all'||(filter==='excluded'?s.status==='excluded':s.needs_review&&s.status!=='excluded'));
 document.querySelector('#summary').textContent=`Сегментов: ${segments.length} · требуют проверки: ${segments.filter(s=>s.needs_review&&s.status!=='excluded').length} · решений выбрано: ${decisions.size}`;
 const root=document.querySelector('#cards');root.innerHTML='';
 for(const s of items){const d=decisions.get(s.segment_id);const card=document.createElement('section');card.className='card';
 card.innerHTML=`<h2>${esc(s.document)} · ${s.location_type==='page'?'страница':'слайд'} ${s.location}</h2><p class="badge">${esc(s.review_reasons.map(r=>reasons[r]||r).join(' · '))}</p><p>${esc(s.warnings.join(' · '))}</p><div class="columns"><div>${s.artifacts.map(a=>`<details open><summary>Изображение · OCR ${esc(a.confidence??'нет текста')}</summary><a href="${esc(a.image)}" target="_blank"><img loading="lazy" src="${esc(a.image)}"></a><pre>${esc(a.text)}</pre></details>`).join('')||'<p>Растровых изображений нет. Проверь исходный файл.</p>'}</div><div><label>Итоговый текст<textarea spellcheck="false">${esc(d?.text??s.text)}</textarea></label><label>Решение <select class="decision"><option value="">Не проверено</option><option value="approve" ${s.service_content?'disabled':''}>Подтвердить текст</option><option value="exclude">Исключить из базы знаний</option></select></label><label>Комментарий<input class="note" placeholder="Что исправлено / почему исключено" value="${esc(d?.note??'')}"></label><details><summary>Исходные текстовые блоки и SmartArt</summary><pre>${esc(JSON.stringify(s.blocks,null,2))}</pre></details><p class="hint">${esc(s.segment_id)}</p></div></div>`;
 const choice=card.querySelector('.decision');choice.value=d?.decision??'';
 const update=()=>{if(choice.value){decisions.set(s.segment_id,{segment_id:s.segment_id,source_sha256:s.source_sha256,content_fingerprint:s.content_fingerprint,decision:choice.value,text:card.querySelector('textarea').value,note:card.querySelector('.note').value});}else decisions.delete(s.segment_id);document.querySelector('#summary').textContent=`Сегментов: ${segments.length} · решений выбрано: ${decisions.size}`;};
 card.querySelectorAll('textarea,select,input').forEach(e=>e.addEventListener('input',update));root.append(card);}
}
document.querySelector('#filter').addEventListener('change',render);
document.querySelector('#save').onclick=()=>{const value={schema_version:1,decisions:[...decisions.values()]};const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='reviews.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
document.querySelector('#import').onchange=async e=>{try{const p=JSON.parse(await e.target.files[0].text());if(p.schema_version!==1)throw Error('Неизвестная схема');const incoming=[];for(const d of p.decisions){const s=segments.find(s=>s.segment_id===d.segment_id);if(!s||s.source_sha256!==d.source_sha256||s.content_fingerprint!==d.content_fingerprint)throw Error('Решения относятся к другому запуску или исходнику');incoming.push(d);}for(const d of incoming)decisions.set(d.segment_id,d);render();}catch(err){alert(err.message);}};
render();
</script></html>'''
    Path(path).write_text(template.replace('__DATA__', data), encoding='utf-8')
