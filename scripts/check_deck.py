#!/usr/bin/env python3
"""check_deck.py - проверка согласованности HTML-презентации с реестром фактов.

Только стандартная библиотека Python 3.8+.

Команды:
  check  deck.html facts.json            проверить; код возврата 1, если есть ERROR
  sync   deck.html facts.json            пересчитать производные факты и обновить текст
                                         во ВСЕХ местах data-fact (+ счётчик слайдов)
  where  deck.html facts.json F12        где используется факт, что от него зависит
  grep   deck.html "текст или regex"     найти упоминания по слайдам (для поиска
                                         устаревших формулировок после правок)

Общие флаги check: --json, --strict (WARN тоже ломает), --max-words N,
--max-bullets N, --max-rows N.
"""
import argparse
import ast
import html
import json
import re
import sys
from html.parser import HTMLParser

NBSP = "\u00a0"
VOID = {"br", "hr", "img", "meta", "link", "input", "source", "area", "base",
        "col", "embed", "param", "track", "wbr"}
SKIP_TEXT = {"script", "style", "title"}
ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

UP_WORDS = re.compile(r"(вырос|рост|увелич|прирост|подрос|повысил|выросл)", re.I)
DOWN_WORDS = re.compile(r"(снизил|сниж|упал|падени|сократ|уменьш|просел|спад)", re.I)
SLIDE_REF = re.compile(r"слайд(?:а|е|у|ом|ы|ов)?\s*(?:№\s*)?(\d+)", re.I)
DATE_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b")
NUM_RE = re.compile(
    r"(?<![\w.,/:\-\u2212])([-+\u2212]?(?:\d{1,3}(?:[\u00a0\u202f ]\d{3})+|\d+)(?:[.,]\d+)?)"
    r"(\s?(?:%|₽|руб\.?|тыс\.?|млн\.?|млрд\.?|шт\.?|п\.\s?п\.))?")
PLACEHOLDER_RE = re.compile(
    r"\{[^}]{0,80}\}|\[\s*(?:…|\.\.\.|TODO[^\]]*|вставить[^\]]*)\s*\]|\bTODO\b|\bFIXME\b|\bXXX\b|"
    r"Lorem ipsum|ЗАМЕНИТЬ|\?\?\?", re.I)


def norm(s):
    s = (s or "").replace(NBSP, " ").replace("\u202f", " ")
    s = s.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# Реестр фактов
# --------------------------------------------------------------------------
class FactError(Exception):
    pass


def load_facts(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    meta = data.get("meta", {}) if isinstance(data, dict) else {}
    facts = data.get("facts", None) if isinstance(data, dict) else None
    if facts is None:
        facts = {k: v for k, v in data.items() if k != "meta"}
    return meta, facts


def safe_eval(expr, getv):
    tree = ast.parse(expr, mode="eval")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) \
                and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.Name):
            return getv(n.id)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if b == 0:
                raise FactError("деление на ноль в формуле «%s»" % expr)
            return a / b
        raise FactError("недопустимая формула «%s» (разрешены + - * / скобки, числа и id фактов)" % expr)

    return ev(tree)


def compute_values(facts):
    """Возвращает (values, errors). Производные факты считаются по полю derive."""
    values, errors, state = {}, [], {}

    def get(fid):
        if fid in values:
            return values[fid]
        if fid not in facts:
            raise FactError("формула ссылается на неизвестный факт %s" % fid)
        if state.get(fid) == "busy":
            raise FactError("циклическая зависимость через %s" % fid)
        state[fid] = "busy"
        f = facts[fid]
        if f.get("derive"):
            v = safe_eval(f["derive"], get)
        else:
            if "value" not in f:
                raise FactError("у факта %s нет value и derive" % fid)
            v = f["value"]
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise FactError("факт %s: value должно быть числом или строкой" % fid)
        state[fid] = "done"
        values[fid] = v
        return v

    for fid in facts:
        try:
            get(fid)
        except (FactError, SyntaxError) as exc:
            errors.append((fid, str(exc)))
            state[fid] = "done"
    return values, errors


def fmt_value(fid, f, v, meta):
    if isinstance(v, str):
        return v
    scale = f.get("scale", 1) or 1
    v = v / scale
    dec = int(f.get("decimals", 0))
    s = "{:,.{d}f}".format(abs(v), d=dec)
    s = s.replace(",", NBSP).replace(".", meta.get("decimal", ","))
    if round(abs(v), dec) == 0:
        sign = ""
    elif v < 0:
        sign = "\u2212"
    elif f.get("signed"):
        sign = "+"
    else:
        sign = ""
    unit = f.get("unit", "")
    sep = f.get("unit_sep", "" if unit == "%" else NBSP) if unit else ""
    return sign + s + sep + unit


def display_map(facts, values, meta):
    out = {}
    for fid, f in facts.items():
        if fid in values:
            out[fid] = fmt_value(fid, f, values[fid], meta)
    return out


def canon_number(text):
    """'1 235,5 ₽' -> '1235.5'; '−12,4 %' -> '-12.4'. None, если чисел нет."""
    t = norm(text)
    m = re.match(r"^([-+]?)\s?((?:\d{1,3}(?: \d{3})+|\d+)(?:[.,]\d+)?)", t)
    if not m:
        return None
    num = m.group(2).replace(" ", "").replace(",", ".")
    sign = "-" if m.group(1) == "-" else ""
    return sign + num


# --------------------------------------------------------------------------
# Разбор HTML
# --------------------------------------------------------------------------
class Deck(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.slides = []
        self.cur = None
        self.fact_uses = []
        self.trends = []
        self.links = []
        self.ids = {}
        self.dup_ids = []
        self.refs = []
        self.totals = []
        self.untracked = []
        self.external = []
        self.img_no_alt = []

    # ----- helpers
    def _in_skip(self):
        return any(e["tag"] in SKIP_TEXT for e in self.stack)

    def _finalize(self, e):
        e["raw"] = "".join(e["text"])
        if e.get("heading") and e.get("slide_rec") is not None:
            e["slide_rec"]["headings"].append(norm(e["raw"]))
        if e.get("link") is not None:
            e["link"]["text"] = norm(e["raw"])
        if e.get("slide"):
            self.cur = None

    # ----- parser callbacks
    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        line = self.getpos()[0]
        for anc in self.stack:
            if anc.get("fact"):
                anc["has_child"] = True
        if "id" in a:
            if a["id"] in self.ids:
                self.dup_ids.append((a["id"], line))
            else:
                self.ids[a["id"]] = line
        for key in ("src", "href"):
            val = a.get(key, "")
            if tag in ("img", "script", "link", "source", "iframe", "video", "audio") \
                    and re.match(r"^(https?:)?//", val):
                self.external.append((tag, val, line))
        elem = {"tag": tag, "attrs": a, "line": line, "text": [], "has_child": False,
                "slide_idx": self.cur["index"] if self.cur else 0}
        classes = a.get("class", "").split()
        if tag == "section" and "slide" in classes:
            rec = {"index": len(self.slides) + 1, "id": a.get("id", ""),
                   "title": a.get("data-title"), "role": a.get("data-role", ""),
                   "period": a.get("data-period"), "line": line, "headings": [],
                   "text": [], "li": 0, "tr": 0}
            self.slides.append(rec)
            self.cur = rec
            elem["slide"] = True
            elem["slide_idx"] = rec["index"]
        if self.cur is not None:
            if tag == "li":
                self.cur["li"] += 1
            elif tag == "tr":
                self.cur["tr"] += 1
            elif tag == "img" and not a.get("alt", "").strip() and "data-decor" not in a:
                self.img_no_alt.append((self.cur["index"], line))
        if tag in ("h1", "h2") and self.cur is not None:
            elem["heading"] = True
            elem["slide_rec"] = self.cur
        if "data-fact" in a:
            elem["fact"] = a["data-fact"].strip()
            self.fact_uses.append(elem)
        if "data-trend" in a:
            elem["trend"] = a["data-trend"].strip()
            self.trends.append(elem)
        if "data-total-slides" in a:
            elem["total"] = True
            self.totals.append(elem)
        if "data-agenda" in a:
            elem["agenda"] = True
        if tag == "a" and a.get("href", "").startswith("#"):
            rec = {"href": a["href"][1:], "line": line, "text": "",
                   "agenda": any(x.get("agenda") for x in self.stack),
                   "slide_idx": elem["slide_idx"]}
            self.links.append(rec)
            elem["link"] = rec
        if tag not in VOID:
            self.stack.append(elem)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                while len(self.stack) > i:
                    self._finalize(self.stack.pop())
                return

    def handle_data(self, data):
        if self._in_skip():
            return
        for e in self.stack:
            e["text"].append(data)
        if self.cur is None:
            return
        self.cur["text"].append(data)
        line = self.getpos()[0]
        href = None
        for e in reversed(self.stack):
            if e.get("link") is not None:
                href = e["link"]["href"]
                break
        for m in SLIDE_REF.finditer(data):
            self.refs.append({"slide_idx": self.cur["index"], "line": line,
                              "n": int(m.group(1)), "href": href})
        shielded = any(
            e.get("fact") or e.get("total") or "data-static" in e["attrs"]
            or "data-slide-num" in e["attrs"] for e in self.stack)
        if shielded:
            return
        clean = SLIDE_REF.sub(" ", DATE_RE.sub(" ", data))
        for m in NUM_RE.finditer(clean):
            num, unit = m.group(1), m.group(2)
            digits = re.sub(r"\D", "", num)
            has_dec = bool(re.search(r"[.,]\d", num))
            if len(digits) == 4 and digits[:2] in ("19", "20") and not unit and not has_dec:
                continue
            if not (unit or has_dec or len(digits) >= 3):
                continue
            self.untracked.append((self.cur["index"], line, norm(m.group(0)), canon_number(num)))

    def close(self):
        super().close()
        while self.stack:
            self._finalize(self.stack.pop())


def parse_deck(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    d = Deck()
    d.feed(src)
    d.close()
    return d, src


def slide_label(deck, idx):
    if idx and idx <= len(deck.slides):
        s = deck.slides[idx - 1]
        t = s["title"] or (s["headings"][0] if s["headings"] else "")
        return "слайд %d «%s»" % (idx, t) if t else "слайд %d" % idx
    return "вне слайдов"


# --------------------------------------------------------------------------
# Проверки
# --------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.items = []

    def add(self, level, code, msg, where=""):
        self.items.append({"level": level, "code": code, "where": where, "message": msg})

    def errors(self):
        return [i for i in self.items if i["level"] == "ERROR"]

    def warns(self):
        return [i for i in self.items if i["level"] == "WARN"]


def check_registry(facts, values, errs, rep):
    for fid, f in facts.items():
        if not ID_RE.match(fid):
            rep.add("ERROR", "E-REG-ID", "id факта «%s» должен быть латиницей/цифрами/_ и не начинаться с цифры" % fid)
        if f.get("derive") and "value" in f:
            rep.add("WARN", "W-REG-STALE", "%s: задано и derive, и value; value игнорируется, удали его" % fid)
        if "label" not in f:
            rep.add("WARN", "W-REG-LABEL", "%s: нет label (человекочитаемого названия)" % fid)
    for fid, msg in errs:
        rep.add("ERROR", "E-REG-CALC", "%s: %s" % (fid, msg))


def run_checks(deck, facts, values, meta, args):
    rep = Report()
    disp = display_map(facts, values, meta)
    n = len(deck.slides)

    if n == 0:
        rep.add("ERROR", "E-NOSLIDES", "не найдено ни одного <section class=\"slide\">")
        return rep

    # --- структура слайдов
    for i, s in enumerate(deck.slides, 1):
        where = slide_label(deck, i) + " (строка %d)" % s["line"]
        if s["id"] != "s%d" % i:
            rep.add("ERROR", "E-SLIDE-ID",
                    "id слайда «%s», ожидается «s%d»: после вставки/удаления слайдов перенумеруй id и ссылки на них" % (s["id"], i), where)
        if not s["title"]:
            rep.add("WARN", "W-NOTITLE", "нет data-title (каноническое название слайда для оглавления)", where)
        if not s["headings"]:
            rep.add("WARN", "W-NOHEAD", "в слайде нет заголовка h1/h2", where)
        elif s["title"] and norm(s["headings"][0]) != norm(s["title"]):
            rep.add("ERROR", "E-TITLE",
                    "заголовок «%s» расходится с data-title «%s»: исправили одно, забыли другое" % (s["headings"][0], s["title"]), where)
        words = len(re.findall(r"\w+", " ".join(s["text"])))
        if words > args.max_words:
            rep.add("WARN", "W-DENSE", "слов: %d (лимит %d), раздели слайд или убери лишнее" % (words, args.max_words), where)
        if s["li"] > args.max_bullets:
            rep.add("WARN", "W-DENSE", "пунктов списка: %d (лимит %d)" % (s["li"], args.max_bullets), where)
        if s["tr"] > args.max_rows:
            rep.add("WARN", "W-DENSE", "строк таблицы: %d (лимит %d), вынеси в приложение" % (s["tr"], args.max_rows), where)
        if not "".join(s["text"]).strip():
            rep.add("ERROR", "E-EMPTY", "пустой слайд", where)
        raw_text = " ".join(s["text"])
        for m in PLACEHOLDER_RE.finditer(raw_text):
            rep.add("ERROR", "E-PLACEHOLDER", "остался заполнитель «%s»" % norm(m.group(0)), where)

    seen = {}
    for i, s in enumerate(deck.slides, 1):
        t = norm(s["title"] or "")
        if t and t in seen:
            rep.add("WARN", "W-DUPTITLE", "название повторяет слайд %d" % seen[t], slide_label(deck, i))
        seen.setdefault(t, i)
    for dup, line in deck.dup_ids:
        rep.add("ERROR", "E-DUPID", "повторяющийся id «%s»" % dup, "строка %d" % line)
    for tag, val, line in deck.external:
        rep.add("WARN", "W-EXT", "внешний ресурс %s (файл должен быть самодостаточным)" % val, "строка %d" % line)
    for idx, line in deck.img_no_alt:
        rep.add("WARN", "W-ALT", "картинка без alt", slide_label(deck, idx) + " (строка %d)" % line)

    # --- счётчик слайдов
    for e in deck.totals:
        txt = norm(e["raw"])
        if txt != str(n):
            rep.add("ERROR", "E-TOTAL", "счётчик слайдов показывает «%s», фактически слайдов %d (исправляет sync)" % (txt, n),
                    "строка %d" % e["line"])

    # --- ссылки и оглавление
    ids = deck.ids
    agenda_links = [l for l in deck.links if l["agenda"]]
    for l in deck.links:
        if l["href"] not in ids:
            rep.add("ERROR", "E-LINK", "ссылка #%s ведёт в никуда" % l["href"], "строка %d" % l["line"])
    slide_by_id = {s["id"]: s for s in deck.slides}
    for l in agenda_links:
        tgt = slide_by_id.get(l["href"])
        if tgt is None:
            continue
        want = norm(tgt["title"] or (tgt["headings"][0] if tgt["headings"] else ""))
        if want and l["text"] != want:
            rep.add("ERROR", "E-AGENDA", "в оглавлении «%s», а слайд называется «%s»" % (l["text"], want), "строка %d" % l["line"])
    if agenda_links:
        listed = [l["href"] for l in agenda_links]
        for s in deck.slides[1:]:
            if s["role"] not in ("appendix",) and s["id"] not in listed:
                rep.add("WARN", "W-AGENDA-MISSING", "слайда нет в оглавлении", slide_label(deck, s["index"]))
        order = [int(re.sub(r"\D", "", h) or 0) for h in listed]
        if order != sorted(order):
            rep.add("WARN", "W-AGENDA-ORDER", "порядок пунктов оглавления не совпадает с порядком слайдов")

    # --- ссылки «см. слайд N»
    for r in deck.refs:
        where = slide_label(deck, r["slide_idx"]) + " (строка %d)" % r["line"]
        if r["n"] < 1 or r["n"] > n:
            rep.add("ERROR", "E-SLIDEREF", "«слайд %d», но слайдов только %d" % (r["n"], n), where)
        elif r["href"] and r["href"] in slide_by_id and slide_by_id[r["href"]]["index"] != r["n"]:
            rep.add("ERROR", "E-SLIDEREF", "текст ссылки «слайд %d», а ссылка ведёт на слайд %d" % (r["n"], slide_by_id[r["href"]]["index"]), where)

    # --- использование фактов
    used = {}
    for e in deck.fact_uses:
        fid = e["fact"]
        used.setdefault(fid, []).append(e)
        where = slide_label(deck, e["slide_idx"]) + " (строка %d)" % e["line"]
        if fid not in facts:
            rep.add("ERROR", "E-FACT-UNKNOWN", "факта %s нет в реестре" % fid, where)
            continue
        if e["has_child"]:
            rep.add("ERROR", "E-FACT-NESTED", "внутри data-fact=%s есть вложенные теги: sync не сможет обновить, оставь только текст" % fid, where)
        if fid not in disp:
            continue
        have, want = norm(e["raw"]), norm(disp[fid])
        if have != want:
            rep.add("ERROR", "E-FACT-TEXT", "%s показывает «%s», в реестре «%s» (исправляет sync)" % (fid, have, want), where)
        slide = deck.slides[e["slide_idx"] - 1] if e["slide_idx"] else None
        fp = facts[fid].get("period")
        if slide and slide["period"] and fp and fp != "any" and norm(fp) != norm(slide["period"]):
            rep.add("WARN", "W-PERIOD", "факт %s за период «%s», а слайд помечен «%s»" % (fid, fp, slide["period"]), where)

    # --- ключевые факты и мёртвые факты
    summary_idx = {s["index"] for s in deck.slides if s["role"] == "summary"}
    for fid, f in facts.items():
        uses = used.get(fid, [])
        if not uses:
            rep.add("WARN", "W-UNUSED", "факт %s (%s) нигде не используется: удалён из слайдов, но остался в реестре?" % (fid, f.get("label", "")))
        if f.get("key"):
            in_summary = any(u["slide_idx"] in summary_idx for u in uses)
            elsewhere = any(u["slide_idx"] not in summary_idx for u in uses)
            if not summary_idx:
                rep.add("ERROR", "E-KEY", "есть ключевые факты, но нет слайда с data-role=\"summary\"")
                break
            if not in_summary:
                rep.add("ERROR", "E-KEY", "ключевой факт %s не вынесен в итоговый слайд" % fid)
            elif not elsewhere:
                rep.add("WARN", "W-KEY-EVIDENCE", "ключевой факт %s есть в выводах, но не подтверждён ни на одном слайде с данными" % fid)

    # --- тренды: слова вроде «вырос» должны совпадать со знаком факта
    for e in deck.trends:
        fid = e["trend"]
        where = slide_label(deck, e["slide_idx"]) + " (строка %d)" % e["line"]
        if fid not in values or isinstance(values[fid], str):
            rep.add("ERROR", "E-TREND", "data-trend=%s: нет числового факта" % fid, where)
            continue
        v = values[fid]
        text = e["raw"]
        up, down = bool(UP_WORDS.search(text)), bool(DOWN_WORDS.search(text))
        if v > 0 and down and not up:
            rep.add("ERROR", "E-TREND", "текст говорит о снижении, а %s = %s (рост)" % (fid, disp.get(fid)), where)
        elif v < 0 and up and not down:
            rep.add("ERROR", "E-TREND", "текст говорит о росте, а %s = %s (снижение)" % (fid, disp.get(fid)), where)
        elif v == 0 and (up or down):
            rep.add("WARN", "W-TREND", "изменение %s равно нулю, а в тексте есть рост/снижение" % fid, where)
        elif not up and not down:
            rep.add("WARN", "W-TREND", "в тексте нет слова о направлении изменения (рост/снижение) для %s" % fid, where)
        elif up and down:
            rep.add("WARN", "W-TREND", "в тексте и рост, и снижение: проверь формулировку", where)

    # --- числа вне реестра
    by_num = {}
    for fid, d in disp.items():
        c = canon_number(d)
        if c:
            by_num.setdefault(c.lstrip("+"), []).append(fid)
    grouped = {}
    for idx, line, token, canon in deck.untracked:
        grouped.setdefault(idx, []).append((token, canon, line))
    for idx, toks in grouped.items():
        matches, loose = [], []
        for token, canon, line in toks:
            key = (canon or "").lstrip("+")
            hit = by_num.get(key) or by_num.get(key.lstrip("-"))
            (matches if hit else loose).append((token, hit, line))
        for token, hit, line in matches:
            rep.add("WARN", "W-UNTRACKED-FACT",
                    "число «%s» совпадает с фактом %s, но не размечено data-fact: при правке реестра оно останется старым" % (token, "/".join(hit)),
                    slide_label(deck, idx) + " (строка %d)" % line)
        if loose:
            sample = ", ".join("«%s»" % t for t, _, _ in loose[:6])
            more = " и ещё %d" % (len(loose) - 6) if len(loose) > 6 else ""
            rep.add("WARN", "W-UNTRACKED", "числа вне реестра: %s%s. Занеси в facts.json и разметь data-fact (или пометь data-static, если это не метрика)" % (sample, more),
                    slide_label(deck, idx))
    return rep


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------
def cmd_check(a):
    meta, facts = load_facts(a.facts)
    values, errs = compute_values(facts)
    rep = Report()
    check_registry(facts, values, errs, rep)
    deck, _ = parse_deck(a.deck)
    rep.items += run_checks(deck, facts, values, meta, a).items
    if a.json:
        print(json.dumps({"errors": len(rep.errors()), "warnings": len(rep.warns()), "items": rep.items},
                         ensure_ascii=False, indent=2))
    else:
        for it in rep.items:
            loc = (" | " + it["where"]) if it["where"] else ""
            print("[%s] %s%s: %s" % (it["level"], it["code"], loc, it["message"]))
        print("\nИтого: ошибок %d, предупреждений %d, слайдов %d, фактов в реестре %d"
              % (len(rep.errors()), len(rep.warns()), len(deck.slides), len(facts)))
        if rep.errors():
            print("Есть ошибки. Многие из них снимает: python check_deck.py sync %s %s" % (a.deck, a.facts))
    bad = len(rep.errors()) + (len(rep.warns()) if a.strict else 0)
    return 1 if bad else 0


FACT_TAG = re.compile(
    r"(?P<open><(?P<tag>[A-Za-z][\w-]*)\b(?P<a1>[^>]*?)\bdata-fact\s*=\s*(?P<q>[\"'])(?P<id>[^\"']+)(?P=q)(?P<a2>[^>]*)>)"
    r"(?P<inner>[^<]*)(?P<close></(?P=tag)\s*>)")
TOTAL_TAG = re.compile(
    r"(?P<open><(?P<tag>[A-Za-z][\w-]*)\b[^>]*\bdata-total-slides\b[^>]*>)(?P<inner>[^<]*)(?P<close></(?P=tag)\s*>)")


def cmd_sync(a):
    meta, facts = load_facts(a.facts)
    values, errs = compute_values(facts)
    if errs:
        for fid, msg in errs:
            print("[ERROR] %s: %s" % (fid, msg))
        print("Исправь реестр, затем повтори sync.")
        return 1
    disp = display_map(facts, values, meta)
    deck, src = parse_deck(a.deck)
    changes = []

    def repl_fact(m):
        fid = m.group("id").strip()
        if fid not in disp:
            return m.group(0)
        if norm(html.unescape(m.group("inner"))) == norm(disp[fid]):
            return m.group(0)
        new = html.escape(disp[fid], quote=False).replace(NBSP, "&nbsp;")
        changes.append((fid, norm(html.unescape(m.group("inner"))), norm(disp[fid])))
        return m.group("open") + new + m.group("close")

    def repl_total(m):
        if norm(m.group("inner")) == str(len(deck.slides)):
            return m.group(0)
        changes.append(("слайдов", norm(m.group("inner")), str(len(deck.slides))))
        return m.group("open") + str(len(deck.slides)) + m.group("close")

    out = FACT_TAG.sub(repl_fact, src)
    out = TOTAL_TAG.sub(repl_total, out)
    if changes and not a.dry_run:
        with open(a.deck, "w", encoding="utf-8") as fh:
            fh.write(out)
    for what, old, new in changes:
        print("%s: «%s» -> «%s»" % (what, old, new))
    print("%s: %d изменений" % ("Было бы сделано" if a.dry_run else "Обновлено", len(changes)))
    nested = [e for e in deck.fact_uses if e["has_child"]]
    for e in nested:
        print("[WARN] строка %d: data-fact=%s содержит вложенные теги, пропущено" % (e["line"], e["fact"]))
    print("Дальше: python check_deck.py check %s %s" % (a.deck, a.facts))
    return 0


def cmd_where(a):
    meta, facts = load_facts(a.facts)
    values, errs = compute_values(facts)
    fid = a.fact_id
    if fid not in facts:
        print("Факта %s нет в реестре. Есть: %s" % (fid, ", ".join(sorted(facts))))
        return 1
    disp = display_map(facts, values, meta)
    deck, _ = parse_deck(a.deck)
    f = facts[fid]
    print("%s: %s" % (fid, f.get("label", "")))
    print("  значение: %s%s" % (disp.get(fid, "н/д"), ("  = " + f["derive"]) if f.get("derive") else ""))
    print("  период: %s" % f.get("period", "не задан"))
    deps, queue = [], [fid]
    while queue:
        cur = queue.pop()
        for k, v in facts.items():
            if v.get("derive") and re.search(r"\b%s\b" % re.escape(cur), v["derive"]) and k not in deps:
                deps.append(k)
                queue.append(k)
    if deps:
        print("  от него зависят (пересчитает sync): " + ", ".join(deps))
    uses = [e for e in deck.fact_uses if e["fact"] == fid]
    print("  размечено в презентации: %d" % len(uses))
    for e in uses:
        print("    - %s, строка %d: «%s»" % (slide_label(deck, e["slide_idx"]), e["line"], norm(e["raw"])))
    want = canon_number(disp.get(fid, ""))
    if want:
        loose = [(i, l, t) for i, l, t, c in deck.untracked if (c or "").lstrip("+") == want.lstrip("+")]
        if loose:
            print("  то же число без разметки (исправь вручную или оберни в data-fact):")
            for i, l, t in loose:
                print("    - %s, строка %d: «%s»" % (slide_label(deck, i), l, t))
    for dep in deps:
        du = [e for e in deck.fact_uses if e["fact"] == dep]
        print("  зависимый %s размечен в %d местах" % (dep, len(du)))
    return 0


def cmd_grep(a):
    deck, _ = parse_deck(a.deck)
    try:
        rx = re.compile(a.pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(a.pattern), re.I)
    total = 0
    for s in deck.slides:
        text = norm(" ".join(s["text"]))
        for m in rx.finditer(text):
            lo, hi = max(0, m.start() - 40), min(len(text), m.end() + 40)
            print("%s: …%s…" % (slide_label(deck, s["index"]), text[lo:hi]))
            total += 1
    print("Найдено: %d" % total)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("deck")
    c.add_argument("facts")
    c.add_argument("--json", action="store_true")
    c.add_argument("--strict", action="store_true")
    c.add_argument("--max-words", type=int, default=140)
    c.add_argument("--max-bullets", type=int, default=7)
    c.add_argument("--max-rows", type=int, default=12)
    c.set_defaults(fn=cmd_check)
    s = sub.add_parser("sync")
    s.add_argument("deck")
    s.add_argument("facts")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_sync)
    w = sub.add_parser("where")
    w.add_argument("deck")
    w.add_argument("facts")
    w.add_argument("fact_id")
    w.set_defaults(fn=cmd_where)
    g = sub.add_parser("grep")
    g.add_argument("deck")
    g.add_argument("pattern")
    g.set_defaults(fn=cmd_grep)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
