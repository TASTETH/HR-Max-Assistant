import re
import snowballstemmer
from rank_bm25 import BM25Okapi

VERSION='ru-snowball-v1'
STEMMER=snowballstemmer.stemmer('russian')
TOKENS=re.compile(r'[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}|(?:https?://)?[\w-]+(?:\.[\w-]+){2,}|\d+(?:[.:-]\d+)+|[a-zа-я0-9]+(?:[-_][a-zа-я0-9]+)*',re.I)


def prepare(text):
    out=[]
    for token in TOKENS.findall(text.lower().replace('ё','е')):
        out.append(STEMMER.stemWord(token) if re.fullmatch('[а-я]{4,}',token) else token)
    return out


class LexicalIndex:
    def __init__(self,chunks):
        self.ids=[c['chunk_id'] for c in chunks]
        self.tokens=[prepare(c['embedding_text']) for c in chunks]
        self.sets=[set(t) for t in self.tokens]
        self.bm25=BM25Okapi(self.tokens)

    def search(self,query,allowed_ids,limit):
        tokens=prepare(query);q=set(tokens)
        if not q:return []
        scores=self.bm25.get_scores(tokens)
        candidates=[(cid,float(scores[i])) for i,cid in enumerate(self.ids) if cid in allowed_ids and q.intersection(self.sets[i])]
        return sorted(candidates,key=lambda x:(-x[1],x[0]))[:limit]


def rrf(dense,bm25,k=60):
    totals={};details={}
    for name,listing in [('dense',dense),('bm25',bm25)]:
        seen=set()
        for rank,(cid,score) in enumerate(listing,1):
            if cid in seen:continue
            seen.add(cid);totals[cid]=totals.get(cid,0)+1/(k+rank)
            details.setdefault(cid,{})[name]={'rank':rank,'score':score}
    return [{'chunk_id':cid,'rrf_score':score,'retrieval':details[cid]} for cid,score in sorted(totals.items(),key=lambda x:(-x[1],x[0]))]
