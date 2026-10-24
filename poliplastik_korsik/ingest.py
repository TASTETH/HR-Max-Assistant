"""POLIPLASTIK local PDF/PPTX ingestion. CLI and reusable run()/load_current()."""
from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import logging
import os
import platform
import sys
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from chunks import split_chunks
from extract import apply_light_policy, finish, pdf_segments, pptx_segments
from ocr_core import OCR, VERSION, sha, write_json

LOG = logging.getLogger('ingest')
ALLOWED = {'.pdf', '.pptx'}


def checked_zip(archive, args):
    infos = archive.infolist()
    if len(infos) > 10000 or sum(x.file_size for x in infos) > args.max_total_mb*1024**2:
        raise ValueError('ZIP exceeds archive limits')
    for info in infos:
        path = PurePosixPath(info.filename.replace('\\', '/'))
        if path.is_absolute() or '..' in path.parts or (path.parts and ':' in path.parts[0]):
            raise ValueError('Unsafe ZIP path')
        if info.file_size > args.max_file_mb*1024**2:
            raise ValueError(f'ZIP entry too large: {info.filename}')
        if info.flag_bits & 1:
            raise ValueError('Encrypted ZIP not supported')
        if info.file_size > 1024**2 and info.file_size/max(1,info.compress_size) > 2000:
            raise ValueError('Suspicious ZIP compression ratio')
    return infos


def sources(path, args, policy, skipped):
    excluded = policy.get('exclude_names', {})
    def eligible(name):
        p = PurePosixPath(name.replace('\\', '/'))
        reason = excluded.get(p.name)
        if not reason and (p.name.startswith(('~$', '.')) or '__MACOSX' in p.parts):
            reason = 'temporary_or_system_file'
        if not reason and p.suffix.lower() not in ALLOWED:
            reason = 'unsupported_format'
        if reason:
            skipped.append({'path': name, 'reason': reason})
            return False
        return True
    if not path.exists():
        raise FileNotFoundError(f'Не найден исходник: {path}')
    if path.is_dir():
        files = sorted(p for p in path.rglob('*') if p.is_file())
        if sum(p.stat().st_size for p in files) > args.max_total_mb*1024**2:
            raise ValueError('Input directory exceeds --max-total-mb')
        for p in files:
            rel = p.relative_to(path).as_posix()
            if eligible(rel):
                if p.stat().st_size > args.max_file_mb*1024**2:
                    raise ValueError('Input file exceeds --max-file-mb')
                yield rel, p.read_bytes()
    elif path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            for info in sorted(checked_zip(archive,args), key=lambda i:i.filename):
                if not info.is_dir() and eligible(info.filename):
                    yield info.filename, archive.read(info)
    elif eligible(path.name):
        if path.stat().st_size > args.max_file_mb*1024**2:
            raise ValueError('Input file exceeds --max-file-mb')
        yield path.name, path.read_bytes()


def write_jsonl(path, records):
    with path.open('w', encoding='utf-8', newline='\n') as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')


def load_current(output, require_ready=True):
    """Only this pointer is the committed product handoff. Verify before embedding."""
    output = Path(output).resolve()
    pointer = json.loads((output/'current.json').read_text(encoding='utf-8'))
    snapshot = (output/pointer['snapshot']).resolve()
    if not snapshot.is_relative_to(output):
        raise ValueError('Invalid snapshot path')
    for name, expected in pointer['files'].items():
        if sha((snapshot/name).read_bytes()) != expected:
            raise ValueError(f'Integrity check failed: {name}')
    report = json.loads((snapshot/'report.json').read_text(encoding='utf-8'))
    if require_ready and not report['ready_for_embeddings']:
        raise RuntimeError('Корпус не готов: есть ошибки или проблемные сегменты. См. report.json.')
    return snapshot, report


@contextlib.contextmanager
def output_lock(out):
    out.mkdir(parents=True, exist_ok=True)
    lock = out/'.ingest.lock'
    handle = lock.open('a+b')
    if lock.stat().st_size == 0:
        handle.write(b' '); handle.flush()
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(f'Выход занят другим запуском: {lock}') from exc
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == 'nt':
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def run(args):
    started = time.monotonic()
    if not 100 <= args.dpi <= 600 or args.max_pages < 1 or args.ocr_timeout < 1 or args.max_pixels < 1:
        raise ValueError('Некорректные лимиты DPI/pages/timeout/pixels')
    if args.max_chars < 200 or not 0 <= args.overlap_chars < args.max_chars//2:
        raise ValueError('Bad chunk sizes')
    out = Path(args.output).resolve()
    source_path = Path(args.input).resolve()
    if source_path.is_dir() and (out == source_path or out.is_relative_to(source_path)):
        raise ValueError('Output must be outside input folder')
    policy = json.loads(Path(args.policy).read_text(encoding='utf-8-sig'))
    args.audit = args.audit or args.save_page_images or bool(args.reviews)
    args.image_policy = policy.get('image_policy', {})
    args.non_knowledge_segments = policy.get('non_knowledge_segments', {})
    with output_lock(out):
        ocr = OCR(args.tesseract_path or 'tesseract', args.lang, args.tessdata_dir,
                  out/'cache', args.ocr_timeout, args.max_pixels)
        ident = time.strftime('%Y%m%dT%H%M%S', time.gmtime())+'_'+uuid.uuid4().hex[:8]
        snapshot = out/'runs'/ident
        snapshot.mkdir(parents=True)
        assets = snapshot/'assets' if args.audit else None
        if assets:
            assets.mkdir()
        segments, documents, skipped, seen = [], [], [], {}
        source_cache_hits = 0
        input_error = None
        try:
            for path, raw in sources(source_path,args,policy,skipped):
                digest = sha(raw)
                if digest in seen:
                    skipped.append({'path': path, 'reason': 'duplicate_content', 'canonical': seen[digest]})
                    continue
                seen[digest] = path
                source = {'name': PurePosixPath(path).name, 'path': path, 'sha256': digest, 'size_bytes':len(raw)}
                try:
                    cache_key = sha(json.dumps([source,VERSION,ocr.signature,policy,
                        args.dpi,args.max_pages,args.max_pixels,args.max_file_mb,args.max_total_mb],sort_keys=True))
                    cache_path = out/'cache'/'sources'/(cache_key+'.json')
                    extracted = None
                    if not args.audit and cache_path.is_file():
                        try:
                            cached = json.loads(cache_path.read_text(encoding='utf-8'))
                            payload = cached['segments']
                            if cached['key'] == cache_key and cached['digest'] == sha(json.dumps(payload,ensure_ascii=False,sort_keys=True)):
                                extracted = payload
                                source_cache_hits += 1
                        except (OSError,ValueError,KeyError,TypeError):
                            pass
                    if extracted is None:
                        if Path(path).suffix.lower() == '.pptx':
                            with zipfile.ZipFile(__import__('io').BytesIO(raw)) as package:
                                checked_zip(package,args)
                            extracted = pptx_segments(source,raw,args,ocr,assets)
                        else:
                            extracted = pdf_segments(source,raw,args,ocr,assets)
                        if not args.audit:
                            write_json(cache_path,{'key':cache_key,'segments':extracted,
                                'digest':sha(json.dumps(extracted,ensure_ascii=False,sort_keys=True))})
                    if not extracted:
                        raise ValueError('No pages or slides found')
                    segments.extend(extracted)
                    documents.append({**source, 'segments':len(extracted), 'error':None})
                except Exception as exc:
                    LOG.exception('Ошибка документа %s',path)
                    documents.append({**source, 'segments':0, 'error':f'{type(exc).__name__}: {exc}'})
        except Exception as exc:
            input_error = f'{type(exc).__name__}: {exc}'
            LOG.exception('Ошибка входных данных')
        for seg in segments:
            extra_reason = policy.get('require_review_segments', {}).get(seg['segment_id']) if args.audit else None
            if extra_reason:
                seg['review_reasons'].append(extra_reason)
                finish(seg)
            if not args.audit:
                apply_light_policy(seg)
        if args.reviews:
            from review import apply_reviews
            apply_reviews(segments,args.reviews)
        chunks = [c for s in segments for c in split_chunks(s,args.max_chars,args.overlap_chars)]
        for doc in documents:
            group = [s for s in segments if s['source_sha256'] == doc['sha256']]
            doc.update(chars=sum(s['char_count'] for s in group),
                       review_segments=sum(s['status']=='needs_review' for s in group),
                       excluded_segments=sum(s['status']=='excluded' for s in group),
                       ocr_pages=sum(s['ocr_attempted'] for s in group),
                       warning_segments=sum(bool(s.get('quality_warnings',s['review_reasons'])) for s in group if not s['service_content']))
        pending = sum(s['status']=='needs_review' for s in segments)
        failed = sum(bool(d['error']) for d in documents)
        ready = bool(documents and segments and chunks and not input_error and not failed and not pending)
        report = {'schema_version':2,'pipeline_version':VERSION,'snapshot_id':ident,
                  'mode':'audit' if args.audit else 'main_text',
                  'ready_for_embeddings':ready,'documents':documents,'skipped':skipped,
                  'input_error':input_error,'segments':len(segments),'chunks':len(chunks),
                  'review_required':pending,'failed_documents':failed,
                  'excluded_segments':sum(s['status']=='excluded' for s in segments),
                  'ocr':ocr.signature,'ocr_subprocess_calls':ocr.calls,'ocr_cache_hits':ocr.hits,
                  'source_cache_hits':source_cache_hits,
                  'warning_segments':sum(bool(s.get('quality_warnings',s['review_reasons'])) for s in segments if not s['service_content']),
                  'skipped_supporting_images':sum(bool(a.get('skipped_reason')=='supporting_screenshot_native_text_retained') for s in segments for a in s['artifacts']),
                  'runtime':{'python':platform.python_version(), **{p:importlib.metadata.version(p) for p in ('PyMuPDF','python-pptx','Pillow','lxml')}},
                  'config':{'dpi':args.dpi,'max_chars':args.max_chars,'overlap_chars':args.overlap_chars,'policy':policy},
                  'elapsed_seconds':round(time.monotonic()-started,2),
                  'note':'Main-text mode excludes known supporting screenshots, PDF cover and contents. OCR confidence is heuristic; ready means extraction succeeded, not human validation.' if not args.audit else 'Audit mode requires manual review of flagged segments.'}
        write_jsonl(snapshot/'segments.jsonl',segments)
        write_jsonl(snapshot/'chunks.jsonl',chunks)
        write_jsonl(snapshot/'review_required.jsonl',[s for s in segments if s['status']=='needs_review'])
        write_json(snapshot/'report.json',report)
        # Immutable snapshot first, one atomic pointer last. Failed batches cannot look ready.
        names = ['segments.jsonl','chunks.jsonl','report.json','review_required.jsonl']
        if args.audit:
            from review import write_review
            write_review(snapshot/'review.html',segments,report)
            names.append('review.html')
        pointer = {'schema_version':1,'snapshot':snapshot.relative_to(out).as_posix(),
                   'files':{name:sha((snapshot/name).read_bytes()) for name in names}}
        write_json(out/'current.json',pointer)
        # Friendly root entry; review HTML uses assets relative to its immutable run.
        write_json(out/'report.json',report)
        for name in ('segments.jsonl','chunks.jsonl'):
            temp = out/(name+'.tmp')
            temp.write_bytes((snapshot/name).read_bytes())
            os.replace(temp,out/name)
        if args.audit:
            relative = snapshot.relative_to(out).as_posix()+'/review.html'
            (out/'review.html').write_text('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url='+relative+'"><a href="'+relative+'">Открыть проверку</a>',encoding='utf-8')
        summary = {key:report[key] for key in ('pipeline_version','mode','ready_for_embeddings','documents',
                   'segments','chunks','review_required','failed_documents','excluded_segments','input_error',
                   'ocr_subprocess_calls','ocr_cache_hits','source_cache_hits','warning_segments','skipped_supporting_images','elapsed_seconds')}
        summary['chunks_file'] = str(out/'chunks.jsonl')
        if args.audit:
            summary['review_file'] = str(out/'review.html')
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        return report


def parser():
    p = argparse.ArgumentParser(description='ПОЛИПЛАСТИК: основной текст PDF/PPTX → OCR → чанки')
    p.add_argument('--input',help='ZIP, папка с документами, PDF или PPTX')
    p.add_argument('--output',default='data/ingested')
    p.add_argument('--policy',default=str(Path(__file__).with_name('policy.json')))
    p.add_argument('--lang',default='rus+eng')
    p.add_argument('--tesseract-path')
    p.add_argument('--tessdata-dir')
    p.add_argument('--dpi',type=int,default=300)
    p.add_argument('--ocr-timeout',type=int,default=90)
    p.add_argument('--max-pages',type=int,default=500)
    p.add_argument('--max-pixels',type=int,default=40_000_000)
    p.add_argument('--max-file-mb',type=int,default=200)
    p.add_argument('--max-total-mb',type=int,default=1024)
    p.add_argument('--max-chars',type=int,default=1000)
    p.add_argument('--overlap-chars',type=int,default=120)
    p.add_argument('--reviews',help='reviews.json, скачанный из страницы проверки')
    p.add_argument('--audit',action='store_true',help='Подробная диагностика с изображениями, OCR скриншотов и HTML-проверкой')
    p.add_argument('--diagnose',action='store_true')
    p.add_argument('--save-page-images',action='store_true',help='Совместимость: включает режим --audit')
    return p


def main():
    p = parser();args = p.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.diagnose:
            ocr = OCR(args.tesseract_path or 'tesseract',args.lang,args.tessdata_dir)
            print(json.dumps({'ready':True,'ocr':ocr.signature},ensure_ascii=False,indent=2))
            return 0
        if not args.input:
            p.error('--input обязателен, кроме --diagnose')
        report = run(args)
        if report['failed_documents'] or report['input_error'] or not report['segments']:
            return 1
        return 0 if report['ready_for_embeddings'] else 2
    except Exception as exc:
        LOG.error('%s: %s',type(exc).__name__,exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
