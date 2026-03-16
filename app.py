#!/usr/bin/env python3
"""
Basil — Fisher's personal assistant
Flask web app: Chat + CRM + Task Manager + Daily Digest
"""

import os, json, logging, re, smtplib, threading, time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path

import schedule
from anthropic import Anthropic
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "basil-rslf-2026-secret")

anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

APP_PASSWORD   = os.environ.get("APP_PASSWORD", "92627Seal")
EMAIL_ADDRESS  = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
DIGEST_EMAIL   = os.environ.get("DIGEST_EMAIL", "fisher@scruton.org")

DATA_DIR = Path("/app/data") if os.environ.get("RAILWAY_ENVIRONMENT") else Path("./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "store.json"

DEFAULT_TAXONOMY = [
    {"name": "RSLF US", "subs": ["Donors", "Administration", "IMLS Partnership - Event", "IMLS Partnership - Contract", "Grant Projects - NEH", "Grant Projects - State Dept", "Grant Projects - Other", "Relationships", "Board"]},
    {"name": "RSLF UK", "subs": ["MCC", "Administration", "Donors", "Scruton Lecture", "Relationships", "Board"]},
    {"name": "NCAS", "subs": ["Donors and Fundraising", "Embassy Architecture", "Strategic Plan", "Newsletters", "Website Updates"]},
    {"name": "Dux Culturae", "subs": ["Business Admin", "Writing"]},
    {"name": "City Council Campaign", "subs": ["Fundraising", "Events", "Canvassing", "Issues", "Relationships"]},
    {"name": "City Business", "subs": ["Arts Commission", "Issues"]},
    {"name": "Personal", "subs": ["Tasks", "Family", "Ideas"]},
]

def load_data():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE) as f:
                d = json.load(f)
                d.setdefault("tasks", [])
                d.setdefault("contacts", [])
                d.setdefault("taxonomy", DEFAULT_TAXONOMY)
                d.setdefault("messages", [])
                return d
        except:
            pass
    return {"tasks": [], "contacts": [], "taxonomy": DEFAULT_TAXONOMY, "messages": []}

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=2)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def build_system_prompt(data):
    today = datetime.now().strftime("%A, %B %d, %Y")
    tax = "\n".join(f"- {c['name']}: {', '.join(c['subs'])}" for c in data["taxonomy"])
    open_tasks = [t for t in data["tasks"] if not t.get("done")]
    tasks_str = "\n".join(f"[{t['id']}] {t['name']} | {t.get('project','')}/{t.get('section','')} | due:{t.get('due','none')} | {t.get('priority','medium')}" for t in open_tasks[:30]) or "none"
    contacts_str = "\n".join(f"[{c['id']}] {c['name']} | {c.get('org','')} | last:{c.get('lastContact','never')} | next:{c.get('nextAction','')}" for c in data["contacts"][:25]) or "none"

    return f"""You are Basil, the personal assistant to Fisher, Executive Director of the Roger Scruton Legacy Foundation.
Today is {today}.

Fisher's roles: Executive Director of RSLF (US and UK operations), Vice Chair of the Costa Mesa Arts Commission, Special Advisor to the President at the National Civic Art Society.

TAXONOMY:
{tax}

OPEN TASKS ({len(open_tasks)} total):
{tasks_str}

CONTACTS ({len(data['contacts'])} total):
{contacts_str}

You can perform actions by including JSON wrapped in <ACTION></ACTION> tags after your response.

Action types:
<ACTION>{{"type":"create_task","name":"...","project":"...","section":"...","due":"YYYY-MM-DD or null","priority":"high|medium|low","notes":"..."}}</ACTION>
<ACTION>{{"type":"complete_task","id":"..."}}</ACTION>
<ACTION>{{"type":"log_contact","name":"...","org":"...","role":"...","lastContact":"YYYY-MM-DD","nextAction":"...","notes":"..."}}</ACTION>
<ACTION>{{"type":"update_contact","id":"...","lastContact":"YYYY-MM-DD","nextAction":"...","notes":"..."}}</ACTION>

Rules:
- Parse relative dates ("Friday", "next week", "end of month") from today's date
- Match project/section exactly to the taxonomy above
- Use multiple ACTION tags for multiple actions
- Keep responses concise and direct — Fisher prefers substance
- Preserve specific names, dates, and details from notes
- When asked what's open or due, summarise from the task list above
- Address Fisher by name occasionally but don't be sycophantic
- You embody the spirit of Roger Scruton's thought — clarity, tradition, and intellectual seriousness"""

def apply_action(action, data):
    t = action.get("type")
    if t == "create_task":
        data["tasks"].append({
            "id": "T" + str(int(time.time()*1000))[-8:],
            "name": action.get("name", ""),
            "project": action.get("project", ""),
            "section": action.get("section", ""),
            "due": action.get("due") or None,
            "priority": action.get("priority", "medium"),
            "notes": action.get("notes", ""),
            "done": False,
            "created": datetime.now().strftime("%Y-%m-%d")
        })
    elif t == "complete_task":
        for task in data["tasks"]:
            if task["id"] == action.get("id"):
                task["done"] = True
    elif t == "log_contact":
        name = action.get("name", "")
        existing = next((c for c in data["contacts"] if c["name"].lower() == name.lower()), None)
        if existing:
            existing.update({k: v for k, v in action.items() if k not in ["type", "id"] and v})
        else:
            data["contacts"].append({
                "id": "C" + str(int(time.time()*1000))[-8:],
                "name": name,
                "org": action.get("org", ""),
                "role": action.get("role", ""),
                "lastContact": action.get("lastContact") or None,
                "nextAction": action.get("nextAction", ""),
                "notes": action.get("notes", ""),
                "added": datetime.now().strftime("%Y-%m-%d")
            })
    elif t == "update_contact":
        for c in data["contacts"]:
            if c["id"] == action.get("id"):
                c.update({k: v for k, v in action.items() if k not in ["type"] and v})

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Incorrect password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    data = load_data()
    return render_template("index.html", taxonomy=data["taxonomy"])

@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    body = request.json
    user_msg = body.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "Empty message"}), 400

    data = load_data()
    history = data["messages"][-20:]
    api_messages = [{"role": m["role"], "content": m["content"]} for m in history if m["role"] in ["user", "assistant"]]
    api_messages.append({"role": "user", "content": user_msg})

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=build_system_prompt(data),
        messages=api_messages
    )
    raw = response.content[0].text

    action_re = re.compile(r"<ACTION>([\s\S]*?)</ACTION>")
    actions = []
    for match in action_re.finditer(raw):
        try:
            actions.append(json.loads(match.group(1)))
        except:
            pass

    display = action_re.sub("", raw).strip()
    data["messages"].append({"role": "user", "content": user_msg})
    data["messages"].append({"role": "assistant", "content": display})
    data["messages"] = data["messages"][-60:]

    for action in actions:
        apply_action(action, data)

    save_data(data)
    return jsonify({"reply": display, "actions": len(actions)})

@app.route("/api/tasks", methods=["GET"])
@login_required
def get_tasks():
    data = load_data()
    return jsonify(data["tasks"])

@app.route("/api/tasks/<task_id>/toggle", methods=["POST"])
@login_required
def toggle_task(task_id):
    data = load_data()
    for t in data["tasks"]:
        if t["id"] == task_id:
            t["done"] = not t["done"]
    save_data(data)
    return jsonify({"ok": True})

@app.route("/api/contacts", methods=["GET"])
@login_required
def get_contacts():
    data = load_data()
    return jsonify(data["contacts"])

@app.route("/api/taxonomy", methods=["GET", "POST"])
@login_required
def taxonomy():
    data = load_data()
    if request.method == "POST":
        data["taxonomy"] = request.json
        save_data(data)
        return jsonify({"ok": True})
    return jsonify(data["taxonomy"])

@app.route("/api/digest/send", methods=["POST"])
@login_required
def send_digest_now():
    send_daily_digest()
    return jsonify({"ok": True})

# ── Daily digest ───────────────────────────────────────────────────────────────

def send_daily_digest():
    logger.info("Sending daily digest...")
    data = load_data()
    today = datetime.now().date()
    soon = today + timedelta(days=7)

    def diff(ds):
        if not ds: return None
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        return (d - today).days

    open_tasks = [t for t in data["tasks"] if not t.get("done")]
    overdue  = [t for t in open_tasks if t.get("due") and diff(t["due"]) is not None and diff(t["due"]) < 0]
    due_today= [t for t in open_tasks if t.get("due") and diff(t["due"]) == 0]
    due_week = [t for t in open_tasks if t.get("due") and diff(t["due"]) is not None and 0 < diff(t["due"]) <= 7]
    no_date  = [t for t in open_tasks if not t.get("due")]
    cold     = [c for c in data["contacts"] if not c.get("lastContact") or diff(c["lastContact"]) is not None and diff(c["lastContact"]) < -21]

    def fmt(t):
        return f"  • {t['name']} ({t.get('project','')}/{t.get('section','')}){' — ' + t['due'] if t.get('due') else ''}"

    lines = [f"Good morning — Basil's daily briefing for {today.strftime('%A, %B %d')}.", ""]

    if overdue:
        lines += [f"OVERDUE ({len(overdue)})", ""] + [fmt(t) for t in overdue] + [""]
    if due_today:
        lines += [f"DUE TODAY ({len(due_today)})", ""] + [fmt(t) for t in due_today] + [""]
    if due_week:
        lines += [f"DUE THIS WEEK ({len(due_week)})", ""] + [fmt(t) for t in due_week] + [""]
    if cold:
        lines += [f"CONTACTS NEEDING ATTENTION ({len(cold)})", ""] + [f"  • {c['name']} ({c.get('org','')})" for c in cold] + [""]
    if not (overdue or due_today or due_week):
        lines.append("Nothing due in the next 7 days.")

    body = "\n".join(lines)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Basil — {today.strftime('%A, %B %d')}"
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = DIGEST_EMAIL
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, DIGEST_EMAIL, msg.as_string())
        logger.info("Digest sent.")
    except Exception as e:
        logger.error(f"Digest failed: {e}")

def run_scheduler():
    schedule.every().day.at("07:30").do(send_daily_digest)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
