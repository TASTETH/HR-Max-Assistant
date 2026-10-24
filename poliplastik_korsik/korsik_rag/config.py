from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv,find_dotenv


@dataclass(frozen=True)
class Settings:
    ingestion_output: str = 'data/ingested'
    index_dir: str = 'data/rag'
    scope_file: str = 'document_scope.json'
    qdrant_url: str = 'http://127.0.0.1:6333'
    qdrant_api_key: str = ''
    qdrant_timeout: float = 15
    http_trust_env: bool = False
    embedding_model: str = 'BAAI/bge-m3'
    embedding_revision: str = '5617a9f61b028005a4858fdac845db406aefb181'
    reranker_model: str = 'BAAI/bge-reranker-v2-m3'
    reranker_revision: str = '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e'
    model_cache: str = 'data/models'
    offline: bool = False
    device: str = 'cpu'
    batch_size: int = 2
    embedding_max_tokens: int = 1024
    reranker_max_tokens: int = 1024
    query_max_tokens: int = 256
    torch_threads: int = 2
    dense_top_k: int = 20
    bm25_top_k: int = 20
    rerank_top_n: int = 12
    context_top_k: int = 5
    rrf_k: int = 60
    context_max_chars: int = 16000
    context_max_tokens: int = 6500
    rule_max_chars: int = 18000
    llm_base_url: str = ''
    llm_model: str = ''
    llm_api_key: str = ''
    llm_timeout: float = 90
    llm_max_tokens: int = 1200
    llm_input_max_tokens: int = 16000
    llm_json_mode: bool = False
    llm_tokenizer: str = ''
    llm_tokenizer_revision: str = ''
    service_token: str = ''
    server_org_id: str = ''
    server_site_id: str = ''
    server_permissions: str = 'kb:read'
    address_style: str = 'вы'
    queue_timeout: float = 5
    history_messages: int = 4
    history_max_chars: int = 3000

    @classmethod
    def from_env(cls):
        env_file=os.getenv('RAG_ENV_FILE') or find_dotenv()
        load_dotenv(env_file or None,override=False)
        base=Path(env_file).resolve().parent if env_file else Path.cwd()
        types = {bool: lambda s:s.lower() in ('1','true','yes'), int:int, float:float, str:str}
        defaults=cls();values={}
        for name in cls.__dataclass_fields__:
            raw=os.getenv('RAG_'+name.upper())
            if raw is not None:
                values[name]=types[type(getattr(defaults,name))](raw)
        obj=cls(**values)
        from dataclasses import replace
        paths={}
        for name in ('ingestion_output','index_dir','scope_file','model_cache'):
            path=Path(getattr(obj,name)).expanduser()
            paths[name]=str(path if path.is_absolute() else (base/path).resolve())
        obj=replace(obj,**paths);obj.validate();return obj

    def validate(self):
        for name in ('batch_size','embedding_max_tokens','reranker_max_tokens','query_max_tokens','torch_threads',
                     'dense_top_k','bm25_top_k','rerank_top_n','context_top_k','rrf_k','context_max_chars',
                     'context_max_tokens','rule_max_chars','llm_max_tokens','llm_input_max_tokens'):
            if getattr(self,name)<1:raise ValueError(f'{name} must be positive')
        if self.device not in ('cpu','cuda'):raise ValueError('device must be cpu or cuda')
        if self.address_style not in ('ты','вы'):raise ValueError('address_style must be ты or вы')
        if self.context_top_k>self.rerank_top_n:raise ValueError('context_top_k exceeds rerank_top_n')
        if self.history_messages<0 or self.history_max_chars<0:raise ValueError('history limits must be >= 0')
        if self.queue_timeout<=0 or self.llm_timeout<=0:raise ValueError('timeouts must be positive')
        if not self.embedding_revision or not self.reranker_revision:raise ValueError('Pin model revisions')

    def index_spec(self):
        return {'embedding_model':self.embedding_model,'embedding_revision':self.embedding_revision,
                'normalized':True,'distance':'Cosine','embedding_max_tokens':self.embedding_max_tokens,
                'bm25_preparation':'ru-snowball-v1','point_id_mapping':'uuid5-url-korsik-v1'}
