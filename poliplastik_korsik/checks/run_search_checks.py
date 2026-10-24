"""Small real retrieval check. Does NOT judge LLM answers or OCR accuracy."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from korsik_rag.config import Settings
from korsik_rag.service import RagService
from korsik_rag.types import AccessContext


def run(service,questions):
    ctx=AccessContext(org_id='polyplastic_group_llc',permissions=['kb:read'])
    results=[]
    for item in questions:
        for reference in item['expected_sources']:
            assert reference['verified_quote'] in service.corpus.by_segment[reference['segment_id']]['text']
        started=time.monotonic()
        try:
            found=service.search(item['question'],ctx,item['history'])
            locations={(s['document'],s['location_type'],s['location']) for s in found.get('sources',[])}
            required={(s['document'],s['location_type'],s['location']) for s in item['expected_sources']}
            positive=item['kind'] in ('positive','follow_up')
            row={'id':item['id'],'question':item['question'],'kind':item['kind'],'status':found['status'],
                 'expected_source_coverage':required.issubset(locations) if positive else None,
                 'context_locations':[list(x) for x in sorted(locations)],'seconds':round(time.monotonic()-started,3),
                 'top_candidates':[{k:c[k] for k in ('chunk_id','document','location','reranker_score')} for c in found.get('candidates',[])[:3]],
                 'generation':'not_run'}
        except Exception as exc:
            row={'id':item['id'],'question':item['question'],'kind':item['kind'],'error_code':getattr(exc,'code',type(exc).__name__),
                 'error':str(exc),'seconds':round(time.monotonic()-started,3),'generation':'not_run'}
        results.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    positives=[x for x in results if x['kind'] in ('positive','follow_up')]
    return {'models':'real BGE-M3 and BGE-reranker-v2-m3','llm':'not used',
            'positive_count':len(positives),'positive_context_coverage':sum(x.get('expected_source_coverage',False) for x in positives),
            'errors':sum('error_code' in x for x in results),'results':results,
            'note':'Source coverage is a retrieval check, not a correctness estimate for generated answers. Negative questions require an LLM/manual answer check.'}


if __name__=='__main__':
    service=RagService(Settings.from_env())
    try:report=run(service,json.loads(Path(__file__).with_name('questions.json').read_text(encoding='utf-8')))
    finally:service.close()
    dest=Path('verification/search_checks.json');dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False,indent=2))
