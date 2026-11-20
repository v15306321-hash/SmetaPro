"""Форматирование денег, количеств и дат; сумма прописью (рус.)."""
import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

TASHKENT_OFFSET = timedelta(hours=5)  # Asia/Tashkent, UTC+5, без перехода на летнее время


def D(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value).replace(" ", "").replace(",", "."))


def money_round(value) -> Decimal:
    return D(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money(value, currency: str = "сум") -> str:
    v = money_round(value)
    whole = int(v)
    frac = abs(v - whole)
    s = f"{whole:,}".replace(",", ".")  # 660.000 — привычный в Узбекистане формат
    if frac:
        s += "," + f"{frac:.2f}"[2:]
    return f"{s} {currency}" if currency else s


def money0(value, currency: str = "сум") -> str:
    """Целые суммы — для крупных показателей (KPI)."""
    return money(D(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP), currency)


_THOUSANDS = re.compile(r"^-?\d{1,3}(?:[.,]\d{3})+$")
_PLAIN = re.compile(r"^-?\d+(?:[.,]\d+)?$")
_FIRST_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def parse_money(text: str) -> Decimal:
    """Сумма из свободного ввода: «660.000», «660 000 сум», «660000», «1.250.000,50»."""
    s = re.sub(r"(?i)(сум|сўм|so'?m|uzs)", "", str(text)).replace("\u00a0", "").replace(" ", "").strip()
    if "," in s and "." in s:            # 1.250.000,50
        s = s.replace(".", "").replace(",", ".")
    elif _THOUSANDS.match(s):            # 660.000 / 660,000
        s = s.replace(".", "").replace(",", "")
    elif _PLAIN.match(s):
        s = s.replace(",", ".")
    else:
        raise ValueError(text)
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(text)


def parse_qty(text: str) -> Decimal:
    """Количество из свободного ввода: «3», «1,5», «2.5 листа»."""
    m = _FIRST_NUMBER.search(str(text).replace("\u00a0", ""))
    if not m:
        raise ValueError(text)
    return Decimal(m.group(0).replace(",", "."))


def qty(value) -> str:
    v = D(value).normalize()
    s = f"{v:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def local_dt(dt: datetime | None) -> datetime | None:
    return dt + TASHKENT_OFFSET if dt else None


def dt_str(dt: datetime | None) -> str:
    return local_dt(dt).strftime("%d.%m.%Y %H:%M") if dt else "—"


def date_str(dt) -> str:
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        dt = local_dt(dt)
    return dt.strftime("%d.%m.%Y")


# ---------------------------------------------------------------- сумма прописью

_UNITS = [
    ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"],
    ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"],
]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
         "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
             "восемьсот", "девятьсот"]
# (формы: 1, 2-4, 5+), род: 0 — муж, 1 — жен
_SCALES = [
    (("", "", ""), 0),
    (("тысяча", "тысячи", "тысяч"), 1),
    (("миллион", "миллиона", "миллионов"), 0),
    (("миллиард", "миллиарда", "миллиардов"), 0),
]


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    if n == 1:
        return forms[0]
    if 2 <= n <= 4:
        return forms[1]
    return forms[2]


def _triad(n: int, gender: int) -> list[str]:
    words = [_HUNDREDS[n // 100]]
    rest = n % 100
    if 10 <= rest <= 19:
        words.append(_TEENS[rest - 10])
    else:
        words.append(_TENS[rest // 10])
        words.append(_UNITS[gender][rest % 10])
    return [w for w in words if w]


def amount_in_words(value) -> str:
    """Сумма прописью: «Один миллион двести тысяч сум 00 тийин»."""
    v = money_round(value)
    whole = int(v)
    tiyin = int((v - whole) * 100)
    if whole == 0:
        words = ["ноль"]
    else:
        words = []
        scale = 0
        n = whole
        parts = []
        while n > 0 and scale < len(_SCALES):
            triad = n % 1000
            if triad:
                forms, gender = _SCALES[scale]
                chunk = _triad(triad, gender)
                if scale:
                    chunk.append(plural(triad, forms))
                parts.append(chunk)
            n //= 1000
            scale += 1
        for chunk in reversed(parts):
            words.extend(chunk)
    text = " ".join(words)
    text = text[0].upper() + text[1:]
    return f"{text} сум {tiyin:02d} тийин"
