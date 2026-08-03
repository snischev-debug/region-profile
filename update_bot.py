#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_bot.py — ежемесячный обновлятор regions.json
(проект «Профиль заказчика в регионе», Ростелеком / Башинформсвязь)

Режимы:
  python update_bot.py --mode ratings --filter full   # рейтинги+высказывания (ежемесячно)
  python update_bot.py --mode verify  --filter all    # проверка смены глав (ежеквартально)

Окружение:
  ANTHROPIC_API_KEY  — обязательно
  REGIONS_PATH       — путь к regions.json (по умолчанию ./regions.json)
  MODEL              — по умолчанию claude-sonnet-4-6 (дёшево и достаточно;
                       для глубоких прогонов менять осознанно)

Коммит/пуш выполняет вызывающая среда (GitHub Actions / Railway cron),
скрипт только обновляет файл. Ошибки по одному региону не валят весь прогон.
"""
import os, sys, json, time, argparse, re, urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
KEY = os.environ.get("ANTHROPIC_API_KEY")
PATH = os.environ.get("REGIONS_PATH", "regions.json")
TODAY = time.strftime("%Y-%m-%d")

def ask(prompt, use_search=True, max_tokens=1000):
    body = {"model": MODEL, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    if use_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-api-key": KEY,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    return "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

def parse_json(text):
    clean = re.sub(r"```json|```", "", text).strip()
    s, e = clean.find("{"), clean.rfind("}")
    if s < 0 or e < 0:
        raise ValueError("JSON not found in model reply")
    return json.loads(clean[s:e + 1])

def mark(r, secs):
    r.setdefault("updatedAt", {})
    for s in secs:
        r["updatedAt"][s] = TODAY
    r["lastUpdated"] = TODAY

def upd_ratings(r):
    """Ежемесячно: новые точки АПЭК/Минченко + 1-2 высказывания."""
    p = ('Найди актуальные позиции главы региона «%s» (%s) в рейтинге влияния АПЭК (место) '
         'и «Госсовет 2.0» Минченко (баллы), плюс 1-2 ключевых высказывания/решения главы '
         'за последние 40 дней (стратегия/инвестиции/цифровизация/безопасность/бюджет). '
         'Ответь ТОЛЬКО JSON: {"apek":{"t":"мес гг","v":число}|null,'
         '"minchenko":{"t":"мес гг","v":число}|null,'
         '"items":[{"d":"ГГГГ-ММ","tag":"тема","txt":"суть своими словами 1-2 предложения",'
         '"src":"источник, дата"}]}') % (r["region"], r["name"])
    j = parse_json(ask(p))
    changed = []
    if r.get("ratings"):
        for key, name in (("apek", "АПЭК"), ("minchenko", "Госсовет")):
            v = j.get(key)
            if not v: continue
            s = next((x for x in r["ratings"]["series"] if name in x["name"]), None)
            if s and s["points"] and s["points"][-1]["v"] != v["v"]:
                s["points"].append(v); changed.append(name + "→" + str(v["v"]))
    items = j.get("items") or []
    if items:
        r["statements"] = items + (r.get("statements") or [])
        changed.append("+%d выск." % len(items))
    mark(r, ["ratings", "statements"])
    return changed

def upd_verify(r):
    """Ежеквартально: проверка смены главы, переоценка осей."""
    p = ('Кто сейчас глава региона «%s» (Россия)? Если сменился/врио — укажи нового. '
         'Оцени: X «Хозяйственник(0)—Лидер(100)», Y «Операционщик(0)—Стратег(100)». '
         'Ответь ТОЛЬКО JSON: {"fio":"Имя Фамилия","initials":"ИФ","changed":true|false,'
         '"x":число,"y":число,"label":"тип 2-4 слова","summary":"обоснование 2-3 предложения"}'
         ) % r["region"]
    j = parse_json(ask(p))
    changed = []
    if j.get("changed") or j.get("fio") != r["name"]:
        changed.append("СМЕНА: %s → %s" % (r["name"], j.get("fio")))
        r["name"] = j.get("fio", r["name"])
        r["initials"] = j.get("initials", r["initials"])
    r["x"], r["y"] = j.get("x", r["x"]), j.get("y", r["y"])
    c = r.setdefault("classification", {})
    c["hozLider"], c["opStrat"] = r["x"], r["y"]
    c["label"] = (j.get("label", "") + " · верифицировано ботом").strip()
    c["summary"] = (j.get("summary", "") +
                    " — Верифицировано ботом %s; экспертная оценка, уровень base." % TODAY)
    mark(r, ["profile", "team"])
    return changed

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["ratings", "verify"], default="ratings")
    ap.add_argument("--filter", choices=["full", "all"], default="full",
                    help="full: только исследованные регионы (isDemo=false) — экономия; all: все 89")
    ap.add_argument("--sleep", type=float, default=3.0)
    a = ap.parse_args()
    if not KEY:
        sys.exit("ANTHROPIC_API_KEY is not set")
    regions = json.load(open(PATH, encoding="utf-8"))
    targets = [r for r in regions if a.filter == "all" or not r.get("isDemo")]
    print("Mode=%s, model=%s, targets=%d/%d" % (a.mode, MODEL, len(targets), len(regions)))
    ok = err = 0
    for r in targets:
        try:
            ch = upd_ratings(r) if a.mode == "ratings" else upd_verify(r)
            ok += 1
            print("  ✓ %-45s %s" % (r["region"][:45], "; ".join(ch) or "без изменений"))
        except Exception as e:
            err += 1
            print("  ✗ %-45s %s" % (r["region"][:45], e))
        time.sleep(a.sleep)
    json.dump(regions, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("Done: ok=%d err=%d → %s" % (ok, err, PATH))

if __name__ == "__main__":
    main()
