#!/usr/bin/env python3
"""Generate compact The Homie Architecture Excalidraw diagram."""
import json, sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "thehomie-architecture.excalidraw"

# ── Layout Grid ──────────────────────────────────────
C1X, C1W = 0, 520
C2X, C2W = 550, 660
C3X, C3W = 1240, 580

R1Y, R1H = 80, 300
R2Y, R2H = 410, 305
R3Y, R3H = 745, 270

# ── Colors ───────────────────────────────────────────
BLU = "#3b82f6"; DBLU = "#1e3a5f"; HBLU = "#1e40af"
PUR = "#7c3aed"; DPUR = "#4c1d95"
SUB = "#64748b"; BODY = "#334155"
EVBG = "#1e293b"; EVTX = "#94a3b8"
LBBG = "#dbeafe"; LPBG = "#ede9fe"
BORD = "#94a3b8"

_seed = 1000
def S():
    global _seed; _seed += 1; return _seed

# ── Element Factories ────────────────────────────────
def T(id, x, y, w, h, txt, fs=11, c=BODY, a="left", va="top", ci=None):
    return {"type":"text","id":id,"x":x,"y":y,"width":w,"height":h,
            "text":txt,"originalText":txt,"fontSize":fs,"fontFamily":3,
            "textAlign":a,"verticalAlign":"middle" if ci else va,
            "strokeColor":c,"backgroundColor":"transparent",
            "fillStyle":"solid","strokeWidth":1,"strokeStyle":"solid",
            "roughness":0,"opacity":100,"angle":0,
            "seed":S(),"version":1,"versionNonce":S(),
            "isDeleted":False,"groupIds":[],"boundElements":None,
            "link":None,"locked":False,"containerId":ci,"lineHeight":1.25}

def R(id, x, y, w, h, f="transparent", s=DBLU, d=False, sw=2, bt=None, rnd=True):
    el = {"type":"rectangle","id":id,"x":x,"y":y,"width":w,"height":h,
          "strokeColor":s,"backgroundColor":f,
          "fillStyle":"solid","strokeWidth":sw,
          "strokeStyle":"dashed" if d else "solid",
          "roughness":0,"opacity":100,"angle":0,
          "seed":S(),"version":1,"versionNonce":S(),
          "isDeleted":False,"groupIds":[],
          "boundElements":[{"id":bt,"type":"text"}] if bt else None,
          "link":None,"locked":False}
    if rnd: el["roundness"] = {"type":3}
    return el

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
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
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
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
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

# ── Build Elements ───────────────────────────────────
els = []

# ═══ TITLE + LEGEND ═══
els += [
    T("t1", 660, 5, 500, 35, "The Homie Architecture", fs=28, c=DBLU, a="center"),
    T("t2", 720, 42, 380, 20, "Claude Code + Agent SDK", fs=14, c=SUB, a="center"),
    D("lg1", 1680, 10, 10, BLU),
    T("lg1t", 1695, 7, 100, 16, "Claude Code", fs=11, c=BLU),
    D("lg2", 1680, 28, 10, PUR),
    T("lg2t", 1695, 25, 90, 16, "Agent SDK", fs=11, c=PUR),
]

# ═══ DIRECT INTEGRATIONS (Col1, Row1) ═══
bx, by, bw, bh = C1X, R1Y, C1W, R1H
els += [
    R("int-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("int-t", bx+15, by+10, 250, 22, "Direct Integrations", fs=18, c=HBLU),
    T("int-st", bx+15, by+33, 300, 14, "8 APIs converge into Python layer", fs=10, c=SUB),
]
# API dots + labels (2 columns)
apis = [("Gmail",0),("Calendar",0),("Asana",0),("Slack",0),
        ("Sheets",1),("Docs",1),("Drive",1),("Circle",1)]
for i,(name,col) in enumerate(apis):
    cx = bx + 20 + col*120
    cy = by + 58 + (i%4)*20
    els += [D(f"i-d{i}", cx, cy+4, 6, BLU), T(f"i-l{i}", cx+12, cy, 70, 14, name, fs=11)]
# Python API Layer box
px, py = bx+290, by+70
els += [
    R("int-py", px, py, 210, 50, f=LBBG, s=DBLU, bt="int-pyt"),
    T("int-pyt", px+10, py+5, 190, 20, "Python API Layer", fs=13, c=DBLU, a="center", ci="int-py"),
]
# Convergence arrows
els += [
    A("int-a1", bx+100, by+98, [[0,0],[190,0]], s=BLU, sw=1),
    A("int-a2", bx+210, by+98, [[0,0],[80,0]], s=BLU, sw=1),
]
# Auth summary
els.append(T("int-au", bx+290, by+128, 210, 55,
    "OAuth2 (Google 5 APIs)\nPAT (Asana)\nBot Token (Slack)\nAdmin Token (Circle)", fs=9, c=SUB))
# Evidence
evy = by + bh - 60
els += [
    R("int-ev", bx+10, evy, bw-20, 50, f=EVBG, s=EVBG),
    T("int-evt", bx+18, evy+5, bw-36, 40,
      "query.py CLI: gmail list | calendar today\nasana overdue | slack check | sheets read\ndocs read | drive find | circle search", fs=9, c=EVTX),
]

# ═══ HOOKS (Col2, Row1) ═══
bx, by, bw, bh = C2X, R1Y, C2W, R1H
els += [
    R("hk-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("hk-t", bx+15, by+10, 120, 22, "Hooks", fs=18, c=HBLU),
    D("hk-cd", bx+80, by+16, 8, BLU),
    T("hk-ct", bx+92, by+13, 100, 14, "Claude Code", fs=10, c=BLU),
    T("hk-st", bx+15, by+33, 400, 14, "Lifecycle events that manage memory automatically", fs=10, c=SUB),
]
# Timeline
tl_y = by + 80
tl_x1 = bx + 60
tl_x3 = bx + bw - 60
tl_x2 = (tl_x1 + tl_x3) // 2
els += [
    LN("hk-tl", tl_x1, tl_y, [[0,0],[tl_x3-tl_x1,0]], s=BLU, sw=2),
    D("hk-d1", tl_x1-5, tl_y-5, 10, BLU),
    D("hk-d2", tl_x2-5, tl_y-5, 10, BLU),
    D("hk-d3", tl_x3-5, tl_y-5, 10, BLU),
]
hooks = [
    (tl_x1-30, "SessionStart", "Loads SOUL.md,\nUSER.md, MEMORY.md\ninto context"),
    (tl_x2-30, "PreCompact", "Saves context\nbefore auto-compact\nto daily log"),
    (tl_x3-40, "SessionEnd", "Saves context\non session end\nto daily log"),
]
for i,(hx,name,desc) in enumerate(hooks):
    els += [
        T(f"hk-n{i}", hx, tl_y+14, 140, 14, name, fs=12, c=DBLU),
        T(f"hk-desc{i}", hx, tl_y+32, 150, 45, desc, fs=9, c=SUB),
    ]
# Evidence
evy = by + bh - 55
els += [
    R("hk-ev", bx+10, evy, bw-20, 45, f=EVBG, s=EVBG),
    T("hk-evt", bx+18, evy+5, bw-36, 35,
      ".claude/hooks/ configured in settings.json\nFires: context injection | memory flush (background)", fs=9, c=EVTX),
]

# ═══ HEARTBEAT (Col3, Row1) ═══
bx, by, bw, bh = C3X, R1Y, C3W, R1H
els += [
    R("hb-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("hb-t", bx+15, by+10, 200, 22, "Heartbeat", fs=18, c=HBLU),
    D("hb-cd", bx+120, by+16, 8, PUR),
    T("hb-ct", bx+132, by+13, 100, 14, "Agent SDK", fs=10, c=PUR),
    T("hb-st", bx+15, by+33, 350, 14, "Proactive monitoring every 30 minutes", fs=10, c=SUB),
]
# Assembly line: 3 stages
sy = by + 65
stages = [
    (bx+20, "Gather\nAPIs", LBBG, DBLU),
    (bx+210, "Claude\nReasons", LPBG, DPUR),
    (bx+400, "Notify", "#a7f3d0", "#047857"),
]
for i,(sx,label,fill,stroke) in enumerate(stages):
    sid = f"hb-s{i}"; tid = f"hb-st{i}"
    w = 150 if i < 2 else 140
    els += [
        R(sid, sx, sy, w, 50, f=fill, s=stroke, bt=tid),
        T(tid, sx+5, sy+5, w-10, 30, label, fs=11, c=stroke, a="center", ci=sid),
    ]
els += [
    A("hb-a1", bx+170, sy+25, [[0,0],[40,0]], s=DBLU, sw=2),
    A("hb-a2", bx+360, sy+25, [[0,0],[40,0]], s=DBLU, sw=2),
]
# Descriptions under stages
els += [
    T("hb-d1", bx+20, sy+58, 150, 28, "Python calls\n8 integrations", fs=9, c=SUB),
    T("hb-d2", bx+210, sy+58, 150, 28, "Agent SDK with\npre-loaded context", fs=9, c=SUB),
    T("hb-d3", bx+400, sy+58, 140, 28, "Toast + Slack\n+ daily log", fs=9, c=SUB),
]
els.append(T("hb-sch", bx+15, sy+95, 350, 14, "Schedule: every 30min, 8am-10pm CST", fs=10, c=BODY))
# Evidence
evy = by + bh - 55
els += [
    R("hb-ev", bx+10, evy, bw-20, 45, f=EVBG, s=EVBG),
    T("hb-evt", bx+18, evy+5, bw-36, 35,
      "heartbeat.py | gather_heartbeat_context()\nCost: ~$0.05/run | State: heartbeat-state.json", fs=9, c=EVTX),
]

# ═══ SKILLS (Col1, Row2) ═══
bx, by, bw, bh = C1X, R2Y, C1W, R2H
els += [
    R("sk-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("sk-t", bx+15, by+10, 120, 22, "Skills", fs=18, c=HBLU),
    D("sk-cd", bx+75, by+16, 8, BLU),
    T("sk-ct", bx+87, by+13, 100, 14, "Claude Code", fs=10, c=BLU),
    T("sk-st", bx+15, by+33, 300, 14, "22 skills across 3 tiers", fs=10, c=SUB),
]
# Hub ellipse
hx, hy = bx+20, by+100
els += [
    E("sk-hub", hx, hy, 90, 45, f=LBBG, s=DBLU, bt="sk-hubt"),
    T("sk-hubt", hx+10, hy+5, 70, 20, "Skills\nHub", fs=11, c=DBLU, a="center", ci="sk-hub"),
]
# Fan-out to 3 tiers
tiers = [
    (by+65, "Infrastructure", "direct-integrations, memory-search,\nskill-creator"),
    (by+125, "Utility", "excalidraw-diagram, pptx-generator,\npdf, docs-reviewer"),
    (by+185, "Content", "linkedin-post, x-post, video-script,\nintro-polish"),
]
for i,(ty,tier_name,examples) in enumerate(tiers):
    els += [
        A(f"sk-a{i}", hx+90, hy+22, [[0,0],[60, ty-hy-22+10]], s=BLU, sw=1),
        T(f"sk-tn{i}", bx+180, ty, 130, 16, tier_name, fs=12, c=DBLU),
        T(f"sk-te{i}", bx+180, ty+17, 320, 28, examples, fs=9, c=SUB),
    ]
# Evidence
evy = by + bh - 45
els += [
    R("sk-ev", bx+10, evy, bw-20, 35, f=EVBG, s=EVBG),
    T("sk-evt", bx+18, evy+5, bw-36, 25,
      ".claude/skills/*/SKILL.md | Invoked via /skill-name", fs=9, c=EVTX),
]

# ═══ MEMORY LAYER (Col2, Row2) ═══
bx, by, bw, bh = C2X, R2Y, C2W, R2H
els += [
    R("mem-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("mem-t", bx+15, by+10, 200, 22, "Memory Layer", fs=18, c=HBLU),
    T("mem-core", bx+145, by+13, 50, 16, "(Core)", fs=12, c=SUB),
    T("mem-st", bx+15, by+33, 400, 14, "Obsidian vault at vault/memory/", fs=10, c=SUB),
]
# Memory files: 2 columns of 3
files_left = [
    ("SOUL.md", "Identity & rules"),
    ("USER.md", "Cole's info"),
    ("MEMORY.md", "Decisions & lessons"),
]
files_right = [
    ("HEARTBEAT.md", "Proactive checklist"),
    ("daily/", "Session logs (auto)"),
    ("plans/", "Project plans"),
]
fy = by + 55
for i,(fname,desc) in enumerate(files_left):
    y = fy + i*22
    els += [
        D(f"mem-dl{i}", bx+20, y+4, 6, BLU),
        T(f"mem-fl{i}", bx+32, y, 100, 14, fname, fs=11, c=DBLU),
        T(f"mem-el{i}", bx+140, y, 150, 14, desc, fs=9, c=SUB),
    ]
for i,(fname,desc) in enumerate(files_right):
    y = fy + i*22
    cx = bx + 330
    els += [
        D(f"mem-dr{i}", cx, y+4, 6, BLU),
        T(f"mem-fr{i}", cx+12, y, 120, 14, fname, fs=11, c=DBLU),
        T(f"mem-er{i}", cx+140, y, 150, 14, desc, fs=9, c=SUB),
    ]
# Search info
sy = fy + 78
els += [
    T("mem-srch", bx+15, sy, 450, 16, "Hybrid Search: 0.7 vector + 0.3 keyword", fs=12, c=BODY),
    T("mem-model", bx+15, sy+20, 450, 14, "FastEmbed ONNX | all-MiniLM-L6-v2 (384-dim) | fully local", fs=9, c=SUB),
]
# Evidence
evy = by + bh - 55
els += [
    R("mem-ev", bx+10, evy, bw-20, 45, f=EVBG, s=EVBG),
    T("mem-evt", bx+18, evy+5, bw-36, 35,
      "memory_search.py --mode hybrid \"query\"\nSQLite + sqlite-vec + FTS5 | Postgres + pgvector", fs=9, c=EVTX),
]

# ═══ CHAT INTERFACE (Col3, Row2) ═══
bx, by, bw, bh = C3X, R2Y, C3W, R2H
els += [
    R("ch-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("ch-t", bx+15, by+10, 200, 22, "Chat Interface", fs=18, c=HBLU),
    D("ch-cd", bx+160, by+16, 8, PUR),
    T("ch-ct", bx+172, by+13, 100, 14, "Agent SDK", fs=10, c=PUR),
    T("ch-st", bx+15, by+33, 350, 14, "Slack DM / persistent conversations", fs=10, c=SUB),
]
# Cycle: 4 nodes in square layout
nodes = [
    (bx+30, by+65, 130, "Slack Event", LBBG, DBLU),
    (bx+30, by+155, 130, "Engine", LBBG, DBLU),
    (bx+300, by+155, 130, "Agent SDK", LPBG, DPUR),
    (bx+300, by+65, 130, "Response", "#a7f3d0", "#047857"),
]
for i,(nx,ny,nw,label,fill,stroke) in enumerate(nodes):
    nid = f"ch-n{i}"; ntid = f"ch-nt{i}"
    els += [
        R(nid, nx, ny, nw, 40, f=fill, s=stroke, bt=ntid),
        T(ntid, nx+5, ny+5, nw-10, 20, label, fs=11, c=stroke, a="center", ci=nid),
    ]
# Cycle arrows
els += [
    A("ch-a1", bx+95, by+105, [[0,0],[0,50]], s=DBLU, sw=1),
    A("ch-a2", bx+160, by+175, [[0,0],[140,0]], s=DBLU, sw=1),
    A("ch-a3", bx+365, by+155, [[0,0],[0,-50]], s=DBLU, sw=1),
    A("ch-a4", bx+300, by+85, [[0,0],[-140,0]], s=DBLU, sw=1),
]
els.append(T("ch-db", bx+190, by+132, 80, 14, "Session DB", fs=9, c=SUB, a="center"))
# Evidence
evy = by + bh - 50
els += [
    R("ch-ev", bx+10, evy, bw-20, 40, f=EVBG, s=EVBG),
    T("ch-evt", bx+18, evy+5, bw-36, 30,
      "Socket Mode (no public URL) | .claude/chat/\nmain.py + engine.py + adapters/", fs=9, c=EVTX),
]

# ═══ CONTENT ENGINE CALLOUT (Col1, Row3) ═══
bx, by = C1X, R3Y
els += [
    R("ce-b", bx, by, 200, R3H, f="#fffbeb", s="#d97706", d=True, sw=1),
    T("ce-t", bx+15, by+10, 170, 18, "Content Engine", fs=14, c="#92400e"),
    T("ce-st", bx+15, by+30, 170, 14, "Future Phase", fs=10, c="#d97706"),
    T("ce-desc", bx+15, by+55, 170, 100,
      "Auto-generate:\n- LinkedIn posts\n- X posts\n- Video scripts\n- Shorts\nfrom YouTube\nlong-form content", fs=9, c="#92400e"),
]

# ═══ DAILY REFLECTION (Col2, Row3) ═══
bx, by, bw, bh = C2X, R3Y, C2W, R3H
els += [
    R("rf-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("rf-t", bx+15, by+10, 250, 22, "Daily Reflection", fs=18, c=HBLU),
    D("rf-cd", bx+180, by+16, 8, PUR),
    T("rf-ct", bx+192, by+13, 100, 14, "Agent SDK", fs=10, c=PUR),
    T("rf-st", bx+15, by+33, 300, 14, "8 AM daily memory curation", fs=10, c=SUB),
]
# Funnel: Daily Log → Diamond → MEMORY.md
fy = by + 55
els += [
    R("rf-s1", bx+30, fy, 160, 35, f=LBBG, s=DBLU, bt="rf-s1t"),
    T("rf-s1t", bx+35, fy+5, 150, 20, "Daily Log (raw)", fs=11, c=DBLU, a="center", ci="rf-s1"),
    A("rf-a1", bx+110, fy+35, [[0,0],[0,15]], s=DBLU, sw=1),
]
dy = fy + 55
els += [
    DI("rf-dm", bx+55, dy, 110, 50, bt="rf-dmt"),
    T("rf-dmt", bx+60, dy+10, 100, 20, "Worth\nkeeping?", fs=10, c="#92400e", a="center", ci="rf-dm"),
    A("rf-a2", bx+110, dy+50, [[0,0],[0,15]], s=DBLU, sw=1),
]
my = dy + 70
els += [
    R("rf-s3", bx+30, my, 160, 35, f="#a7f3d0", s="#047857", bt="rf-s3t"),
    T("rf-s3t", bx+35, my+5, 150, 20, "MEMORY.md", fs=11, c="#047857", a="center", ci="rf-s3"),
]
# Right side info
els.append(T("rf-info", bx+250, fy, 380, 120,
    "Reviews yesterday's daily log\n\nExtracts:\n- Key decisions made\n- Lessons learned\n- Important facts\n- Active project updates\n\nUpdates MEMORY.md directly", fs=9, c=SUB))
# Evidence
evy = by + bh - 40
els += [
    R("rf-ev", bx+10, evy, bw-20, 30, f=EVBG, s=EVBG),
    T("rf-evt", bx+18, evy+4, bw-36, 22,
      "memory_reflect.py | Promotes decisions & lessons to long-term memory", fs=9, c=EVTX),
]

# ═══ INFRASTRUCTURE (Col3, Row3) ═══
bx, by, bw, bh = C3X, R3Y, C3W, R3H
els += [
    R("if-b", bx, by, bw, bh, s=BORD, d=True, sw=1),
    T("if-t", bx+15, by+10, 200, 22, "Infrastructure", fs=18, c=HBLU),
    T("if-st", bx+15, by+33, 350, 14, "Local development + VPS deployment", fs=10, c=SUB),
]
half = (bw - 40) // 2
lx = bx + 15
rx = bx + half + 25
mid = bx + half + 20
els.append(LN("if-div", mid, by+55, [[0,0],[0,bh-75]], s=BORD, sw=1, d=True))
els += [
    T("if-lt", lx, by+55, 150, 16, "Local (Windows)", fs=13, c=DBLU),
    T("if-ld", lx, by+75, half, 120,
      "- SQLite + sqlite-vec\n- FTS5 keyword search\n- Windows Toast notifs\n- Task Scheduler (30min)\n- Obsidian Sync\n- ~80MB model cache", fs=10, c=BODY),
    T("if-rt", rx, by+55, 150, 16, "VPS (Linux)", fs=13, c=DBLU),
    T("if-rd", rx, by+75, half, 120,
      "- Postgres + pgvector\n- tsvector + GIN search\n- Slack-only notifications\n- Cron jobs (30min)\n- Git-based sync\n- Headless Google OAuth", fs=10, c=BODY),
]
els.append(T("if-note", bx+15, by+bh-28, bw-30, 14,
    "db.py abstraction normalizes both backends", fs=9, c=SUB))

# ═══ CONNECTION ARROWS (in the gaps between sections) ═══
# 1. Hooks → Memory (vertical, center of col2)
cx2 = C2X + C2W//2
els += [
    A("cn-1", cx2, R1Y+R1H, [[0,0],[0,R2Y-R1Y-R1H]], s=BLU, sw=2),
    T("cn-1t", cx2+5, R1Y+R1H+3, 80, 12, "loads/saves", fs=8, c=SUB),
]
# 2. Skills → Memory (horizontal, row2 mid-height)
my2 = R2Y + R2H//2
els += [
    A("cn-2", C1X+C1W, my2, [[0,0],[C2X-C1X-C1W,0]], s=BLU, sw=2),
    T("cn-2t", C1X+C1W+2, my2-13, 80, 12, "read/search", fs=8, c=SUB),
]
# 3. Memory → Chat (horizontal, row2 mid-height)
els += [
    A("cn-3", C2X+C2W, my2, [[0,0],[C3X-C2X-C2W,0]], s=PUR, sw=2),
    T("cn-3t", C2X+C2W+2, my2-13, 60, 12, "access", fs=8, c=SUB),
]
# 4. Reflection → Memory (vertical, center of col2)
els += [
    A("cn-4", cx2, R3Y, [[0,0],[0,-(R3Y-R2Y-R2H)]], s=PUR, sw=2),
    T("cn-4t", cx2+5, R3Y-22, 60, 12, "curates", fs=8, c=SUB),
]
# 5. Heartbeat → Memory (diagonal from bottom of col3/row1 to top-right of col2/row2)
els += [
    A("cn-5", C3X+100, R1Y+R1H, [[0,0],[-(C3X+100-C2X-C2W+20), R2Y-R1Y-R1H]], s=PUR, sw=1),
    T("cn-5t", C3X+20, R1Y+R1H+3, 90, 12, "writes log", fs=8, c=SUB),
]

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
