"""Deterministic bounded chunks with exact character offsets in final segment text."""
import re
from ocr_core import sha


def split_chunks(seg, max_chars=1000, overlap=120):
    if max_chars < 200 or not 0 <= overlap < max_chars//2:
        raise ValueError('max_chars >= 200; 0 <= overlap < max_chars/2')
    if seg['status'] not in ('accepted', 'reviewed'):
        return []
    text = seg['text']
    out, start = [], 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start == len(text):
            break
        end = min(len(text), start+max_chars)
        if end < len(text):
            lower = start+max_chars//2
            candidates = [m.end() for m in re.finditer(r'\n\s*\n|(?<=[.!?;])\s+', text[start:end]) if start+m.end() >= lower]
            if candidates:
                end = start+candidates[-1]
            else:
                space = text.rfind(' ', lower, end)
                if space != -1:
                    end = space
        while end > start and text[end-1].isspace():
            end -= 1
        content = text[start:end]
        location = f"{seg['location_type']} {seg['location']}"
        out.append({'chunk_id': sha(f"{seg['segment_id']}:{start}:{end}:{sha(content)}"),
                    'segment_id': seg['segment_id'], 'document': seg['document'],
                    'source_path': seg['source_path'], 'source_sha256': seg['source_sha256'],
                    'location_type': seg['location_type'], 'location': seg['location'],
                    'char_start': start, 'char_end': end, 'text': content,
                    'embedding_text': f"{seg['document']}\n{location}\n{content}",
                    'extraction_method': seg['extraction_method'], 'status': seg['status'],
                    'needs_review': False})
        if end >= len(text):
            break
        next_start = max(start+1, end-overlap)
        if next_start > 0 and not text[next_start-1].isspace():
            while next_start < end and not text[next_start].isspace():
                next_start += 1
        start = next_start
    return out
