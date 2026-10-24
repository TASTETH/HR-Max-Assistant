import contextlib
import json
import logging
import re
import threading
import time
import uuid
from qdrant_client import models as qm
from .config import Settings
from .context import ContextBuilder
from .corpus import point_id
from .index import client,load_active
from .lexical import LexicalIndex,rrf
from .llm import LLMClient,validate_answer
from .models import Models
from .types import AccessContext,AskRequest,RagError,RagResult

LOG=logging.getLogger('korsik.rag')


def public_text(text,style):
    if style=='ты':
        for a,b in [('Уточните','Уточни'),('уточните','уточни'),('Сформулируйте','Сформулируй'),
                    ('Попробуйте','Попробуй'),('Повторите','Повтори'),('обратитесь','обратись'),
                    ('вашей','твоей'),('вы работаете','ты работаешь')]:
            text=text.replace(a,b)
    return text


class RagService:
    def __init__(self,settings=None,models=None,qdrant=None,llm=None,warmup=True):
        self.s=settings or Settings.from_env();self.s.validate()
        self.q=qdrant or client(self.s)
        try:self.corpus,self.manifest,self.directory=load_active(self.s,self.q)
        except FileNotFoundError as exc:raise RagError('index_missing','Build the index with python -m korsik_rag index') from exc
        self.lexical=LexicalIndex(self.corpus.chunks)
        self.models=models or Models(self.s);self.llm=llm or LLMClient(self.s)
        self.gate=threading.BoundedSemaphore(1)
        if warmup:
            try:self.models.load_encoder();self.models.load_reranker()
            except RagError:raise
            except Exception as exc:raise RagError('models_unavailable','Download the pinned models and verify device/cache configuration') from exc
        self.builder=ContextBuilder(self.corpus,self.s,self.models.count)

    def default_context(self):
        return AccessContext(org_id=self.s.server_org_id or None,site_id=self.s.server_site_id or None,
            permissions=[p.strip() for p in self.s.server_permissions.split(',') if p.strip()])

    @contextlib.contextmanager
    def compute_slot(self):
        if not self.gate.acquire(timeout=self.s.queue_timeout):raise RagError('busy','Compute slot is busy')
        try:yield
        finally:self.gate.release()

    def _event(self,request_id,stage,**fields):
        LOG.info(json.dumps({'event':'rag','request_id':request_id,'index_version':self.manifest['corpus_fingerprint'],
                             'stage':stage,**fields},ensure_ascii=False))

    def _history(self,history):
        selected=[];length=0
        for m in reversed(history[-self.s.history_messages:] if self.s.history_messages else []):
            if length+len(m.content)>self.s.history_max_chars:break
            selected.append({'role':m.role,'content':m.content});length+=len(m.content)
        return list(reversed(selected))

    def _preflight(self,question,ctx,history):
        if not ctx.org_id:return 'Уточните, пожалуйста, в какой организации вы работаете. Для ответа нужен применимый к ней регламент.'
        if not question.strip():return 'Уточните, пожалуйста, вопрос.'
        follow=bool(re.match(r'(?i)^(а\s+(если|как|что|можно)|а это|а там|а сколько|тогда|почему так)',question.strip()))
        if (follow or len(question.strip())<12) and not history:return 'Уточните, пожалуйста, о каком правиле или ситуации идёт речь.'
        if re.search(r'на (?:другом заводе|всех предприятиях)|для всей группы|в другом филиале',question,re.I):
            return 'Уточните организацию и площадку. Регламент ООО «Группа ПОЛИПЛАСТИК» нельзя автоматически применять ко всем предприятиям группы.'
        return None

    def _search(self,question,ctx,history,request_id):
        allowed_ids=self.corpus.allowed_ids(ctx)
        if not allowed_ids:return {'candidates':[],'context':None,'query':question,'timings':{}}
        query=question
        if re.match(r'(?i)^(а\b|тогда\b|почему так)',question.strip()):
            last_user=next((m['content'] for m in reversed(history) if m['role']=='user'),None)
            if last_user:query=question+'\nПредыдущий вопрос: '+last_user
        started=time.monotonic()
        try:
            vector=self.models.encode([query],query=True)[0]
            points=self.q.query_points(self.manifest['collection'],query=vector.tolist(),
                query_filter=qm.Filter(must=[qm.HasIdCondition(has_id=[point_id(cid) for cid in sorted(allowed_ids)])]),
                limit=self.s.dense_top_k,with_payload=True).points
            dense=[]
            for point in points:
                cid=(point.payload or {}).get('chunk_id')
                if cid not in allowed_ids:raise RagError('access_filter','Qdrant returned an unauthorized chunk')
                if point.payload.get('corpus_fingerprint')!=self.manifest['corpus_fingerprint']:raise RagError('index_inconsistent','Active collection changed')
                dense.append((cid,float(point.score)))
            bm25=self.lexical.search(query,allowed_ids,self.s.bm25_top_k)
        except RagError:raise
        except Exception as exc:raise RagError('retrieval_unavailable','Qdrant/embedding retrieval failed') from exc
        retrieval_seconds=time.monotonic()-started
        fused=rrf(dense,bm25,self.s.rrf_k)[:self.s.rerank_top_n]
        if not fused:return {'candidates':[],'context':None,'query':query,'timings':{'retrieval_seconds':retrieval_seconds}}
        started=time.monotonic()
        try:scores=self.models.rerank(query,[self.corpus.by_id[x['chunk_id']] for x in fused])
        except RagError:raise
        except Exception as exc:raise RagError('reranker_unavailable','Reranker failed') from exc
        for candidate,score in zip(fused,scores):candidate['reranker_score']=score
        fused.sort(key=lambda c:(-c['reranker_score'],-c['rrf_score'],c['chunk_id']))
        reranker_seconds=time.monotonic()-started
        started=time.monotonic();context=self.builder.build(fused,ctx);context_seconds=time.monotonic()-started
        timings={'retrieval_seconds':round(retrieval_seconds,4),'reranker_seconds':round(reranker_seconds,4),
                 'context_seconds':round(context_seconds,4)}
        self._event(request_id,'retrieved',**timings,chunk_ids=[x['chunk_id'] for x in fused],
                    context_source_ids=[s.citation_id for s in context.sources])
        return {'candidates':fused,'context':context,'query':query,'timings':timings}

    def search(self,question,context=None,history=None):
        req=AskRequest(question=question,history=history or []);ctx=context or self.default_context();request_id=str(uuid.uuid4())
        history=self._history(req.history);clarification=self._preflight(question,ctx,history)
        if clarification:return {'status':'needs_clarification','answer':clarification,'request_id':request_id,'sources':[]}
        with self.compute_slot():result=self._search(question,ctx,history,request_id)
        sources=result['context'].sources if result['context'] else []
        candidates=[{**c,'document':self.corpus.by_id[c['chunk_id']]['document'],
            'location':self.corpus.by_id[c['chunk_id']]['location'],
            'location_type':self.corpus.by_id[c['chunk_id']]['location_type'],
            'text':self.corpus.by_id[c['chunk_id']]['text']} for c in result['candidates']]
        return {'status':'retrieved' if sources else 'insufficient_evidence','request_id':request_id,
                'index_version':self.manifest['corpus_fingerprint'],'candidates':candidates,
                'sources':[s.model_dump() for s in sources],'timings':result['timings'],
                'note':'Retrieval diagnostics; reranker scores are relevance logits, not answer probabilities.'}

    def ask(self,question,context=None,history=None,address_style=None):
        request_id=str(uuid.uuid4());ctx=context or self.default_context();stage='input';stage_started=time.monotonic()
        style=address_style or self.s.address_style
        try:
            req=AskRequest(question=question,history=history or [],address_style=address_style)
            selected=self._history(req.history);clarification=self._preflight(question,ctx,selected)
            if clarification:return RagResult(status='needs_clarification',answer=public_text(clarification,style),sources=[],can_escalate=ctx.can_escalate,request_id=request_id)
            if not self.llm.configured:
                stage='llm';stage_started=time.monotonic()
                raise RagError('llm_not_configured','LLM endpoint/model not configured')
            with self.compute_slot():
                stage='retrieval';stage_started=time.monotonic();found=self._search(question,ctx,selected,request_id)
                if not found['context'] or not found['context'].sources:
                    return RagResult(status='insufficient_evidence',answer=public_text('В доступных для вашей организации материалах не найдено подтверждение ответа.',style),
                        can_escalate=ctx.can_escalate,request_id=request_id)
                stage='llm';started=time.monotonic();stage_started=started
                payload=self.llm.generate(question,selected,found['context'],ctx,address_style or self.s.address_style)
                elapsed=time.monotonic()-started;stage='validation';stage_started=time.monotonic()
                result=validate_answer(payload,found['context'],self.corpus,ctx.can_escalate,request_id)
                self._event(request_id,'completed',llm_seconds=round(elapsed,4),status=result.status,
                    source_ids=[s.citation_id for s in result.sources],chunk_ids=[s.chunk_id for s in result.sources])
                return result
        except Exception as exc:
            code=exc.code if isinstance(exc,RagError) else 'internal_error'
            error_stage='reranker' if code.startswith('reranker') else 'context' if code.startswith('context') else stage
            self._event(request_id,'error',error_stage=error_stage,error_code=code,error_type=type(exc).__name__,
                        duration_seconds=round(time.monotonic()-stage_started,4))
            if code=='input_too_long':return RagResult(status='needs_clarification',answer=public_text('Сформулируйте, пожалуйста, более короткий вопрос.',style),can_escalate=ctx.can_escalate,request_id=request_id)
            messages={'llm_not_configured':'Генерация ответа ещё не настроена. Поиск доступен; нужен адрес и модель LLM.',
                'busy':'Сервис сейчас обрабатывает другой запрос. Повторите немного позже.',
                'context_budget':'Полное правило не помещается в настроенный контекст. Нужна настройка сервиса.',
                'llm_timeout':'Модель не ответила за отведённое время. Попробуйте повторить запрос.'}
            return RagResult(status='service_error',answer=public_text(messages.get(code,'Не удалось подготовить проверенный ответ из-за технической ошибки. Попробуйте позже или обратитесь к HR.'),style),
                sources=[],can_escalate=ctx.can_escalate,request_id=request_id,error_code=code)

    def close(self):
        self.llm.close();self.q.close()
