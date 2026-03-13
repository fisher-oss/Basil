#!/usr/bin/env python3
"""
Fisher Task Automation System
Slack → Claude → Asana + Daily Email Digest
"""

import os
import json
import time
import logging
import re
from datetime import datetime, timedelta
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import asana
from asana.rest import ApiException
from anthropic import Anthropic
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import schedule
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── Clients ────────────────────────────────────────────────────────────────────
slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

asana_configuration = asana.Configuration()
asana_configuration.access_token = os.environ["ASANA_ACCESS_TOKEN"]
asana_api_client = asana.ApiClient(asana_configuration)

projects_api   = asana.ProjectsApi(asana_api_client)
sections_api   = asana.SectionsApi(asana_api_client)
tasks_api      = asana.TasksApi(asana_api_client)

ASANA_WORKSPACE_GID = os.environ["ASANA_WORKSPACE_GID"]
SLACK_CHANNEL_ID    = os.environ["SLACK_CHANNEL_ID"]
EMAIL_ADDRESS       = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD      = os.environ["EMAIL_APP_PASSWORD"]
DIGEST_EMAIL        = os.environ["DIGEST_EMAIL"]

# ── Taxonomy ───────────────────────────────────────────────────────────────────
TAXONOMY = {
    "RSLF US": [
        "Donors", "Administration", "IMLS Partnership - Event",
        "IMLS Partnership - Contract", "Grant Projects - NEH",
        "Grant Projects - State Dept", "Grant Projects - Other",
        "Relationships", "Board"
    ],
    "RSLF UK": [
        "MCC", "Administration", "Donors",
        "Scruton Lecture", "Relationships", "Board"
    ],
    "NCAS": [
        "Donors and Fundraising", "Embassy Architecture",
        "Strategic Plan", "Newsletters", "Website Updates"
    ],
    "Dux Culturae": [
        "Business Admin", "Writing"
    ],
    "City Council Campaign": [
        "Fundraising", "Events", "Canvassing", "Issues", "Relationships"
    ],
    "City Business": [
        "Arts Commission", "Issues"
    ]
}

TAXONOMY_STRING = "\n".join(
    f"- {cat}: {', '.join(subs)}" for cat, subs in TAXONOMY.items()
)

PARSE_PROMPT = f"""You are a task parser for Fisher, Executive Director of the Roger Scruton Legacy Foundation.
Your job is to convert casual notes into structured Asana tasks.

Fisher's project taxonomy:
{TAXONOMY_STRING}

Given a casual note, extract and return a JSON object with these fields:
- task_name: concise, action-oriented task title (start with a verb)
- project: one of the exact category names above (RSLF US, RSLF UK, NCAS, Dux Culturae, City Council Campaign, City Business)
- section: the most relevant sub-category from that project's list
- due_date: ISO format date (YYYY-MM-DD) if mentioned, otherwise null. Interpret relative dates like "Friday", "next week", "end of month" based on today's date: {{today}}
- priority: "High", "Medium", or "Low" based on urgency/importance signals in the note
- notes: any additional context, names, or details worth preserving from the original note
- original: the original note text verbatim

Rules:
- If the note mentions a specific person (donor, contact, official), preserve their name in the task_name or notes
- If no clear project match exists, default to the most plausible one
- Return ONLY valid JSON, no preamble or explanation
- If the note contains multiple distinct tasks, return a JSON array of task objects
"""

# ── Asana project/team cache ──────────────────────────────────────────────────
_asana_project_cache = {}
_asana_team_gid = None

def get_team_gid():
    """Get the first team in the workspace — required for project creation."""
    global _asana_team_gid
    if _asana_team_gid:
        return _asana_team_gid
    teams_api = asana.TeamsApi(asana_api_client)
    teams = list(teams_api.get_teams_for_workspace(ASANA_WORKSPACE_GID, {"opt_fields": "gid,name"}))
    if teams:
        _asana_team_gid = teams[0]["gid"]
        logger.info(f"Using Asana team: {teams[0]['name']} ({_asana_team_gid})")
        return _asana_team_gid
    raise Exception("No teams found in Asana workspace")

def get_or_create_asana_project(project_name):
    if project_name in _asana_project_cache:
        return _asana_project_cache[project_name]

    all_projects = projects_api.get_projects_for_workspace(ASANA_WORKSPACE_GID, {"opt_fields": "name,gid"})
    for p in all_projects:
        if p["name"] == project_name:
            _asana_project_cache[project_name] = p["gid"]
            return p["gid"]

    team_gid = get_team_gid()
    body = {"data": {"name": project_name, "workspace": ASANA_WORKSPACE_GID, "team": team_gid, "color": "light-blue"}}
    new_project = projects_api.create_project(body, {})
    _asana_project_cache[project_name] = new_project["gid"]
    logger.info(f"Created Asana project: {project_name}")
    return new_project["gid"]


def get_or_create_section(project_gid, section_name):
    all_sections = sections_api.get_sections_for_project(project_gid, {"opt_fields": "name,gid"})
    for s in all_sections:
        if s["name"] == section_name:
            return s["gid"]
    body = {"data": {"name": section_name}}
    new_section = sections_api.create_section_for_project(project_gid, body, {})
    return new_section["gid"]


def parse_note_with_claude(note):
    today = datetime.now().strftime("%Y-%m-%d (%A, %B %d %Y)")
    prompt = PARSE_PROMPT.replace("{today}", today)
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": f"{prompt}\n\nNote to parse:\n{note}"}]
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, list) else [parsed]


def create_asana_task(task_data):
    project_name = task_data.get("project", "RSLF US")
    section_name = task_data.get("section", "")
    project_gid = get_or_create_asana_project(project_name)

    notes_parts = []
    if task_data.get("notes"):
        notes_parts.append(task_data["notes"])
    if task_data.get("priority"):
        notes_parts.append(f"Priority: {task_data['priority']}")
    if task_data.get("original"):
        notes_parts.append(f"Original note: {task_data['original']}")

    task_body = {"data": {
        "name": task_data["task_name"],
        "projects": [project_gid],
        "workspace": ASANA_WORKSPACE_GID,
        "notes": "\n\n".join(notes_parts),
    }}
    if task_data.get("due_date"):
        task_body["data"]["due_on"] = task_data["due_date"]

    created = tasks_api.create_task(task_body, {})

    if section_name:
        try:
            section_gid = get_or_create_section(project_gid, section_name)
            sections_api.add_task_for_section(section_gid, {"data": {"task": created["gid"]}}, {})
        except Exception as e:
            logger.warning(f"Could not assign section '{section_name}': {e}")

    logger.info(f"Created task: '{task_data['task_name']}' in {project_name} / {section_name}")
    return created


# ── Slack polling ──────────────────────────────────────────────────────────────
_last_ts = str(time.time() - 60)

def poll_slack():
    global _last_ts
    try:
        logger.info(f"Polling Slack channel {SLACK_CHANNEL_ID}...")
        result = slack_client.conversations_history(
            channel=SLACK_CHANNEL_ID,
            oldest=_last_ts,
            limit=20
        )
        messages = result.get("messages", [])
        logger.info(f"Slack returned {len(messages)} messages.")
        if messages:
            _last_ts = messages[0]["ts"]
        new_messages = [
            m for m in reversed(messages)
            if m.get("type") == "message" and not m.get("bot_id")
        ]
        logger.info(f"{len(new_messages)} new non-bot messages to process.")
        for msg in new_messages:
            text = msg.get("text", "").strip()
            if not text:
                continue
            logger.info(f"Processing message: {text[:80]}...")
            try:
                tasks = parse_note_with_claude(text)
                created = []
                for task in tasks:
                    create_asana_task(task)
                    created.append(task["task_name"])
                confirmation = "✅ Created:\n" + "\n".join(f"• {t}" for t in created)
                slack_client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=confirmation)
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                slack_client.chat_postMessage(
                    channel=SLACK_CHANNEL_ID,
                    text=f"⚠️ Couldn't parse that note. Error: {str(e)}"
                )
            _last_ts = msg["ts"]
        if new_messages:
            _last_ts = new_messages[-1]["ts"]
    except SlackApiError as e:
        logger.error(f"Slack API error: {e}")


# ── Daily email digest ─────────────────────────────────────────────────────────
def send_daily_digest():
    logger.info("Generating daily digest...")
    today = datetime.now().date()
    soon = today + timedelta(days=7)
    try:
        opts = {
            "completed": False,
            "due_on.before": soon.isoformat(),
            "opt_fields": "name,due_on,projects.name,memberships.section.name,notes"
        }
        all_tasks = list(tasks_api.search_tasks_for_workspace(ASANA_WORKSPACE_GID, opts))
    except Exception as e:
        logger.error(f"Could not fetch tasks: {e}")
        return

    overdue   = [t for t in all_tasks if t.get("due_on") and t["due_on"] < today.isoformat()]
    due_today = [t for t in all_tasks if t.get("due_on") == today.isoformat()]
    due_week  = [t for t in all_tasks if t.get("due_on") and today.isoformat() < t["due_on"] <= soon.isoformat()]
    no_date   = [t for t in all_tasks if not t.get("due_on")]

    def format_task(t):
        project = t["projects"][0]["name"] if t.get("projects") else "—"
        section = ""
        if t.get("memberships"):
            sec = t["memberships"][0].get("section")
            if sec:
                section = f" / {sec['name']}"
        due = f" [{t['due_on']}]" if t.get("due_on") else ""
        return f"  • {t['name']} ({project}{section}){due}"

    lines = [f"Good morning — here's your task digest for {today.strftime('%A, %B %d')}.", ""]
    if overdue:
        lines += [f"🔴 OVERDUE ({len(overdue)})", ""] + [format_task(t) for t in overdue] + [""]
    if due_today:
        lines += [f"🟡 DUE TODAY ({len(due_today)})", ""] + [format_task(t) for t in due_today] + [""]
    if due_week:
        lines += [f"🔵 DUE THIS WEEK ({len(due_week)})", ""] + [format_task(t) for t in due_week] + [""]
    if no_date:
        lines += [f"⚪ NO DUE DATE ({len(no_date)})", ""] + [format_task(t) for t in no_date] + [""]
    if not (overdue or due_today or due_week):
        lines.append("Nothing due in the next 7 days. Clear skies.")

    body = "\n".join(lines)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Task Digest — {today.strftime('%B %d, %Y')}"
    msg["From"]    = EMAIL_ADDRESS
    msg["To"]      = DIGEST_EMAIL
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, DIGEST_EMAIL, msg.as_string())
        logger.info("Daily digest sent.")
    except Exception as e:
        logger.error(f"Email failed: {e}")


# ── Main loop ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Task automation system started.")
    schedule.every().day.at("07:30").do(send_daily_digest)
    while True:
        schedule.run_pending()
        poll_slack()
        time.sleep(30)
