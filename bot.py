from instagrapi import Client
import time
import os
from dotenv import load_dotenv
import requests
from datetime import datetime, timedelta

load_dotenv()

USERNAME = os.getenv("INSTA_USERNAME")
PASSWORD = os.getenv("INSTA_PASSWORD")

try:
    client = Client()
    client.load_settings("session.json")
    client.login(USERNAME, PASSWORD)
except:
    client = Client()
    client.login(USERNAME, PASSWORD)
    client.dump_settings("session.json")

processed_messages = set()

def generate_ai_response_text(prompt: str) -> str:
    try:
        url = f"https://text.pollinations.ai/{prompt}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.text.strip()
    except Exception as e:
        print(f"⚠️ AI request failed: {e}")
        return "Sorry, I couldn't get a reply right now."

def handle_message(msg, thread_id: str):
    text = msg.text.strip()
    if msg.id in processed_messages:
        return
    processed_messages.add(msg.id)
    print(f"💬 Mentioned Prompt: {text}")
    ai_reply = generate_ai_response_text(text)
    print(f"🤖 Reply: {ai_reply[:80]}...")
    try:
        client.direct_send(ai_reply, thread_ids=[thread_id])
    except Exception as e:
        print(f"⚠️ Error sending message: {e}")

print("🤖 Nefer Bot is running (mention-based mode)...")

while True:
    try:
        threads = client.direct_threads(amount=10)
        two_minutes_ago = datetime.now() - timedelta(minutes=2)
        for thread in threads:
            if len(thread.users) > 2:
                messages = client.direct_messages(thread.id, amount=20)
                for msg in messages:
                    if msg.user_id == client.user_id:
                        continue
                    if msg.timestamp and msg.timestamp.replace(tzinfo=None) >= two_minutes_ago:
                        if msg.mentions:
                            for mention in msg.mentions:
                                if mention.user.username.lower() == client.username.lower():
                                    handle_message(msg, thread.id)
                                    break
        time.sleep(10)
    except Exception as e:
        print(f"⚠️ Main loop error: {e}")
        time.sleep(10)
