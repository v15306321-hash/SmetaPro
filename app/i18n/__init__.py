"""Переводы интерфейса: русский (исходный), узбекский (латиница), казахский.

Тексты в коде остаются русскими. Перевод выполняется на выходе:
  • бот — перед отправкой каждого сообщения (текст, подпись, кнопки) по языку получателя;
  • сайт — при рендере страницы.
Нажатые кнопки на других языках сопоставляются обратно с русскими (`to_source`), поэтому
обработчики бота работают с одними и теми же русскими константами.

Словари uz.json / kk.json: ключ — русская фраза или шаблон с {} на месте подстановок
(в переводе можно менять порядок: {0}, {1}…). Шаблоны применяются первыми, затем фразы
(сначала длинные), уже переведённые фрагменты повторно не обрабатываются.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

LANGS = {"ru": "🇷🇺 Русский", "uz": "🇺🇿 Oʻzbekcha", "kk": "🇰🇿 Қазақша"}
DEFAULT = "ru"
_DIR = Path(__file__).parent


def normalize(lang: str | None) -> str:
    lang = (lang or "").lower()[:2]
    if lang == "kz":
        lang = "kk"
    return lang if lang in LANGS else DEFAULT


class _Lang:
    def __init__(self, data: dict[str, str]):
        self.templates: list[tuple[re.Pattern, str]] = []
        phrases: dict[str, str] = {}
        for src, dst in data.items():
            if "{}" in src:
                self.templates.append((self._compile(src), dst))
            else:
                phrases[src] = dst
        self.templates.sort(key=lambda t: -len(t[0].pattern))
        self.phrases = phrases
        keys = sorted(phrases, key=len, reverse=True)
        self.phrase_re = re.compile("|".join(self._bounded(k) for k in keys)) if keys else None

    @staticmethod
    def _bounded(key: str) -> str:
        p = re.escape(key)
        if re.match(r"\w", key[0]):
            p = r"(?<!\w)" + p
        if re.match(r"\w", key[-1]):
            p = p + r"(?!\w)"
        return p

    @staticmethod
    def _compile(src: str) -> re.Pattern:
        parts = src.split("{}")
        out = []
        for i, part in enumerate(parts):
            out.append(re.escape(part))
            if i < len(parts) - 1:
                last = i == len(parts) - 2 and parts[-1] == ""
                out.append(r"([^\n<]*)" if last else r"([^\n<]*?)")
        pattern = "".join(out)
        if re.match(r"\w", src[0]):
            pattern = r"(?<!\w)" + pattern
        return re.compile(pattern)

    def phrases_only(self, text: str) -> str:
        if not self.phrase_re or not text:
            return text
        return self.phrase_re.sub(lambda m: self.phrases[m.group(0)], text)

    def translate(self, text: str) -> str:
        if not text:
            return text
        segments: list[tuple[str, bool]] = [(text, False)]  # (текст, уже переведён)
        for pattern, dst in self.templates:
            nxt = []
            for seg, done in segments:
                if done or not pattern.search(seg):
                    nxt.append((seg, done))
                    continue
                pos = 0
                for m in pattern.finditer(seg):
                    if m.start() == m.end():
                        continue
                    nxt.append((seg[pos:m.start()], False))
                    args = [self.phrases_only(g) for g in m.groups()]
                    try:
                        nxt.append((dst.format(*args), True))
                    except (IndexError, KeyError, ValueError):
                        nxt.append((m.group(0), False))
                    pos = m.end()
                nxt.append((seg[pos:], False))
            segments = nxt
        return "".join(seg if done else self.phrases_only(seg) for seg, done in segments)


@lru_cache
def _lang(lang: str) -> _Lang | None:
    path = _DIR / f"{lang}.json"
    if lang == DEFAULT or not path.exists():
        return None
    return _Lang(json.loads(path.read_text()))


def tr(text: str | None, lang: str | None) -> str | None:
    """Перевести русский текст (сообщение, кнопку, страницу) на язык lang."""
    if not text:
        return text
    t = _lang(normalize(lang))
    return t.translate(text) if t else text


# ---------------------------------------------------------------- кнопки: обратное сопоставление

_reverse: dict[str, str] = {}


def remember_button(source: str, translated: str) -> None:
    if source != translated:
        _reverse[translated] = source


def to_source(text: str | None) -> str | None:
    """Текст нажатой кнопки на любом языке → исходная русская кнопка (или сам текст)."""
    if not text:
        return text
    return _reverse.get(text, text)


def warm_up() -> None:
    """Заранее запомнить переводы всех коротких фраз (кнопок) — чтобы кнопки работали
    и после перезапуска, когда клавиатура была отправлена до него."""
    for lang in LANGS:
        t = _lang(lang)
        if not t:
            continue
        for src, dst in t.phrases.items():
            if len(src) <= 40:
                remember_button(src, dst)


# ---------------------------------------------------------------- кэш языка пользователей бота

_user_lang: dict[int, str] = {}


def set_user_lang(tg_id: int | None, lang: str | None) -> None:
    if tg_id:
        _user_lang[tg_id] = normalize(lang)


def user_lang(tg_id: int | None) -> str | None:
    return _user_lang.get(tg_id) if tg_id else None
