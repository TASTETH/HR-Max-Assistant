import os
import threading
import numpy as np
from .types import RagError


class Models:
    """One pair of loaded models per service, float32 on CPU, strict input lengths."""
    def __init__(self,settings):
        self.s=settings;self.encoder=None;self.reranker=None;self._lock=threading.Lock()
        self._torch=None

    def _setup(self):
        if self._torch is None:
            import torch
            torch.set_num_threads(self.s.torch_threads)
            if self.s.device=='cuda' and not torch.cuda.is_available():raise RagError('model_device','CUDA unavailable')
            self._torch=torch

    def load_encoder(self):
        with self._lock:
            if self.encoder is None:
                self._setup()
                from sentence_transformers import SentenceTransformer
                self.encoder=SentenceTransformer(self.s.embedding_model,revision=self.s.embedding_revision,
                    device=self.s.device,cache_folder=self.s.model_cache,local_files_only=self.s.offline,
                    trust_remote_code=False,model_kwargs={'torch_dtype':self._torch.float32})
                native=self.encoder.max_seq_length
                if self.s.embedding_max_tokens>native:raise RagError('model_length',f'Embedding limit exceeds model maximum {native}')
                self.encoder.max_seq_length=self.s.embedding_max_tokens
        return self.encoder

    def load_reranker(self):
        with self._lock:
            if self.reranker is None:
                self._setup()
                from sentence_transformers import CrossEncoder
                self.reranker=CrossEncoder(self.s.reranker_model,revision=self.s.reranker_revision,
                    device=self.s.device,cache_folder=self.s.model_cache,local_files_only=self.s.offline,
                    max_length=self.s.reranker_max_tokens,trust_remote_code=False,
                    model_kwargs={'torch_dtype':self._torch.float32})
                native=getattr(self.reranker.model.config,'max_position_embeddings',8192)-2
                if self.s.reranker_max_tokens>native:raise RagError('model_length','Reranker limit exceeds model maximum')
        return self.reranker

    def count(self,text):
        return len(self.load_encoder().tokenizer(text,truncation=False,add_special_tokens=True)['input_ids'])

    def encode(self,texts,query=False):
        model=self.load_encoder();limit=min(self.s.query_max_tokens,self.s.embedding_max_tokens) if query else self.s.embedding_max_tokens
        for i,t in enumerate(texts):
            n=len(model.tokenizer(t,truncation=False,add_special_tokens=True)['input_ids'])
            if n>limit:raise RagError('input_too_long',f'Embedding input {i}: {n} tokens exceeds {limit}; no truncation applied')
        result=model.encode(texts,batch_size=self.s.batch_size,normalize_embeddings=True,
                            convert_to_numpy=True,show_progress_bar=False)
        result=np.asarray(result,dtype=np.float32)
        if result.ndim!=2 or not np.isfinite(result).all():raise RagError('model_output','Invalid embeddings')
        return result

    def rerank(self,query,chunks):
        model=self.load_reranker();pairs=[(query,c['embedding_text']) for c in chunks]
        for q,text in pairs:
            n=len(model.tokenizer(q,text,truncation=False,add_special_tokens=True)['input_ids'])
            if n>self.s.reranker_max_tokens:raise RagError('input_too_long',f'Rerank pair: {n} tokens exceeds {self.s.reranker_max_tokens}; no truncation applied')
        # Identity activation returns raw relevance logits, not a probability.
        scores=model.predict(pairs,batch_size=self.s.batch_size,show_progress_bar=False,
                             activation_fn=self._torch.nn.Identity(),convert_to_numpy=True)
        scores=np.asarray(scores).reshape(-1)
        if len(scores)!=len(chunks) or not np.isfinite(scores).all():raise RagError('model_output','Invalid reranker output')
        return [float(v) for v in scores]


def download_models(settings):
    os.environ.setdefault('HF_HUB_DISABLE_XET','1')
    from huggingface_hub import snapshot_download
    paths=[]
    for name,revision in [(settings.embedding_model,settings.embedding_revision),(settings.reranker_model,settings.reranker_revision)]:
        paths.append(snapshot_download(name,revision=revision,cache_dir=settings.model_cache,
            allow_patterns=['*.json','*.safetensors','*.bin','*.model','1_Pooling/*'],max_workers=2,local_files_only=settings.offline))
    return paths
