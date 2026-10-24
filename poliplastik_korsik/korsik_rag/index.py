import json
import logging
import shutil
import uuid
from pathlib import Path
from qdrant_client import QdrantClient,models as qm
from ingest import output_lock
from ocr_core import sha,write_json
from .corpus import Corpus,canonical,jsonl,point_id
from .types import RagError

LOG=logging.getLogger('korsik.index')


def client(settings):
    return QdrantClient(url=settings.qdrant_url,api_key=settings.qdrant_api_key or None,
                        timeout=settings.qdrant_timeout,prefer_grpc=False,trust_env=settings.http_trust_env)


def verify_remote(q,manifest,corpus):
    name=manifest['collection'];info=q.get_collection(name)
    vector=info.config.params.vectors
    if vector.size!=manifest['dimension'] or vector.distance!=qm.Distance.COSINE:raise RagError('index_inconsistent','Vector configuration mismatch')
    ids={};offset=None
    while True:
        points,offset=q.scroll(name,limit=256,offset=offset,with_payload=True,with_vectors=False)
        for p in points:
            payload=p.payload or {};cid=payload.get('chunk_id')
            if cid not in corpus.by_id or str(p.id)!=point_id(cid):raise RagError('index_inconsistent','Unexpected Qdrant point')
            if payload.get('corpus_fingerprint')!=manifest['corpus_fingerprint']:raise RagError('index_inconsistent','Mixed corpus versions')
            if payload.get('chunk_digest')!=sha(canonical(corpus.by_id[cid])):raise RagError('index_inconsistent','Payload chunk differs from BM25 corpus')
            ids[cid]=str(p.id)
        if offset is None:break
    if set(ids)!=set(corpus.by_id) or len(ids)!=manifest['chunk_count']:raise RagError('index_inconsistent','Dense and lexical chunk sets differ')


def load_active(settings,q=None):
    root=Path(settings.index_dir).resolve();pointer=json.loads((root/'active.json').read_text(encoding='utf-8'))
    directory=(root/pointer['version']).resolve()
    if not directory.is_relative_to(root):raise RagError('index_inconsistent','Unsafe manifest path')
    raw=(directory/'manifest.json').read_bytes()
    if sha(raw)!=pointer['manifest_sha256']:raise RagError('index_inconsistent','Manifest checksum mismatch')
    manifest=json.loads(raw)
    for name,digest in manifest['files'].items():
        if Path(name).name!=name or sha((directory/name).read_bytes())!=digest:raise RagError('index_inconsistent','Index artifact checksum mismatch')
    corpus=Corpus(jsonl(directory/'chunks.jsonl'),jsonl(directory/'segments.jsonl'),json.loads((directory/'scope.json').read_text(encoding='utf-8')))
    if corpus.fingerprint(manifest['index_spec'])!=manifest['corpus_fingerprint']:raise RagError('index_inconsistent','Corpus fingerprint mismatch')
    if manifest['index_spec']!=settings.index_spec():raise RagError('index_configuration','Index uses different embedding/BM25 settings; rebuild or restore configuration')
    if len(corpus.chunks)!=manifest['chunk_count']:raise RagError('index_inconsistent','Manifest count mismatch')
    verify_remote(q or client(settings),manifest,corpus)
    return corpus,manifest,directory


def build_index(settings,model_service,q=None):
    """New immutable collection + disk bundle; active pointer changes only on success."""
    root=Path(settings.index_dir).resolve();q=q or client(settings)
    with output_lock(root):
        corpus,snapshot,report=Corpus.from_ingestion(settings.ingestion_output,settings.scope_file)
        spec=settings.index_spec();fingerprint=corpus.fingerprint(spec)
        if (root/'active.json').is_file():
            # Validate existing generation before reusing it. Config/corpus changes build a new one.
            pointer=json.loads((root/'active.json').read_text());old=json.loads((root/pointer['version']/'manifest.json').read_text())
            if old.get('corpus_fingerprint')==fingerprint:
                _,manifest,_=load_active(settings,q)
                return {**manifest,'reused':True}
        version=fingerprint[:20]+'_'+uuid.uuid4().hex[:8];collection='korsik_'+version
        directory=root/'versions'/version;directory.mkdir(parents=True)
        created=False
        try:
            # Strict token checks occur before the collection is created.
            vectors=model_service.encode([c['embedding_text'] for c in corpus.chunks])
            dimension=int(vectors.shape[1])
            q.create_collection(collection,vectors_config=qm.VectorParams(size=dimension,distance=qm.Distance.COSINE))
            created=True
            for start in range(0,len(corpus.chunks),64):
                points=[]
                for c,v in zip(corpus.chunks[start:start+64],vectors[start:start+64]):
                    points.append(qm.PointStruct(id=point_id(c['chunk_id']),vector=v.tolist(),payload={**c,
                        'corpus_fingerprint':fingerprint,'chunk_digest':sha(canonical(c)),
                        'scope':corpus.scopes[c['source_sha256']]}))
                q.upsert(collection,points=points,wait=True)
            for name in ('chunks.jsonl','segments.jsonl'):shutil.copyfile(snapshot/name,directory/name)
            write_json(directory/'scope.json',corpus.scopes)
            manifest={'schema_version':1,'corpus_fingerprint':fingerprint,'index_spec':spec,
                      'collection':collection,'dimension':dimension,'chunk_count':len(corpus.chunks),
                      'ingestion_snapshot':report['snapshot_id'],'ingestion_pipeline_version':report['pipeline_version'],
                      'files':{n:sha((directory/n).read_bytes()) for n in ('chunks.jsonl','segments.jsonl','scope.json')}}
            verify_remote(q,manifest,corpus)
            write_json(directory/'manifest.json',manifest)
            write_json(root/'active.json',{'version':directory.relative_to(root).as_posix(),
                                        'manifest_sha256':sha((directory/'manifest.json').read_bytes())})
            return {**manifest,'reused':False}
        except Exception:
            if created:
                try:q.delete_collection(collection)
                except Exception:LOG.error('partial_collection_cleanup_failed',extra={'collection':collection})
            shutil.rmtree(directory,ignore_errors=True)
            raise
