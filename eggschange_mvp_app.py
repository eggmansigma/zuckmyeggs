#!/usr/bin/env python3
"""
Eggschange v1.1 — Agency-first mobile tool (single file, no external deps)

Features
- Secret deck page with RFQ guidance + multi line items
- Admin suppliers (add/edit) + CSV export/import
- RFQ parsing + supplier matching (area, welfare, sizes, pack formats, MOQs, delivery days)
- Email/WhatsApp/Call outreach buttons (email-first)
- Manual quote capture + apples-to-apples comparison (landed £/tray)
- Client share page (yellow theme + Eggschange header) with story PDF links
- Facts + progress bar on the deck page
- CLI “chat” still exports RFQ CSV
- Render-ready: binds to $PORT on 0.0.0.0
"""

from __future__ import annotations
import csv
import json
import os
import re
import sqlite3
import sys
import secrets
import string
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse, quote_plus

# -------------------- Config --------------------
DB_PATH = Path("./eggschange_v11.sqlite").as_posix()
HOST = os.getenv("EGGSCHANGE_HOST", "0.0.0.0")  # 0.0.0.0 for Render
PORT = int(os.getenv("PORT") or os.getenv("EGGSCHANGE_PORT") or "8080")
DEFAULT_SLUG = os.getenv("EGGSCHANGE_SLUG")  # if not set, generated at init

# -------------------- DB --------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    con = db()
    con.execute("""
    CREATE TABLE IF NOT EXISTS rfq(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      client_name TEXT,
      postcodes TEXT,
      welfare TEXT,
      delivery_windows TEXT,
      payment_terms TEXT,
      notes TEXT,
      line_items_json TEXT, -- list of {kind: retail/wholesale, size: L/M/XL..., pack: tray/box, qty_week:int, target_price:str?}
      share_token TEXT,
      created_at TEXT
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS supplier(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT, welfare TEXT, certs TEXT,
      sizes TEXT,                -- CSV of sizes they can supply, e.g., L,XL,M
      pack_formats TEXT,         -- CSV of tray,box
      moq_trays INTEGER,         -- min trays per drop (nullable)
      delivery_days TEXT,        -- CSV of Mon,Tue...
      delivery_postcodes TEXT,   -- CSV of prefixes BN,BN1,RH12
      email TEXT, phone TEXT, whatsapp TEXT,
      story_pdf_url TEXT,
      price_band_low REAL, price_band_high REAL,
      notes TEXT
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS quote(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      rfq_id INTEGER, supplier_id INTEGER,
      line_item_index INTEGER DEFAULT 0,   -- which RFQ line item this price refers to
      unit_price REAL,                     -- £/tray or £/box depending on line item pack
      delivery_cost REAL,
      lead_time_days INTEGER, hold_weeks INTEGER,
      remarks TEXT,
      created_at TEXT,
      FOREIGN KEY(rfq_id) REFERENCES rfq(id),
      FOREIGN KEY(supplier_id) REFERENCES supplier(id)
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS facts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      text TEXT
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS profile(
      id INTEGER PRIMARY KEY CHECK (id=1),
      slug TEXT,
      progress_value INTEGER DEFAULT 0
    )""")
    # seed profile
    row = con.execute("SELECT slug FROM profile WHERE id=1").fetchone()
    if not row:
        slug = DEFAULT_SLUG or f"deck-{secrets.token_hex(3)}"
        con.execute("INSERT INTO profile(id, slug, progress_value) VALUES (1, ?, 20)", (slug,))
        con.commit()
    # seed demo suppliers if none
    if con.execute("SELECT COUNT(*) c FROM supplier").fetchone()["c"] == 0:
        demo = [
            ("Orchard Eggs","free-range","Lion","L,XL","tray,box",40,"Tue,Fri","BN,BN1,RH","demo+orchard@example.com","+447700900111","+447700900111","https://example.com/orchard.pdf",2.1,2.8,"Sussex family farm."),
            ("Marshwood Farm","organic","Organic,Lion","M,L","tray","30","Mon,Wed","BN,PO","demo+marshwood@example.com","+447700900222","+447700900222","https://example.com/marshwood.pdf",2.2,3.0,"Dorset organic."),
        ]
        con.executemany("""
        INSERT INTO supplier(name,welfare,certs,sizes,pack_formats,moq_trays,delivery_days,delivery_postcodes,email,phone,whatsapp,story_pdf_url,price_band_low,price_band_high,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, demo)
        con.commit()
    con.close()

init_db()

# -------------------- Helpers --------------------
def get_profile() -> sqlite3.Row:
    con = db()
    row = con.execute("SELECT * FROM profile WHERE id=1").fetchone()
    con.close()
    return row

def parse_line_items_json(txt: str) -> List[Dict[str, Any]]:
    try:
        arr = json.loads(txt)
        if not isinstance(arr, list): return []
        out = []
        for it in arr:
            out.append({
                "kind": (it.get("kind") or "").strip().lower(),     # retail/wholesale
                "size": (it.get("size") or "").upper(),
                "pack": (it.get("pack") or "").lower(),              # tray/box
                "qty_week": int(it.get("qty_week") or 0),
                "target_price": (it.get("target_price") or "").strip()
            })
        return out
    except Exception:
        return []

def postcode_matches(supplier_prefixes: str, rfq_postcodes: List[str]) -> bool:
    if not supplier_prefixes: return False
    prefs = [p.strip().upper() for p in supplier_prefixes.split(",") if p.strip()]
    for rp in rfq_postcodes:
        for sp in prefs:
            if rp.upper().startswith(sp):
                return True
    return False

def mock_extract_meta(text: str) -> Dict[str, Any]:
    lower = text.lower()
    postcodes = []
    for token in ["bn1","bn2","bn","rh","po","se","sw","w1","ec"]:
        if token in lower and token.upper() not in postcodes:
            postcodes.append(token.upper())
    welfare = "organic" if "organic" in lower else ("free-range" if ("free-range" in lower or "free range" in lower) else None)
    # delivery windows
    days = {"mon":"Mon","tue":"Tue","wed":"Wed","thu":"Thu","fri":"Fri","sat":"Sat","sun":"Sun"}
    found = [name for key,name in days.items() if key in lower]
    delivery = "Tue/Fri" if ("tue" in lower and "fri" in lower) else ("/".join(found[:2]) if found else None)
    # payment terms
    m = re.search(r"(\d{1,2})\s*day", lower)
    terms = f"{m.group(1)} days" if m else None
    # price prefer £
    m_gbp = re.search(r"£\s*(\d+(?:\.\d{1,2})?)", text)
    target = f"£{m_gbp.group(1)}" if m_gbp else None
    return {"postcodes": postcodes, "welfare": welfare, "delivery_windows": delivery, "payment_terms": terms, "target_price": target}

def percent(n: int) -> int:
    return max(0, min(100, n))

def mailto_link(email: str, subject: str, body: str) -> str:
    return f"mailto:{email}?subject={quote_plus(subject)}&body={quote_plus(body)}"

def whatsapp_link(number: str, text: str) -> str:
    digits = re.sub(r"\D+","", number or "")
    if digits.startswith("0"): digits = "44" + digits[1:]
    return f"https://wa.me/{digits}?text={quote_plus(text)}"

# -------------------- HTML (inline, mobile-first) --------------------
STYLES = """
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#fafafa}
.wrap{max-width:960px;margin:0 auto;padding:16px}
.card{background:#fff;border-radius:16px;padding:16px;box-shadow:0 10px 30px rgba(0,0,0,.06)}
h1,h2,h3{margin:.2em 0}
input,textarea,select{width:100%;padding:10px;border:1px solid #ddd;border-radius:10px}
button{padding:10px 16px;border-radius:12px;border:0;background:black;color:#fff;cursor:pointer}
.btns{display:flex;gap:8px;flex-wrap:wrap}
table{width:100%;border-collapse:collapse}
th,td{padding:8px;border-bottom:1px solid #eee;text-align:left}
.hint{color:#666;font-size:14px}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#eef;color:#223;font-size:12px}
.yellow{background:#fff8cc}
.fixed{position:sticky;bottom:0;background:#fff;padding:8px;border-top:1px solid #eee}
.small{font-size:12px;color:#666}
</style>
"""

HEADER = "<div class='wrap'><div class='card'><h1>Eggschange</h1>"

def page(body: str) -> bytes:
    html = f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>{STYLES}</head><body>{body}</body></html>"
    return html.encode("utf-8")

# Deck (secret mobile tab)
def deck_html(slug: str, facts: List[str], progress_value: int) -> bytes:
    guidance = """
<ul class='hint'>
  <li>Client name</li>
  <li>Delivery postcodes (e.g., BN1, BN2)</li>
  <li>Delivery days (e.g., Tue/Fri)</li>
  <li>Payment terms (e.g., 14 days)</li>
  <li>Line items: size (L/M/XL), pack (tray/box), kind (retail/wholesale), qty/week, target £ (optional)</li>
  <li>Any special welfare/certification requirements</li>
</ul>
"""
    script = """
<script>
function addLine(){
  const t=document.getElementById('lines'); 
  const row=document.createElement('div'); row.className='card';
  row.innerHTML=`<div class='btns'>
    <select name='kind'><option>retail</option><option>wholesale</option></select>
    <select name='size'><option>L</option><option>M</option><option>XL</option><option>Mixed</option></select>
    <select name='pack'><option>tray</option><option>box</option></select>
    <input name='qty' type='number' min='0' placeholder='qty/week'>
    <input name='price' placeholder='target £ (optional)'>
    <button type='button' onclick='this.parentNode.parentNode.remove()'>Remove</button>
  </div>`;
  t.appendChild(row);
}
async function createRFQ(e){
  e.preventDefault();
  const f=e.target;
  const items=[];
  document.querySelectorAll('#lines .btns').forEach(b=>{
    items.push({
      kind:b.querySelector("[name=kind]").value,
      size:b.querySelector("[name=size]").value,
      pack:b.querySelector("[name=pack]").value,
      qty_week:b.querySelector("[name=qty]").value,
      target_price:b.querySelector("[name=price]").value
    });
  });
  const payload={
    client_name:f.client_name.value.trim(),
    meta_text:f.meta.value.trim(),
    line_items_json: JSON.stringify(items)
  };
  const res=await fetch('/rfq/create', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const data=await res.json();
  if(!data.ok){ alert('Error: '+(data.error||'unknown')); return; }
  location.href = '/match/' + data.rfq_id;
}
</script>
"""
    facts_html = "".join([f"<li>{f}</li>" for f in facts]) or "<li class='small'>No facts yet — add some in /admin/facts</li>"
    return page(f"""
{HEADER}<p class='hint'>Secret deck: <span class='badge'>{slug}</span></p>
<div class='card yellow'><h3>Facts</h3><ul>{facts_html}</ul>
<div style='margin-top:8px'><div class='small'>Progress</div>
<div style='height:10px;background:#eee;border-radius:10px;overflow:hidden'><div style='height:10px;width:{percent(progress_value)}%;background:#ffdf00'></div></div></div>
</div>
<div class='card'>
<h2>Create RFQ</h2>
<p class='hint'>What to include (for best matching):</p>
{guidance}
<form onsubmit='createRFQ(event)'>
  <label>Client name</label><input name='client_name' placeholder='(optional for now)'>
  <label>Notes / details (paste from client)</label><textarea name='meta' placeholder='e.g., Organic L, BN1/BN2, Tue/Fri, 14 days, need retail boxes + wholesale trays'></textarea>
  <div id='lines'></div>
  <div class='btns' style='margin-top:8px;'><button type='button' onclick='addLine()'>Add line item</button><button type='submit'>Create & match</button></div>
</form>
</div>
</div></div>
{script}
""")

# Admin suppliers list/edit
def admin_suppliers_html(rows: List[sqlite3.Row], editing: Optional[sqlite3.Row]) -> bytes:
    form = f"""
<div class='card'>
  <h2>{'Edit' if editing else 'Add'} supplier</h2>
  <form method='post' action='/admin/suppliers/save'>
    <input type='hidden' name='id' value='{editing['id'] if editing else ''}'>
    <label>Name</label><input name='name' value='{editing['name'] if editing else ''}' required>
    <label>Welfare</label><input name='welfare' value='{editing['welfare'] if editing else ''}'>
    <label>Certs</label><input name='certs' value='{editing['certs'] if editing else ''}'>
    <label>Sizes (CSV e.g., L,XL,M)</label><input name='sizes' value='{editing['sizes'] if editing else ''}'>
    <label>Pack formats (CSV e.g., tray,box)</label><input name='pack_formats' value='{editing['pack_formats'] if editing else ''}'>
    <label>MOQ trays</label><input name='moq_trays' type='number' min='0' value='{editing['moq_trays'] if editing and editing['moq_trays'] is not None else ''}'>
    <label>Delivery days (CSV e.g., Mon,Tue,Fri)</label><input name='delivery_days' value='{editing['delivery_days'] if editing else ''}'>
    <label>Delivery postcodes (prefix CSV e.g., BN,BN1,RH12)</label><input name='delivery_postcodes' value='{editing['delivery_postcodes'] if editing else ''}'>
    <label>Email</label><input name='email' value='{editing['email'] if editing else ''}'>
    <label>Phone</label><input name='phone' value='{editing['phone'] if editing else ''}'>
    <label>WhatsApp</label><input name='whatsapp' value='{editing['whatsapp'] if editing else ''}'>
    <label>Story PDF URL</label><input name='story_pdf_url' value='{editing['story_pdf_url'] if editing else ''}'>
    <label>Price band low (£)</label><input name='price_band_low' type='number' step='0.01' value='{editing['price_band_low'] if editing and editing['price_band_low'] is not None else ''}'>
    <label>Price band high (£)</label><input name='price_band_high' type='number' step='0.01' value='{editing['price_band_high'] if editing and editing['price_band_high'] is not None else ''}'>
    <label>Notes</label><textarea name='notes'>{editing['notes'] if editing and editing['notes'] else ''}</textarea>
    <div class='btns' style='margin-top:8px;'><button type='submit'>Save</button></div>
  </form>
</div>
"""
    table = "".join([
        f"<tr><td>{r['name']}</td><td>{r['welfare']}</td><td>{r['sizes']}</td><td>{r['pack_formats']}</td>"
        f"<td class='small'>{r['delivery_postcodes']}</td>"
        f"<td><a href='/admin/suppliers?id={r['id']}'>Edit</a></td></tr>"
        for r in rows
    ]) or "<tr><td colspan='6'>No suppliers yet.</td></tr>"
    return page(f"""
{HEADER}<div class='btns' style='margin-bottom:8px'>
  <a href='/admin/suppliers/export'><button>Export CSV</button></a>
  <form method='post' action='/admin/suppliers/import' enctype='application/x-www-form-urlencoded' style='display:inline;'>
    <button type='submit' name='demo' value='1'>Import demo CSV</button>
  </form>
  <a href='/admin/facts'><button>Facts & Progress</button></a>
</div>
<div class='card'>
  <h2>Suppliers</h2>
  <table><thead><tr><th>Name</th><th>Welfare</th><th>Sizes</th><th>Packs</th><th>Areas</th><th></th></tr></thead><tbody>{table}</tbody></table>
</div>
{form}
</div></div>
""")

def facts_html(facts: List[str], progress: int) -> bytes:
    list_html = "".join([f"<li>{f}</li>" for f in facts]) or "<li class='small'>No facts yet</li>"
    return page(f"""
{HEADER}
<div class='card'>
  <h2>Facts & Progress</h2>
  <form method='post' action='/admin/facts/add' class='btns'>
    <input name='text' placeholder='Add a fact'><button type='submit'>Add</button>
  </form>
  <ul>{list_html}</ul>
  <form method='post' action='/admin/progress/set' class='btns'>
    <label>Progress value (0–100)</label>
    <input type='number' name='value' min='0' max='100' value='{progress}'>
    <button type='submit'>Save</button>
  </form>
</div>
</div></div>
""")

def match_html(rfq: sqlite3.Row, items: List[Dict[str, Any]], matches: List[Dict[str, Any]]) -> bytes:
    items_list = "".join([f"<li>{i['kind']} — {i['size']} — {i['pack']} — {i['qty_week']}/week {('— target '+i['target_price']) if i.get('target_price') else ''}</li>" for i in items])
    rows = []
    for m in matches:
        outreach = []
        subject = f"RFQ #{rfq['id']} — {i['qty_week']} {i['pack']} / week" if items else f"RFQ #{rfq['id']}"
        body = f"""Hi {m['name']},

We have a buyer request:
Client: {rfq['client_name'] or '-'}
Areas: {rfq['postcodes']}
Delivery: {rfq['delivery_windows'] or '-'}
Items:
""" + "\n".join([f"- {i['kind']} {i['size']} {i['pack']} x {i['qty_week']}/week" + (f" (target {i['target_price']})" if i.get('target_price') else "") for i in items]) + f"""

Notes: {rfq['notes'] or '-'}

Please reply with unit £/{'tray or box'} and delivery £/drop, lead time and hold period.
"""
        if m["email"]:
            outreach.append(f"<a href='{mailto_link(m['email'], subject, body)}'><button>Email</button></a>")
        if m["whatsapp"]:
            outreach.append(f"<a target='_blank' href='{whatsapp_link(m['whatsapp'], body)}'><button>WhatsApp</button></a>")
        if m["phone"]:
            outreach.append(f"<a href='tel:{m['phone']}'><button>Call</button></a>")
        story = f"<a target='_blank' href='{m['story_pdf_url']}'><button>Story PDF</button></a>" if m["story_pdf_url"] else ""
        rows.append(
            f"<tr><td>{m['name']}</td><td>{m['welfare']}</td><td>{m['sizes']}</td><td>{m['pack_formats']}</td>"
            f"<td>{m['delivery_postcodes']}</td><td class='btns'>{''.join(outreach)} {story} "
            f"<a href='/rfq/{rfq['id']}/compare'><button>Compare</button></a></td></tr>"
        )
    table = "".join(rows) or "<tr><td colspan='6'>No compatible suppliers found. Edit suppliers or RFQ requirements.</td></tr>"
    return page(f"""
{HEADER}
<div class='card'>
  <h2>RFQ #{rfq['id']} — Matches</h2>
  <p class='hint'>Client: {rfq['client_name'] or '-'} • Areas: {rfq['postcodes']} • Delivery: {rfq['delivery_windows'] or '-'} • Terms: {rfq['payment_terms'] or '-'}</p>
  <h3>Items</h3>
  <ul>{items_list}</ul>
</div>
<div class='card'>
  <h3>Top matches</h3>
  <table><thead><tr><th>Supplier</th><th>Welfare</th><th>Sizes</th><th>Packs</th><th>Areas</th><th>Actions</th></tr></thead><tbody>{table}</tbody></table>
</div>
<div class='fixed'><a href='/rfq/{rfq['id']}/compare'><button>Open comparison</button></a></div>
</div></div>
""")

def compare_html(rfq: sqlite3.Row, items: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> bytes:
    if rows:
        body = "\n".join([
            f"<tr><td>{r['supplier']}</td><td>{r['line_item_label']}</td><td>{r['unit_price']:.2f}</td><td>{r['delivery_cost']:.2f}</td><td>{r['qty_week']}</td><td>{r['delivery_per_unit']:.4f}</td><td><b>{r['landed_per_unit']:.4f}</b></td><td>{r.get('lead_time_days') or ''}</td><td>{r.get('hold_weeks') or ''}</td><td>{r.get('remarks') or ''}</td></tr>"
            for r in rows
        ])
    else:
        body = "<tr><td colspan='10'>No quotes yet. Add one below.</td></tr>"

    item_options = "".join([f"<option value='{idx}'>{it['kind']} {it['size']} {it['pack']}</option>" for idx,it in enumerate(items)])
    add_form = f"""
<div class='card'>
  <h3>Add quote</h3>
  <form method='post' action='/rfq/{rfq['id']}/quotes/add' class='btns'>
    <label>Supplier ID</label><input name='supplier_id' type='number' min='1' placeholder='(find in /admin/suppliers list)' required>
    <label>Line item</label><select name='line_item_index'>{item_options}</select>
    <label>Unit £/{'tray or box'}</label><input name='unit_price' type='number' step='0.01' min='0.01' required>
    <label>Delivery £/drop</label><input name='delivery_cost' type='number' step='0.01' min='0'>
    <label>Lead days</label><input name='lead_time_days' type='number' min='0'>
    <label>Hold weeks</label><input name='hold_weeks' type='number' min='0'>
    <label>Remarks</label><input name='remarks'>
    <button type='submit'>Save</button>
  </form>
</div>
"""
    items_list = "".join([f"<li>{i['kind']} — {i['size']} — {i['pack']} — {i['qty_week']}/week</li>" for i in items])
    return page(f"""
{HEADER}
<div class='card'>
  <h2>RFQ #{rfq['id']} — Comparison</h2>
  <p class='hint'>Client: {rfq['client_name'] or '-'} • Areas: {rfq['postcodes']} • Delivery: {rfq['delivery_windows'] or '-'} • Terms: {rfq['payment_terms'] or '-'}</p>
  <h3>Items</h3><ul>{items_list}</ul>
  <table>
    <thead><tr><th>Supplier</th><th>Item</th><th>Unit £</th><th>Del £/drop</th><th>Qty/wk</th><th>Del £/unit</th><th><b>Landed £/unit</b></th><th>Lead</th><th>Hold</th><th>Remarks</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</div>
{add_form}
<div class='card'>
  <a href='/c/{rfq['id']}/{rfq['share_token']}'><button>Open client share page</button></a>
</div>
</div></div>
""")

def client_share_html(rfq: sqlite3.Row, items: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> bytes:
    header = """
<div style='padding:14px;background:#ffdf00;'>
  <div style='max-width:960px;margin:0 auto;font-family:system-ui'><h2 style='margin:0'>🥚 Eggschange</h2></div>
</div>
"""
    items_list = "".join([f"<li>{i['kind']} — {i['size']} — {i['pack']} — {i['qty_week']}/week</li>" for i in items])
    table = "\n".join([
        f"<tr><td>{r['supplier']}</td><td>{r['line_item_label']}</td><td>{r['unit_price']:.2f}</td><td>{r['delivery_cost']:.2f}</td><td><b>{r['landed_per_unit']:.4f}</b></td><td><a target='_blank' href='{r['story_pdf_url']}'>Farm story</a></td></tr>"
        for r in rows
    ]) or "<tr><td colspan='6'>Quotes pending.</td></tr>"
    html = f"""
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>{STYLES}</head>
<body class='yellow'>{header}
<div class='wrap'><div class='card'>
  <h2>Proposed options</h2>
  <p class='hint'>Client: {rfq['client_name'] or '-'} • Areas: {rfq['postcodes']} • Delivery: {rfq['delivery_windows'] or '-'}</p>
  <h3>Items</h3><ul>{items_list}</ul>
  <table><thead><tr><th>Supplier</th><th>Item</th><th>Unit £</th><th>Del £/drop</th><th>Landed £/unit</th><th>Story</th></tr></thead><tbody>{table}</tbody></table>
</div></div>
</body></html>
"""
    return html.encode("utf-8")

# -------------------- Matching logic --------------------
def rank_suppliers_for_rfq(rfq: sqlite3.Row, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rfq_postcodes = [p.strip().upper() for p in (rfq["postcodes"] or "").split(",") if p.strip()]
    rfq_days = set([d.strip().title() for d in (rfq["delivery_windows"] or "").replace("/",",").split(",") if d.strip()])
    wanted_welfare = (rfq["welfare"] or "").lower().strip() or None

    con = db()
    suppliers = con.execute("SELECT * FROM supplier").fetchall()
    con.close()

    ranked = []
    for s in suppliers:
        # hard constraints
        if not postcode_matches(s["delivery_postcodes"] or "", rfq_postcodes):
            continue
        if wanted_welfare and wanted_welfare not in (s["welfare"] or "").lower():
            continue
        sizes = set([x.strip().upper() for x in (s["sizes"] or "").split(",") if x.strip()])
        packs = set([x.strip().lower() for x in (s["pack_formats"] or "").split(",") if x.strip()])
        days = set([x.strip().title() for x in (s["delivery_days"] or "").split(",") if x.strip()])

        # each item must be potentially fulfillable (size+pack)
        can_cover_items = 0
        for it in items:
            if (it["size"].upper() in sizes or it["size"].upper() == "MIXED") and (it["pack"].lower() in packs):
                # MOQ check (approx by trays if pack is tray; if box, ignore MOQ for now)
                moq = s["moq_trays"] or 0
                if it["pack"].lower() == "tray" and moq > (it["qty_week"] or 0):
                    continue
                can_cover_items += 1

        if can_cover_items == 0:
            continue

        # score
        score = 0
        score += can_cover_items * 10
        # delivery day overlap bonus
        overlap = len(rfq_days & days) if rfq_days else 1
        score += overlap * 2
        # price band hint if target present
        t_price = None
        for it in items:
            if it.get("target_price"):
                m = re.search(r"(\d+(?:\.\d{1,2})?)", it["target_price"].replace(",",""))
                if m:
                    t_price = float(m.group(1)); break
        if (t_price is not None) and (s["price_band_low"] is not None) and (s["price_band_high"] is not None):
            if s["price_band_low"] <= t_price <= s["price_band_high"]:
                score += 2

        ranked.append({**dict(s), "score": score})

    ranked.sort(key=lambda x: (-x["score"], x["name"]))
    return ranked

# -------------------- HTTP --------------------
class App(BaseHTTPRequestHandler):
    server_version = "Eggschange/1.1"

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    # -------- GET
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            prof = get_profile()
            return self._send(200, page(f"{HEADER}<p class='hint'>Hi. This is the Eggschange app backend. Use your secret deck URL.</p><div class='card'><code>/deck/{prof['slug']}</code></div></div></div>"))

        if path == "/__health":
            return self._json({"ok": True})

        # secret deck
        if path.startswith("/deck/"):
            slug = path.split("/")[2]
            prof = get_profile()
            if slug != prof["slug"]:
                return self._json({"ok": False, "error": "not found"}, 404)
            con = db()
            facts = [r["text"] for r in con.execute("SELECT text FROM facts ORDER BY id DESC").fetchall()]
            con.close()
            return self._send(200, deck_html(slug, facts, prof["progress_value"]))

        # admin suppliers
        if path.startswith("/admin/suppliers"):
            q = parse_qs(urlparse(self.path).query)
            edit_id = q.get("id", [""])[0]
            con = db()
            rows = con.execute("SELECT * FROM supplier ORDER BY name").fetchall()
            editing = con.execute("SELECT * FROM supplier WHERE id=?", (edit_id,)).fetchone() if edit_id else None
            con.close()
            return self._send(200, admin_suppliers_html(rows, editing))

        if path == "/admin/suppliers/export":
            con = db()
            rows = con.execute("SELECT * FROM supplier ORDER BY id").fetchall()
            con.close()
            # CSV
            out = ["id,name,welfare,certs,sizes,pack_formats,moq_trays,delivery_days,delivery_postcodes,email,phone,whatsapp,story_pdf_url,price_band_low,price_band_high,notes"]
            for r in rows:
                vals = [r["id"],r["name"],r["welfare"],r["certs"],r["sizes"],r["pack_formats"],r["moq_trays"],r["delivery_days"],r["delivery_postcodes"],r["email"],r["phone"],r["whatsapp"],r["story_pdf_url"],r["price_band_low"],r["price_band_high"],(r["notes"] or "").replace("\n"," ")]
                out.append(",".join([quote_csv(str(v) if v is not None else "") for v in vals]))
            data = ("\n".join(out)).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type","text/csv; charset=utf-8"); self.send_header("Content-Disposition","attachment; filename=suppliers.csv"); self.end_headers(); self.wfile.write(data); return

        if path == "/admin/facts":
            con = db()
            facts = [r["text"] for r in con.execute("SELECT text FROM facts ORDER BY id DESC").fetchall()]
            prof = con.execute("SELECT progress_value FROM profile WHERE id=1").fetchone()
            con.close()
            return self._send(200, facts_html(facts, prof["progress_value"] if prof else 0))

        # compare
        if path.startswith("/rfq/") and path.endswith("/compare"):
            rfq_id = int(path.split("/")[2])
            con = db()
            rfq = con.execute("SELECT * FROM rfq WHERE id=?", (rfq_id,)).fetchone()
            quotes = con.execute("SELECT q.*, s.name AS sname FROM quote q JOIN supplier s ON s.id=q.supplier_id WHERE q.rfq_id=? ORDER BY q.created_at DESC",(rfq_id,)).fetchall()
            con.close()
            items = parse_line_items_json(rfq["line_items_json"] or "[]")
            rows = []
            for q in quotes:
                idx = q["line_item_index"] or 0
                it = items[idx] if idx < len(items) else {"qty_week":0,"kind":"","size":"","pack":""}
                qty = int(it.get("qty_week") or 0)
                del_per = (float(q["delivery_cost"] or 0) / qty) if qty else 0.0
                landed = float(q["unit_price"] or 0) + del_per
                rows.append({
                    "supplier": q["sname"],
                    "line_item_label": f"{it.get('kind','')} {it.get('size','')} {it.get('pack','')}",
                    "unit_price": float(q["unit_price"] or 0),
                    "delivery_cost": float(q["delivery_cost"] or 0),
                    "qty_week": qty,
                    "delivery_per_unit": round(del_per,4),
                    "landed_per_unit": round(landed,4),
                    "lead_time_days": q["lead_time_days"],
                    "hold_weeks": q["hold_weeks"],
                    "remarks": q["remarks"],
                })
            rows.sort(key=lambda x: x["landed_per_unit"])
            return self._send(200, compare_html(rfq, items, rows))

        # client share page
        if path.startswith("/c/"):
            parts = path.split("/")
            rfq_id = int(parts[2]); token = parts[3] if len(parts) > 3 else ""
            con = db()
            rfq = con.execute("SELECT * FROM rfq WHERE id=?", (rfq_id,)).fetchone()
            if not rfq or rfq["share_token"] != token:
                con.close(); return self._json({"ok": False, "error": "not found"}, 404)
            quotes = con.execute("SELECT q.*, s.name AS sname, s.story_pdf_url AS story FROM quote q JOIN supplier s ON s.id=q.supplier_id WHERE q.rfq_id=?",(rfq_id,)).fetchall()
            con.close()
            items = parse_line_items_json(rfq["line_items_json"] or "[]")
            rows=[]
            for q in quotes:
                idx = q["line_item_index"] or 0
                it = items[idx] if idx < len(items) else {"qty_week":0,"kind":"","size":"","pack":""}
                qty = int(it.get("qty_week") or 0)
                del_per = (float(q["delivery_cost"] or 0) / qty) if qty else 0.0
                landed = float(q["unit_price"] or 0) + del_per
                rows.append({
                    "supplier": q["sname"],
                    "line_item_label": f"{it.get('kind','')} {it.get('size','')} {it.get('pack','')}",
                    "unit_price": float(q["unit_price"] or 0),
                    "delivery_cost": float(q["delivery_cost"] or 0),
                    "landed_per_unit": round(landed,4),
                    "story_pdf_url": q["story"] or "#",
                })
            rows.sort(key=lambda x: x["landed_per_unit"])
            return self._send(200, client_share_html(rfq, items, rows))

        return self._json({"ok": False, "error": "Not found"}, 404)

    # -------- POST
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        # create RFQ from deck
        if path == "/rfq/create":
            try:
                data = json.loads(raw.decode("utf-8"))
                client_name = (data.get("client_name") or "").strip()
                meta = data.get("meta_text") or ""
                items_json = data.get("line_items_json") or "[]"
                meta_fields = mock_extract_meta(meta)
                postcodes = ",".join(meta_fields["postcodes"])
                welfare = meta_fields["welfare"]
                delivery = meta_fields["delivery_windows"]
                terms = meta_fields["payment_terms"]
                share_token = secrets.token_hex(4)
                con = db()
                cur = con.execute("""
                INSERT INTO rfq(client_name,postcodes,welfare,delivery_windows,payment_terms,notes,line_items_json,share_token,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """, (client_name, postcodes, welfare, delivery, terms, meta, items_json, share_token, datetime.utcnow().isoformat()))
                rfq_id = cur.lastrowid
                con.commit(); con.close()
                return self._json({"ok": True, "rfq_id": rfq_id})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        # save supplier
        if path == "/admin/suppliers/save":
            form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            to_float = lambda x: (float(x) if x not in ("", None) else None)
            to_int = lambda x: (int(x) if x not in ("", None) else None)
            s = {
                "id": form.get("id",[""])[0],
                "name": form.get("name",[""])[0],
                "welfare": form.get("welfare",[""])[0],
                "certs": form.get("certs",[""])[0],
                "sizes": form.get("sizes",[""])[0],
                "pack_formats": form.get("pack_formats",[""])[0],
                "moq_trays": to_int(form.get("moq_trays",[""])[0]),
                "delivery_days": form.get("delivery_days",[""])[0],
                "delivery_postcodes": form.get("delivery_postcodes",[""])[0],
                "email": form.get("email",[""])[0],
                "phone": form.get("phone",[""])[0],
                "whatsapp": form.get("whatsapp",[""])[0],
                "story_pdf_url": form.get("story_pdf_url",[""])[0],
                "price_band_low": to_float(form.get("price_band_low",[""])[0]),
                "price_band_high": to_float(form.get("price_band_high",[""])[0]),
                "notes": form.get("notes",[""])[0],
            }
            con = db()
            if s["id"]:
                con.execute("""
                UPDATE supplier SET name=?, welfare=?, certs=?, sizes=?, pack_formats=?, moq_trays=?, delivery_days=?, delivery_postcodes=?, email=?, phone=?, whatsapp=?, story_pdf_url=?, price_band_low=?, price_band_high=?, notes=? WHERE id=?
                """, (s["name"],s["welfare"],s["certs"],s["sizes"],s["pack_formats"],s["moq_trays"],s["delivery_days"],s["delivery_postcodes"],s["email"],s["phone"],s["whatsapp"],s["story_pdf_url"],s["price_band_low"],s["price_band_high"],s["notes"],s["id"]))
            else:
                con.execute("""
                INSERT INTO supplier(name,welfare,certs,sizes,pack_formats,moq_trays,delivery_days,delivery_postcodes,email,phone,whatsapp,story_pdf_url,price_band_low,price_band_high,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (s["name"],s["welfare"],s["certs"],s["sizes"],s["pack_formats"],s["moq_trays"],s["delivery_days"],s["delivery_postcodes"],s["email"],s["phone"],s["whatsapp"],s["story_pdf_url"],s["price_band_low"],s["price_band_high"],s["notes"]))
            con.commit(); con.close()
            self.send_response(303); self.send_header("Location","/admin/suppliers"); self.end_headers(); return

        # import suppliers (demo CSV)
        if path == "/admin/suppliers/import":
            # For now, just re-seed the demo (safe and simple)
            init_db()
            self.send_response(303); self.send_header("Location","/admin/suppliers"); self.end_headers(); return

        # add quote
        if path.startswith("/rfq/") and path.endswith("/quotes/add"):
            rfq_id = int(path.split("/")[2])
            form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            try:
                supplier_id = int(form.get("supplier_id",[""])[0])
                idx = int(form.get("line_item_index",["0"])[0])
                unit = float(form.get("unit_price",[""])[0])
                if unit <= 0: raise ValueError("Unit price must be > 0")
                delivery = float(form.get("delivery_cost",["0"])[0] or 0)
                lead = int(form.get("lead_time_days",["0"])[0] or 0)
                hold = int(form.get("hold_weeks",["0"])[0] or 0)
                remarks = form.get("remarks",[""])[0]
            except Exception as e:
                return self._json({"ok": False, "error": f"Bad form: {e}"}, 400)
            con = db()
            con.execute("""
            INSERT INTO quote(rfq_id,supplier_id,line_item_index,unit_price,delivery_cost,lead_time_days,hold_weeks,remarks,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """, (rfq_id, supplier_id, idx, unit, delivery, lead, hold, remarks, datetime.utcnow().isoformat()))
            con.commit(); con.close()
            self.send_response(303); self.send_header("Location", f"/rfq/{rfq_id}/compare"); self.end_headers(); return

        # facts add / progress set
        if path == "/admin/facts/add":
            form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            text = (form.get("text",[""])[0] or "").strip()
            if text:
                con = db(); con.execute("INSERT INTO facts(text) VALUES (?)",(text,)); con.commit(); con.close()
            self.send_response(303); self.send_header("Location","/admin/facts"); self.end_headers(); return

        if path == "/admin/progress/set":
            form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            try:
                val = int(form.get("value",["0"])[0]); val = percent(val)
            except Exception: val = 0
            con = db(); con.execute("UPDATE profile SET progress_value=? WHERE id=1",(val,)); con.commit(); con.close()
            self.send_response(303); self.send_header("Location","/admin/facts"); self.end_headers(); return

        return self._json({"ok": False, "error": "Not found"}, 404)

# CSV helper
def quote_csv(s: str) -> str:
    if any(c in s for c in [",",'"',"\n"]):
        return '"' + s.replace('"','""') + '"'
    return s

# -------------------- CLI (CSV export like earlier) --------------------
def export_rfq_to_csv(rfq_id: int, rfq: Dict[str, Any]) -> str:
    path = Path(f"rfq_{rfq_id}.csv")
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Client","Postcodes","Delivery","Terms","Notes"])
        w.writerow([rfq.get("client_name") or "", rfq.get("postcodes") or "", rfq.get("delivery_windows") or "", rfq.get("payment_terms") or "", (rfq.get("notes") or "").replace("\n"," ")])
        w.writerow([])
        w.writerow(["Items: kind","size","pack","qty/week","target £"])
        for it in parse_line_items_json(rfq.get("line_items_json") or "[]"):
            w.writerow([it["kind"], it["size"], it["pack"], it["qty_week"], it.get("target_price") or ""])
    return path.as_posix()

def cli_chat(text: str) -> int:
    meta = mock_extract_meta(text)
    items = [{"kind":"wholesale","size":"L","pack":"tray","qty_week":120,"target_price":meta.get("target_price") or ""}]
    rfq = {
        "client_name": "",
        "postcodes": ",".join(meta["postcodes"]),
        "welfare": meta["welfare"],
        "delivery_windows": meta["delivery_windows"],
        "payment_terms": meta["payment_terms"],
        "notes": text,
        "line_items_json": json.dumps(items),
        "share_token": secrets.token_hex(4),
    }
    con = db()
    cur = con.execute("""
    INSERT INTO rfq(client_name,postcodes,welfare,delivery_windows,payment_terms,notes,line_items_json,share_token,created_at)
    VALUES (?,?,?,?,?,?,?,?,?)
    """, (rfq["client_name"], rfq["postcodes"], rfq["welfare"], rfq["delivery_windows"], rfq["payment_terms"], rfq["notes"], rfq["line_items_json"], rfq["share_token"], datetime.utcnow().isoformat()))
    rfq_id = cur.lastrowid
    con.commit(); con.close()
    csv_path = export_rfq_to_csv(rfq_id, rfq)
    print("\n--- RFQ Created ---")
    print(json.dumps(rfq, indent=2))
    print(f"RFQ saved to {csv_path}")
    return 0

# -------------------- Main --------------------
def start_server() -> None:
    prof = get_profile()
    print(f"Starting Eggschange v1.1 on http://{HOST}:{PORT}")
    print(f"Secret deck: /deck/{prof['slug']}")
    print("Admin suppliers: /admin/suppliers   •   Facts: /admin/facts")
    httpd = HTTPServer((HOST, PORT), App)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        httpd.server_close()

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "chat":
        sys.exit(cli_chat(" ".join(sys.argv[2:])))
    start_server()
