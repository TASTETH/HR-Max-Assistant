import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI,Header,HTTPException,Depends
from fastapi.concurrency import run_in_threadpool
from .config import Settings
from .service import RagService
from .types import AskRequest,RagResult


def create_app(settings=None,service_factory=RagService):
    settings=settings or Settings.from_env()
    @asynccontextmanager
    async def lifespan(app):
        if not settings.service_token or settings.service_token.lower() in ('change-me','changeme'):
            raise RuntimeError('Set a private RAG_SERVICE_TOKEN before starting the API')
        app.state.service=None;app.state.startup_error=None
        try:app.state.service=await run_in_threadpool(service_factory,settings)
        except Exception as exc:
            app.state.startup_error=getattr(exc,'code','startup_failed')
            logging.getLogger('korsik.rag').error('startup_error code=%s type=%s',app.state.startup_error,type(exc).__name__)
        yield
        if app.state.service:await run_in_threadpool(app.state.service.close)
    app=FastAPI(title='Корсик RAG',version='1.0.0',lifespan=lifespan,docs_url=None,redoc_url=None)
    async def authorize(authorization:str|None=Header(default=None)):
        expected='Bearer '+settings.service_token
        if not authorization or not hmac.compare_digest(authorization.encode(),expected.encode()):raise HTTPException(401,'Service authorization required')
    @app.get('/health/live')
    async def live():return {'alive':True}
    @app.get('/health/ready',dependencies=[Depends(authorize)])
    async def ready():
        service=app.state.service
        if service is None:raise HTTPException(503,{'ready':False,'error_code':app.state.startup_error})
        return {'ready':True,'search_ready':True,'llm_configured':service.llm.configured,
                'index_version':service.manifest['corpus_fingerprint'],'chunk_count':service.manifest['chunk_count']}
    @app.post('/rag/ask',response_model=RagResult,dependencies=[Depends(authorize)])
    async def ask(request:AskRequest):
        service=app.state.service
        if service is None:return RagResult(status='service_error',answer='Поисковый сервис не готов. Проверьте индекс, Qdrant и загрузку моделей.',
            sources=[],can_escalate=True,request_id=str(uuid.uuid4()),error_code=app.state.startup_error or 'not_ready')
        # This endpoint is service-to-service: context is supplied by authenticated backend.
        return await run_in_threadpool(service.ask,request.question,request.context,request.history,request.address_style)
    return app
