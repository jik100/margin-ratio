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

  python jpx_margin_ratio.py --no-stocks     # 銘柄別データの取り込みを行わない(市場別の集計だけ)

出力: margin_ratio.csv (1日1行) と、最新20日(--daysで変更可)の折れ線グラフ margin_ratio.html
      stocks/<銘柄コード4桁>.json (銘柄別の履歴。グラフ右上の検索欄で使う)
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


# ---------------------------------------------------------------- 銘柄別データ
# PDFの各銘柄は2行: 1行目=株数(Shs.)、2行目=金額(Val.)。どちらも
#   売残高, 前日比, 上場比(%または*/-), 買残高, 前日比, 上場比, (一般/制度の売買内訳 8個) の14個で終わる
ISSUE_RE = re.compile(
    r"(?P<head>.*?)(?<!\S)(?P<code>[0-9][0-9A-Z]{4})\s*(?P<isin>[A-Z]{2}[0-9A-Z]{10})\s*"
    r"(?P<kind>株数|金額)\s*(?:Shs|Val)\.\s*(?P<rest>.*)$")
MARK_RE = re.compile(r"(?P<mark>Shs|Val)\.\s*(?P<rest>.*)$")
TOTAL_CNT_RE = re.compile(r"総合計\s*(\d[\d,]*)\s*銘柄")
# 小計・総合計の行(銘柄ではない)。英語の銘柄名が "Total ..." で始まっても除外しないよう、Val./Shs.の直前までを見る
SKIP_JP = re.compile(r"^\s*(?:(?:プライム|スタンダード|グロース|投信等)\s*小計|総合計|制度信用銘柄|貸借銘柄|貸借取引銘柄)")
SKIP_EN = re.compile(
    r"^\s*(?:(?:(?:Prime|Standard|Growth|Investment\s+trusts)\s+sub-?total|total)\s*(?:金額)?\s*Val"
    r"|(?:[A-Za-z]+\s+){0,3}trading\s+issue\b)", re.I)   # 小計・総合計・制度信用/貸借銘柄の集計行
INT_RE = re.compile(r"^\d[\d,]*$")
RATIO_RE = re.compile(r"^(?:-|\*|\d+(?:\.\d+)?%)$")
CODE_RE = re.compile(r"^[0-9]{3}[0-9A-Z][0-9]$")


def tail_numbers(rest):
    """行の数値部分から (売残高, 買残高) を返す。
    並び: 売残高, 前日比, 上場比(0.0% / * / -), 買残高, 前日比, 上場比, 一般/制度の内訳8個 = 14個。
    名前の折り返し断片が前にも後ろにも混ざり得るので、この並びに合う最後の14個を探す。"""
    rest = unicodedata.normalize("NFKC", rest).replace("▲", " ")
    toks = re.sub(r"%(?=\d)", "% ", rest).split()
    for start in range(len(toks) - 14, -1, -1):
        w = toks[start:start + 14]
        if all(RATIO_RE.match(w[i]) if i in (2, 5) else INT_RE.match(w[i]) for i in range(14)):
            return int(w[0].replace(",", "")), int(w[3].replace(",", ""))
    return None


def recover_ids(head):
    """行の左側から (コード5桁, ISIN, コードが確実か) を取り出す。
    長い銘柄名が重なって文字が入り混じっても、コードとISINは半角英数字なので、
    半角英数字だけを順に並べて復元する。名前の文字(小文字、ETNの E,T,N など)が混ざった場合は
    復元はするが「確実でない」と記録し、別の行のコードと一致したときだけ採用する。"""
    s = "".join(ch for ch in head if ch.isascii() and ch.isalnum())
    isin, region, dirty = None, "", False
    m = re.search(r"[A-Z]{2}[0-9A-Z]{10}$", s)
    if m and (m.group()[:2] != "JP" or m.group()[2:].isdigit()):
        isin, region = m.group(), s[:m.start()]
    else:                                   # 日本のISINは JP+数字10桁。間に混ざった名前の文字は除く
        dirty = True
        u = re.sub(r"[a-z]", "", s)         # ISINやコードに小文字はない
        i = u.rfind("JP")
        while i != -1:
            tail = u[i + 2:]
            digits = [c for c in tail if c.isdigit()]
            if len(digits) == 10 and len(tail) <= 20 and all(c.isdigit() or c.isupper() for c in tail):
                isin, region = "JP" + "".join(digits), u[:i]
                break
            i = u.rfind("JP", 0, i)
    if isin is None:
        return None, None, False
    if CODE_RE.match(region[-5:]):
        return region[-5:], isin, not dirty
    r = re.sub(r"[a-z]", "", region)        # コードに小文字はない(名前の文字が混ざったもの)
    r = re.sub(r"[A-Z]+$", "", r)           # ISINの直前に混ざった名前の末尾(ETNなど)
    return (r[-5:] if CODE_RE.match(r[-5:]) else None), isin, False


KINDS = ["普通株式", "受益証券", "投資証券", "優先株式", "出資証券"]
MKTS = ["プライム", "スタンダード", "グロース", "投信等"]


def is_interleave(t, a, b):
    """t が、a と b を(各々の文字の順序を保ったまま)混ぜ合わせた文字列かどうか"""
    n, m = len(a), len(b)
    if len(t) != n + m:
        return False
    dp = [[False] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = True
    for i in range(n + 1):
        for j in range(m + 1):
            if not dp[i][j] or i + j >= len(t):
                continue
            if i < n and t[i + j] == a[i]:
                dp[i + 1][j] = True
            if j < m and t[i + j] == b[j]:
                dp[i][j + 1] = True
    return dp[n][m]


def strip_interleaved(h):
    """銘柄名が長く、右隣の欄の「普通株式」「プライム」と重なって文字が入り混じった末尾を取り除く
    (例: 『…グループ 普プ通ラ株イ式ム』 → 『…グループ』)"""
    h = h.rstrip()
    combos = sorted(((k, m) for k in KINDS for m in MKTS), key=lambda km: -(len(km[0]) + len(km[1])))
    for k, m in combos:
        L = len(k) + len(m)
        if len(h) > L and is_interleave(h[-L:], k, m):
            return h[:-L].rstrip()
    return h


def clean_name(head):
    h = re.sub(r"^[A-Z]\s+", "", head.strip())      # 先頭の売買単位記号
    h = re.sub(r"\s+\S\s*$", "", h)                  # 末尾の 貸/制/他
    h = strip_interleaved(h)
    h = re.sub(r"\s*(プライム|スタンダード|グロース|投信等)\s*$", "", h)
    h = re.sub(r"\s*(普通株式|受益証券|投資証券|優先株式|出資証券)\s*$", "", h)
    h = re.sub(r"\s*(?:普通株式|受益証券|投資証券|優先株式|出資証券)\s*(?:プライム|スタンダード|グロース|投信等)?\s*(?:貸|制|他)?\s*$", "", h)
    return h.strip()


def clean_en_name(head):
    h = re.sub(r"\s*(?:Prime|Standard|Growth|Investment\s*trusts)\s*(?:Loan|Margin|Other)?\s*$", "", head.strip(), flags=re.I)
    return h.strip()


def pick_name(line1, line2, code):
    """日本語名(1行目)を使う。名前が長くて文字が入り混じった行は、英語名(2行目)で代用する"""
    for ln, cleaner in ((line1, clean_name), (line2, clean_en_name)):
        m = ISSUE_RE.search(unicodedata.normalize("NFKC", ln))
        if m and m.group("code") == code:
            nm = cleaner(m.group("head"))
            if nm:
                return nm
    return ""


def parse_issues(pdf):
    """全ページの『株数の行』『金額の行』を集め、ISINで1銘柄ずつ対応づける。
    戻り値: 株数, 金額, 名前, PDFの総合計の銘柄数, 読み取れなかった行のリスト, ISIN"""
    recs, n_total = [], None
    for page in pdf.pages:
        text = page.extract_text() or ""
        for raw in text.splitlines():
            nf = unicodedata.normalize("NFKC", raw)
            mt = TOTAL_CNT_RE.search(nf)
            if mt:
                n_total = int(mt.group(1).replace(",", ""))
            if SKIP_JP.match(nf) or SKIP_EN.match(nf):
                continue
            m = MARK_RE.search(raw)
            if not m:
                continue
            code, isin, clean = recover_ids(raw[:m.start()])
            recs.append({"kind": m.group("mark"), "raw": raw, "code": code, "isin": isin, "clean": clean,
                         "nums": tail_numbers(m.group("rest"))})
        page.flush_cache()

    shs, val, names, isins, bad = {}, {}, {}, {}, []
    by = {"Shs": {}, "Val": {}}
    for r in recs:
        tag = "株数" if r["kind"] == "Shs" else "金額"
        if r["nums"] is None:
            bad.append(f"[{tag}・数値] " + r["raw"])
        elif not (r["isin"] or r["code"]):
            bad.append(f"[{tag}・コード読取不可] " + r["raw"])
        elif (r["isin"] or r["code"]) in by[r["kind"]]:
            bad.append(f"[{tag}・重複] " + r["raw"])
        else:
            by[r["kind"]][r["isin"] or r["code"]] = r
    for key, r1 in by["Shs"].items():
        r2 = by["Val"].get(key)
        if r2 is None:
            bad.append("[金額の行なし] " + r1["raw"])
            continue
        c1, c2 = r1["code"], r2["code"]
        if c1 and c2:
            if c1 == c2:
                code = c1
            elif r1["clean"] != r2["clean"]:
                code = c1 if r1["clean"] else c2        # 確実なほうを採用
            else:
                code = None
        elif c1 and r1["clean"]:
            code = c1
        elif c2 and r2["clean"]:
            code = c2
        else:
            code = None
        if not code:
            bad.append("[コード不確実] " + r2["raw"])  # 誤ったコードで別の銘柄を汚さないよう、採用しない
            continue
        shs[code], val[code] = r1["nums"], r2["nums"]
        names[code] = pick_name(r1["raw"], r2["raw"], code)
        isins[code] = r1["isin"] or r2["isin"]
    for key, r2 in by["Val"].items():
        if key not in by["Shs"]:
            bad.append("[株数の行なし] " + r2["raw"])
    return shs, val, names, n_total, bad, isins


def check_issues(shs, val, n_total, total):
    """読み取り結果をPDFの総合計(銘柄数・売残高・買残高)と照合する。
    返り値: (保存してはいけない問題のリスト, 注意のリスト)
    - 銘柄ごとの合計が総合計を超える、または大きく不足する場合は、読み取りが誤っているので保存しない
    - ごく一部(名前が極端に長いETN/ETFなど)の読み取り漏れは、注意として知らせて保存する"""
    fatal, notes = [], []
    if not val:
        return ["銘柄を1件も読み取れませんでした"], notes
    if not total:
        return ["総合計が読み取れていないため、照合できません"], notes
    sell, buy = sum(v[0] for v in val.values()), sum(v[1] for v in val.values())
    if sell > total[0] or buy > total[1]:
        fatal.append(f"銘柄ごとの合計(売 {sell:,} / 買 {buy:,})が、総合計(売 {total[0]:,} / 買 {total[1]:,})を超えています")
    miss_s, miss_b = total[0] - sell, total[1] - buy
    if miss_s > total[0] * 0.01 or miss_b > total[1] * 0.01:
        fatal.append(f"銘柄ごとの合計が、総合計より1%以上不足しています(売 {miss_s:,} / 買 {miss_b:,})")
    if n_total is not None:
        missing = n_total - len(val)
        if missing < 0:
            fatal.append(f"読み取れた銘柄数 {len(val)} 件が、PDFの総合計 {n_total} 銘柄を超えています")
        elif missing > max(60, n_total * 0.02):
            fatal.append(f"読み取れた銘柄数 {len(val)} 件が、PDFの総合計 {n_total} 銘柄より大きく不足しています")
        elif missing:
            notes.append(f"{missing} 銘柄が読み取れませんでした(売残高の不足 {miss_s:,} 円 / 買残高の不足 {miss_b:,} 円)")
    return fatal, notes


def stock_key(code5):
    return code5[:4] if code5[4] == "0" else code5


def save_issues(sdir, date, shs, val, names, isins=None):
    """銘柄ごとのJSON(stocks/4461.json)に、その日の [売金額, 買金額, 売株数, 買株数] を追記する"""
    sdir.mkdir(parents=True, exist_ok=True)
    iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    saved = conflicts = 0
    for code, (sv, bv) in val.items():
        ss, bs = shs.get(code, (0, 0))
        if sv == 0 and bv == 0 and ss == 0 and bs == 0:
            continue  # 残高がゼロの銘柄は記録しない
        key = stock_key(code)
        fp = sdir / f"{key}.json"
        obj = {"c": key, "n": "", "h": {}}
        if fp.exists():
            try:
                obj = json.loads(fp.read_text(encoding="utf-8"))
            except ValueError:
                pass
        isin = (isins or {}).get(code)
        if isin and obj.get("i") and obj["i"] != isin:
            conflicts += 1          # 同じコードで別のISIN: 別の銘柄の可能性があるので書き込まない
            continue
        if isin:
            obj["i"] = isin
        if names.get(code):
            obj["n"] = names[code]
        obj["h"][iso] = [sv, bv, ss, bs]
        fp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        saved += 1
    if conflicts:
        print(f"::notice::{date} コードとISINが既存の記録と合わない {conflicts} 銘柄は、保存しませんでした")
    dates = read_stock_dates(sdir) | {date}
    (sdir / "_dates.json").write_text(json.dumps(sorted(dates)), encoding="utf-8")
    return saved


def read_stock_dates(sdir):
    fp = sdir / "_dates.json"
    if not fp.exists():
        return set()
    try:
        return set(json.loads(fp.read_text(encoding="utf-8")))
    except ValueError:
        return set()


def process(date, src, out, debug, do_totals=True, do_stocks=True):
    with pdfplumber.open(src) as pdf:
        text = pdf.pages[-1].extract_text() or ""
        found = parse_subtotals(text)
        if debug:
            (BASE / f"debug_{date}_lastpage.txt").write_text(text, encoding="utf-8")
        if do_totals:
            missing = [jp for k, jp in MARKETS if k not in found]
            if missing:
                raise RuntimeError(
                    f"{date}: 最終ページから {', '.join(missing)} の小計が読み取れませんでした。"
                    " --debug で最終ページのテキストを確認してください。"
                )
            row = append_row(out, date, found)
            print(f"{row[0]}  " + "  ".join(f"{jp}={row[3 + 3 * i]}" for i, (_, jp) in enumerate(MARKETS)))
        if do_stocks:
            process_stocks(date, pdf, found, out, debug)


def process_stocks(date, pdf, found, out, debug):
    """銘柄別データを保存する。失敗しても、市場別の集計の記録は止めない(警告だけ出す)"""
    try:
        shs, val, names, n_total, bad, isins = parse_issues(pdf)
        if debug:
            (BASE / f"debug_{date}_page1.txt").write_text(pdf.pages[0].extract_text() or "", encoding="utf-8")
            (BASE / f"debug_{date}_unparsed.txt").write_text("\n".join(bad), encoding="utf-8")
        problems, notes = check_issues(shs, val, n_total, found.get("total"))
        if problems:
            for msg in problems:
                print(f"::warning::{date} 銘柄別データ: {msg}")
            shown = {}
            for ex in bad:
                tag = ex.split("]")[0]
                if shown.get(tag, 0) < 2:
                    shown[tag] = shown.get(tag, 0) + 1
                    print(f"::warning::{date} 読み取れなかった行の例: {ex[:260]}")
            print(f"{date}: 銘柄別データは保存しませんでした (--debug で詳細を確認できます)")
            return
        for msg in notes:
            print(f"::notice::{date} 銘柄別データ: {msg}")
        for ex in bad[:5]:
            print(f"::notice::{date} 読み取れなかった行の例: {ex[:200]}")
        n = save_issues(out.parent / "stocks", date, shs, val, names, isins)
        print(f"{date}: 銘柄別データを保存しました ({n} 銘柄 / PDF上 {len(val)} 銘柄)")
    except Exception as e:  # noqa: BLE001
        print(f"::warning::{date} 銘柄別データの処理に失敗しました: {e}")


TOOLTIP_JS = r"""<script>
(function(){
var D=__DATA__, G=__GEO__, STK=null, SC='#e11d74', NS='http://www.w3.org/2000/svg';
var tip=document.getElementById('tip'),guide=document.getElementById('guide'),stk=document.getElementById('stk');
var q=document.getElementById('q'),msgEl=document.getElementById('msg');
function esc(t){return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function el(n,a,t){var e=document.createElementNS(NS,n);for(var k in a)e.setAttribute(k,a[k]);if(t!=null)e.textContent=t;return e;}
function yen(v){if(v>=1e8)return (v/1e8).toLocaleString('ja-JP',{maximumFractionDigits:2})+'億円';return Math.round(v/1e4).toLocaleString('ja-JP')+'万円';}
function sh(v){return v.toLocaleString('ja-JP')+'株';}
function say(t){msgEl.textContent=t;}
var KINDS=['普通株式','受益証券','投資証券','優先株式','出資証券'],MKTS=['プライム','スタンダード','グロース','投信等'];
function inter(t,a,b){/* t が a と b を(各々の順序を保って)混ぜ合わせた文字列か */
  var n=a.length,m=b.length;if(t.length!==n+m)return false;
  var dp=[],i,j;for(i=0;i<=n;i++){dp.push(new Array(m+1).fill(false));}
  dp[0][0]=true;
  for(i=0;i<=n;i++)for(j=0;j<=m;j++){
    if(!dp[i][j])continue;var k=i+j;if(k>=t.length)continue;
    if(i<n&&t.charAt(k)===a.charAt(i))dp[i+1][j]=true;
    if(j<m&&t.charAt(k)===b.charAt(j))dp[i][j+1]=true;
  }
  return dp[n][m];
}
function cname(n){
  var h=String(n||'').replace(/\s+$/,''),combos=[];
  KINDS.forEach(function(k){MKTS.forEach(function(m){combos.push([k,m]);});});
  combos.sort(function(x,y){return (y[0].length+y[1].length)-(x[0].length+x[1].length);});
  for(var c=0;c<combos.length;c++){
    var L=combos[c][0].length+combos[c][1].length;
    if(h.length>L&&inter(h.slice(-L),combos[c][0],combos[c][1])){h=h.slice(0,-L).replace(/\s+$/,'');break;}
  }
  return h.replace(/\s*(?:普通株式|受益証券|投資証券|優先株式|出資証券)\s*(?:プライム|スタンダード|グロース|投信等)?\s*$/,'').trim();
}
function show(i,e){
  var d=D[i],h='<b>'+d.d+'</b>';
  d.m.forEach(function(m){
    h+='<div class="row"><span class="nm" style="color:'+m.c+'">'+m.n+'</span> '+m.r+
       '<br><span class="sub">買残高 '+m.b+'億円 / 売残高 '+m.s+'億円</span></div>';
  });
  if(STK){
    var p=STK.pts[i],nm='<span class="nm" style="color:'+SC+'">'+esc(STK.n||STK.c)+' ('+esc(STK.c)+')</span> ';
    if(p&&p.e){
      h+='<div class="row">'+nm+(p.r===null?'売残株数なし':p.r.toFixed(2)+' (株数ベース)')+
         '<br><span class="sub">買残高 '+yen(p.e[1])+' ('+sh(p.e[3])+') / 売残高 '+yen(p.e[0])+' ('+sh(p.e[2])+')</span></div>';
    }else{h+='<div class="row">'+nm+'<span class="sub">この日のデータなし</span></div>';}
  }
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
Array.prototype.forEach.call(document.querySelectorAll('.col'),function(el_){
  el_.addEventListener('mousemove',function(e){show(+el_.getAttribute('data-i'),e);});
  el_.addEventListener('mouseleave',hide);
  el_.addEventListener('click',function(e){show(+el_.getAttribute('data-i'),e);});
});
document.addEventListener('click',function(e){if(!e.target.closest||!e.target.closest('.col'))hide();});

function clearStk(){while(stk.firstChild)stk.removeChild(stk.firstChild);STK=null;}
function tick(v){return v>=100?v.toFixed(0):(v>=10?v.toFixed(1):v.toFixed(2));}
function draw(j,code){
  clearStk();
  var pts=G.dates.map(function(d,i){var e=(j.h&&j.h[d])||null;return {e:e,r:(e&&e[2]>0)?e[3]/e[2]:null,x:D[i].x};});
  STK={c:code,n:cname(j.n),pts:pts};
  var vs=pts.filter(function(p){return p.r!==null;}).map(function(p){return p.r;});
  var have=pts.filter(function(p){return p.e;}).length;
  if(!vs.length){say((cname(j.n)||code)+' ('+code+'): 表示期間に、売残株数があり倍率を計算できる日がありません。');return;}
  var lo=Math.min.apply(null,vs),hi=Math.max.apply(null,vs),pad=(hi-lo)*0.12||Math.max(hi*0.1,0.5);
  lo=Math.max(0,lo-pad);hi=hi+pad;
  var ph=G.H-G.T-G.B;
  function Y(v){return G.T+ph*(1-(v-lo)/(hi-lo));}
  var ax=G.W-G.R+112;
  stk.appendChild(el('line',{x1:ax,x2:ax,y1:G.T,y2:G.H-G.B,stroke:SC}));
  for(var t=0;t<=5;t++){
    var v=lo+(hi-lo)*t/5,y=Y(v);
    stk.appendChild(el('line',{x1:ax-4,x2:ax,y1:y,y2:y,stroke:SC}));
    stk.appendChild(el('text',{x:ax+7,y:y+4,fill:SC,'font-size':12},tick(v)));
  }
  var title=(cname(j.n)||code);if(title.length>16)title=title.slice(0,16)+'…';
  stk.appendChild(el('text',{x:G.W-6,y:G.T-12,'text-anchor':'end',fill:SC,'font-size':12,'font-weight':700},title+' ('+code+') 右軸・株数ベース'));
  var seg=[];
  function flush(){if(seg.length>1)stk.appendChild(el('polyline',{points:seg.map(function(a){return a[0]+','+a[1];}).join(' '),fill:'none',stroke:SC,'stroke-width':2.5,'stroke-dasharray':'7 4'}));seg=[];}
  pts.forEach(function(p){
    if(p.r===null){flush();return;}
    var y=Y(p.r);seg.push([p.x,y]);
    stk.appendChild(el('circle',{cx:p.x,cy:y,r:4,fill:SC}));
  });
  flush();
  say((cname(j.n)||code)+' ('+code+') を右軸(ピンクの破線)で表示中。倍率は株数ベース(買残株数÷売残株数)。データのある日: '+have+' / '+G.dates.length+' 日'+(have<G.dates.length?'(取り込み開始前の日は空白です)':''));
}
function norm(s){s=s.normalize('NFKC').trim().toUpperCase();if(s.length===5&&s.charAt(4)==='0')s=s.slice(0,4);return s;}
function go(){
  var c=norm(q.value);
  if(!/^[0-9][0-9A-Z]{3,4}$/.test(c)){say('銘柄コードは4桁で入力してください(例: 4461)');return;}
  say('読み込み中…');
  fetch('stocks/'+c+'.json?'+Date.now(),{cache:'no-store'}).then(function(r){
    if(r.status===404)throw new Error('404');
    if(!r.ok)throw new Error('http');
    return r.json();
  }).then(function(j){draw(j,c);}).catch(function(e){
    clearStk();
    say(e.message==='404'?'コード '+c+' のデータが見つかりません(取り込み開始前の銘柄、または信用残高がゼロの銘柄です)。'
                          :'読み込めませんでした。この機能は、公開URL(GitHub Pages)でページを開いたときに使えます。');
  });
}
document.getElementById('go').addEventListener('click',go);
document.getElementById('clr').addEventListener('click',function(){clearStk();q.value='';say('');});
q.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();go();}});
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
    W, H, L, R, T, B = (950 if len(rows) <= 7 else 1150), 460, 70, 200, 30, 60
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
    g.append('<g id="stk"></g>')  # 銘柄検索の結果(5本目・右軸)はここにJSが描く
    g.append(f'<line id="guide" x1="0" x2="0" y1="{T}" y2="{H-B}" class="guide" style="display:none"/>')
    for i in range(len(rows)):
        g.append(f'<rect class="col" data-i="{i}" x="{X(i) - step / 2:.1f}" y="{T}" width="{step:.1f}" height="{H-T-B}" fill="transparent"/>')
    geo = {"W": W, "H": H, "L": L, "R": R, "T": T, "B": B, "dates": [r["date"] for r in rows]}
    js = (TOOLTIP_JS.replace("__DATA__", json.dumps(data, ensure_ascii=False))
                    .replace("__GEO__", json.dumps(geo, ensure_ascii=False)))

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
.bar{{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px;max-width:{W}px}} .bar .hint{{flex:1 1 320px}}
.search{{display:flex;gap:6px;align-items:center}}
.search input{{font-size:16px;width:11em;padding:6px 8px;border:1px solid var(--grid);border-radius:6px;background:var(--bg);color:var(--fg)}}
.search button{{font-size:14px;padding:6px 12px;border:1px solid var(--grid);border-radius:6px;background:var(--bg);color:var(--fg);cursor:pointer}}
#tip .row{{margin-top:6px}} #tip .nm{{font-weight:700}} #tip .sub{{color:var(--mut);font-size:12px}}
</style></head><body>
<h1>信用倍率(買残高÷売残高) 直近{len(rows)}日(申込日ベース)</h1>
<div class="bar">
<p class="hint">グラフにマウスを乗せる(スマホはタップ)と、各市場の買残高・売残高(億円)が表示されます。</p>
<div class="search"><input id="q" type="text" maxlength="5" placeholder="銘柄コード 例: 4461" autocomplete="off" aria-label="銘柄コード"><button id="go" type="button">表示</button><button id="clr" type="button">消す</button></div>
</div>
<p id="msg" class="hint"></p>
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
    ap.add_argument("--no-stocks", action="store_true", help="銘柄別データを取り込まない")
    ap.add_argument("--days", type=int, default=20, help="グラフに表示する日数 (既定20)")
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = read_existing(out)
    sdir = out.parent / "stocks"
    done_stocks = read_stock_dates(sdir)

    if a.pdf:
        m = re.search(r"(\d{8})", Path(a.pdf).name)
        if not m:
            sys.exit("ファイル名から日付(YYYYMMDD)を取れません")
        d = m.group(1)
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        process(d, a.pdf, out, a.debug, iso not in done, not a.no_stocks and d not in done_stocks)
        return out, a.days

    pdfs = list_pdfs()
    targets = [a.date] if a.date else sorted(pdfs)
    new = 0
    for d in targets:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        need_totals = iso not in done
        need_stocks = not a.no_stocks and d not in done_stocks
        if not need_totals and not need_stocks:
            continue
        if d not in pdfs:
            print(f"{d}: まだページに掲載されていません")
            continue
        r = requests.get(pdfs[d], headers=HEADERS, timeout=120)
        r.raise_for_status()
        process(d, io.BytesIO(r.content), out, a.debug, need_totals, need_stocks)
        new += 1
    if new == 0:
        print("新しいデータはありません")
    return out, a.days


if __name__ == "__main__":
    out_path, days = main()
    make_html(out_path, out_path.with_suffix(".html"), days)
