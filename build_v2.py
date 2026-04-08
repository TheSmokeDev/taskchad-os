#!/usr/bin/env python3
"""Generate improved The Homie Architecture diagram v2."""
import json

OUT = "thehomie-architecture.excalidraw"

# ── Layout ─────────────────────────────────────────
FULL_W = 1500
HK_Y = 58; HK_H = 155
R2_Y = 238; R2_H = 240
C1X = 0; C1W = 435
C2X = 465; C2W = 570
C3X = 1065; C3W = 435
R3_Y = 503; R3_H = 222
IF_Y = 750; IF_H = 132
CE_Y = 897; CE_H = 40

# ── Colors ─────────────────────────────────────────
BLU = "#3b82f6"; DBLU = "#1e3a5f"; HBLU = "#1e40af"
PUR = "#7c3aed"; DPUR = "#4c1d95"
SUB = "#64748b"; BODY = "#334155"
EVBG = "#1e293b"; EVTX = "#94a3b8"
LBBG = "#dbeafe"; LPBG = "#ede9fe"
BORD = "#94a3b8"
GRN = "#047857"; LGRN = "#a7f3d0"
AMB = "#d97706"; DAMB = "#92400e"; LAMB = "#fffbeb"

_seed = 2000
def S():
    global _seed; _seed += 1; return _seed

def T(id, x, y, w, h, txt, fs=11, c=BODY, a="left", ci=None):
    return {"type":"text","id":id,"x":x,"y":y,"width":w,"height":h,
            "text":txt,"originalText":txt,"fontSize":fs,"fontFamily":3,
            "textAlign":a,"verticalAlign":"middle" if ci else "top",
            "strokeColor":c,"backgroundColor":"transparent",
            "fillStyle":"solid","strokeWidth":1,"strokeStyle":"solid",
            "roughness":0,"opacity":100,"angle":0,
            "seed":S(),"version":1,"versionNonce":S(),
            "isDeleted":False,"groupIds":[],"boundElements":None,
            "link":None,"locked":False,"containerId":ci,"lineHeight":1.25}

def R(id, x, y, w, h, f="transparent", s=DBLU, d=False, sw=2, bt=None):
    return {"type":"rectangle","id":id,"x":x,"y":y,"width":w,"height":h,
            "strokeColor":s,"backgroundColor":f,
            "fillStyle":"solid","strokeWidth":sw,
            "strokeStyle":"dashed" if d else "solid",
            "roughness":0,"opacity":100,"angle":0,
            "seed":S(),"version":1,"versionNonce":S(),
            "isDeleted":False,"groupIds":[],
            "boundElements":[{"id":bt,"type":"text"}] if bt else None,
            "link":None,"locked":False,"roundness":{"type":3}}

def E(id, x, y, w, h, f=BLU, s=DBLU, bt=None):
    return {"type":"ellipse","id":id,"x":x,"y":y,"width":w,"height":h,
            "strokeColor":s,"backgroundColor":f,
            "fillStyle":"solid","strokeWidth":1,"strokeStyle":"solid",
            "roughness":0,"opacity":100,"angle":0,
            "seed":S(),"version":1,"versionNonce":S(),
            "isDeleted":False,"groupIds":[],
            "boundElements":[{"id":bt,"type":"text"}] if bt else None,
            "link":None,"locked":False}

def D(id, x, y, sz=8, f=BLU):
    return E(id, x, y, sz, sz, f=f, s=f)

def A(id, x, y, pts, s=DBLU, sw=2, si=None, ei=None):
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return {"type":"arrow","id":id,"x":x,"y":y,
            "width":max(xs)-min(xs),"height":max(ys)-min(ys),
            "strokeColor":s,"backgroundColor":"transparent",
            "fillStyle":"solid","strokeWidth":sw,"strokeStyle":"solid",
            "roughness":0,"opacity":100,"angle":0,
            "seed":S(),"version":1,"versionNonce":S(),
            "isDeleted":False,"groupIds":[],"boundElements":None,
            "link":None,"locked":False,"points":pts,
            "startBinding":{"elementId":si,"focus":0,"gap":2} if si else None,
            "endBinding":{"elementId":ei,"focus":0,"gap":2} if ei else None,
            "startArrowhead":None,"endArrowhead":"arrow"}

def LN(id, x, y, pts, s=BLU, sw=1, d=False):
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return {"type":"line","id":id,"x":x,"y":y,
            "width":max(xs)-min(xs),"height":max(ys)-min(ys),
            "strokeColor":s,"backgroundColor":"transparent",
            "fillStyle":"solid","strokeWidth":sw,
            "strokeStyle":"dashed" if d else "solid",
            "roughness":0,"opacity":100,"angle":0,
            "seed":S(),"version":1,"versionNonce":S(),
            "isDeleted":False,"groupIds":[],"boundElements":None,
            "link":None,"locked":False,"points":pts}

def DI(id, x, y, w, h, f="#fef3c7", s="#b45309", bt=None):
    return {"type":"diamond","id":id,"x":x,"y":y,"width":w,"height":h,
            "strokeColor":s,"backgroundColor":f,
            "fillStyle":"solid","strokeWidth":2,"strokeStyle":"solid",
            "roughness":0,"opacity":100,"angle":0,
            "seed":S(),"version":1,"versionNonce":S(),
            "isDeleted":False,"groupIds":[],
            "boundElements":[{"id":bt,"type":"text"}] if bt else None,
            "link":None,"locked":False}

els = []

# ═══ TITLE + LEGEND ═══
els += [
    T("t1", 500, 5, 500, 30, "The Homie Architecture", fs=24, c=DBLU, a="center"),
    T("t2", 530, 35, 440, 16, "Claude Code + Agent SDK", fs=13, c=SUB, a="center"),
    D("lg1", 1390, 8, 10, BLU),
    T("lg1t", 1405, 5, 90, 14, "Claude Code", fs=10, c=BLU),
    D("lg2", 1390, 26, 10, PUR),
    T("lg2t", 1405, 23, 80, 14, "Agent SDK", fs=10, c=PUR),
]

# ═══ HOOKS (full width, top) ═══
bx, by, bw, bh = 0, HK_Y, FULL_W, HK_H
els += [
    R("hk-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("hk-t", bx+15, by+8, 80, 20, "Hooks", fs=18, c=HBLU),
    D("hk-bd", bx+100, by+14, 8, BLU),
    T("hk-bl", bx+112, by+11, 90, 14, "Claude Code", fs=10, c=BLU),
    T("hk-sub", bx+15, by+28, 400, 13, "Lifecycle events that manage memory automatically", fs=10, c=SUB),
]
tl_y = by + 62
tl_x1, tl_x3 = bx+120, bx+bw-120
tl_xm = (tl_x1+tl_x3)//2
els += [
    LN("hk-line", tl_x1, tl_y, [[0,0],[tl_x3-tl_x1,0]], s=BLU, sw=2),
    D("hk-d1", tl_x1-6, tl_y-6, 12, BLU),
    D("hk-d2", tl_xm-6, tl_y-6, 12, BLU),
    D("hk-d3", tl_x3-6, tl_y-6, 12, BLU),
]
for i,(hx,name,desc) in enumerate([
    (tl_x1, "SessionStart", "Loads SOUL.md, USER.md,\nMEMORY.md into context"),
    (tl_xm, "PreCompact", "Saves context before\nauto-compact to daily log"),
    (tl_x3, "SessionEnd", "Saves context on\nsession end to daily log"),
]):
    els += [
        T(f"hk-n{i}", hx-50, tl_y+12, 100, 14, name, fs=12, c=DBLU, a="center"),
        T(f"hk-desc{i}", hx-65, tl_y+28, 130, 28, desc, fs=9, c=SUB, a="center"),
    ]
evy = by+bh-32
els += [
    R("hk-ev", bx+10, evy, bw-20, 25, f=EVBG, s=EVBG),
    T("hk-evt", bx+18, evy+4, bw-36, 17,
      ".claude/hooks/ in settings.json  |  Fires: context injection, memory flush (background)", fs=9, c=EVTX),
]

# ═══ INTEGRATIONS (col 1, row 2) ═══
bx, by, bw, bh = C1X, R2_Y, C1W, R2_H
els += [
    R("int-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("int-t", bx+12, by+8, 200, 20, "Direct Integrations", fs=18, c=HBLU),
    D("int-bd", bx+210, by+14, 8, BLU),
    T("int-bl", bx+222, by+11, 90, 14, "Claude Code", fs=10, c=BLU),
    T("int-sub", bx+12, by+28, 300, 13, "8 APIs converge into Python layer", fs=10, c=SUB),
]
api_colors = {"Gmail":BLU,"Calendar":BLU,"Sheets":BLU,"Docs":BLU,"Drive":BLU,
              "Asana":"#f97316","Slack":PUR,"Circle":GRN}
for i,name in enumerate(["Gmail","Calendar","Asana","Slack"]):
    y = by+48+i*18
    els += [D(f"i-dl{i}", bx+18, y+4, 6, api_colors[name]),
            T(f"i-ll{i}", bx+30, y, 80, 14, name, fs=11)]
for i,name in enumerate(["Sheets","Docs","Drive","Circle"]):
    y = by+48+i*18
    els += [D(f"i-dr{i}", bx+165, y+4, 6, api_colors[name]),
            T(f"i-lr{i}", bx+177, y, 80, 14, name, fs=11)]
py_y = by+130
els += [
    A("int-ca1", bx+60, by+118, [[0,0],[100,15]], s=BLU, sw=1),
    A("int-ca2", bx+210, by+118, [[0,0],[-40,15]], s=BLU, sw=1),
    R("int-py", bx+80, py_y, 275, 32, f=LBBG, s=DBLU, bt="int-pyt"),
    T("int-pyt", bx+85, py_y+4, 265, 20, "Python API Layer", fs=13, c=DBLU, a="center", ci="int-py"),
]
els.append(T("int-auth", bx+12, py_y+40, bw-24, 28,
    "OAuth2 (Google 5 APIs) · PAT (Asana)\nBot Token (Slack) · Admin Token (Circle)", fs=9, c=SUB))
evy = by+bh-38
els += [
    R("int-ev", bx+8, evy, bw-16, 30, f=EVBG, s=EVBG),
    T("int-evt", bx+16, evy+5, bw-32, 20,
      "query.py CLI: gmail list | calendar\ntoday | asana overdue | slack check", fs=9, c=EVTX),
]

# ═══ MEMORY (col 2, row 2 — emphasized center) ═══
bx, by, bw, bh = C2X, R2_Y, C2W, R2_H
els += [
    R("mem-b", bx, by, bw, bh, f="#f0f7ff", s=BLU, d=False, sw=2),
    T("mem-t", bx+15, by+8, 170, 22, "Memory Layer", fs=20, c=HBLU),
    T("mem-core", bx+190, by+12, 50, 16, "(Core)", fs=12, c=BLU),
    T("mem-sub", bx+15, by+30, 400, 13, "Obsidian vault — vault/memory/", fs=10, c=SUB),
]
for i,(fn,desc) in enumerate([("SOUL.md","Identity & rules"),("USER.md","User profile"),("MEMORY.md","Decisions & lessons")]):
    y = by+52+i*20
    els += [D(f"mem-dl{i}", bx+18, y+4, 6, BLU),
            T(f"mem-fl{i}", bx+30, y, 110, 14, fn, fs=11, c=DBLU),
            T(f"mem-el{i}", bx+145, y, 130, 14, desc, fs=9, c=SUB)]
for i,(fn,desc) in enumerate([("HEARTBEAT.md","Check checklist"),("daily/","Session logs"),("plans/","Project plans")]):
    y = by+52+i*20
    cx = bx+290
    els += [D(f"mem-dr{i}", cx, y+4, 6, BLU),
            T(f"mem-fr{i}", cx+12, y, 120, 14, fn, fs=11, c=DBLU),
            T(f"mem-er{i}", cx+140, y, 130, 14, desc, fs=9, c=SUB)]
els.append(LN("mem-sep1", bx+15, by+118, [[0,0],[bw-30,0]], s=BORD, sw=1, d=True))
els += [
    T("mem-srch", bx+15, by+125, 400, 16, "Hybrid Search: 0.7 vector + 0.3 keyword", fs=12, c=BODY),
    T("mem-model", bx+15, by+145, 500, 13, "FastEmbed ONNX | all-MiniLM-L6-v2 (384-dim) | fully local", fs=9, c=SUB),
]
els.append(LN("mem-sep2", bx+15, by+165, [[0,0],[bw-30,0]], s=BORD, sw=1, d=True))
els.append(T("mem-sync", bx+15, by+172, 400, 13, "Synced via Obsidian Sync — accessible on all devices", fs=9, c=SUB))
evy = by+bh-38
els += [
    R("mem-ev", bx+8, evy, bw-16, 30, f=EVBG, s=EVBG),
    T("mem-evt", bx+16, evy+5, bw-32, 20,
      "memory_search.py --mode hybrid \"query\" | SQLite + sqlite-vec + FTS5", fs=9, c=EVTX),
]

# ═══ SKILLS (col 3, row 2) ═══
bx, by, bw, bh = C3X, R2_Y, C3W, R2_H
els += [
    R("sk-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("sk-t", bx+12, by+8, 80, 20, "Skills", fs=18, c=HBLU),
    D("sk-bd", bx+80, by+14, 8, BLU),
    T("sk-bl", bx+92, by+11, 90, 14, "Claude Code", fs=10, c=BLU),
    T("sk-sub", bx+12, by+28, 300, 13, "22 skills across 3 tiers", fs=10, c=SUB),
]
hx, hy = bx+15, by+85
els += [
    E("sk-hub", hx, hy, 80, 65, f=LBBG, s=DBLU, bt="sk-hubt"),
    T("sk-hubt", hx+10, hy+10, 60, 30, "22\nSkills", fs=12, c=DBLU, a="center", ci="sk-hub"),
]
for i,(ty,name,ex) in enumerate([
    (by+52, "Infrastructure", "direct-integrations,\nmemory-search, skill-creator"),
    (by+112, "Utility", "excalidraw-diagram,\npptx-generator, pdf"),
    (by+172, "Content", "linkedin-post, x-post,\nvideo-script, intro-polish"),
]):
    els.append(A(f"sk-a{i}", hx+80, hy+32, [[0,0],[55, ty-hy-32+12]], s=BLU, sw=1))
    els += [T(f"sk-tn{i}", bx+160, ty, 130, 14, name, fs=12, c=DBLU),
            T(f"sk-te{i}", bx+160, ty+16, 260, 28, ex, fs=9, c=SUB)]
evy = by+bh-38
els += [
    R("sk-ev", bx+8, evy, bw-16, 30, f=EVBG, s=EVBG),
    T("sk-evt", bx+16, evy+5, bw-32, 20,
      ".claude/skills/*/SKILL.md\nInvoked via /skill-name", fs=9, c=EVTX),
]

# ═══ HEARTBEAT (col 1, row 3) ═══
bx, by, bw, bh = C1X, R3_Y, C1W, R3_H
els += [
    R("hb-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("hb-t", bx+12, by+8, 120, 20, "Heartbeat", fs=18, c=HBLU),
    D("hb-bd", bx+120, by+14, 8, PUR),
    T("hb-bl", bx+132, by+11, 90, 14, "Agent SDK", fs=10, c=PUR),
    T("hb-sub", bx+12, by+28, 350, 13, "Proactive monitoring every 30 minutes", fs=10, c=SUB),
]
sy = by+50
for i,(sx,sw2,label,fill,stroke) in enumerate([
    (bx+12, 120, "Gather\nAPIs", LBBG, DBLU),
    (bx+158, 120, "Claude\nReasons", LPBG, DPUR),
    (bx+304, 120, "Notify", LGRN, GRN),
]):
    sid=f"hb-s{i}"; tid=f"hb-st{i}"
    els += [R(sid, sx, sy, sw2, 40, f=fill, s=stroke, bt=tid),
            T(tid, sx+5, sy+5, sw2-10, 20, label, fs=11, c=stroke, a="center", ci=sid)]
els += [A("hb-a1", bx+132, sy+20, [[0,0],[26,0]], s=DBLU, sw=2),
        A("hb-a2", bx+278, sy+20, [[0,0],[26,0]], s=DBLU, sw=2)]
els += [T("hb-dd1", bx+12, sy+45, 120, 25, "Python calls\n8 integrations", fs=9, c=SUB),
        T("hb-dd2", bx+158, sy+45, 120, 25, "Agent SDK with\npre-loaded context", fs=9, c=SUB),
        T("hb-dd3", bx+304, sy+45, 120, 25, "Toast + Slack\n+ daily log", fs=9, c=SUB)]
els.append(T("hb-sch", bx+12, sy+78, 350, 13, "Schedule: every 30min, 8am-10pm CST", fs=10, c=BODY))
els.append(T("hb-chk", bx+12, sy+95, 350, 13, "Reads HEARTBEAT.md for what to check", fs=9, c=SUB))
evy = by+bh-38
els += [R("hb-ev", bx+8, evy, bw-16, 30, f=EVBG, s=EVBG),
        T("hb-evt", bx+16, evy+5, bw-32, 20,
          "heartbeat.py | ~$0.05/run\nState: heartbeat-state.json", fs=9, c=EVTX)]

# ═══ CHAT INTERFACE (col 2, row 3) ═══
bx, by, bw, bh = C2X, R3_Y, C2W, R3_H
els += [
    R("ch-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("ch-t", bx+15, by+8, 170, 20, "Chat Interface", fs=18, c=HBLU),
    D("ch-bd", bx+160, by+14, 8, PUR),
    T("ch-bl", bx+172, by+11, 90, 14, "Agent SDK", fs=10, c=PUR),
    T("ch-sub", bx+15, by+28, 350, 13, "Slack DM / persistent conversations", fs=10, c=SUB),
]
n_w, n_h = 115, 32
for i,(nx,ny,label,fill,stroke) in enumerate([
    (bx+40, by+55, "Slack Event", LBBG, DBLU),
    (bx+40, by+130, "Engine", LBBG, DBLU),
    (bx+330, by+130, "Agent SDK", LPBG, DPUR),
    (bx+330, by+55, "Response", LGRN, GRN),
]):
    nid=f"ch-n{i}"; ntid=f"ch-nt{i}"
    els += [R(nid, nx, ny, n_w, n_h, f=fill, s=stroke, bt=ntid),
            T(ntid, nx+5, ny+4, n_w-10, 16, label, fs=11, c=stroke, a="center", ci=nid)]
els += [A("ch-a1", bx+97, by+87, [[0,0],[0,43]], s=DBLU, sw=1),
        A("ch-a2", bx+155, by+146, [[0,0],[175,0]], s=DBLU, sw=1),
        A("ch-a3", bx+387, by+130, [[0,0],[0,-43]], s=DBLU, sw=1),
        A("ch-a4", bx+330, by+71, [[0,0],[-175,0]], s=DBLU, sw=1)]
els.append(T("ch-db", bx+195, by+98, 95, 14, "Session DB", fs=9, c=SUB, a="center"))
els.append(T("ch-info", bx+470, by+70, 90, 60, "Socket Mode\nNo public\nURL needed", fs=9, c=SUB))
evy = by+bh-38
els += [R("ch-ev", bx+8, evy, bw-16, 30, f=EVBG, s=EVBG),
        T("ch-evt", bx+16, evy+5, bw-32, 20,
          ".claude/chat/ | main.py + engine.py + adapters/", fs=9, c=EVTX)]

# ═══ REFLECTION (col 3, row 3) ═══
bx, by, bw, bh = C3X, R3_Y, C3W, R3_H
els += [
    R("rf-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("rf-t", bx+12, by+8, 180, 20, "Daily Reflection", fs=18, c=HBLU),
    D("rf-bd", bx+185, by+14, 8, PUR),
    T("rf-bl", bx+197, by+11, 90, 14, "Agent SDK", fs=10, c=PUR),
    T("rf-sub", bx+12, by+28, 300, 13, "8 AM daily memory curation", fs=10, c=SUB),
]
fx = bx+20; fy1 = by+48
els += [R("rf-s1", fx, fy1, 140, 28, f=LBBG, s=DBLU, bt="rf-s1t"),
        T("rf-s1t", fx+5, fy1+4, 130, 14, "Daily Log (raw)", fs=11, c=DBLU, a="center", ci="rf-s1")]
els.append(A("rf-a1", fx+70, fy1+28, [[0,0],[0,12]], s=DBLU, sw=1))
fy2 = fy1+45
els += [DI("rf-dm", fx+15, fy2, 110, 45, bt="rf-dmt"),
        T("rf-dmt", fx+20, fy2+10, 100, 16, "Worth\nkeeping?", fs=10, c=DAMB, a="center", ci="rf-dm")]
els.append(A("rf-a2", fx+70, fy2+45, [[0,0],[0,12]], s=DBLU, sw=1))
fy3 = fy2+62
els += [R("rf-s3", fx, fy3, 140, 28, f=LGRN, s=GRN, bt="rf-s3t"),
        T("rf-s3t", fx+5, fy3+4, 130, 14, "MEMORY.md", fs=11, c=GRN, a="center", ci="rf-s3")]
els.append(T("rf-info", bx+185, by+50, 240, 120,
    "Extracts:\n· Key decisions made\n· Lessons learned\n· Important facts\n· Active project updates\n\nUpdates MEMORY.md directly", fs=9, c=SUB))
evy = by+bh-35
els += [R("rf-ev", bx+8, evy, bw-16, 28, f=EVBG, s=EVBG),
        T("rf-evt", bx+16, evy+4, bw-32, 18,
          "memory_reflect.py | Promotes to long-term memory", fs=9, c=EVTX)]

# ═══ INFRASTRUCTURE (full width) ═══
bx, by, bw, bh = 0, IF_Y, FULL_W, IF_H
els += [
    R("if-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("if-t", bx+15, by+8, 170, 20, "Infrastructure", fs=18, c=HBLU),
    T("if-sub", bx+15, by+28, 350, 13, "Local development + VPS deployment", fs=10, c=SUB),
]
mid = bw//2
els.append(LN("if-div", mid, by+45, [[0,0],[0,bh-58]], s=BORD, sw=1, d=True))
els += [
    T("if-lt", bx+20, by+45, 200, 16, "Local (Windows)", fs=14, c=DBLU),
    T("if-ld", bx+20, by+63, 700, 50,
      "SQLite + sqlite-vec · FTS5 keyword search · Windows Toast notifications\nTask Scheduler (30min) · Obsidian Sync · ~80MB model cache", fs=10, c=BODY),
    T("if-rt", mid+20, by+45, 200, 16, "VPS (Linux)", fs=14, c=DBLU),
    T("if-rd", mid+20, by+63, 700, 50,
      "Postgres + pgvector · tsvector + GIN search · Slack-only notifications\nCron jobs (30min) · Git-based sync · Headless Google OAuth", fs=10, c=BODY),
]
els.append(T("if-note", bx+15, by+bh-20, bw-30, 13,
    "db.py abstraction normalizes both backends — same code, different storage", fs=9, c=SUB))

# ═══ CONTENT ENGINE (thin strip) ═══
bx, by, bw, bh = 0, CE_Y, FULL_W, CE_H
els += [
    R("ce-b", bx, by, bw, bh, f=LAMB, s=AMB, d=True, sw=1),
    T("ce-t", bx+15, by+10, 170, 16, "Content Engine", fs=14, c=DAMB),
    T("ce-phase", bx+190, by+12, 100, 14, "Future Phase", fs=10, c=AMB),
    T("ce-desc", bx+400, by+12, 800, 14,
      "Auto-generate LinkedIn posts · X posts · Video scripts · Shorts from YouTube long-form content", fs=10, c=DAMB),
]

# ═══ CONNECTION ARROWS ═══
cx2 = C2X + C2W//2
hk_bot = HK_Y + HK_H
r2_mid = R2_Y + R2_H//2
mem_bot = R2_Y + R2_H
hb_top = R3_Y

# 1. Hooks → Memory (vertical)
els += [A("cn-hm", cx2, hk_bot, [[0,0],[0,R2_Y-hk_bot]], s=BLU, sw=2),
        T("cn-hm-t", cx2+5, hk_bot+3, 70, 12, "loads/saves", fs=8, c=SUB)]
# 2. Integrations → Memory (horizontal)
els += [A("cn-im", C1X+C1W, r2_mid, [[0,0],[C2X-C1X-C1W,0]], s=BLU, sw=2),
        T("cn-im-t", C1X+C1W+2, r2_mid-14, 55, 12, "feeds", fs=8, c=SUB)]
# 3. Memory → Skills (horizontal)
els += [A("cn-ms", C2X+C2W, r2_mid, [[0,0],[C3X-C2X-C2W,0]], s=BLU, sw=2),
        T("cn-ms-t", C2X+C2W+2, r2_mid-14, 55, 12, "powers", fs=8, c=SUB)]
# 4. Memory → Chat (vertical)
els += [A("cn-mc", cx2, mem_bot, [[0,0],[0,hb_top-mem_bot]], s=PUR, sw=2),
        T("cn-mc-t", cx2+5, mem_bot+3, 55, 12, "context", fs=8, c=SUB)]
# 5. Heartbeat → Memory (diagonal up)
els += [A("cn-hbm", C1W-50, hb_top, [[0,0],[C2X-C1W+80, -(hb_top-mem_bot)]], s=PUR, sw=1),
        T("cn-hbm-t", C1W-20, hb_top-15, 70, 12, "writes log", fs=8, c=SUB)]
# 6. Reflection → Memory (diagonal up)
els += [A("cn-rm", C3X+50, hb_top, [[0,0],[-(C3X+50-C2X-C2W+30), -(hb_top-mem_bot)]], s=PUR, sw=1),
        T("cn-rm-t", C3X-5, hb_top-15, 60, 12, "curates", fs=8, c=SUB)]

# ═══ OUTPUT ═══
doc = {
    "type": "excalidraw",
    "version": 2,
    "source": "https://excalidraw.com",
    "elements": els,
    "appState": {"viewBackgroundColor": "#ffffff", "gridSize": 20},
    "files": {}
}
with open(OUT, "w") as f:
    json.dump(doc, f, indent=2)
print(f"Generated {len(els)} elements -> {OUT}")
