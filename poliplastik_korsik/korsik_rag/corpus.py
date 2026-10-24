import json
from pathlib import Path
from uuid import uuid5,NAMESPACE_URL
from ocr_core import sha,write_json
from ingest import load_current
from .types import AccessContext,RagError


def jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding='utf-8').splitlines() if l.strip()]


def point_id(chunk_id):
    return str(uuid5(NAMESPACE_URL,'korsik:chunk:v1:'+chunk_id))


def canonical(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def allowed(scope,ctx:AccessContext):
    if not set(scope['required_permissions']).issubset(ctx.permissions):return False
    if scope['org_ids'] and ctx.org_id not in scope['org_ids']:return False
    if scope['site_ids'] and ctx.site_id not in scope['site_ids']:return False
    return True


class Corpus:
    def __init__(self,chunks,segments,scopes):
        self.chunks=chunks;self.segments=segments;self.scopes=scopes
        self.by_id={c['chunk_id']:c for c in chunks};self.by_segment={s['segment_id']:s for s in segments}
        if len(self.by_id)!=len(chunks) or len(self.by_segment)!=len(segments):raise ValueError('Duplicate corpus IDs')
        self.pages={(s['source_sha256'],s['location_type'],s['location']):s for s in segments}
        for c in chunks:
            required={'chunk_id','segment_id','document','source_path','source_sha256','location_type','location',
                      'char_start','char_end','text','embedding_text','status','needs_review'}
            if not required.issubset(c):raise ValueError('Incompatible ingestion chunk schema')
            if c['needs_review'] or c['status'] not in ('accepted','reviewed'):raise ValueError('Unaccepted chunk')
            seg=self.by_segment[c['segment_id']]
            if c['text']!=seg['text'][c['char_start']:c['char_end']]:raise ValueError('Broken segment offsets')
            for key in ('source_sha256','document','source_path','location_type','location'):
                if c[key]!=seg[key]:raise ValueError('Broken source lineage: '+key)
            if c['source_sha256'] not in scopes:raise ValueError('Missing explicit document scope: '+c['document'])
        if len({point_id(c['chunk_id']) for c in chunks})!=len(chunks):raise ValueError('Point ID collision')

    @classmethod
    def from_ingestion(cls,output,scope_file):
        snapshot,report=load_current(output,require_ready=True)
        scopes=json.loads(Path(scope_file).read_text(encoding='utf-8'))['documents']
        used={c['source_sha256'] for c in jsonl(snapshot/'chunks.jsonl')}
        selected={}
        for key in used:
            if key not in scopes:raise ValueError('Annotate scope for new document SHA-256 '+key)
            s=scopes[key]
            if not {'org_ids','site_ids','required_permissions','scope_note'}.issubset(s):raise ValueError('Invalid scope schema')
            if not s['required_permissions'] or not s['org_ids']:raise ValueError('Explicit organization and permissions required')
            selected[key]=s
        return cls(jsonl(snapshot/'chunks.jsonl'),jsonl(snapshot/'segments.jsonl'),selected),snapshot,report

    def fingerprint(self,spec):
        return sha(canonical({'chunks':self.chunks,'segments':self.segments,'scopes':self.scopes,'index_spec':spec}))

    def allowed_ids(self,ctx):
        return {c['chunk_id'] for c in self.chunks if allowed(self.scopes[c['source_sha256']],ctx)}
