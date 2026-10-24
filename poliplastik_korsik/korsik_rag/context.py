"""Complete rule spans, same-heading slides, cross-page continuation and provenance."""
import re
from dataclasses import dataclass
from .corpus import allowed
from .types import Source,RagError

ANCHOR=re.compile(r'(?m)^[ \t]*(\d+(?:\.\d+){1,5})\.?[ \t]+')


@dataclass
class Context:
    sources:list
    groups:list
    chars:int
    tokens:int

    def prompt_sources(self):
        return [{'id':s.citation_id,'document':s.document,'location_type':s.location_type,
                 'location':s.location,'text':s.text} for s in self.sources]


class ContextBuilder:
    def __init__(self,corpus,settings,counter):
        self.corpus=corpus;self.s=settings;self.count=counter

    def _spans(self,chunk,ctx):
        seg=self.corpus.by_segment[chunk['segment_id']];doc=seg['source_sha256']
        accessible=lambda s:s and s.get('status') in ('accepted','reviewed') and allowed(self.corpus.scopes[doc],ctx)
        if seg['location_type']=='slide':
            spans=[(seg,0,len(seg['text']))]
            title=lambda s:re.sub(r'\s+',' ',s['text'].splitlines()[0]).strip().lower() if s['text'] else ''
            heading=title(seg)
            for direction in (-1,1):
                n=seg['location']+direction
                while True:
                    adjacent=self.corpus.pages.get((doc,'slide',n))
                    if not accessible(adjacent) or len(heading)<12 or title(adjacent)!=heading:break
                    spans.append((adjacent,0,len(adjacent['text'])));n+=direction
            return spans
        # At this corpus size scanning page offsets is cheap. Use the full document
        # so a local page window cannot hide the end of a list or an exception.
        pages=sorted([p for p in self.corpus.segments if p['source_sha256']==doc and p['location_type']=='page' and accessible(p)],key=lambda p:p['location'])
        text='';offsets=[]
        for p in pages:
            offsets.append((p,len(text),len(text)+len(p['text'])))
            text+=p['text']+'\n\n'
        current=next(start for p,start,end in offsets if p['segment_id']==seg['segment_id'])
        position=current+chunk['char_start'];end_position=current+chunk['char_end']
        anchors=list(ANCHOR.finditer(text));before=[a for a in anchors if a.start()<=position]
        selected=([before[-1]] if before else [])+[a for a in anchors if position<a.start()<end_position]
        if not selected:
            begin=current;end=current+len(seg['text'])
        else:
            bounds=[]
            # Include EVERY section touched by the unchanged OCR chunk, including
            # one starting near its end. Using only char_start loses answers.
            prefixes={'.'.join(a.group(1).split('.')[:2]) for a in selected}
            for prefix in prefixes:
                section=[a for a in anchors if a.group(1)==prefix or a.group(1).startswith(prefix+'.')]
                first=min(a.start() for a in section);last=max(a.start() for a in section)
                after=[a.start() for a in anchors if a.start()>last and not a.group(1).startswith(prefix+'.')]
                bounds.append((first,min(after) if after else len(text)))
            begin=min(a for a,b in bounds);end=max(b for a,b in bounds)
        spans=[]
        for p,start,stop in offsets:
            a=max(begin,start)-start;b=min(end,stop)-start
            if b>a:spans.append((p,a,b))
        return spans

    def build(self,candidates,ctx):
        spans_by_segment={};groups=[]
        for candidate in candidates[:self.s.context_top_k]:
            chunk=self.corpus.by_id[candidate['chunk_id']]
            additions=self._spans(chunk,ctx)
            if sum(b-a for s,a,b in additions)>self.s.rule_max_chars:
                if not spans_by_segment:raise RagError('context_rule_too_long','Complete rule exceeds configured context limit')
                continue
            proposed={key:list(value) for key,value in spans_by_segment.items()}
            for seg,a,b in additions:proposed.setdefault(seg['segment_id'],[]).append((a,b))
            merged={}
            for key,intervals in proposed.items():
                result=[]
                for a,b in sorted(intervals):
                    if result and a<=result[-1][1]:result[-1]=(result[-1][0],max(b,result[-1][1]))
                    else:result.append((a,b))
                merged[key]=result
            texts=[self.corpus.by_segment[key]['text'][a:b] for key,intervals in merged.items() for a,b in intervals]
            chars=sum(map(len,texts));tokens=sum(self.count(t) for t in texts)
            if chars>self.s.context_max_chars or tokens>self.s.context_max_tokens:
                if not spans_by_segment:raise RagError('context_budget','Highest-ranked complete rule does not fit context; no text was truncated')
                continue
            spans_by_segment=merged
            if len(additions)>1 and chunk['location_type']=='slide':
                groups.append({s['segment_id'] for s,a,b in additions})
        sources=[]
        for key,intervals in spans_by_segment.items():
            seg=self.corpus.by_segment[key]
            for a,b in intervals:
                related=[c['chunk_id'] for c in self.corpus.chunks if c['segment_id']==key and c['char_start']<b and c['char_end']>a]
                if not related:raise RagError('source_lineage','Expanded text has no indexed source chunk')
                sources.append(Source(chunk_id=related[0],chunk_ids=related,document=seg['document'],
                    location_type=seg['location_type'],location=seg['location'],segment_id=key,
                    source_sha256=seg['source_sha256'],source_path=seg['source_path'],
                    text=seg['text'][a:b],char_start=a,char_end=b,citation_id=f'S{len(sources)+1}'))
        # If a rule crosses pages, citation validation requires the complete group.
        citation_groups=[sorted(s.citation_id for s in sources if s.segment_id in group) for group in groups]
        return Context(sources,citation_groups,sum(len(s.text) for s in sources),sum(self.count(s.text) for s in sources))
