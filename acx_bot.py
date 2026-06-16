import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import gspread
from google.oauth2.service_account import Credentials
from google import genai
from pydantic import BaseModel, Field
from typing import List, Literal, Optional
import datetime
import json
import logging
import os
import tempfile
import requests
import time
from collections import deque
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import urllib3

import invoice_module as inv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(stream=sys.stdout),
        logging.FileHandler("ledger_bot.log", encoding="utf-8")
    ]
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# =====================================================================
# 1. CONFIGURATION & CREDENTIALS
# =====================================================================
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY",       "")
WHATSAPP_TOKEN       = os.environ.get("WHATSAPP_TOKEN",       "")
WHATSAPP_PHONE_ID    = os.environ.get("WHATSAPP_PHONE_ID",    "")
WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", "")

# ENTITY_TYPE is now a fallback only — real routing comes from the Users tab
DEFAULT_ENTITY_TYPE  = os.environ.get("ENTITY_TYPE", "ngo")

# --- MASTER USER (super admin / business owner) ---
# This phone number gets access to invoice generation and master commands,
# checked BEFORE the normal Users tab lookup.
MASTER_PHONE = os.environ.get("MASTER_PHONE", "").strip().replace("+", "").replace(" ", "")

# --- Master_Users sheet — your own business profile and invoice log ---
MASTER_SHEET_NAME = os.environ.get("MASTER_SHEET_NAME", "Master_Users")

# Your business profile, used on every generated invoice/receipt.
# Hardcoded here since it changes rarely; could be moved to the Profile tab later.
BUSINESS_PROFILE = {
    "name":       "ACX BUSINESS MANAGEMENT & CONSULTANCY",
    "address":    "Wairaka B, Mwiri, Kakira Town Council, Jinja, Eastern Uganda",
    "tin":        "1058473921",
    "email":      "info@acxconsultants.online",
    "phone":      "256790152102",
    "currency":   "UGX",
    "logo_path":  "logo.png",   # place logo.png in the same folder as acx_bot.py
}

INVOICE_NUMBER_PREFIX = "ACX"   # invoice numbers look like ACX-2026-0001

for var_name, var_val in [
    ("GEMINI_API_KEY",       GEMINI_API_KEY),
    ("WHATSAPP_TOKEN",       WHATSAPP_TOKEN),
    ("WHATSAPP_PHONE_ID",    WHATSAPP_PHONE_ID),
    ("WEBHOOK_VERIFY_TOKEN", WEBHOOK_VERIFY_TOKEN),
]:
    if not var_val:
        log.warning(f"WARNING: {var_name} is not set! Check your .env file.")
    else:
        log.info(f"OK: {var_name} loaded ({len(var_val)} chars).")

if MASTER_PHONE:
    log.info(f"Master phone configured: {MASTER_PHONE}")
else:
    log.warning("WARNING: MASTER_PHONE is not set. Invoice features will be unavailable.")

PENDING_TIMEOUT_MINUTES  = 10
DUPLICATE_WINDOW_MINUTES = 5

# =====================================================================
# 2. GOOGLE SHEETS CONNECTION
# =====================================================================
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

try:
    google_creds_env = os.environ.get("GOOGLE_CREDENTIALS", "")
    if google_creds_env:
        # Production (Railway): credentials passed as a JSON string env var
        creds_dict = json.loads(google_creds_env)
        google_cloud_creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        log.info("Google credentials loaded from GOOGLE_CREDENTIALS env var.")
    else:
        # Local development: credentials read from acx.json file
        google_cloud_creds = Credentials.from_service_account_file("acx.json", scopes=scopes)
        log.info("Google credentials loaded from acx.json file.")

    gc_client = gspread.authorize(google_cloud_creds)
    log.info("Google Sheets connected successfully.")
except Exception as e:
    log.error(f"Failed to connect to Google Sheets: {e}")
    raise

try:
    with open("config.json", "r") as f:
        accounting_config = json.load(f)
    log.info("config.json loaded successfully.")
except (json.JSONDecodeError, FileNotFoundError) as e:
    log.warning(f"config.json could not be loaded ({e}). Using fallback.")
    accounting_config = {
        "ngo":        {"sheet_name": "NGO_Grant_Ledger"},
        "for-profit": {"sheet_name": "Whatsapp_Bot_Ledger"}
    }

ENTITY_SYSTEM_PROMPTS = {
    "ngo": (
        "You are a strict NGO fund accounting assistant. "
        "Every transaction must be mapped to an approved grant budget line. "
        "You extract structured financial data from informal WhatsApp messages sent by field staff."
    ),
    "for-profit": (
        "You are a business bookkeeping assistant. "
        "You extract structured financial transactions from WhatsApp messages. "
        "Separate business transactions from personal ones carefully."
    )
}

ai_client = genai.Client(api_key=GEMINI_API_KEY)

# =====================================================================
# 3. USER REGISTRY — Phone number routing via Google Sheets Users tab
# =====================================================================
# Cache structure: { phone: { "name": str, "entity_type": str, "role": str } }
_users_cache: dict         = {}
_users_cache_time: datetime.datetime | None = None
USERS_CACHE_SECONDS = 300   # refresh every 5 minutes

# Expected columns in the Users tab (row 1 is headers):
# A: Phone | B: Name | C: Entity Type | D: Role | E: Sheet Name (optional override)
USERS_TAB_NAME = "Users"

def fetch_users_registry() -> dict:
    """
    Pull the Users tab from the NGO_Grant_Ledger sheet.
    Returns a dict keyed by phone number.
    Caches for 5 minutes so we don't hit Sheets on every message.
    """
    global _users_cache, _users_cache_time

    now = datetime.datetime.now()
    if _users_cache_time and (now - _users_cache_time).total_seconds() < USERS_CACHE_SECONDS:
        return _users_cache

    # Read from the NGO sheet by default — the Users tab is org-wide
    base_sheet_name = accounting_config.get("ngo", {}).get("sheet_name", "NGO_Grant_Ledger")

    try:
        users_sheet = gc_client.open(base_sheet_name).worksheet(USERS_TAB_NAME)
        all_rows    = users_sheet.get_all_values()[1:]  # skip header

        registry = {}
        for row in all_rows:
            if len(row) < 4 or not row[0].strip():
                continue
            phone       = row[0].strip().replace(" ", "").replace("+", "")
            name        = row[1].strip() if len(row) > 1 else "Unknown"
            entity_type = row[2].strip().lower() if len(row) > 2 else DEFAULT_ENTITY_TYPE
            role        = row[3].strip().lower() if len(row) > 3 else "staff"
            sheet_override = row[4].strip() if len(row) > 4 else ""

            # Skip inactive users — they can no longer log transactions
            status = row[7].strip().lower() if len(row) > 7 else "active"
            if status == "inactive":
                continue

            registry[phone] = {
                "name":           name,
                "entity_type":    entity_type,
                "role":           role,
                "sheet_override": sheet_override
            }

        _users_cache      = registry
        _users_cache_time = now
        log.info(f"Users registry refreshed: {len(registry)} user(s) loaded.")
        return registry

    except Exception as e:
        log.error(f"Could not read Users tab: {e}")
        return _users_cache   # return stale cache rather than failing hard


def get_user(phone: str) -> dict | None:
    """Look up a phone number in the registry. Returns None if not found."""
    registry = fetch_users_registry()
    return registry.get(phone)


def get_user_sheet(user: dict) -> any:
    """
    Open and return the correct Google Sheet for this user.
    Uses sheet_override if set, otherwise falls back to entity_type config.
    """
    entity_type = user["entity_type"]
    if user.get("sheet_override"):
        sheet_name = user["sheet_override"]
    else:
        sheet_name = accounting_config.get(entity_type, {}).get(
            "sheet_name",
            accounting_config[DEFAULT_ENTITY_TYPE]["sheet_name"]
        )
    return gc_client.open(sheet_name).sheet1


def invalidate_users_cache():
    """Force a fresh fetch next time get_user() is called."""
    global _users_cache_time
    _users_cache_time = None

# =====================================================================
# 4. LEARN FROM CORRECTIONS — Store corrections to improve future prompts
# =====================================================================
# Corrections are stored in a Corrections tab in Google Sheets.
# Structure: Timestamp | Phone | Original Text | Field Corrected | Old Value | New Value | Activity
CORRECTIONS_TAB_NAME = "Corrections"

def log_correction_to_sheet(
    phone: str,
    field: str,
    old_value: str,
    new_value: str,
    primary_activity: str,
    original_message: str = ""
):
    """Persist a user correction to the Corrections tab for future learning."""
    base_sheet_name = accounting_config.get("ngo", {}).get("sheet_name", "NGO_Grant_Ledger")
    try:
        corrections_sheet = gc_client.open(base_sheet_name).worksheet(CORRECTIONS_TAB_NAME)
        row = [
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            phone,
            original_message,
            field,
            str(old_value),
            str(new_value),
            primary_activity
        ]
        corrections_sheet.append_row(row)
        log.info(f"Correction logged: {field} '{old_value}' -> '{new_value}' for {phone}")
    except Exception as e:
        log.error(f"Could not log correction to sheet: {e}")


def fetch_recent_corrections(entity_type: str, limit: int = 10) -> list[dict]:
    """
    Pull the most recent corrections to inject as few-shot examples into the AI prompt.
    Returns a list of correction dicts, most recent first.
    """
    base_sheet_name = accounting_config.get(entity_type, {}).get("sheet_name", "NGO_Grant_Ledger")
    try:
        corrections_sheet = gc_client.open(base_sheet_name).worksheet(CORRECTIONS_TAB_NAME)
        all_rows = corrections_sheet.get_all_values()[1:]
        recent = all_rows[-limit:] if len(all_rows) > limit else all_rows
        recent.reverse()   # most recent first

        results = []
        for row in recent:
            if len(row) >= 6:
                results.append({
                    "field":    row[3],
                    "old":      row[4],
                    "new":      row[5],
                    "activity": row[6] if len(row) > 6 else ""
                })
        return results
    except Exception as e:
        log.error(f"Could not fetch corrections: {e}")
        return []


def build_corrections_context(entity_type: str) -> str:
    """
    Build a prompt section from recent corrections so Gemini learns from past mistakes.
    Only included when there are corrections to show.
    """
    corrections = fetch_recent_corrections(entity_type, limit=8)
    if not corrections:
        return ""

    lines = ["\n*** LEARNING FROM PAST CORRECTIONS ***"]
    lines.append("Users have previously corrected these classifications. Apply these lessons:")
    for c in corrections:
        if c["field"] == "activity_code":
            lines.append(f"  - For '{c['activity']}': use '{c['new']}' not '{c['old']}'")
        elif c["field"] == "amount":
            lines.append(f"  - Amount '{c['old']}' was corrected to '{c['new']}' for '{c['activity']}'")
        else:
            lines.append(f"  - {c['field'].title()} was corrected from '{c['old']}' to '{c['new']}'")
    lines.append("Apply these corrections when you see similar transactions.\n")
    return "\n".join(lines)

# =====================================================================
# 5. STATE STORES & KEYWORD SETS
# =====================================================================
pending_store:       dict[str, deque] = {}
recent_fingerprints: dict[str, list]  = {}
correction_state:    dict[str, dict]  = {}

CONFIRM_KEYWORDS  = {"yes", "confirm", "ok", "okay", "yep", "sure", "save", "go ahead"}
CANCEL_KEYWORDS   = {"no", "cancel", "stop", "abort", "nope", "discard", "reject"}
CORRECT_KEYWORDS  = {"correct", "fix", "wrong", "edit", "change", "update"}
UNDO_KEYWORDS     = {"undo", "undo last", "/undo"}
BALANCE_KEYWORDS  = {"balance", "/balance", "total", "how much"}
GREETING_KEYWORDS = {"hi", "hello", "hey", "good morning", "good afternoon",
                     "good evening", "hie", "howdy", "greetings"}
HELP_KEYWORDS     = {"help", "/help", "what can you do", "commands",
                     "menu", "options", "guide", "how does this work"}

# Admin command prefixes (case-insensitive, matched against lowercased text)
ADDUSER_PREFIX    = "adduser "
REMOVEUSER_PREFIX = "removeuser "

# Retry queue: messages that failed due to Gemini overload, waiting to be retried
# Structure: { phone: [ { "raw_text": str, "user": dict, "prompt": str, "attempts": int, "next_retry": datetime }, ... ] }
retry_queue: dict[str, list] = {}
MAX_RETRY_ATTEMPTS  = 3
RETRY_DELAY_SECONDS = 15   # wait 15s between attempts

# =====================================================================
# 6. PYDANTIC SCHEMA
# =====================================================================
class TransactionItem(BaseModel):
    purpose: str = Field(
        description=(
            "A detailed narrative combining: the PRIMARY ACTIVITY (e.g. 'Home Tracing'), "
            "the person (e.g. 'James'), the location (e.g. 'Buikwe'), AND the specific "
            "spend type for this line item (e.g. 'Meals' or 'Transportation'). "
            "Format: '<Activity> for <Person> to <Location> - <Spend Type>'. "
            "Example: 'Home Tracing for James to Buikwe - Meals'."
        )
    )
    line_item: str = Field(
        description=(
            "The specific spend type for this individual row. "
            "Examples: 'Meals', 'Transportation', 'Accommodation', 'Airtime', 'Soap'. "
            "Do NOT put the whole activity name here."
        )
    )
    amount: int = Field(description="The transaction amount as a whole number.")
    who: str = Field(description="Name of the person involved. If none, set to 'N/A'.")
    location: str = Field(description="Location, district, or school. If none, set to 'N/A'.")
    category: str = Field(
        description=(
            "THE MAIN ACCOUNT from the Chart of Accounts matching the PRIMARY ACTIVITY. "
            "COST CENTER ANCHORING: All line items from the same activity share the same category. "
            "Example: 'Home Tracing' meals AND transport both get '4700 - Social Work & Field'."
        )
    )
    activity_code: str = Field(
        description=(
            "THE ACTIVITY CODE from the Chart of Accounts matching the PRIMARY ACTIVITY. "
            "COST CENTER ANCHORING: All line items from the same activity share the same code. "
            "For item-based messages (soap, vaseline), each item gets its OWN specific code. "
            "Must match exactly an entry from the Chart of Accounts."
        )
    )
    type: Literal["Income", "Expense", "Drawings", "Capital Investment"]
    record_type: Literal["Business", "Personal"] = Field(
        description=(
            "'Business' for normal NGO/company operations. "
            "'Personal' ONLY if the owner is directly touching business funds "
            "(Drawing or Capital Investment)."
        )
    )


class MultiTransactionRequest(BaseModel):
    primary_activity: str = Field(
        description=(
            "The PRIMARY ACTIVITY identified from the message. "
            "Activity-based: the field activity (e.g. 'Home Tracing'). "
            "Item-based procurement: the category (e.g. 'Welfare Items Procurement'). "
            "Multiple unrelated activities: join with ' + '."
        )
    )
    transactions: List[TransactionItem] = Field(
        description=(
            "One row per amount. All rows from the same activity share the same "
            "category and activity_code. Item-based messages give each item its own code."
        )
    )

# =====================================================================
# 7. CHART OF ACCOUNTS
# =====================================================================
_coa_cache: dict = {}

def fetch_coa_rules(entity_type: str) -> list[str]:
    global _coa_cache
    cache_entry = _coa_cache.get(entity_type)
    if cache_entry:
        cached_time, cached_rules = cache_entry
        if (datetime.datetime.now() - cached_time).total_seconds() < 600:
            return cached_rules

    sheet_name = accounting_config.get(entity_type, {}).get("sheet_name", "NGO_Grant_Ledger")
    try:
        coa_sheet = gc_client.open(sheet_name).worksheet("CoA_Reference")
        all_data  = coa_sheet.get_all_values()[1:]
        rules = [
            f"{r[0].strip()} -> {r[1].strip()}"
            for r in all_data if len(r) >= 2 and r[0].strip() and r[1].strip()
        ]
        log.info(f"Fetched {len(rules)} CoA rules for '{entity_type}'.")
        _coa_cache[entity_type] = (datetime.datetime.now(), rules)
        return rules
    except Exception as e:
        log.error(f"Could not read CoA_Reference for '{entity_type}': {e}")
        return []


def build_coa_context(entity_type: str) -> str:
    rules = fetch_coa_rules(entity_type)
    if not rules:
        return (
            "*** CHART OF ACCOUNTS: Not available. Use best judgment. ***\n"
            "If unsure: Account '9999 - Unassigned/Review', Activity 'UNCATEGORIZED'."
        )
    rules_text = "\n    ".join(rules)
    return f"""
*** STRICT CHART OF ACCOUNTS - USE THESE EXACTLY ***

    {rules_text}

================================================================
*** TWO MESSAGE TYPES - IDENTIFY WHICH ONE FIRST ***

TYPE A - ACTIVITY-BASED (field activity named, e.g. Home Tracing, School Visit):
  ALL rows share the SAME category and activity_code (the field activity).
  Do NOT reassign meals/transport/etc. to different cost centers.
  EXAMPLE: "Home Tracing for James to Buikwe, 50,000 meals, 20,000 transport"
  CORRECT: Both rows: category="4700 - Social Work & Field", activity_code="4701 - Home Tracing"
  WRONG: meals -> Food & Nutrition, transport -> Vehicle Use

TYPE B - ITEM-BASED (shopping/procurement list, no named activity):
  Category is shared, but each item gets its OWN specific activity_code.
  NEVER default to "4317 - Others" if the exact item exists in the CoA.
  EXAMPLE: "Spent 20,000 on soap and 3,000 on vaseline"
  CORRECT: Row 1: activity_code="4301 - Soaps"  Row 2: activity_code="4308 - Vasline"
  WRONG: both rows -> "4317 - Others"

SUSPENSE GUARDRAIL: Only use '9999 - Unassigned/Review' / 'UNCATEGORIZED'
if the item genuinely does not appear anywhere in the Chart of Accounts.
================================================================
"""

# =====================================================================
# 8. WHATSAPP REPLY
# =====================================================================
def send_whatsapp_reply(recipient_phone: str, message_text: str):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        log.warning("WhatsApp credentials not set - reply skipped.")
        return

    url     = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type":  "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to":   recipient_phone,
        "type": "text",
        "text": {"body": message_text}
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15, verify=False)
        log.info(f"Meta API response: {r.status_code} - {r.text[:200]}")
        r.raise_for_status()
        log.info(f"Reply sent to {recipient_phone}.")
    except requests.exceptions.HTTPError as e:
        log.error(f"HTTP error sending reply: {e} | Response: {r.text}")
    except requests.exceptions.ConnectionError as e:
        log.error(f"Connection error sending reply: {e}")
    except requests.RequestException as e:
        log.error(f"Failed to send reply: {e}")


def send_whatsapp_document(recipient_phone: str, file_path: str, filename: str, caption: str = "") -> bool:
    """
    Send a local file (e.g. a generated PDF) directly to a WhatsApp user as a
    document attachment. Two-step process per Meta's API:
      1. Upload the file to get a media_id
      2. Send a message of type 'document' referencing that media_id

    Returns True on success, False on failure (caller can fall back to other
    delivery methods, e.g. a Drive link, if this fails).
    """
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        log.warning("WhatsApp credentials not set - document send skipped.")
        return False

    upload_url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/media"
    headers    = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

    try:
        with open(file_path, "rb") as f:
            files = {
                "file": (filename, f, "application/pdf"),
            }
            data = {"messaging_product": "whatsapp", "type": "application/pdf"}
            r = requests.post(upload_url, headers=headers, files=files, data=data, timeout=30, verify=False)

        log.info(f"Media upload response: {r.status_code} - {r.text[:200]}")
        r.raise_for_status()
        media_id = r.json().get("id")

        if not media_id:
            log.error("Media upload succeeded but no media_id returned.")
            return False

        # Step 2: send the document message
        send_url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
        send_headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type":  "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to":   recipient_phone,
            "type": "document",
            "document": {
                "id":       media_id,
                "filename": filename,
            }
        }
        if caption:
            payload["document"]["caption"] = caption

        r2 = requests.post(send_url, headers=send_headers, json=payload, timeout=15, verify=False)
        log.info(f"Document send response: {r2.status_code} - {r2.text[:200]}")
        r2.raise_for_status()
        log.info(f"Document '{filename}' sent to {recipient_phone}.")
        return True

    except requests.exceptions.HTTPError as e:
        log.error(f"HTTP error sending document: {e}")
        return False
    except requests.exceptions.ConnectionError as e:
        log.error(f"Connection error sending document: {e}")
        return False
    except Exception as e:
        log.error(f"Failed to send document: {e}")
        return False

# =====================================================================
# 9. PENDING QUEUE HELPERS
# =====================================================================
def enqueue_pending(phone: str, primary_activity: str, transactions: list):
    if phone not in pending_store:
        pending_store[phone] = deque()
    expires_at = datetime.datetime.now() + datetime.timedelta(minutes=PENDING_TIMEOUT_MINUTES)
    pending_store[phone].append({
        "primary_activity": primary_activity,
        "transactions":     transactions,
        "expires_at":       expires_at
    })
    log.info(f"Queued batch for {phone}. Queue depth: {len(pending_store[phone])}")


def peek_pending(phone: str) -> dict | None:
    if phone not in pending_store:
        return None
    q = pending_store[phone]
    while q and datetime.datetime.now() > q[0]["expires_at"]:
        expired = q.popleft()
        log.info(f"Expired pending batch for {phone}: '{expired['primary_activity']}'")
    return q[0] if q else None


def pop_pending(phone: str) -> dict | None:
    if phone not in pending_store or not pending_store[phone]:
        return None
    return pending_store[phone].popleft()


def queue_depth(phone: str) -> int:
    if phone not in pending_store:
        return 0
    return len(pending_store[phone])


def build_preview_message(batch: dict, phone: str = "") -> str:
    lines = [f"*{batch['primary_activity']}*\n"]
    total = 0
    for i, t in enumerate(batch["transactions"], 1):
        lines.append(f"  {i}. {t['line_item']} - UGX {t['amount']:,}")
        lines.append(f"     {t['activity_code']}")
        if t["who"] != "N/A":
            lines.append(f"     Person: {t['who']}")
        if t["location"] != "N/A":
            lines.append(f"     Location: {t['location']}")
        total += t["amount"]
    lines.append(f"\nTotal: UGX {total:,}")
    lines.append("\nReply:\n  *yes* to save\n  *no* to cancel\n  *correct* to fix something")
    if phone:
        remaining = queue_depth(phone)
        if remaining > 1:
            lines.append(f"(You have {remaining - 1} more batch(es) after this one.)")
    return "\n".join(lines)

# =====================================================================
# 10. DUPLICATE DETECTION
# =====================================================================
def make_fingerprint(transactions: list) -> str:
    pairs = sorted((t["line_item"].lower(), t["amount"]) for t in transactions)
    return json.dumps(pairs)


def is_duplicate(phone: str, fingerprint: str) -> bool:
    if phone not in recent_fingerprints:
        return False
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=DUPLICATE_WINDOW_MINUTES)
    recent_fingerprints[phone] = [
        f for f in recent_fingerprints[phone] if f["logged_at"] > cutoff
    ]
    return any(f["fingerprint"] == fingerprint for f in recent_fingerprints[phone])


def register_fingerprint(phone: str, fingerprint: str):
    if phone not in recent_fingerprints:
        recent_fingerprints[phone] = []
    recent_fingerprints[phone].append({
        "fingerprint": fingerprint,
        "logged_at":   datetime.datetime.now()
    })

# =====================================================================
# 11. WRITE TO GOOGLE SHEETS
# =====================================================================
def write_batch_to_sheet(phone: str, batch: dict, user: dict) -> tuple[int, int]:
    transactions     = batch["transactions"]
    primary_activity = batch["primary_activity"]
    entity_type      = user["entity_type"]
    written, skipped = 0, 0

    target_sheet = get_user_sheet(user)

    for item in transactions:
        if (
            entity_type == "for-profit"
            and item["record_type"] == "Personal"
            and item["type"] not in ("Drawings", "Capital Investment")
        ):
            log.warning(f"Skipping personal item: '{item['purpose']}'")
            skipped += 1
            continue

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            timestamp,
            phone,
            user.get("name", "Unknown"),   # NEW: staff member's name
            primary_activity,
            item["purpose"],
            item["line_item"],
            item["amount"],
            item["who"],
            item["location"],
            item["category"],
            item["activity_code"],
            item["type"],
            item["record_type"]
        ]
        target_sheet.append_row(row)
        log.info(f"Row written for {user.get('name')} ({phone}): {row}")
        written += 1

    return written, skipped

# =====================================================================
# 11b. SHEET HEADERS — Written on startup if missing
# =====================================================================

# Column headers matching the exact order written by write_batch_to_sheet
TRANSACTIONS_HEADERS = [
    "Timestamp",        # A
    "Phone",            # B
    "Staff Name",       # C
    "Primary Activity", # D
    "Purpose",          # E
    "Line Item",        # F
    "Amount (UGX)",     # G
    "Who",              # H
    "Location",         # I
    "Category",         # J
    "Activity Code",    # K
    "Type",             # L
    "Record Type",      # M
]

USERS_HEADERS = [
    "Phone",            # A
    "Name",             # B
    "Entity Type",      # C
    "Role",             # D
    "Sheet Override",   # E
    "Added By",         # F
    "Date Added",       # G
    "Status",           # H
]

CORRECTIONS_HEADERS = [
    "Timestamp",        # A
    "Phone",            # B
    "Original Message", # C
    "Field Corrected",  # D
    "Old Value",        # E
    "New Value",        # F
    "Activity",         # G
]

COA_HEADERS = [
    "Main Account",     # A
    "Sub-Account / Activity Code",  # B
]

# Headers for the Invoices tab in the Master_Users sheet
INVOICES_TAB_NAME = "Invoices"
INVOICES_HEADERS = [
    "Invoice Number",   # A
    "Date",             # B
    "Client Name",      # C
    "Client Address",   # D
    "Items (JSON)",     # E
    "Total",            # F
    "Currency",         # G
    "Payment Status",   # H
    "PDF Link",         # I
    "Created At",       # J
]


def ensure_headers(worksheet, expected_headers: list):
    """
    Check if row 1 of a worksheet matches expected headers.
    If the sheet is empty or headers are missing/wrong, write them.
    Does NOT overwrite existing data rows.
    """
    try:
        existing = worksheet.row_values(1)
        if existing == expected_headers:
            return  # already correct, nothing to do

        if not existing:
            # Sheet is completely empty — write headers
            worksheet.insert_row(expected_headers, index=1)
            log.info(f"Headers written to empty sheet: {worksheet.title}")
        else:
            # Row 1 exists but doesn't match — log a warning, don't overwrite
            # (could be data already there from before headers were introduced)
            log.warning(
                f"Sheet '{worksheet.title}' row 1 does not match expected headers. "
                f"Found: {existing[:3]}... Expected: {expected_headers[:3]}... "
                "Skipping to avoid overwriting data."
            )
    except Exception as e:
        log.error(f"Could not check/write headers for '{worksheet.title}': {e}")


def ensure_all_headers():
    """
    Run at startup. Checks all known tabs across all configured sheets
    and writes headers where they are missing.
    """
    log.info("Checking sheet headers on startup...")

    for entity_type, config in accounting_config.items():
        sheet_name = config.get("sheet_name", "")
        if not sheet_name:
            continue
        try:
            workbook = gc_client.open(sheet_name)

            # Main transactions sheet (Sheet1)
            ensure_headers(workbook.sheet1, TRANSACTIONS_HEADERS)

            # Users tab
            try:
                users_ws = workbook.worksheet(USERS_TAB_NAME)
                ensure_headers(users_ws, USERS_HEADERS)
            except gspread.exceptions.WorksheetNotFound:
                log.warning(f"'{USERS_TAB_NAME}' tab not found in '{sheet_name}'. Create it manually.")

            # Corrections tab
            try:
                corr_ws = workbook.worksheet(CORRECTIONS_TAB_NAME)
                ensure_headers(corr_ws, CORRECTIONS_HEADERS)
            except gspread.exceptions.WorksheetNotFound:
                log.warning(f"'{CORRECTIONS_TAB_NAME}' tab not found in '{sheet_name}'. Create it manually.")

            # CoA_Reference tab — headers only, don't touch data
            try:
                coa_ws = workbook.worksheet("CoA_Reference")
                ensure_headers(coa_ws, COA_HEADERS)
            except gspread.exceptions.WorksheetNotFound:
                log.warning(f"'CoA_Reference' tab not found in '{sheet_name}'.")

            log.info(f"Header check complete for '{sheet_name}'.")

        except Exception as e:
            log.error(f"Could not open sheet '{sheet_name}' for header check: {e}")

    # --- Master_Users sheet (your own business invoicing) ---
    if MASTER_PHONE:
        try:
            try:
                master_workbook = gc_client.open(MASTER_SHEET_NAME)
                log.info(f"Master sheet '{MASTER_SHEET_NAME}' found.")
            except gspread.exceptions.SpreadsheetNotFound:
                master_workbook = gc_client.create(MASTER_SHEET_NAME)
                log.info(f"Master sheet '{MASTER_SHEET_NAME}' created.")
                # Share with the master user's Google account if needed could go here

            # Invoices tab
            try:
                invoices_ws = master_workbook.worksheet(INVOICES_TAB_NAME)
            except gspread.exceptions.WorksheetNotFound:
                invoices_ws = master_workbook.add_worksheet(
                    title=INVOICES_TAB_NAME, rows=1000, cols=len(INVOICES_HEADERS)
                )
                log.info(f"'{INVOICES_TAB_NAME}' tab created in '{MASTER_SHEET_NAME}'.")

            ensure_headers(invoices_ws, INVOICES_HEADERS)

            # Rename default Sheet1 to something harmless if it's still empty and unused
            try:
                default_ws = master_workbook.sheet1
                if default_ws.title == "Sheet1" and not default_ws.row_values(1):
                    default_ws.update_title("Notes")
            except Exception:
                pass

            log.info(f"Master sheet '{MASTER_SHEET_NAME}' header check complete.")

        except Exception as e:
            log.error(f"Could not set up Master_Users sheet: {e}")

    log.info("All header checks done.")


# =====================================================================
# 12. GREETING & HELP MESSAGES
# =====================================================================
def build_greeting(user: dict | None, phone: str = "") -> str:
    if MASTER_PHONE and phone == MASTER_PHONE:
        base = (
            f"Hello! Welcome back to the ACX Ledger Bot.\n\n"
            f"You are signed in as the *Master User*.\n\n"
            "Type *invoice* to create a new invoice/receipt for a client.\n"
        )
        if user:
            base += (
                f"\nYou are also registered as *{user['role'].title()}* "
                f"on the *{user['entity_type'].upper()}* account, "
                "so you can log transactions too."
            )
        return base

    if user:
        return (
            f"Hello {user['name']}! Welcome back to the Ledger Bot.\n\n"
            f"You are registered as *{user['role'].title()}* "
            f"on the *{user['entity_type'].upper()}* account.\n\n"
            "Just describe what you spent and I will handle the rest. "
            "Type *help* to see everything I can do."
        )
    return (
        "Hello! Welcome to the Ledger Bot.\n\n"
        "I help field staff log expenses and track spending — all through WhatsApp.\n\n"
        "It looks like your number is not registered yet. "
        "Please contact your administrator to be added to the system."
    )


HELP_REPLY_STAFF = (
    "*What I can do for you:*\n\n"
    "*Log an expense*\n"
    "Just describe it naturally:\n"
    "  \"Home tracing for James to Buikwe, 50,000 meals, 20,000 transport\"\n"
    "  \"Bought soap 20,000 and vaseline 3,000\"\n\n"
    "*After I show you a summary:*\n"
    "  yes        - save the transactions\n"
    "  no         - cancel and discard\n"
    "  correct    - fix something before saving\n\n"
    "*Other commands:*\n"
    "  balance    - see your total income and expenses\n"
    "  undo       - delete your last saved entry\n"
    "  help       - show this menu\n\n"
    "Send me a transaction message to get started!"
)

HELP_REPLY_ADMIN = (
    "*What I can do for you:*\n\n"
    "*Log an expense*\n"
    "Just describe it naturally:\n"
    "  \"Home tracing for James to Buikwe, 50,000 meals, 20,000 transport\"\n\n"
    "*After I show you a summary:*\n"
    "  yes / no / correct\n\n"
    "*Staff commands:*\n"
    "  balance    - your income and expense totals\n"
    "  undo       - delete your last entry\n\n"
    "*Admin commands:*\n"
    "  adduser <phone> <name> <role>  - add a new user\n"
    "  removeuser <phone>             - deactivate a user\n"
    "  listusers                      - see all active users\n\n"
    "Example:\n"
    "  adduser 256700123456 Jane Apio staff"
)

HELP_REPLY_MASTER = (
    "*Master commands:*\n\n"
    "  invoice  - create a new invoice/receipt for a client\n\n"
    "Once started, follow the prompts:\n"
    "  - Enter client name\n"
    "  - Enter client address (or 'skip')\n"
    "  - Add items as: description, amount\n"
    "  - Type 'done' when finished\n"
    "  - Review, then 'yes' to generate the PDF\n"
    "  - 'edit' to fix something, 'cancel' to discard\n"
)

def get_help_reply(user: dict | None, phone: str = "") -> str:
    if MASTER_PHONE and phone == MASTER_PHONE:
        reply = HELP_REPLY_MASTER
        if user and user.get("role") == "admin":
            reply += "\n" + HELP_REPLY_ADMIN
        elif user:
            reply += "\n" + HELP_REPLY_STAFF
        return reply
    if user and user.get("role") == "admin":
        return HELP_REPLY_ADMIN
    return HELP_REPLY_STAFF

# =====================================================================
# 13. CORRECTION FLOW
# =====================================================================
CORRECTABLE_FIELDS = {
    "1": "amount",   "2": "who",       "3": "location",  "4": "line_item",
    "amount":    "amount",  "person":   "who",  "who":      "who",
    "name":      "who",     "location": "location", "district": "location",
    "place":     "location","item":     "line_item", "line item": "line_item",
    "type":      "line_item",
}

def build_correction_menu(batch: dict) -> str:
    lines = ["What would you like to correct?\n"]
    lines.append("Which field?")
    lines.append("  1 - Amount")
    lines.append("  2 - Person name (who)")
    lines.append("  3 - Location / district")
    lines.append("  4 - Line item / spend type")
    if len(batch["transactions"]) > 1:
        lines.append("\nWhich transaction? Reply field then number.")
        lines.append("e.g. \"1 2\" = fix the amount of transaction 2")
    else:
        lines.append("\nReply with just the field number (e.g. \"1\" for amount)")
    return "\n".join(lines)


def apply_correction(phone: str, raw_text: str, user: dict | None) -> bool:
    state = correction_state.get(phone)

    if state and state.get("step") == "choose_field":
        parts     = raw_text.strip().lower().split()
        field_key = parts[0] if parts else ""
        tx_index  = int(parts[1]) - 1 if len(parts) > 1 and parts[1].isdigit() else 0

        field = CORRECTABLE_FIELDS.get(field_key)
        if not field:
            send_whatsapp_reply(
                phone,
                "Sorry, I did not recognise that field.\n"
                "Reply with 1 (amount), 2 (person), 3 (location), or 4 (line item)."
            )
            return True

        batch = peek_pending(phone)
        if not batch or tx_index >= len(batch["transactions"]):
            send_whatsapp_reply(phone, "Could not find that transaction. Please try again.")
            correction_state.pop(phone, None)
            return True

        correction_state[phone] = {
            "step":     "enter_value",
            "field":    field,
            "tx_index": tx_index,
        }

        field_labels = {
            "amount":    "amount (numbers only, e.g. 45000)",
            "who":       "person's name",
            "location":  "location or district",
            "line_item": "line item / spend type"
        }
        tx = batch["transactions"][tx_index]
        send_whatsapp_reply(
            phone,
            f"Current {field_labels[field]}: {tx[field]}\n"
            f"Please type the correct value:"
        )
        return True

    if state and state.get("step") == "enter_value":
        field    = state["field"]
        tx_index = state["tx_index"]
        batch    = peek_pending(phone)

        if not batch:
            send_whatsapp_reply(phone, "Your pending batch expired. Please send the transaction again.")
            correction_state.pop(phone, None)
            return True

        new_value = raw_text.strip()
        old_value = batch["transactions"][tx_index][field]

        if field == "amount":
            cleaned = new_value.replace(",", "").replace(" ", "")
            if not cleaned.isdigit():
                send_whatsapp_reply(phone, "Amount must be a number (e.g. 45000). Please try again:")
                return True
            new_value = int(cleaned)

        # Apply correction in memory
        batch["transactions"][tx_index][field] = new_value

        # Rebuild the purpose narrative
        t = batch["transactions"][tx_index]
        t["purpose"] = (
            f"{batch['primary_activity']} for {t['who']} "
            f"to {t['location']} - {t['line_item']}"
        )

        # Persist correction to Corrections tab for future learning
        entity_type = user["entity_type"] if user else DEFAULT_ENTITY_TYPE
        log_correction_to_sheet(
            phone        = phone,
            field        = field,
            old_value    = str(old_value),
            new_value    = str(new_value),
            primary_activity = batch["primary_activity"],
            original_message = state.get("original_message", "")
        )

        correction_state.pop(phone, None)

        send_whatsapp_reply(
            phone,
            f"Correction applied.\n\n"
            f"Updated summary:\n{build_preview_message(batch, phone)}"
        )
        return True

    return False

# =====================================================================
# 13b. MANUAL ONBOARDING — Admin adds/removes staff via WhatsApp
# =====================================================================

def handle_adduser(raw_text: str, admin_phone: str, admin_user: dict) -> bool:
    """
    Admin command: adduser <phone> <name> <role> [entity_type]
    Example:  adduser 256700123456 Jane Apio staff
    Example:  adduser 256700123456 Jane Apio admin ngo
    Role options: staff, admin
    Entity type: ngo (default) or for-profit
    """
    parts = raw_text.strip().split(None, 4)
    # parts[0] = "adduser", parts[1] = phone, parts[2...] = name parts, last = role [entity]

    if len(parts) < 4:
        send_whatsapp_reply(
            admin_phone,
            "Incorrect format. Use:\n"
            "  adduser <phone> <name> <role>\n\n"
            "Example:\n"
            "  adduser 256700123456 Jane Apio staff\n"
            "  adduser 256700123456 Jane Apio admin\n\n"
            "Role options: staff, admin\n"
            "Phone must include country code, no + sign."
        )
        return True

    new_phone   = parts[1].strip().replace("+", "").replace(" ", "")
    role        = parts[-1].strip().lower()
    entity_type = admin_user.get("entity_type", DEFAULT_ENTITY_TYPE)

    # Extract name — everything between phone and role
    name_parts = parts[2:-1]
    name = " ".join(name_parts).strip()

    if role not in ("staff", "admin"):
        send_whatsapp_reply(
            admin_phone,
            f"Invalid role '{role}'. Use 'staff' or 'admin'."
        )
        return True

    if not new_phone.isdigit() or len(new_phone) < 9:
        send_whatsapp_reply(
            admin_phone,
            f"Invalid phone number '{new_phone}'. "
            "Use full number with country code, no + or spaces.\n"
            "Example: 256700123456"
        )
        return True

    # Write to Users tab
    base_sheet_name = accounting_config.get(
        admin_user["entity_type"], {}
    ).get("sheet_name", "NGO_Grant_Ledger")

    try:
        workbook   = gc_client.open(base_sheet_name)
        users_ws   = workbook.worksheet(USERS_TAB_NAME)
        all_rows   = users_ws.get_all_values()[1:]  # skip header

        # Check if phone already exists
        existing_phones = [r[0].strip() for r in all_rows if r]
        if new_phone in existing_phones:
            send_whatsapp_reply(
                admin_phone,
                f"{new_phone} is already registered as a user.\n"
                "Use removeuser first if you want to re-add them."
            )
            return True

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_row = [
            new_phone,
            name,
            entity_type,
            role,
            "",                              # Sheet Override (blank)
            admin_user.get("name", "Admin"), # Added By
            timestamp,                       # Date Added
            "active"                         # Status
        ]
        users_ws.append_row(new_row)

        # Invalidate users cache so the new user is picked up immediately
        invalidate_users_cache()

        log.info(f"User added by {admin_phone}: {new_row}")

        # Notify the admin
        send_whatsapp_reply(
            admin_phone,
            f"Done! {name} ({new_phone}) has been added as *{role}*.\n\n"
            f"Entity: {entity_type.upper()}\n"
            f"They can now log transactions immediately."
        )

        # Send a welcome message to the new user
        send_whatsapp_reply(
            new_phone,
            f"Hello {name}! You have been added to the ACX Ledger Bot "
            f"by {admin_user.get('name', 'your administrator')}.\n\n"
            f"You are registered as *{role}* on the *{entity_type.upper()}* account.\n\n"
            "You can start logging expenses right away. "
            "Type *help* to see all available commands."
        )

    except gspread.exceptions.WorksheetNotFound:
        send_whatsapp_reply(
            admin_phone,
            f"Could not find the Users tab in '{base_sheet_name}'. "
            "Please create a tab named 'Users' in your Google Sheet first."
        )
    except Exception as e:
        log.error(f"adduser failed: {e}")
        send_whatsapp_reply(admin_phone, f"Failed to add user. Error: {str(e)[:100]}")

    return True


def handle_removeuser(raw_text: str, admin_phone: str, admin_user: dict) -> bool:
    """
    Admin command: removeuser <phone>
    Sets the user's status to inactive. Keeps their history intact.
    Example: removeuser 256700123456
    """
    parts = raw_text.strip().split()
    if len(parts) < 2:
        send_whatsapp_reply(
            admin_phone,
            "Incorrect format. Use:\n"
            "  removeuser <phone>\n\n"
            "Example:\n"
            "  removeuser 256700123456"
        )
        return True

    target_phone = parts[1].strip().replace("+", "").replace(" ", "")
    base_sheet_name = accounting_config.get(
        admin_user["entity_type"], {}
    ).get("sheet_name", "NGO_Grant_Ledger")

    try:
        workbook = gc_client.open(base_sheet_name)
        users_ws = workbook.worksheet(USERS_TAB_NAME)
        all_rows = users_ws.get_all_values()  # includes header at row 0

        target_row_index = None
        target_name      = ""
        for i, row in enumerate(all_rows[1:], start=2):  # gspread rows are 1-indexed, data starts row 2
            if row and row[0].strip() == target_phone:
                target_row_index = i
                target_name      = row[1] if len(row) > 1 else target_phone
                break

        if target_row_index is None:
            send_whatsapp_reply(
                admin_phone,
                f"No user found with phone number {target_phone}."
            )
            return True

        # Update status column (H = column 8) to "inactive"
        # gspread update_cell(row, col, value) — col 8 = Status
        users_ws.update_cell(target_row_index, 8, "inactive")
        invalidate_users_cache()

        log.info(f"User {target_phone} ({target_name}) marked inactive by {admin_phone}")

        send_whatsapp_reply(
            admin_phone,
            f"Done. {target_name} ({target_phone}) has been deactivated.\n\n"
            "Their transaction history is preserved. "
            "They will no longer be able to log transactions."
        )

    except gspread.exceptions.WorksheetNotFound:
        send_whatsapp_reply(
            admin_phone,
            f"Could not find the Users tab in '{base_sheet_name}'."
        )
    except Exception as e:
        log.error(f"removeuser failed: {e}")
        send_whatsapp_reply(admin_phone, f"Failed to remove user. Error: {str(e)[:100]}")

    return True


def handle_listusers(admin_phone: str, admin_user: dict) -> bool:
    """
    Admin command: listusers
    Shows all active users in the organisation.
    """
    base_sheet_name = accounting_config.get(
        admin_user["entity_type"], {}
    ).get("sheet_name", "NGO_Grant_Ledger")

    try:
        workbook = gc_client.open(base_sheet_name)
        users_ws = workbook.worksheet(USERS_TAB_NAME)
        all_rows = users_ws.get_all_values()[1:]  # skip header

        active_users = [
            r for r in all_rows
            if r and len(r) >= 4 and (len(r) <= 7 or r[7].strip().lower() != "inactive")
        ]

        if not active_users:
            send_whatsapp_reply(admin_phone, "No active users found in the system.")
            return True

        lines = [f"*Active users ({len(active_users)}):*\n"]
        for r in active_users:
            phone_val = r[0] if len(r) > 0 else "?"
            name_val  = r[1] if len(r) > 1 else "?"
            role_val  = r[3] if len(r) > 3 else "?"
            lines.append(f"  {name_val} | {phone_val} | {role_val}")

        send_whatsapp_reply(admin_phone, "\n".join(lines))

    except Exception as e:
        log.error(f"listusers failed: {e}")
        send_whatsapp_reply(admin_phone, "Could not retrieve user list. Please try again.")

    return True



# =====================================================================
# 13c. MASTER USER — INVOICE GENERATION
# =====================================================================

INVOICE_TRIGGER_WORDS = {"invoice", "/invoice", "new invoice", "create invoice", "receipt", "/receipt"}
INVOICE_HELP_WORDS    = {"invoices", "invoice help", "/invoices"}


def handle_master_command(raw_text: str, phone: str) -> bool:
    """
    Handle commands available only to the master phone number.
    Returns True if the message was handled here.
    Returns False to fall through (caller checks invoice flow next).
    """
    text_lower = raw_text.strip().lower()

    if text_lower in INVOICE_TRIGGER_WORDS:
        prompt = inv.start_invoice_flow(phone)
        send_whatsapp_reply(phone, prompt)
        return True

    if text_lower in INVOICE_HELP_WORDS:
        send_whatsapp_reply(
            phone,
            "*Master commands:*\n\n"
            "  invoice  - create a new invoice/receipt\n\n"
            "Once started, follow the prompts:\n"
            "  - Enter client name\n"
            "  - Enter client address (or 'skip')\n"
            "  - Add items as: description, amount\n"
            "  - Type 'done' when finished\n"
            "  - Review, then 'yes' to generate the PDF\n"
            "  - 'edit' to fix something, 'cancel' to discard"
        )
        return True

    return False


def handle_invoice_flow_message(raw_text: str, phone: str) -> bool:
    """
    Process one message while the master user is mid-invoice-flow.
    If the flow completes, generates the PDF, then tries to deliver it:
      1. Upload to Drive and send the link (preferred — keeps a record)
      2. If Drive isn't available (folder not shared, quota issue, etc.),
         send the PDF directly as a WhatsApp document attachment instead.
    Either way, the invoice is logged to the Invoices tab.
    """
    reply_text, finished_state = inv.process_invoice_message(phone, raw_text)

    if finished_state is None:
        send_whatsapp_reply(phone, reply_text)
        return True

    send_whatsapp_reply(phone, reply_text)

    try:
        invoice_number = inv.get_next_invoice_number(
            gc_client, MASTER_SHEET_NAME, INVOICES_TAB_NAME, INVOICE_NUMBER_PREFIX
        )
        invoice_date    = finished_state.get("invoice_date") or datetime.datetime.now().strftime("%Y-%m-%d")
        payment_status  = finished_state.get("payment_status", "Pending")

        safe_invoice_num = invoice_number.replace("/", "-")
        local_filename = os.path.join(tempfile.gettempdir(), f"{safe_invoice_num}.pdf")

        total = inv.generate_invoice_pdf(
            output_path=local_filename,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            business_profile=BUSINESS_PROFILE,
            client_name=finished_state["client_name"],
            client_address=finished_state.get("client_address", ""),
            items=finished_state["items"],
            payment_status=payment_status,
        )

        # --- Try Drive first ---
        pdf_link = ""
        try:
            pdf_link = inv.upload_pdf_to_drive(
                gc_client, local_filename, f"{safe_invoice_num}.pdf"
            )
            log.info(f"Invoice {invoice_number} uploaded to Drive: {pdf_link}")
        except inv.DriveFolderNotFound as e:
            log.warning(f"Drive upload skipped for {invoice_number}: {e}")
        except Exception as e:
            log.warning(f"Drive upload failed for {invoice_number}: {e}")

        # Log to Invoices tab regardless of delivery method
        inv.log_invoice_to_sheet(
            gc_client, MASTER_SHEET_NAME, INVOICES_TAB_NAME,
            invoice_number, invoice_date,
            finished_state["client_name"], finished_state.get("client_address", ""),
            finished_state["items"], total, BUSINESS_PROFILE["currency"],
            payment_status,
            pdf_link or "(sent directly via WhatsApp)"
        )

        if pdf_link:
            send_whatsapp_reply(
                phone,
                f"Invoice {invoice_number} created.\n\n"
                f"Client: {finished_state['client_name']}\n"
                f"Date: {invoice_date}\n"
                f"Status: {payment_status}\n"
                f"Total: UGX {total:,}\n\n"
                f"Download: {pdf_link}"
            )
        else:
            # Fallback: send the PDF directly as a WhatsApp document
            sent = send_whatsapp_document(
                phone, local_filename, f"{safe_invoice_num}.pdf",
                caption=f"Invoice {invoice_number} - {finished_state['client_name']}"
            )
            if sent:
                send_whatsapp_reply(
                    phone,
                    f"Invoice {invoice_number} created.\n\n"
                    f"Client: {finished_state['client_name']}\n"
                    f"Date: {invoice_date}\n"
                    f"Status: {payment_status}\n"
                    f"Total: UGX {total:,}\n\n"
                    "(PDF sent above as an attachment.)"
                )
            else:
                send_whatsapp_reply(
                    phone,
                    f"Invoice {invoice_number} was generated (Total: UGX {total:,}) "
                    "but could not be delivered via Drive or WhatsApp attachment. "
                    "Please check the bot logs."
                )

        try:
            os.remove(local_filename)
        except OSError:
            pass

    except Exception as e:
        log.error(f"Invoice generation failed: {e}", exc_info=True)
        send_whatsapp_reply(
            phone,
            "Something went wrong generating the PDF. "
            "Your invoice details were not saved. Please try again with *invoice*."
        )

    inv.cancel_invoice_flow(phone)
    return True


# =====================================================================
# 14. COMMAND HANDLER
# =====================================================================
def handle_command(raw_text: str, phone: str, user: dict | None) -> bool:
    text_lower = raw_text.strip().lower()

    # --- MASTER USER COMMANDS (invoice generation) ---
    # Checked first, before anything else, regardless of Users tab status.
    if MASTER_PHONE and phone == MASTER_PHONE:
        if handle_master_command(raw_text, phone):
            return True
        # If master is mid-invoice-flow, intercept here even if text doesn't
        # match a fresh command keyword
        if inv.is_in_invoice_flow(phone):
            return handle_invoice_flow_message(raw_text, phone)

    # --- ADMIN COMMANDS (adduser, removeuser, listusers) ---
    # These are only available to users with role = admin
    if text_lower.startswith(ADDUSER_PREFIX) and len(text_lower) > len(ADDUSER_PREFIX):
        if user and user.get("role") == "admin":
            return handle_adduser(raw_text, phone, user)
        elif user:
            send_whatsapp_reply(phone, "Sorry, only admins can add users.")
        else:
            send_whatsapp_reply(phone, "You are not registered. Contact your administrator.")
        return True

    if text_lower.startswith(REMOVEUSER_PREFIX) and len(text_lower) > len(REMOVEUSER_PREFIX):
        if user and user.get("role") == "admin":
            return handle_removeuser(raw_text, phone, user)
        elif user:
            send_whatsapp_reply(phone, "Sorry, only admins can remove users.")
        else:
            send_whatsapp_reply(phone, "You are not registered. Contact your administrator.")
        return True

    if text_lower in ("listusers", "/listusers", "list users"):
        if user and user.get("role") == "admin":
            return handle_listusers(phone, user)
        elif user:
            send_whatsapp_reply(phone, "Sorry, only admins can list users.")
        else:
            send_whatsapp_reply(phone, "You are not registered. Contact your administrator.")
        return True

    # --- GREETING ---
    if text_lower in GREETING_KEYWORDS:
        send_whatsapp_reply(phone, build_greeting(user, phone))
        return True

    # --- HELP ---
    if text_lower in HELP_KEYWORDS:
        send_whatsapp_reply(phone, get_help_reply(user, phone))
        return True

    # --- CORRECTION IN PROGRESS ---
    if phone in correction_state:
        return apply_correction(phone, raw_text, user)

    # --- START CORRECTION ---
    if text_lower in CORRECT_KEYWORDS:
        batch = peek_pending(phone)
        if not batch:
            send_whatsapp_reply(phone, "Nothing pending to correct.")
            return True
        correction_state[phone] = {"step": "choose_field"}
        send_whatsapp_reply(phone, build_correction_menu(batch))
        return True

    # --- CONFIRM ---
    if text_lower in CONFIRM_KEYWORDS:
        batch = peek_pending(phone)
        if not batch:
            send_whatsapp_reply(phone, "Nothing pending to confirm.")
            return True

        fingerprint = make_fingerprint(batch["transactions"])

        if is_duplicate(phone, fingerprint):
            pop_pending(phone)
            send_whatsapp_reply(
                phone,
                f"Warning: This looks like a duplicate - the same transactions were "
                f"already saved within the last {DUPLICATE_WINDOW_MINUTES} minutes. "
                "Entry discarded. Send again if this was intentional."
            )
            return True

        pop_pending(phone)
        written, skipped = write_batch_to_sheet(phone, batch, user)
        register_fingerprint(phone, fingerprint)

        reply_lines = [f"Saved! {written} transaction(s) recorded."]
        if skipped:
            reply_lines.append(f"({skipped} personal item(s) skipped.)")

        next_batch = peek_pending(phone)
        if next_batch:
            reply_lines.append(f"\nNext pending batch:\n{build_preview_message(next_batch, phone)}")
        else:
            reply_lines.append("No more pending batches.")

        send_whatsapp_reply(phone, "\n".join(reply_lines))
        return True

    # --- CANCEL ---
    if text_lower in CANCEL_KEYWORDS:
        correction_state.pop(phone, None)
        batch = pop_pending(phone)
        if not batch:
            send_whatsapp_reply(phone, "Nothing pending to cancel.")
            return True

        remaining = queue_depth(phone)
        reply = f"Cancelled: '{batch['primary_activity']}'."
        if remaining:
            next_batch = peek_pending(phone)
            reply += f"\n\nNext pending batch:\n{build_preview_message(next_batch, phone)}"
        else:
            reply += " No more pending batches."
        send_whatsapp_reply(phone, reply)
        return True

    # --- UNDO ---
    if text_lower in UNDO_KEYWORDS:
        try:
            target_sheet = get_user_sheet(user) if user else gc_client.open(
                accounting_config[DEFAULT_ENTITY_TYPE]["sheet_name"]
            ).sheet1
            all_values = target_sheet.get_all_values()
            if len(all_values) <= 1:
                send_whatsapp_reply(phone, "Nothing to undo - the sheet is empty.")
                return True
            last_row_index = None
            for i in range(len(all_values) - 1, 0, -1):
                if all_values[i][1] == phone:
                    last_row_index = i + 1
                    break
            if last_row_index is None:
                send_whatsapp_reply(phone, "No entries found from your number to undo.")
                return True
            deleted_row = all_values[last_row_index - 1]
            target_sheet.delete_rows(last_row_index)
            send_whatsapp_reply(
                phone,
                f"Undone! Deleted your last entry:\n"
                f"  Activity: {deleted_row[3]}\n"
                f"  Item: {deleted_row[5]} - UGX {deleted_row[6]}"
            )
            log.info(f"Row {last_row_index} deleted by {phone}")
        except Exception as e:
            log.error(f"Undo failed: {e}")
            send_whatsapp_reply(phone, "Undo failed. Please try again.")
        return True

    # --- BALANCE ---
    if text_lower in BALANCE_KEYWORDS:
        try:
            target_sheet = get_user_sheet(user) if user else gc_client.open(
                accounting_config[DEFAULT_ENTITY_TYPE]["sheet_name"]
            ).sheet1
            all_values = target_sheet.get_all_values()[1:]
            # Column indices shifted by 1 since we added Name column
            total = sum(
                int(row[6]) for row in all_values
                if row[1] == phone and len(row) > 11 and row[11] == "Expense"
            )
            income = sum(
                int(row[6]) for row in all_values
                if row[1] == phone and len(row) > 11 and row[11] == "Income"
            )
            send_whatsapp_reply(
                phone,
                f"Your logged totals:\n"
                f"  Income:  UGX {income:,}\n"
                f"  Expense: UGX {total:,}\n"
                f"  Net:     UGX {income - total:,}"
            )
        except Exception as e:
            log.error(f"Balance check failed: {e}")
            send_whatsapp_reply(phone, "Could not retrieve balance. Please try again.")
        return True

    return False

# =====================================================================
# 15. CORE AI HANDLER
# =====================================================================
# =====================================================================
# 15b. GEMINI RETRY ENGINE
# =====================================================================

def is_gemini_overload_error(e: Exception) -> bool:
    """Detect whether an exception is a Gemini capacity/overload error worth retrying."""
    error_str = str(e).lower()
    overload_signals = [
        "503", "overloaded", "resource exhausted", "quota", "rate limit",
        "unavailable", "capacity", "try again", "429", "500"
    ]
    return any(signal in error_str for signal in overload_signals)


def call_gemini_with_retry(raw_text: str, phone: str, user: dict, full_prompt: str, attempt: int):
    """
    Call Gemini and handle the response. If Gemini is overloaded:
    - Attempt 1: tell user we are processing, retry silently after 15s
    - Attempt 2: retry silently, no message
    - Attempt 3: last attempt, if still failing notify user calmly
    - After 3 failures: save raw message to retry queue so nothing is lost
    """
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=full_prompt,
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=MultiTransactionRequest,
                temperature=0.1
            ),
        )

        structured_data  = json.loads(response.text)
        transactions     = structured_data.get("transactions", [])
        primary_activity = structured_data.get("primary_activity", "N/A")

        log.info(f"Gemini parsed (attempt {attempt}): activity='{primary_activity}', {len(transactions)} row(s)")

        if not transactions:
            send_whatsapp_reply(
                phone,
                "I received your message but could not find a clear transaction in it.\n"
                "Please describe the expense again — for example:\n"
                "\"Spent 50,000 on meals for home tracing to Buikwe\""
            )
            return

        enqueue_pending(phone, primary_activity, transactions)
        batch   = peek_pending(phone)
        preview = build_preview_message(batch, phone)
        send_whatsapp_reply(phone, preview)

    except json.JSONDecodeError as e:
        log.error(f"JSON parse error on attempt {attempt}: {e}")
        # JSON errors are usually not overload — they mean Gemini returned garbage
        # Retry once, then give up gracefully
        if attempt < 2:
            log.info(f"Retrying after JSON error for {phone}...")
            time.sleep(RETRY_DELAY_SECONDS)
            call_gemini_with_retry(raw_text, phone, user, full_prompt, attempt + 1)
        else:
            save_to_retry_queue(phone, raw_text, user, full_prompt)
            send_whatsapp_reply(
                phone,
                "Your message was received but we had a small hiccup processing it. "
                "We have saved it and will process it automatically in a moment. "
                "You do not need to send it again."
            )

    except Exception as e:
        log.error(f"Gemini error on attempt {attempt}: {e}", exc_info=True)

        if is_gemini_overload_error(e) and attempt < MAX_RETRY_ATTEMPTS:
            # First failure — reassure the user, then retry silently
            if attempt == 1:
                send_whatsapp_reply(
                    phone,
                    "Got your message! Our AI assistant is a little busy right now "
                    "but your transaction has been saved. We will process it automatically "
                    "in a few seconds — no need to send it again."
                )
            log.info(f"Gemini overloaded. Waiting {RETRY_DELAY_SECONDS}s before retry {attempt + 1} for {phone}...")
            time.sleep(RETRY_DELAY_SECONDS)
            call_gemini_with_retry(raw_text, phone, user, full_prompt, attempt + 1)

        elif is_gemini_overload_error(e) and attempt >= MAX_RETRY_ATTEMPTS:
            # All retries exhausted — save to queue, calm message
            save_to_retry_queue(phone, raw_text, user, full_prompt)
            log.warning(f"All {MAX_RETRY_ATTEMPTS} Gemini attempts failed for {phone}. Saved to retry queue.")
            send_whatsapp_reply(
                phone,
                "Our AI assistant is experiencing high demand right now. "
                "Your message has been safely saved and will be processed automatically "
                "once things settle down — usually within a few minutes. "
                "You will receive your transaction summary as normal when it is ready."
            )
        else:
            # Non-overload error — unexpected, log it and notify calmly
            save_to_retry_queue(phone, raw_text, user, full_prompt)
            send_whatsapp_reply(
                phone,
                "We received your message but ran into a small technical issue. "
                "Your transaction has been saved and our team will ensure it is logged correctly. "
                "If you do not receive a confirmation within 10 minutes, please type *help*."
            )


def save_to_retry_queue(phone: str, raw_text: str, user: dict, full_prompt: str):
    """Save a failed message so it can be retried later."""
    if phone not in retry_queue:
        retry_queue[phone] = []
    retry_queue[phone].append({
        "raw_text":   raw_text,
        "user":       user,
        "full_prompt": full_prompt,
        "attempts":   0,
        "saved_at":   datetime.datetime.now().isoformat()
    })
    log.info(f"Saved to retry queue for {phone}. Queue size: {len(retry_queue[phone])}")


def process_retry_queue():
    """
    Attempt to process any messages sitting in the retry queue.
    Call this from a background thread or a scheduled endpoint.
    """
    if not retry_queue:
        return

    for phone, items in list(retry_queue.items()):
        if not items:
            del retry_queue[phone]
            continue

        item = items[0]   # process oldest first
        log.info(f"Retrying queued message for {phone}: '{item['raw_text'][:50]}...'")

        try:
            response = ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=item["full_prompt"],
                config=genai.types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=MultiTransactionRequest,
                    temperature=0.1
                ),
            )
            structured_data  = json.loads(response.text)
            transactions     = structured_data.get("transactions", [])
            primary_activity = structured_data.get("primary_activity", "N/A")

            if transactions:
                enqueue_pending(phone, primary_activity, transactions)
                batch   = peek_pending(phone)
                preview = build_preview_message(batch, phone)
                send_whatsapp_reply(
                    phone,
                    f"Good news — we have processed your earlier message!\n\n{preview}"
                )
                items.pop(0)
                log.info(f"Retry queue item processed successfully for {phone}.")
            else:
                items.pop(0)
                send_whatsapp_reply(phone, "We processed your saved message but could not find a transaction in it. Please resend.")

        except Exception as e:
            item["attempts"] += 1
            log.warning(f"Retry queue processing failed for {phone} (attempt {item['attempts']}): {e}")
            if item["attempts"] >= MAX_RETRY_ATTEMPTS:
                items.pop(0)
                send_whatsapp_reply(
                    phone,
                    "We were unable to automatically process one of your saved messages. "
                    "Please resend it when you are ready."
                )


def handle_incoming_whatsapp_message(raw_text: str, phone: str):
    log.info(f"Message from {phone}: '{raw_text}'")

    # --- Step 1: Resolve the user from the registry ---
    user = get_user(phone)

    # --- Step 2: Check for commands first ---
    if handle_command(raw_text, phone, user):
        return

    # --- Step 3: Block unregistered users from logging transactions ---
    if not user:
        send_whatsapp_reply(
            phone,
            "Sorry, your number is not registered in our system.\n\n"
            "Please contact your administrator to be added before you can log transactions.\n\n"
            "Type *help* to see what this bot does."
        )
        return

    # --- Step 4: Parse the transaction with Gemini ---
    entity_type        = user["entity_type"]
    system_instruction = ENTITY_SYSTEM_PROMPTS.get(entity_type, "You are a financial accounting assistant.")
    coa_context        = build_coa_context(entity_type)
    corrections_context = build_corrections_context(entity_type)  # inject learned corrections

    full_prompt = f"""
{system_instruction}

{coa_context}

{corrections_context}

================================================================
HOW TO PROCESS A MESSAGE:

STEP 1 - Classify as TYPE A (activity-based) or TYPE B (item-based).
STEP 2 - Identify the primary activity / procurement type.
STEP 3 - Find the matching cost center in the CoA.
STEP 4 - Apply any correction lessons from above if relevant.
STEP 5 - Create one row per amount. Apply cost center anchoring rules.
STEP 6 - Fill purpose, line_item, who, location, amount, type, record_type.
================================================================

Now process this message from {user.get('name', 'a staff member')}:
"{raw_text}"
"""

    call_gemini_with_retry(raw_text, phone, user, full_prompt, attempt=1)

# =====================================================================
# 16. WEBHOOK ROUTE
# =====================================================================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():

    if request.method == "GET":
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
            log.info("Webhook verified by Meta.")
            return challenge, 200
        return "Verification failed", 403

    elif request.method == "POST":
        payload = request.get_json()
        try:
            value = payload["entry"][0]["changes"][0]["value"]

            if "messages" not in value:
                log.info("Status update / read receipt received. Ignored.")
                return jsonify({"status": "ignored"}), 200

            message = value["messages"][0]

            if message.get("type") != "text":
                log.info(f"Non-text message type '{message.get('type')}' received. Ignored.")
                return jsonify({"status": "ignored"}), 200

            raw_text     = message["text"]["body"]
            sender_phone = message["from"]
            handle_incoming_whatsapp_message(raw_text, sender_phone)

        except (KeyError, IndexError, TypeError) as e:
            log.info(f"Unhandled payload structure: {e}. Ignored.")

        return jsonify({"status": "success"}), 200

# =====================================================================
# 17. HEALTH CHECK
# =====================================================================
@app.route("/health", methods=["GET"])
def health_check():
    registry = fetch_users_registry()
    return jsonify({
        "status":            "running",
        "default_entity":    DEFAULT_ENTITY_TYPE,
        "registered_users":  len(registry),
        "pending_users":     len(pending_store),
        "correcting_users":  len(correction_state),
        "retry_queue_size":  sum(len(v) for v in retry_queue.values()),
        "timestamp":         datetime.datetime.now().isoformat()
    }), 200


@app.route("/retry", methods=["POST"])
def trigger_retry():
    """Manually trigger retry queue processing. Useful for testing or scheduled calls."""
    process_retry_queue()
    return jsonify({
        "status": "retry triggered",
        "queue_size": sum(len(v) for v in retry_queue.values())
    }), 200

# =====================================================================
# 18. START SERVER
# =====================================================================
if __name__ == "__main__":
    log.info("Starting ACX Ledger Bot")
    ensure_all_headers()       # write column headers to all sheets if missing
    fetch_users_registry()     # warm up the users cache on startup
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
