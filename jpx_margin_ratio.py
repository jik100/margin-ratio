#!/usr/bin/env python3
"""
JPX(東証) 銘柄別信用取引残高PDF -> 信用倍率(買残高 ÷ 売残高)を毎日CSVに記録する

対象: プライム / スタンダード / グロース の「小計」行(PDF最終ページ)の 金額(円) 行
      信用倍率 = 買残高(合計) ÷ 売残高(合計)

使い方:
  pip install requests pdfplumber
  python jpx_margin_ratio.py                 # ページに載っている未記録の日付をすべて取得して追記
  python jpx_margin_ratio.py --date 20261102 # 特定の申込日だけ
  python jpx_margin_ratio.py --pdf x.pdf     # ローカルPDFを解析 (日付はファイル名から)
  python jpx_margin_ratio.py --debug         # 最終ページのテキストを debug_*.txt に保存

出力: margin_ratio.csv (1日1行) と、最新20日(--daysで変更可)の折れ線グラフ margin_ratio.html
"""
import argparse
import csv
import io
import json
import re
import sys
import unicodedata
from decimal import Decimal, getcontext
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
import requests

getcontext().prec = 40

# 保存先の基準フォルダ: exe化したときはexeのある場所、通常はこのスクリプトのある場所
BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
PAGE_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (jpx-margin-ratio)"}
MARKETS = [("prime", "プライム"), ("standard", "スタンダード"), ("growth", "グロース"), ("total", "総合計")]
PDF_RE = re.compile(r'href="([^"]*?(\d{8})_mtall\.pdf)"')
LABEL_RE = re.compile(r"(プライム小計|スタンダード小計|グロース小計|総合計|GrandTotal)")
# PDF上の見出し -> 内部キー ("合計 Total" 単体はページ見出しにもあるため対象外)
LABEL2KEY = {"プライム小計": "prime", "スタンダード小計": "standard",
             "グロース小計": "growth", "総合計": "total", "GrandTotal": "total"}
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


def list_pdfs():
    """公表ページから {YYYYMMDD: PDFのURL} を取得"""
    r = requests.get(PAGE_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return {d: urljoin(PAGE_URL, href) for href, d in PDF_RE.findall(r.text)}


def parse_subtotals(text):
    """最終ページのテキストから {market_key: (売残高, 買残高)} を返す(単位:円)"""
    found = {}
    label = None
    for raw in text.splitlines():
        line = unicodedata.normalize("NFKC", raw)
        compact = re.sub(r"\s+", "", line)
        m = LABEL_RE.search(compact)
        if m:
            label = m.group(1)
        if label and "金額" in compact and "Val" in line:
            part = line.split("Val.", 1)[-1]
            toks = [t for t in NUM_RE.findall(part) if "." not in t and "%" not in t]
            nums = [int(t.replace(",", "")) for t in toks]
            # 並び: 売残高, 前日比, (上場比 "-"), 買残高, 前日比, ... ("-"は数字でないので除外済み)
            if len(nums) >= 3:
                found[LABEL2KEY[label]] = (nums[0], nums[2])
                label = None
    return found


def extract(src):
    with pdfplumber.open(src) as pdf:
        text = pdf.pages[-1].extract_text() or ""
    return text, parse_subtotals(text)


def ratio(sell, buy):
    return (Decimal(buy) / Decimal(sell)).quantize(Decimal("1e-10"))


def csv_header():
    cols = ["date"]
    for k, _ in MARKETS:
        cols += [f"{k}_buy", f"{k}_sell", f"{k}_ratio"]
    return cols


def read_existing(path):
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {row["date"] for row in csv.DictReader(f)}


def append_row(path, date, found):
    new = not path.exists()
    if not new:
        with path.open(encoding="utf-8-sig", newline="") as f:
            if next(csv.reader(f), None) != csv_header():
                raise RuntimeError(
                    f"{path} の列構成が現在の版と違います。古いCSVを別名に移してから再実行してください。")
    row = [f"{date[:4]}-{date[4:6]}-{date[6:]}"]
    for k, _ in MARKETS:
        sell, buy = found[k]
        row += [buy, sell, ratio(sell, buy)]
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(csv_header())
        w.writerow(row)
    return row


def process(date, src, out, debug):
    text, found = extract(src)
    if debug:
        (BASE / f"debug_{date}_lastpage.txt").write_text(text, encoding="utf-8")
    missing = [jp for k, jp in MARKETS if k not in found]
    if missing:
        raise RuntimeError(
            f"{date}: 最終ページから {', '.join(missing)} の小計が読み取れませんでした。"
            " --debug で最終ページのテキストを確認してください。"
        )
    row = append_row(out, date, found)
    print(f"{row[0]}  " + "  ".join(f"{jp}={row[3 + 3 * i]}" for i, (_, jp) in enumerate(MARKETS)))


TOOLTIP_JS = """<script>
(function(){
var D=__DATA__;
var tip=document.getElementById('tip'),guide=document.getElementById('guide');
function show(i,e){
  var d=D[i],h='<b>'+d.d+'</b>';
  d.m.forEach(function(m){
    h+='<div class="row"><span class="nm" style="color:'+m.c+'">'+m.n+'</span> '+m.r+
       '<br><span class="sub">買残高 '+m.b+'億円 / 売残高 '+m.s+'億円</span></div>';
  });
  tip.innerHTML=h;tip.style.display='block';
  var w=tip.offsetWidth,hh=tip.offsetHeight,x=e.clientX+14,y=e.clientY+14;
  if(x+w>window.innerWidth-8)x=e.clientX-w-14;
  if(x<8)x=8;
  if(y+hh>window.innerHeight-8)y=window.innerHeight-hh-8;
  if(y<8)y=8;
  tip.style.left=x+'px';tip.style.top=y+'px';
  guide.setAttribute('x1',d.x);guide.setAttribute('x2',d.x);guide.style.display='';
}
function hide(){tip.style.display='none';guide.style.display='none';}
Array.prototype.forEach.call(document.querySelectorAll('.col'),function(el){
  el.addEventListener('mousemove',function(e){show(+el.getAttribute('data-i'),e);});
  el.addEventListener('mouseleave',hide);
  el.addEventListener('click',function(e){show(+el.getAttribute('data-i'),e);});
});
document.addEventListener('click',function(e){if(!e.target.closest||!e.target.closest('.col'))hide();});
})();
</script>"""


COLORS = {"prime": "#2a6fdb", "standard": "#e07b00", "growth": "#1a9a5b", "total": "#8a3ffc"}


def make_html(csv_path, html_path, n=20):
    """CSVの最新n日分の信用倍率(4種類)を折れ線グラフにした単体HTMLを作る"""
    if not csv_path.exists():
        return
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = sorted(csv.DictReader(f), key=lambda r: r["date"])[-n:]
    if not rows:
        return
    W, H, L, R, T, B = (900 if len(rows) <= 7 else 1100), 460, 70, 150, 30, 60
    show_all = len(rows) <= 7  # 点が多いときは数値ラベルを最終日だけにする(マウスを乗せると各点の値が出る)
    vals = [float(r[f"{k}_ratio"]) for k, _ in MARKETS for r in rows]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.12 or 1.0
    lo, hi = max(0.0, lo - pad), hi + pad

    def X(i):
        return L + (W - L - R) * (0.5 if len(rows) == 1 else i / (len(rows) - 1))

    def Y(v):
        return T + (H - T - B) * (1 - (v - lo) / (hi - lo))

    g = []
    for t in range(6):  # 横罫線とY軸目盛
        v = lo + (hi - lo) * t / 5
        g.append(f'<line x1="{L}" x2="{W-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="grid"/>'
                 f'<text x="{L-8}" y="{Y(v)+4:.1f}" text-anchor="end" class="ax">{v:.1f}</text>')
    for i, r in enumerate(rows):  # X軸(日付)
        g.append(f'<text x="{X(i):.1f}" y="{H-B+24}" text-anchor="middle" class="ax">{r["date"][5:].replace("-", "/")}</text>')
    for k, jp in MARKETS:
        pts = [(X(i), Y(float(r[f"{k}_ratio"])), float(r[f"{k}_ratio"])) for i, r in enumerate(rows)]
        c = COLORS[k]
        g.append(f'<polyline fill="none" stroke="{c}" stroke-width="2.5" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)}"/>')
        for j, (x, y, v) in enumerate(pts):
            r_ = 4 if show_all else 3
            g.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r_}" fill="{c}"/>')
            if show_all or j == len(pts) - 1:
                g.append(f'<text x="{x:.1f}" y="{y-9:.1f}" text-anchor="middle" class="lb" fill="{c}">{v:.2f}</text>')
        x, y, v = pts[-1]
        g.append(f'<text x="{x+14:.1f}" y="{y+4:.1f}" class="lg" fill="{c}">{jp}</text>')

    def oku(yen):  # 円 -> 億円 (小数1桁、3桁区切り)
        return f"{int(yen) / 1e8:,.1f}"

    data = []
    for i, r in enumerate(rows):
        data.append({"d": r["date"], "x": round(X(i), 1), "m": [
            {"n": jp, "c": COLORS[k], "r": f'{float(r[f"{k}_ratio"]):.2f}',
             "b": oku(r[f"{k}_buy"]), "s": oku(r[f"{k}_sell"])} for k, jp in MARKETS]})
    step = (W - L - R) / (len(rows) - 1) if len(rows) > 1 else (W - L - R)
    g.append(f'<line id="guide" x1="0" x2="0" y1="{T}" y2="{H-B}" class="guide" style="display:none"/>')
    for i in range(len(rows)):
        g.append(f'<rect class="col" data-i="{i}" x="{X(i) - step / 2:.1f}" y="{T}" width="{step:.1f}" height="{H-T-B}" fill="transparent"/>')
    js = TOOLTIP_JS.replace("__DATA__", json.dumps(data, ensure_ascii=False))

    head = "".join(f"<th>{jp}</th>" for _, jp in MARKETS)
    body = "".join(
        f'<tr><td>{r["date"]}</td>' + "".join(f'<td>{float(r[f"{k}_ratio"]):.2f}</td>' for k, _ in MARKETS) + "</tr>"
        for r in reversed(rows))
    html = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>信用倍率(買残高÷売残高) 直近{len(rows)}日</title>
<style>
:root{{--bg:#fff;--fg:#222;--grid:#ddd;--mut:#666}}
@media (prefers-color-scheme: dark){{:root{{--bg:#1b1b1f;--fg:#eee;--grid:#3a3a40;--mut:#aaa}}}}
body{{background:var(--bg);color:var(--fg);font-family:-apple-system,"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;margin:24px}}
h1{{font-size:1.2rem}} svg{{max-width:100%;height:auto}}
.grid{{stroke:var(--grid)}} .ax{{fill:var(--mut);font-size:12px}} .lb{{font-size:11px;font-weight:600}} .lg{{font-size:13px;font-weight:700}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;margin-top:16px}}
th,td{{border:1px solid var(--grid);padding:6px 12px;text-align:right}}
.col{{cursor:crosshair}} .guide{{stroke:var(--mut);stroke-dasharray:4 4}} .hint{{color:var(--mut);font-size:12px;margin:4px 0}}
#tip{{position:fixed;display:none;pointer-events:none;background:var(--bg);color:var(--fg);border:1px solid var(--grid);border-radius:8px;padding:8px 12px;font-size:13px;line-height:1.5;box-shadow:0 4px 14px rgba(0,0,0,.35);z-index:10;white-space:nowrap}}
#tip .row{{margin-top:6px}} #tip .nm{{font-weight:700}} #tip .sub{{color:var(--mut);font-size:12px}}
</style></head><body>
<h1>信用倍率(買残高÷売残高) 直近{len(rows)}日(申込日ベース)</h1>
<p class="hint">グラフにマウスを乗せる(スマホはタップ)と、各市場の買残高・売残高(億円)が表示されます。</p>
<div class="wrap"><svg viewBox="0 0 {W} {H}" width="{W}" role="img" aria-label="信用倍率の推移">{"".join(g)}</svg></div>
<div class="wrap"><table><tr><th>申込日</th>{head}</tr>{body}</table></div>
<div id="tip"></div>
{js}
</body></html>"""
    html_path.write_text(html, encoding="utf-8")
    print(f"グラフを更新しました: {html_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BASE / "margin_ratio.csv"))
    ap.add_argument("--date", help="申込日 YYYYMMDD (省略時は未記録の日付すべて)")
    ap.add_argument("--pdf", help="ローカルPDFを使う")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--days", type=int, default=20, help="グラフに表示する日数 (既定20)")
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = read_existing(out)

    if a.pdf:
        m = re.search(r"(\d{8})", Path(a.pdf).name)
        if not m:
            sys.exit("ファイル名から日付(YYYYMMDD)を取れません")
        process(m.group(1), a.pdf, out, a.debug)
        return out, a.days

    pdfs = list_pdfs()
    targets = [a.date] if a.date else sorted(pdfs)
    new = 0
    for d in targets:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        if iso in done:
            continue
        if d not in pdfs:
            print(f"{d}: まだページに掲載されていません")
            continue
        r = requests.get(pdfs[d], headers=HEADERS, timeout=120)
        r.raise_for_status()
        process(d, io.BytesIO(r.content), out, a.debug)
        new += 1
    if new == 0:
        print("新しいデータはありません")
    return out, a.days


if __name__ == "__main__":
    out_path, days = main()
    make_html(out_path, out_path.with_suffix(".html"), days)
