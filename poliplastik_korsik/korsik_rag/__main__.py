import argparse
import json
import logging
from dataclasses import replace
from pathlib import Path
from .config import Settings
from .index import build_index
from .models import Models,download_models
from .service import RagService
from .types import AccessContext


def main():
    p=argparse.ArgumentParser(description='Корсик: OCR snapshots → hybrid RAG')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('download-models')
    sub.add_parser('index')
    for command in ('search','ask'):
        q=sub.add_parser(command);q.add_argument('question');q.add_argument('--org-id');q.add_argument('--site-id')
        q.add_argument('--history',help='JSON file: [{"role":"user","content":"..."}]')
        q.add_argument('--address-style',choices=['ты','вы'])
    serve=sub.add_parser('serve');serve.add_argument('--host',default='127.0.0.1');serve.add_argument('--port',type=int,default=8003)
    args=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings=Settings.from_env()
    try:
        if args.command=='download-models':result={'downloaded':download_models(settings)}
        elif args.command=='index':result=build_index(settings,Models(settings))
        elif args.command=='serve':
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(settings),host=args.host,port=args.port,workers=1);return 0
        else:
            service=RagService(settings)
            try:
                ctx=service.default_context()
                if args.org_id:ctx=ctx.model_copy(update={'org_id':args.org_id})
                if args.site_id:ctx=ctx.model_copy(update={'site_id':args.site_id})
                history=json.loads(Path(args.history).read_text(encoding='utf-8')) if args.history else []
                if args.command=='search':result=service.search(args.question,ctx,history)
                else:result=service.ask(args.question,ctx,history,args.address_style).model_dump()
            finally:service.close()
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if isinstance(result,dict) and result.get('status')=='service_error' else 0
    except Exception as exc:
        print(json.dumps({'status':'service_error','error_code':getattr(exc,'code','command_failed'),
                          'message':str(exc)},ensure_ascii=False,indent=2));return 1


if __name__=='__main__':raise SystemExit(main())
