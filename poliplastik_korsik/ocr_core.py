"""Local Tesseract adapter, cache and evidence. No network calls."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import unicodedata
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageOps

VERSION = '2.1.0'


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode('utf-8') if isinstance(data, str) else data).hexdigest()


def tidy(text: str) -> str:
    # Preserve spelling, dates, numbers and legal wording; never LLM-rewrite OCR.
    text = unicodedata.normalize('NFC', text).replace('\u00a0', ' ').replace('\u00ad', '')
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\v', '\n')
    text = ''.join(c for c in text if c in '\n\t' or unicodedata.category(c) != 'Cc')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n *\n(?: *\n)+', '\n\n', text).strip()


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def image_rgb(image: Image.Image) -> Image.Image:
    if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
        rgba = image.convert('RGBA')
        base = Image.new('RGBA', rgba.size, 'white')
        return Image.alpha_composite(base, rgba).convert('RGB')
    return image.convert('RGB')


def parse_tsv(text: str, scale: float = 1.0) -> dict:
    if not text.startswith('level\tpage_num\t') and not text.startswith('level\tblock_num\t'):
        raise ValueError('Tesseract did not return TSV; check tessdata/config installation')
    paragraphs = defaultdict(lambda: defaultdict(list))
    words = []
    for row in csv.DictReader(io.StringIO(text), delimiter='\t', quoting=csv.QUOTE_NONE):
        token = (row.get('text') or '').strip()
        if not token or row.get('level') != '5':
            continue
        conf = float(row['conf'])
        if conf < 0:
            continue
        box = [round(float(row[k])/scale, 2) for k in ('left', 'top', 'width', 'height')]
        word = {'text': token, 'confidence': round(conf, 2), 'bbox': box}
        words.append(word)
        paragraphs[(row['block_num'], row['par_num'])][row['line_num']].append(token)
    extracted = '\n\n'.join('\n'.join(' '.join(ws) for ws in lines.values()) for lines in paragraphs.values())
    weight = sum(len(w['text']) for w in words)
    conf = sum(w['confidence']*len(w['text']) for w in words)/weight if weight else None
    return {'text': tidy(extracted), 'words': words,
            'confidence': round(conf, 2) if conf is not None else None,
            'low_confidence_ratio': round(sum(w['confidence'] < 50 for w in words)/len(words), 3) if words else 0,
            'median_word_height': statistics.median([w['bbox'][3] for w in words]) if words else None}


class OCR:
    def __init__(self, executable='tesseract', lang='rus+eng', tessdata=None,
                 cache=None, timeout=90, max_pixels=40_000_000):
        self.executable = shutil.which(executable) or executable
        self.lang, self.tessdata = lang, tessdata
        self.cache = Path(cache) if cache else None
        self.timeout, self.max_pixels = timeout, max_pixels
        self.calls = self.hits = 0
        try:
            version = self.command(['--version']).stdout.splitlines()[0]
            langs = self.command(['--list-langs']).stdout.splitlines()[1:]
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('Tesseract недоступен. Укажи --tesseract-path к tesseract.exe.') from exc
        missing = set(lang.split('+')) - {v.strip() for v in langs}
        if missing:
            raise RuntimeError(f'Нет языков Tesseract: {sorted(missing)}; установи rus и eng / проверь --tessdata-dir.')
        # Include actual language model bytes in the cache identity, when reported by Tesseract.
        header = self.command(['--list-langs']).stdout.splitlines()[0]
        match = re.search(r'"(.+?)"', header)
        model_dir = Path(tessdata or (match.group(1) if match else ''))
        models = {}
        for language in lang.split('+'):
            p = model_dir / (language + '.traineddata')
            models[language] = sha(p.read_bytes()) if p.is_file() else 'unresolved'
        self.signature = {'version': version, 'languages': lang, 'models': models, 'adapter': VERSION}

    def command(self, args, **kwargs):
        cmd = [self.executable]
        if self.tessdata:
            cmd += ['--tessdata-dir', str(self.tessdata)]
        cmd += args
        env = dict(os.environ, OMP_THREAD_LIMIT='1')
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding='utf-8', errors='replace', timeout=self.timeout,
                              check=True, env=env, **kwargs)
        if any(marker in result.stderr for marker in ('Failed loading language', 'Error opening data file', "Can't open")):
            raise RuntimeError('Tesseract language/config error: ' + result.stderr[:1000])
        return result

    def recognize(self, image: Image.Image, kind='page') -> dict:
        if image.width*image.height > self.max_pixels:
            raise ValueError('Изображение превышает --max-pixels; уменьши --dpi или проверь исходник.')
        image = image_rgb(image)
        # Hash pixels directly: PNG compression solely for a cache key costs CPU.
        key = sha(image.tobytes() + json.dumps([self.signature, kind, image.size, image.mode], sort_keys=True).encode())
        cache = self.cache / (key + '.json') if self.cache else None
        if cache and cache.is_file():
            try:
                value = json.loads(cache.read_text(encoding='utf-8'))
                if value.get('cache_key') == key:
                    self.hits += 1
                    return value
            except (ValueError, OSError):
                pass
        # Upscale small screenshots to help recognition; retain original-pixel boxes.
        scale = min(3.0, max(1.0, 1800/max(image.size))) if kind == 'image' else 1.0
        grey = ImageOps.autocontrast(ImageOps.grayscale(image))
        if scale != 1:
            grey = grey.resize((round(grey.width*scale), round(grey.height*scale)), Image.Resampling.LANCZOS)
        attempts = []
        with tempfile.TemporaryDirectory(prefix='poly_ocr_') as temp:
            filename = Path(temp)/'input.png'
            grey.save(filename)
            def attempt(psm):
                self.calls += 1
                result = self.command([str(filename), 'stdout', '-l', self.lang,
                                       '--oem', '1', '--psm', str(psm), '-c', 'tessedit_create_tsv=1'])
                value = parse_tsv(result.stdout, scale)
                value['psm'], value['rotation'] = psm, 0
                attempts.append(value)
                return value
            first = attempt(3 if kind == 'page' else 11)
            if kind == 'image' or not first['text'] or (first['confidence'] or 0) < 80:
                attempt(6 if kind == 'page' else 3)
            # OSD only for failed page recognition; do not rotate normal scans speculatively.
            if kind == 'page' and max((x['confidence'] or 0) for x in attempts) < 65:
                try:
                    osd = self.command([str(filename), 'stdout', '-l', 'osd', '--psm', '0']).stdout
                    rot = int(re.search(r'Rotate: (\d+)', osd).group(1))
                    if rot:
                        grey.rotate(-rot, expand=True).save(filename)
                        a = attempt(3); a['rotation'] = rot
                except (subprocess.SubprocessError, AttributeError):
                    pass
        # Avoid selecting a tiny high-confidence fragment instead of the full page.
        def rank(a):
            return len(a['text'])*max(0.05, ((a['confidence'] or 0)/100)**2)
        best = max(attempts, key=rank)
        result = dict(best)
        result['attempts'] = [{k: v for k, v in a.items() if k != 'words'} for a in attempts]
        result['cache_key'] = key
        result['original_size'] = list(image.size)
        if cache:
            write_json(cache, result)
        return result


def ocr_reasons(result: dict, kind: str) -> list[str]:
    text, conf = result['text'], result['confidence']
    reasons = []
    if not text:
        return ['empty_ocr'] if kind == 'page' else []
    letters = sum(c.isalpha() for c in text)
    if (conf or 0) < 75:
        reasons.append('low_ocr_confidence')
    if result['low_confidence_ratio'] > .18:
        reasons.append('many_uncertain_words')
    if kind == 'page' and letters < 40:
        reasons.append('very_little_page_text')
    if letters/max(1, len(text)) < .45:
        reasons.append('suspicious_character_mix')
    if kind == 'image' and len(text) > 250 and (result['median_word_height'] or 0) < 11:
        reasons.append('small_text_in_screenshot')
    lengths = [len(a['text']) for a in result['attempts'] if len(a['text']) > 80]
    if len(lengths) > 1 and min(lengths)/max(lengths) < .55:
        reasons.append('ocr_attempts_disagree')
    return reasons
