#!/usr/bin/env python3
"""Portfolio Desk mailer: builds the stock dashboard and emails it once a day.

Runs on GitHub Actions after the US close.

Sections: unrealized P/L from portfolio.csv, positions, news and analyst
ratings per holding, and the most active stocks.

Quotes come from Yahoo Finance through yfinance, so they are delayed and each
one is labelled with its own date.

Usage:
    python dashboard_mailer.py            # send if it is after the close on a weekday
    python dashboard_mailer.py --force    # build and send now
    python dashboard_mailer.py --dry-run  # write dashboard.html, send nothing
"""
import argparse
import csv
import datetime as dt
import html
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import yfinance as yf

NY = ZoneInfo("America/New_York")

# The workflow fires twice a day (US daylight and standard time). Only the
# firing that lands in this New York window sends, so you get one email a day
# all year. The US market closes at 16:00 New York time.
SEND_WINDOW = (16 * 60, 16 * 60 + 45)


# ----------------------------------------------------------------- schedule
def in_send_window(now=None):
    now = now or dt.datetime.now(NY)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return SEND_WINDOW[0] <= minutes < SEND_WINDOW[1]


# ---------------------------------------------------------------- portfolio
def load_portfolio(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items()}
            if not r.get("ticker"):
                continue
            rows.append({
                "ticker": r["ticker"].upper(),
                "shares": float(r["shares"]),
                "avg_cost": float(r["avg_cost"]),
                "buy_date": r.get("buy_date", ""),
            })
    return rows


# ------------------------------------------------------------------- quotes
def parse_news(item):
    c = item.get("content", item)
    provider = c.get("provider") or {}
    link = c.get("canonicalUrl") or c.get("clickThroughUrl") or {}
    return {
        "title": c.get("title") or "",
        "source": provider.get("displayName") or c.get("publisher") or "",
        "when": (c.get("pubDate") or c.get("displayTime") or "")[:10],
        "url": link.get("url") if isinstance(link, dict) else c.get("link"),
    }


def get_holding(pos):
    sym = pos["ticker"]
    out = dict(pos, name=sym, price=None, change=None, as_of=None, rating=None,
               target=None, analysts=None, pe=None, news=[], error=None)
    try:
        t = yf.Ticker(sym)
        hist = t.history(period="7d", interval="1d", auto_adjust=False)
        hist = hist.dropna(subset=["Close"])
        if hist.empty:
            out["error"] = "no price data"
            return out
        out["price"] = float(hist["Close"].iloc[-1])
        out["as_of"] = hist.index[-1].date().isoformat()
        if len(hist) > 1:
            prev = float(hist["Close"].iloc[-2])
            out["change"] = (out["price"] / prev - 1) * 100 if prev else None
    except Exception as exc:  # network or parsing problem
        out["error"] = str(exc)[:120]
        return out
    try:
        info = t.info or {}
        out["name"] = info.get("shortName") or sym
        out["rating"] = info.get("recommendationKey")
        out["target"] = info.get("targetMeanPrice")
        out["analysts"] = info.get("numberOfAnalystOpinions")
        out["pe"] = info.get("trailingPE")
    except Exception:
        pass
    try:
        out["news"] = [parse_news(n) for n in (t.news or [])[:3]]
    except Exception:
        pass
    return out


def get_trending(limit=8):
    try:
        res = yf.screen("most_actives", count=limit)
        rows = []
        for q in res.get("quotes", [])[:limit]:
            rows.append({
                "symbol": q.get("symbol"),
                "name": q.get("shortName") or q.get("symbol"),
                "price": q.get("regularMarketPrice"),
                "change": q.get("regularMarketChangePercent"),
                "volume": q.get("regularMarketVolume"),
            })
        return rows
    except Exception as exc:
        print(f"trending list unavailable: {exc}", file=sys.stderr)
        return []


# ------------------------------------------------------------------- render
C = {"fg": "#14202b", "muted": "#5d6b78", "line": "#dde3e8", "bg": "#f3f5f7",
     "card": "#ffffff", "gain": "#0f7a45", "loss": "#b3261e", "accent": "#0b6e7f", "warn": "#8a5a00"}
FONT = "font-family:Arial,Helvetica,sans-serif;"
MONO = "font-family:Consolas,Menlo,monospace;"


def usd(x, d=2):
    return f"{'-' if x < 0 else ''}${abs(x):,.{d}f}"


def signed_usd(x, d=2):
    return ("+" if x > 0 else "") + usd(x, d)


def pct(x):
    return f"{'+' if x > 0 else ''}{x:.2f}%"


def color(x):
    return C["gain"] if x > 0 else C["loss"] if x < 0 else C["muted"]


def esc(x):
    return html.escape(str(x if x is not None else ""))


def th(label, left=False):
    return (f'<th style="{FONT}font-size:11px;text-transform:uppercase;color:{C["muted"]};'
            f'text-align:{"left" if left else "right"};padding:6px 8px;border-bottom:1px solid {C["line"]}">{label}</th>')


def td(content, left=False, extra=""):
    return (f'<td style="{FONT}font-size:13px;text-align:{"left" if left else "right"};padding:8px;'
            f'border-bottom:1px solid {C["line"]};{extra}">{content}</td>')


def section(title, body):
    return (f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;background:{C["card"]};'
            f'border:1px solid {C["line"]}"><tr><td style="padding:14px">'
            f'<div style="{FONT}font-size:12px;font-weight:bold;letter-spacing:1px;text-transform:uppercase;'
            f'color:{C["muted"]};margin-bottom:8px">{title}</div>{body}</td></tr></table>')


def render(holdings, trending, now):
    priced = [h for h in holdings if h["price"] is not None]
    for h in holdings:
        h["basis"] = h["shares"] * h["avg_cost"]
        if h["price"] is not None:
            h["value"] = h["shares"] * h["price"]
            h["pl"] = h["value"] - h["basis"]
            h["plp"] = h["pl"] / h["basis"] * 100
    mv = sum(h["value"] for h in priced)
    cb = sum(h["basis"] for h in priced)
    pl = mv - cb
    plp = pl / cb * 100 if cb else 0.0

    kpi = (f'<table width="100%" cellpadding="0" cellspacing="0"><tr>'
           f'<td style="{FONT}padding:4px 16px 4px 0"><div style="font-size:12px;color:{C["muted"]}">Unrealized P/L</div>'
           f'<div style="{MONO}font-size:26px;color:{color(pl)}">{signed_usd(pl)}</div>'
           f'<div style="{MONO}font-size:12px;color:{color(pl)}">{pct(plp)} on cost</div></td>'
           f'<td style="{FONT}padding:4px 16px"><div style="font-size:12px;color:{C["muted"]}">Market value</div>'
           f'<div style="{MONO}font-size:20px">{usd(mv, 0)}</div></td>'
           f'<td style="{FONT}padding:4px 0 4px 16px"><div style="font-size:12px;color:{C["muted"]}">Cost basis</div>'
           f'<div style="{MONO}font-size:20px">{usd(cb, 0)}</div></td></tr></table>')

    rows = "<tr>" + th("Holding", True) + th("Shares") + th("Avg cost") + th("Last") + th("Day") + th("Value") + th("P/L") + th("Return") + "</tr>"
    for h in holdings:
        if h["price"] is None:
            rows += ("<tr>" + td(f'<b>{esc(h["ticker"])}</b>', True) + td(f'{h["shares"]:g}') + td(usd(h["avg_cost"]))
                     + td(f'no quote ({esc(h["error"])})', False, f"color:{C['warn']}") + td("") + td("") + td("") + td("") + "</tr>")
            continue
        day = pct(h["change"]) if h["change"] is not None else "-"
        rows += ("<tr>"
                 + td(f'<b>{esc(h["ticker"])}</b><br><span style="color:{C["muted"]};font-size:11px">{esc(h["name"])}</span>', True)
                 + td(f'{h["shares"]:g}', False, MONO) + td(usd(h["avg_cost"]), False, MONO)
                 + td(f'{usd(h["price"])}<br><span style="color:{C["muted"]};font-size:11px">{esc(h["as_of"])}</span>', False, MONO)
                 + td(day, False, MONO + f"color:{color(h['change'] or 0)}")
                 + td(usd(h["value"]), False, MONO)
                 + td(signed_usd(h["pl"]), False, MONO + f"color:{color(h['pl'])}")
                 + td(pct(h["plp"]), False, MONO + f"color:{color(h['plp'])}") + "</tr>")
    positions = (f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">{rows}</table>'
                 f'<div style="{FONT}font-size:11px;color:{C["muted"]};margin-top:8px">Prices are daily bars from Yahoo Finance; '
                 f'the date under each price is the bar it comes from.</div>')

    news = ""
    for h in holdings:
        label = (h["rating"] or "n/a").replace("_", " ").title()
        meta = []
        if h["target"]:
            meta.append(f'avg target {usd(h["target"], 0)}')
        if h["analysts"]:
            meta.append(f'{h["analysts"]} analysts')
        if h["pe"]:
            meta.append(f'P/E {h["pe"]:.1f}')
        news += (f'<div style="{FONT}margin-bottom:12px"><b style="font-size:13px">{esc(h["ticker"])}</b> '
                 f'<span style="font-size:12px;color:{C["accent"]};font-weight:bold">{esc(label)}</span> '
                 f'<span style="font-size:12px;color:{C["muted"]}">{esc(" · ".join(meta))}</span>')
        for n in h["news"]:
            title = esc(n["title"])
            if n.get("url"):
                title = f'<a href="{esc(n["url"])}" style="color:{C["fg"]}">{title}</a>'
            news += (f'<div style="font-size:13px;margin-top:4px">{title} '
                     f'<span style="font-size:11px;color:{C["muted"]}">{esc(n["source"])} · {esc(n["when"])}</span></div>')
        if not h["news"]:
            news += f'<div style="font-size:12px;color:{C["muted"]};margin-top:4px">No headlines returned.</div>'
        news += "</div>"

    trend = ""
    if trending:
        r = "<tr>" + th("Stock", True) + th("Price") + th("Change") + th("Volume") + "</tr>"
        for q in trending:
            chg = q["change"] or 0
            vol = f'{q["volume"] / 1e6:.1f}M' if q["volume"] else "-"
            r += ("<tr>" + td(f'{esc(q["name"])} <span style="color:{C["muted"]}">{esc(q["symbol"])}</span>', True)
                  + td(usd(q["price"]) if q["price"] else "-", False, MONO)
                  + td(pct(chg), False, MONO + f"color:{color(chg)}") + td(vol, False, MONO) + "</tr>")
        trend = f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">{r}</table>'

    stamp = now.strftime("%a %d %b %Y, %H:%M %Z")
    body = (f'<table width="100%" cellpadding="0" cellspacing="0" style="background:{C["bg"]}"><tr><td align="center" style="padding:16px">'
            f'<table width="720" cellpadding="0" cellspacing="0" style="max-width:720px;width:100%"><tr><td>'
            f'<div style="{FONT}font-size:22px;font-weight:bold;color:{C["fg"]}">Portfolio Desk</div>'
            f'<div style="{FONT}font-size:12px;color:{C["muted"]}">Daily update · {stamp}</div>'
            + section("Unrealized P/L", kpi)
            + section("Positions", positions)
            + section("News and analyst ratings", news)
            + (section("Most active stocks", trend) if trend else "")
            + '</td></tr></table></td></tr></table>')
    page = f'<!doctype html><html><head><meta charset="utf-8"><title>Portfolio Desk</title></head><body style="margin:0">{body}</body></html>'
    return page, pl, plp


# --------------------------------------------------------------------- mail
def send_email(subject, html_body):
    need = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing secrets: {', '.join(missing)}")
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or 465)
    user = os.environ["SMTP_USER"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM") or user
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content("This email needs an HTML-capable mail client.")
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, os.environ["SMTP_PASS"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(user, os.environ["SMTP_PASS"])
            s.send_message(msg)


def run(portfolio="portfolio.csv", force=False, dry_run=False):
    """Build the dashboard and email it. Returns the HTML, or None if skipped.

    Importable from a notebook: dashboard_mailer.run(force=True, dry_run=True).
    """
    now = dt.datetime.now(NY)
    if not force and not in_send_window(now):
        print(f"{now:%a %H:%M %Z}: outside the daily send window (16:00-16:45 ET, weekdays), nothing to send.")
        return None

    holdings = [get_holding(p) for p in load_portfolio(portfolio)]
    page, pl, plp = render(holdings, get_trending(), now)
    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(page)
    subject = f"Portfolio Desk: unrealized P/L {signed_usd(pl, 0)} ({pct(plp)})"
    print(subject)
    if dry_run:
        print("Dry run: wrote dashboard.html, no email sent.")
        return page
    send_email(subject, page)
    print("Email sent.")
    return page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio", default="portfolio.csv")
    ap.add_argument("--force", action="store_true", help="ignore the send window")
    ap.add_argument("--dry-run", action="store_true", help="write dashboard.html and skip the email")
    args = ap.parse_args()
    # A run started by hand from the GitHub Actions tab always sends.
    force = args.force or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    run(args.portfolio, force, args.dry_run)


if __name__ == "__main__":
    main()
