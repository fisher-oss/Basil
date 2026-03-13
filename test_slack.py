#!/usr/bin/env python3
"""
Slack connection diagnostic — run this on Railway by temporarily
changing your Procfile to: worker: python test_slack.py
"""
import os
import time
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

load_dotenv()

token = os.environ["SLACK_BOT_TOKEN"]
channel = os.environ["SLACK_CHANNEL_ID"]

print(f"Token starts with: {token[:20]}...")
print(f"Channel ID: {channel}")
print()

client = WebClient(token=token)

# Test 1: auth check
print("TEST 1: Auth check...")
try:
    auth = client.auth_test()
    print(f"  Bot name: {auth['user']}")
    print(f"  Workspace: {auth['team']}")
    print(f"  Bot ID: {auth['user_id']}")
    print("  Auth: PASSED")
except SlackApiError as e:
    print(f"  Auth FAILED: {e}")
print()

# Test 2: channel info
print("TEST 2: Channel info...")
try:
    info = client.conversations_info(channel=channel)
    ch = info["channel"]
    print(f"  Channel name: #{ch['name']}")
    print(f"  Is private: {ch['is_private']}")
    print(f"  Is member: {ch['is_member']}")
    print("  Channel info: PASSED")
except SlackApiError as e:
    print(f"  Channel info FAILED: {e}")
print()

# Test 3: fetch history with no oldest filter
print("TEST 3: Fetching last 5 messages (no timestamp filter)...")
try:
    result = client.conversations_history(channel=channel, limit=5)
    messages = result.get("messages", [])
    print(f"  Found {len(messages)} messages")
    for m in messages:
        print(f"  - [{m.get('ts')}] {m.get('text', '')[:60]}")
    if not messages:
        print("  Channel appears empty or bot cannot see messages")
except SlackApiError as e:
    print(f"  History FAILED: {e}")
print()

# Test 4: post a message
print("TEST 4: Posting test message to channel...")
try:
    client.chat_postMessage(channel=channel, text="🔧 Diagnostic test — connection confirmed.")
    print("  Post: PASSED")
except SlackApiError as e:
    print(f"  Post FAILED: {e}")

print()
print("Done. Check results above.")
