#!/usr/bin/env python3
"""
ProductHunt Daily Intel - Automated Product Analysis
"""

import os
import json
import time
import anthropic
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import requests
import tempfile

# Configuration
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GOOGLE_DRIVE_FOLDER_ID = os.environ["GOOGLE_DRIVE_FOLDER_ID"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

# Retry configuration
MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 60  # Start with 60 seconds

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are an expert product analyst specializing in reverse engineering SaaS products and analyzing user feedback. Your task is to analyze ProductHunt's Product of the Day and create a comprehensive product specification.

Your output must be a complete product specification in Markdown with these 12 sections:

1. Executive Summary (product name, one-liner, target user, value prop, URLs, date)
2. Product Overview (problem, solution, differentiators)
3. User Personas & Jobs-to-be-Done
4. Feature Specification (core features with user stories, inputs/outputs, business rules)
5. Technical Architecture (recommended tech stack with rationale, data model, API endpoints)
6. User Flows (critical journeys with success/error states)
7. UI/UX Specification (key screens, design system notes)
8. Non-Functional Requirements (performance, security, scalability, accessibility)
9. Implementation Roadmap (phased checklist)
10. Open Questions & Assumptions
11. Competitive Context (competitor comparison table, market positioning)
12. Enhancement: Pain Point Solution (from ProductHunt comments - source quote, frequency, proposed feature with user story, technical approach, UI changes)

Be comprehensive enough that someone could build the product from your spec."""

USER_PROMPT_TEMPLATE = """Today's date is {date}.

Analyze ProductHunt's Product of the Day and create a comprehensive product specification.

## Instructions

1. **Find Product of the Day**: Search ProductHunt for today's #1 Product of the Day (or yesterday's if today's winner hasn't been announced - winners are announced ~3pm PT / 11pm GMT). If #1 has insufficient info, use #2.

2. **Research thoroughly**:
   - Product's official website (features, pricing)
   - ProductHunt page and ALL comments
   - 2-3 direct competitors
   - Tech blogs, reviews, job postings (for tech stack signals)

3. **Analyze pain points** from ProductHunt comments:
   - Feature requests
   - Complaints and concerns
   - "I wish it could..." statements
   - Workarounds users mention
   - Select the highest-priority UNADDRESSED pain point

4. **Generate the full 12-section specification** with:
   - Inferred tech stack with rationale
   - Actionable implementation roadmap
   - Section 12: Enhancement based on real user feedback

Begin your research now."""


def call_claude_with_retry(messages: list, system: str) -> anthropic.types.Message:
    """Call Claude API with automatic retry on rate limit errors."""
    for attempt in range(MAX_RETRIES):
        try:
            return client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=16000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                system=system,
                messages=messages
            )
        except anthropic.RateLimitError as e:
            if attempt == MAX_RETRIES - 1:
                raise  # Re-raise on final attempt
            delay = INITIAL_RETRY_DELAY * (2 ** attempt)  # Exponential backoff: 60s, 120s, 240s, 480s
            print(f"  Rate limited. Waiting {delay} seconds before retry {attempt + 2}/{MAX_RETRIES}...")
            time.sleep(delay)
    raise Exception("Max retries exceeded")


def run_analysis() -> tuple[str, str, str]:
    """Run the ProductHunt analysis using Claude with web search."""
    today = datetime.now().strftime("%A, %B %d, %Y")
    print(f"Starting ProductHunt analysis for {today}...")

    messages = [{"role": "user", "content": USER_PROMPT_TEMPLATE.format(date=today)}]

    response = call_claude_with_retry(messages, SYSTEM_PROMPT)

    while response.stop_reason == "tool_use":
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"  Searching: {block.input.get('query', 'N/A')}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Search completed"
                })
        messages.append({"role": "user", "content": tool_results})
        response = call_claude_with_retry(messages, SYSTEM_PROMPT)

    spec_content = "".join(block.text for block in response.content if hasattr(block, "text"))

    product_name = "Unknown Product"
    product_url = ""
    for line in spec_content.split("\n"):
        if line.startswith("# ") and " - Product Specification" in line:
            product_name = line.replace("# ", "").replace(" - Product Specification", "").strip()
        if "**Product URL:**" in line:
            product_url = line.split("**Product URL:**")[1].strip()

    print(f"Analysis complete for: {product_name}")
    return product_name, spec_content, product_url


def upload_to_drive(product_name: str, spec_content: str) -> str:
    """Upload the spec to Google Drive as a Google Doc in a Shared Drive."""
    print("Uploading to Google Drive...")

    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive_service = build("drive", "v3", credentials=credentials)

    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{today} - {product_name}"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(spec_content)
        temp_path = f.name

    try:
        file_metadata = {
            "name": filename,
            "parents": [GOOGLE_DRIVE_FOLDER_ID],
            "mimeType": "application/vnd.google-apps.document"
        }
        media = MediaFileUpload(temp_path, mimetype="text/markdown", resumable=True)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True
        ).execute()

        doc_url = file.get("webViewLink", f"https://docs.google.com/document/d/{file['id']}")
        print(f"Uploaded: {doc_url}")
        return doc_url
    finally:
        os.unlink(temp_path)


def send_slack_notification(success: bool, product_name: str = "", doc_url: str = "", product_url: str = "", error_message: str = ""):
    """Send Slack notification."""
    if success:
        payload = {
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "✅ ProductHunt Daily Intel Complete", "emoji": True}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Product:*\n{product_name}"},
                    {"type": "mrkdwn", "text": f"*Date:*\n{datetime.now().strftime('%Y-%m-%d')}"}
                ]},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"📄 <{doc_url}|View Specification>"}}
            ]
        }
        if product_url:
            payload["blocks"].append({"type": "section", "text": {"type": "mrkdwn", "text": f"🔗 <{product_url}|Visit Product>"}})
    else:
        payload = {
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "❌ ProductHunt Daily Intel Failed", "emoji": True}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Error:*\n```{error_message[:500]}```"}}
            ]
        }

    try:
        requests.post(SLACK_WEBHOOK_URL, json=payload, headers={"Content-Type": "application/json"}).raise_for_status()
        print("Slack notification sent")
    except Exception as e:
        print(f"Slack notification failed: {e}")


def main():
    try:
        product_name, spec_content, product_url = run_analysis()
        doc_url = upload_to_drive(product_name, spec_content)
        send_slack_notification(True, product_name, doc_url, product_url)
        print("Daily intel complete!")
    except Exception as e:
        send_slack_notification(False, error_message=str(e))
        raise


if __name__ == "__main__":
    main()
