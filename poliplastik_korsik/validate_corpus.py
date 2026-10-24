"""Mechanical corpus checks, not a claim of measured OCR accuracy."""
import argparse
import json
from collections import Counter
from pathlib import Path
from ingest import load_current


EXPECTED={
 'Антикоррупционная политика.pptx':14,
 'Политика в области качества и экологии.pptx':5,
 'Положение о коммерческой тайне.pptx':14,
 'Правила внутреннего трудового распорядка П 3.11-07 Издание 1.pdf':23,
}


def validate(output):
    snap,report=load_current(output,require_ready=False)
    segments=[json.loads(l) for l in (snap/'segments.jsonl').read_text(encoding='utf-8').splitlines()]
    chunks=[json.loads(l) for l in (snap/'chunks.jsonl').read_text(encoding='utf-8').splitlines()]
    checks={}
    checks['four_documents_and_56_segments']=dict(Counter(s['document'] for s in segments))==EXPECTED
    checks['no_document_errors']=not report['failed_documents'] and not report['input_error']
    checks['unique_segment_ids']=len({s['segment_id'] for s in segments})==len(segments)
    checks['unique_chunk_ids']=len({c['chunk_id'] for c in chunks})==len(chunks)
    lookup={s['segment_id']:s for s in segments}
    checks['chunk_lineage_and_quarantine']=all(c['segment_id'] in lookup and lookup[c['segment_id']]['status'] in ('accepted','reviewed') and c['text']==lookup[c['segment_id']]['text'][c['char_start']:c['char_end']] for c in chunks)
    checks['bounded_chunks']=all(0<len(c['text'])<=report['config']['max_chars'] for c in chunks)
    if report.get('mode','audit') == 'audit':
        checks['audit_assets_exist']=all((snap/a['image']).is_file() and ((snap/a['ocr_evidence']).is_file() if a['ocr_evidence'] else a.get('classification',{}).get('role')=='illustration') for s in segments for a in s['artifacts'])
    else:
        checks['no_audit_files_in_main_mode']=not (snap/'assets').exists() and not (snap/'review.html').exists()
    checks['smartart_quality_slides_3_4_5']=all(any(b['kind']=='smartart' and len(b['text'])>200 for b in s['blocks']) for s in segments if s['document']=='Политика в области качества и экологии.pptx' and s['location'] in (3,4,5))
    checks['commercial_secret_smartart_slide_3']=any(s['document']=='Положение о коммерческой тайне.pptx' and s['location']==3 and any(b['kind']=='smartart' and 'Коммерческая тайна' in b['text'] for b in s['blocks']) for s in segments)
    if report.get('mode','audit') == 'audit':
        checks['all_23_scan_pages_processed']=sum(s['location_type']=='page' and s['ocr_attempted'] for s in segments)==23
    else:
        checks['all_21_main_scan_pages_processed']=sum(s['location_type']=='page' and s['ocr_attempted'] for s in segments)==21
        checks['cover_and_contents_explicitly_excluded']=sum(s['location_type']=='page' and s['service_content'] and s['location'] in (1,2) for s in segments)==2
    checks['quiz_placeholder_excluded']=any(s['document']=='Антикоррупционная политика.pptx' and s['location']==13 and s['status']=='excluded' for s in segments)
    combined='\n'.join(s['text'] for s in segments)
    checks['known_contact_anti_corruption']='anti-corruption@polyplastic.ru' in combined and '4384' in combined
    result={'all_mechanical_checks_passed':all(checks.values()),'checks':checks,
            'ready_for_embeddings':report['ready_for_embeddings'],'review_required':report['review_required'],
            'note':'Coverage/lineage checks do not measure OCR character accuracy.'}
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',default='data/ingested');args=p.parse_args()
    result=validate(args.output);print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['all_mechanical_checks_passed'] else 1)
