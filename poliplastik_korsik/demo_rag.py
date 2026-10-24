"""VS Code retrieval demo. No LLM, no automatic semantic no-answer detector."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run_case(service, case):
    from korsik_rag.types import AccessContext
    ctx = AccessContext(**case.get('context', {
        'org_id': 'polyplastic_group_llc', 'permissions': ['kb:read']}))
    try:
        found = service.search(case['question'], ctx, case.get('history', []))
    except Exception as exc:
        found = {'status': 'service_error', 'error_code': getattr(exc, 'code', type(exc).__name__)}
    status = found['status']
    if status == 'service_error':
        observed = 'техническая ошибка'
    elif status == 'needs_clarification':
        observed = 'не определён: требуется уточнение'
    elif status == 'insufficient_evidence':
        observed = 'не определён: доступных источников нет'
    else:
        observed = 'кандидаты найдены; наличие ответа проверить вручную'
    sources = found.get('sources', [])
    required = case.get('expected_sources', [])
    coverage = all(any((s['document'], s['location_type'], s['location']) ==
                      (ref['document'], ref['location_type'], ref['location'])
                      for s in sources) for ref in required) if required else None
    candidates = found.get('candidates', [])
    before = sorted(candidates, key=lambda c: (-c['rrf_score'], c['chunk_id']))
    ocr = [dict(document=ref['document'], location=ref['location'],
                text=service.corpus.by_segment[ref['segment_id']]['text']) for ref in required]
    return {**case, 'retrieval_status': status, 'observed': observed,
            'expected_source_coverage': coverage, 'before_reranker': before,
            'actual_ranking': candidates, 'actual_sources': sources,
            'ocr_segments': ocr, 'diagnostics': found, 'llm_called': False}


def markdown(rows):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    lines = ['# Проверка OCR и поиска без LLM', '',
             '«Ожидается» — ручная разметка, «Наблюдаем» — фактический результат. '
             'Покрытие источников не доказывает правильность ответа. '
             'Score реранкера — релевантность, не вероятность.', '']
    for row in rows:
        lines += [f"## {row['id']}: {row['question']}", '',
                  f"Ожидается: **{row['expected_answer']}**. {row.get('note', '')}", '',
                  f"Наблюдаем: **{row['observed']}**. Статус: `{row['retrieval_status']}`.", '',
                  f"Покрытие ожидаемых источников: {row['expected_source_coverage']}. "
                  f"Время этапов: {row['diagnostics'].get('timings', {})}", '']
        for label, key, score in [('До реранкера: кандидаты RRF', 'before_reranker', 'rrf_score'),
                                  ('После реранкера', 'actual_ranking', 'reranker_score')]:
            lines += [f'### {label}', '', '| Место | Документ | Тип | Номер | Score |',
                      '|---:|---|---|---:|---:|']
            for rank, c in enumerate(row[key], 1):
                lines.append(f"| {rank} | {cell(c['document'])} | {c['location_type']} | "
                             f"{c['location']} | {c[score]:.4f} |")
            lines.append('')
        lines += ['### Собранный контекст', '']
        for source in row['actual_sources']:
            lines += [f"**{source['document']} — {source['location_type']} {source['location']}**", '',
                      *['> ' + line for line in source['text'].splitlines()], '']
        if row['ocr_segments']:
            lines += ['### Исходный текст из segments.jsonl', '']
            for segment in row['ocr_segments']:
                lines += [f"**{segment['document']}, {segment['location']}**", '',
                          *['> ' + line for line in segment['text'].splitlines()], '']
    return '\n'.join(lines) + '\n'


def main():
    os.chdir(ROOT)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', help='Например q02; без параметра — все вопросы')
    args = parser.parse_args()
    cases = json.loads((ROOT / 'mini_rag_checks.json').read_text(encoding='utf-8-sig'))
    cases = [c for c in cases if c['id'] == args.case] if args.case else cases
    if not cases:
        parser.error('Такой id вопроса не найден')
    from korsik_rag.service import RagService
    from korsik_rag.config import Settings
    print('Загрузка моделей один раз. LLM не вызывается.', flush=True)
    try:
        service = RagService(Settings.from_env())
    except Exception as exc:
        print(f"service_error: {getattr(exc, 'code', type(exc).__name__)}: {exc}")
        return 1
    rows = []
    dest = ROOT / 'data' / 'demo_rag'
    dest.mkdir(parents=True, exist_ok=True)
    try:
        for case in cases:
            for ref in case.get('expected_sources', []):
                if ref['verified_quote'] not in service.corpus.by_segment[ref['segment_id']]['text']:
                    raise ValueError('Разметка не совпадает с активным корпусом: ' + case['id'])
        for case in cases:
            print(f"\n{case['id']}: {case['question']}", flush=True)
            row = run_case(service, case)
            rows.append(row)
            print('Ожидается:', row['expected_answer'], '| Наблюдаем:', row['observed'], flush=True)
            for rank, c in enumerate(row['actual_ranking'][:5], 1):
                print(f"  {rank}. {c['document']} / {c['location']} / score={c['reranker_score']:.3f}")
            (dest / 'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
            (dest / 'results.md').write_text(markdown(rows), encoding='utf-8')
    finally:
        service.close()
    print('\nРезультаты:', dest / 'results.md', '\nПолные данные:', dest / 'results.json')
    print('Отрицательные вопросы с найденными кандидатами требуют ручной проверки текста.')
    return int(any(r['retrieval_status'] == 'service_error' or r['expected_source_coverage'] is False for r in rows))


if __name__ == '__main__':
    raise SystemExit(main())
