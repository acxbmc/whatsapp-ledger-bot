import sys
import io
import re

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
import threading
from collections import deque
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import urllib3

import invoice_module as inv
import requisition_module as req

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


def get_user_sheet_name(user: dict) -> str:
    """Same resolution as get_user_sheet() but returns just the sheet name string."""
    entity_type = user["entity_type"]
    if user.get("sheet_override"):
        return user["sheet_override"]
    return accounting_config.get(entity_type, {}).get(
        "sheet_name",
        accounting_config[DEFAULT_ENTITY_TYPE]["sheet_name"]
    )


def invalidate_users_cache():
    """Force a fresh fetch next time get_user() is called."""
    global _users_cache_time
    _users_cache_time = None


def fetch_master_orgs() -> list[dict]:
    """
    Return ALL org memberships for the master phone from the Users tab.
    Each entry: { "name": display_name, "sheet_name": sheet_to_write_to }.
    Used so the master can be a member of multiple client orgs at once.
    """
    if not MASTER_PHONE:
        return []

    base_sheet_name = accounting_config.get("ngo", {}).get("sheet_name", "NGO_Grant_Ledger")
    try:
        users_sheet = gc_client.open(base_sheet_name).worksheet(USERS_TAB_NAME)
        all_rows    = users_sheet.get_all_values()[1:]
        orgs = []
        for row in all_rows:
            if not row or not row[0].strip():
                continue
            phone_val = row[0].strip().replace(" ", "").replace("+", "")
            if phone_val != MASTER_PHONE:
                continue
            status = row[7].strip().lower() if len(row) > 7 else "active"
            if status == "inactive":
                continue
            sheet_override = row[4].strip() if len(row) > 4 else ""
            entity_type    = row[2].strip().lower() if len(row) > 2 else DEFAULT_ENTITY_TYPE
            sheet_name = sheet_override or accounting_config.get(
                entity_type, {}
            ).get("sheet_name", "NGO_Grant_Ledger")
            org_name = sheet_override or entity_type.upper()
            orgs.append({"name": org_name, "sheet_name": sheet_name})
        return orgs
    except Exception as e:
        log.error(f"Could not fetch master orgs: {e}")
        return []


def get_master_active_sheet(phone: str) -> tuple[str | None, bool]:
    """
    Resolve which sheet the master user should currently write to.

    Returns:
        (sheet_name, needs_choice)
        - If sheet_name is set and needs_choice is False → proceed normally
        - If sheet_name is None and needs_choice is True → a prompt was sent,
          caller should return early and wait for the user's reply
    """
    global master_session_state, master_routing_state

    # Check active session
    session = master_session_state
    if session.get("sheet_name") and session.get("expires_at"):
        if datetime.datetime.now() < session["expires_at"]:
            return (session["sheet_name"], False)
        else:
            master_session_state = {}   # expired — clear it

    # Resolve orgs
    test_sheet = accounting_config.get("ngo", {}).get("sheet_name", "NGO_Grant_Ledger")
    orgs = fetch_master_orgs()

    if not orgs:
        # Master not in any org → default to test sheet
        expires = datetime.datetime.now() + datetime.timedelta(hours=MASTER_SESSION_HOURS)
        master_session_state = {
            "sheet_name": test_sheet,
            "org_name":   "Test Sheet",
            "expires_at": expires,
        }
        return (test_sheet, False)

    if len(orgs) == 1:
        # Only one org → auto-route, tell them
        expires = datetime.datetime.now() + datetime.timedelta(hours=MASTER_SESSION_HOURS)
        master_session_state = {
            "sheet_name": orgs[0]["sheet_name"],
            "org_name":   orgs[0]["name"],
            "expires_at": expires,
        }
        send_whatsapp_reply(
            phone,
            f"Routing to *{orgs[0]['name']}* for the next {MASTER_SESSION_HOURS} hours.\n"
            "Type *switch sheet* to change at any time."
        )
        return (orgs[0]["sheet_name"], False)

    # Multiple orgs → ask master to choose
    master_routing_state = {"orgs": orgs}
    lines = ["You are registered with multiple organisations. Which one should I log to?\n"]
    for i, org in enumerate(orgs, 1):
        lines.append(f"  {i}. {org['name']}")
    lines.append(f"  {len(orgs) + 1}. Test Sheet (NGO_Grant_Ledger)")
    lines.append("\nReply with the number.")
    send_whatsapp_reply(phone, "\n".join(lines))
    return (None, True)


def resolve_sender(phone: str) -> tuple[dict | None, str | None, bool]:
    """
    Unified resolver used by both the transaction flow and the requisition flow.

    Returns (user, sheet_name, needs_choice):
      - Regular registered user  → (user_dict, their_sheet_name, False)
      - Master, single/no org    → (synthetic_user, resolved_sheet_name, False)
      - Master, multiple orgs,
        no choice made yet       → (None, None, True) — a prompt was already sent
      - Unregistered, non-master → (None, None, False) — caller should reject
    """
    user = get_user(phone)

    if MASTER_PHONE and phone == MASTER_PHONE:
        sheet_name, needs_choice = get_master_active_sheet(phone)
        if needs_choice:
            return (None, None, True)
        if not user:
            user = {
                "name":           "Xan (Master)",
                "entity_type":    DEFAULT_ENTITY_TYPE,
                "role":           "admin",
                "sheet_override": sheet_name,
            }
        else:
            user = dict(user)
            user["sheet_override"] = sheet_name
        return (user, sheet_name, False)

    if not user:
        return (None, None, False)

    return (user, get_user_sheet_name(user), False)

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
            lines.append(f"  - For '{c['activity']}': use activity code '{c['new']}' not '{c['old']}'")
        elif c["field"] == "category":
            lines.append(f"  - For '{c['activity']}': use account/category '{c['new']}' not '{c['old']}'")
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

# Requisition flow triggers
REQ_TRIGGER_KEYWORDS = {"req", "requisition", "request", "purchase request", "new req"}

# Admin requisition-management keywords (exact match)
PENDING_LIST_KEYWORDS   = {"pending", "/pending"}
APPROVED_LIST_KEYWORDS  = {"approved", "/approved"}
RECEIPTED_LIST_KEYWORDS = {"receipted", "/receipted"}
REJECTED_LIST_KEYWORDS  = {"rejected", "/rejected"}
SUMMARY_KEYWORDS        = {"summary", "/summary"}

# Admin command prefixes (case-insensitive, matched against lowercased text)
ADDUSER_PREFIX    = "adduser "
REMOVEUSER_PREFIX = "removeuser "

# Retry queue: messages that failed due to Gemini overload, waiting to be retried
# Structure: { phone: [ { "raw_text": str, "user": dict, "prompt": str, "attempts": int, "next_retry": datetime }, ... ] }
retry_queue: dict[str, list] = {}
MAX_RETRY_ATTEMPTS  = 3
RETRY_DELAY_SECONDS = 15   # wait 15s between attempts

# --- Message ID deduplication ---
# WhatsApp/Meta can redeliver the same webhook notification (e.g. if our
# response is slow, or if multiple apps are subscribed). Each message has a
# unique id ("wamid..."). We remember IDs we've already processed so a
# redelivered duplicate is silently ignored instead of being handled twice.
processed_message_ids: dict[str, datetime.datetime] = {}
MESSAGE_ID_DEDUP_HOURS = 24

# --- Master user session routing ---
# Remembers which org sheet the master has chosen to write to for 2 hours.
# Structure: { "sheet_name": str, "org_name": str, "expires_at": datetime }
master_session_state: dict = {}
MASTER_SESSION_HOURS = 2

# --- Master org-choice state ---
# Set when master has multiple orgs and we need them to pick one.
# Structure: { "orgs": [{"name": str, "sheet_name": str}, ...] }
master_routing_state: dict = {}

# --- Pending receipt references ---
# After a batch is confirmed, we store info here so a follow-up photo
# can be linked to the right sheet rows.
# Structure: { "TXN-2026-0001": { "phone": str, "sheet_name": str,
#              "row_numbers": [int, ...], "expires_at": datetime } }
pending_receipt_refs: dict[str, dict] = {}
RECEIPT_REF_HOURS   = 48     # refs expire after 48 hours
RECEIPT_REF_PATTERN = re.compile(r"\bTXN-\d{4}-\d{4}\b", re.IGNORECASE)

# Master sheet-switch keywords (only the master phone can use these)
MASTER_SWITCH_KEYWORDS = {
    "test mode", "switch to test", "test sheet", "testing mode",
    "switch sheet", "switch org", "change org", "change sheet",
}

def is_duplicate_message_id(message_id: str) -> bool:
    """Returns True if this message id was already processed recently."""
    if not message_id:
        return False  # can't dedupe without an id — let it through

    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(hours=MESSAGE_ID_DEDUP_HOURS)

    # Light cleanup of old entries so this dict doesn't grow forever
    for mid in list(processed_message_ids.keys()):
        if processed_message_ids[mid] < cutoff:
            del processed_message_ids[mid]

    if message_id in processed_message_ids:
        return True

    processed_message_ids[message_id] = now
    return False

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

def has_coa_tab(entity_type: str) -> bool:
    """
    Check whether a CoA_Reference tab actually exists for this entity.
    For-profit orgs don't require a CoA — it's optional.
    """
    sheet_name = accounting_config.get(entity_type, {}).get("sheet_name", "")
    if not sheet_name:
        return False
    try:
        gc_client.open(sheet_name).worksheet("CoA_Reference")
        return True
    except Exception:
        return False


def fetch_coa_rules(entity_type: str) -> list[str]:
    """
    Fetch Chart of Accounts rules from the CoA_Reference tab.

    For-profit orgs: CoA is OPTIONAL. If no CoA_Reference tab exists,
    returns an empty list (Gemini will use free-form categorisation).
    If a tab DOES exist for a for-profit org, it is used just like NGO.

    NGO orgs: CoA is required. An empty result is logged as a warning.
    """
    global _coa_cache

    # For-profit without a CoA tab — skip entirely, no error
    if entity_type == "for-profit" and not has_coa_tab(entity_type):
        return []

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


def coa_pair_exists(entity_type: str, category: str, activity_code: str) -> bool:
    """Check whether this exact (category, activity_code) pair already exists in the CoA."""
    rules  = fetch_coa_rules(entity_type)
    target = f"{category.strip()} -> {activity_code.strip()}"
    return target in rules


def add_coa_entry(entity_type: str, category: str, activity_code: str) -> bool:
    """
    Append a new (Main Account, Activity Code) pair to the CoA_Reference tab
    so future messages with this classification are recognised automatically.
    Refreshes the in-memory cache so it's available on the very next message.
    Returns True on success, False if it could not be written.
    """
    sheet_name = accounting_config.get(entity_type, {}).get("sheet_name", "NGO_Grant_Ledger")
    try:
        coa_sheet = gc_client.open(sheet_name).worksheet("CoA_Reference")
        coa_sheet.append_row([category.strip(), activity_code.strip()])
        log.info(f"Added new CoA entry for '{entity_type}': {category} -> {activity_code}")
        _coa_cache.pop(entity_type, None)  # force refresh on next fetch
        return True
    except Exception as e:
        log.error(f"Could not add new CoA entry for '{entity_type}': {e}")
        return False


def build_coa_context(entity_type: str) -> str:
    """
    Build the Chart of Accounts section of the Gemini prompt.

    For-profit without a CoA tab: returns a simple free-form prompt —
    no strict account mapping required, Gemini uses best judgment.
    For-profit WITH a CoA tab: treated exactly like NGO (optional but respected).
    NGO: full strict CoA enforcement.
    """
    rules = fetch_coa_rules(entity_type)

    # For-profit with no CoA — light-touch prompt, no strict mapping
    if entity_type == "for-profit" and not rules:
        return """
*** CATEGORISATION (FOR-PROFIT MODE — NO STRICT CHART OF ACCOUNTS) ***
This organisation has not provided a Chart of Accounts.
Use your best judgment to classify transactions:
  - category:      A broad business category (e.g. "Sales Revenue", "Staff Costs",
                   "Office Expenses", "Purchases", "Transport", "Utilities")
  - activity_code: A short descriptive label for the specific item
                   (e.g. "Salary - John", "Airtime", "Fuel", "Stock Purchase")

CASH FLOW RULES:
  - Revenue from sales / services → type = "Income"
  - Owner injecting personal funds → type = "Capital Investment"
  - Business spending / expenses   → type = "Expense"
  - Owner withdrawing cash         → type = "Drawings"
================================================================
"""

    # NGO or for-profit WITH a provided CoA — strict mapping
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
def generate_transaction_ref(sheet_name: str) -> str:
    """
    Generate the next TXN reference number for a given sheet (TXN-YYYY-NNNN).
    Scans column N of the sheet to find the highest sequence for this year.
    Falls back to a timestamp-based ref if the sheet can't be read.
    """
    year = datetime.datetime.now().year
    prefix = f"TXN-{year}-"
    try:
        ws       = gc_client.open(sheet_name).sheet1
        col_n    = ws.col_values(14)  # column N, 1-indexed = 14
        max_seq  = 0
        for cell in col_n[1:]:  # skip header
            if cell.startswith(prefix):
                try:
                    seq = int(cell.split("-")[-1])
                    max_seq = max(max_seq, seq)
                except (ValueError, IndexError):
                    pass
        return f"{prefix}{max_seq + 1:04d}"
    except Exception:
        return f"{prefix}{datetime.datetime.now().strftime('%m%d%H%M%S')}"


def write_batch_to_sheet(phone: str, batch: dict, user: dict) -> tuple[int, int, str]:
    """
    Write confirmed transactions to the correct Google Sheet.
    Returns (written_count, skipped_count, transaction_ref).
    The ref is stored in pending_receipt_refs so a follow-up photo
    can be linked back to these exact rows.
    """
    transactions     = batch["transactions"]
    primary_activity = batch["primary_activity"]
    entity_type      = user["entity_type"]
    written, skipped = 0, 0
    row_numbers      = []

    target_sheet = get_user_sheet(user)
    sheet_name   = target_sheet.spreadsheet.title

    # Generate one reference for the whole batch
    txn_ref = generate_transaction_ref(sheet_name)

    for item in transactions:
        if (
            entity_type == "for-profit"
            and item["record_type"] == "Personal"
            and item["type"] not in ("Drawings", "Capital Investment")
        ):
            log.warning(f"Skipping personal item: '{item['purpose']}'")
            skipped += 1
            continue

        # Grab current row count BEFORE appending so we know the new row's index
        current_count = len(target_sheet.get_all_values())

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        amount    = item["amount"]
        tx_type   = item["type"]

        if entity_type == "for-profit":
            # For-profit layout: Cash In / Cash Out split instead of a single Amount column.
            # Cash In  = money entering the business (Income, Capital Investment)
            # Cash Out = money leaving the business (Expense, Drawings)
            cash_in  = amount if tx_type in ("Income", "Capital Investment") else ""
            cash_out = amount if tx_type in ("Expense", "Drawings")          else ""
            row = [
                timestamp,                  # A: Timestamp
                phone,                      # B: Phone Number
                user.get("name", "Unknown"),# C: Staff Name
                item["purpose"],            # D: Purpose
                item["line_item"],          # E: Line Item
                cash_in,                    # F: Cash In
                cash_out,                   # G: Cash Out
                item["who"],                # H: Who
                tx_type,                    # I: Type
                item["record_type"],        # J: Record Type
                txn_ref,                    # K: Reference
            ]
        else:
            # NGO layout: all columns including Primary Activity, Location, Category, Activity Code
            row = [
                timestamp,                  # A: Timestamp
                phone,                      # B: Phone
                user.get("name", "Unknown"),# C: Staff Name
                primary_activity,           # D: Primary Activity
                item["purpose"],            # E: Purpose
                item["line_item"],          # F: Line Item
                amount,                     # G: Amount
                item["who"],                # H: Who
                item["location"],           # I: Location
                item["category"],           # J: Category
                item["activity_code"],      # K: Activity Code
                tx_type,                    # L: Type
                item["record_type"],        # M: Record Type
                txn_ref,                    # N: Reference
                "",                         # O: Receipt Link
            ]

        target_sheet.append_row(row)
        row_numbers.append(current_count + 1)  # 1-indexed row number in sheet
        log.info(f"Row written for {user.get('name')} ({phone}) ref={txn_ref}: {row}")
        written += 1

    # Store the ref so a follow-up receipt photo can find these rows
    if written > 0:
        expires = datetime.datetime.now() + datetime.timedelta(hours=RECEIPT_REF_HOURS)
        pending_receipt_refs[txn_ref] = {
            "phone":       phone,
            "sheet_name":  sheet_name,
            "row_numbers": row_numbers,
            "expires_at":  expires,
        }
        log.info(f"Stored pending receipt ref {txn_ref} → rows {row_numbers} in '{sheet_name}'")

    return written, skipped, txn_ref

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
    "Reference",        # N  ← batch transaction reference e.g. TXN-2026-0001
    "Receipt Link",     # O  ← Google Drive link added when user attaches photo
]

# For-profit column layout — simpler than NGO, uses Cash In / Cash Out split
FOR_PROFIT_TRANSACTIONS_HEADERS = [
    "Timestamp",        # A
    "Phone Number",     # B
    "Staff Name",       # C
    "Purpose",          # D
    "Line Item",        # E
    "Cash In",          # F  ← Income, Capital Investment
    "Cash Out",         # G  ← Expense, Drawings
    "Who",              # H
    "Type",             # I
    "Record Type",      # J
    "Reference",        # K
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
            # For-profit uses a different column layout than NGO
            tx_headers = (
                FOR_PROFIT_TRANSACTIONS_HEADERS
                if entity_type == "for-profit"
                else TRANSACTIONS_HEADERS
            )
            ensure_headers(workbook.sheet1, tx_headers)

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
            # For-profit: CoA is optional, so missing tab is not a warning
            try:
                coa_ws = workbook.worksheet("CoA_Reference")
                ensure_headers(coa_ws, COA_HEADERS)
            except gspread.exceptions.WorksheetNotFound:
                if entity_type == "for-profit":
                    log.info(f"No CoA_Reference tab in '{sheet_name}' (for-profit — optional, skipping).")
                else:
                    log.warning(f"'CoA_Reference' tab not found in '{sheet_name}'.")

            # Requisitions tab — create if missing (bot writes here, not the admin)
            try:
                req_ws = workbook.worksheet(req.REQUISITIONS_TAB_NAME)
            except gspread.exceptions.WorksheetNotFound:
                req_ws = workbook.add_worksheet(
                    title=req.REQUISITIONS_TAB_NAME, rows=1000, cols=len(req.REQUISITIONS_HEADERS)
                )
                log.info(f"'{req.REQUISITIONS_TAB_NAME}' tab created in '{sheet_name}'.")
            ensure_headers(req_ws, req.REQUISITIONS_HEADERS)

            log.info(f"Header check complete for '{sheet_name}'.")

        except Exception as e:
            log.error(f"Could not open sheet '{sheet_name}' for header check: {e}")

    # --- Per-org sheets (sheet_override in the Users tab) ---
    # Each client org has its own sheet, created manually by the admin. We only
    # ensure the Requisitions tab exists there — the transactions/CoA layout is
    # set up by whoever created the sheet.
    try:
        registry = fetch_users_registry()
        org_sheets = {u["sheet_override"] for u in registry.values() if u.get("sheet_override")}
        for org_sheet_name in org_sheets:
            try:
                org_workbook = gc_client.open(org_sheet_name)
                try:
                    org_req_ws = org_workbook.worksheet(req.REQUISITIONS_TAB_NAME)
                except gspread.exceptions.WorksheetNotFound:
                    org_req_ws = org_workbook.add_worksheet(
                        title=req.REQUISITIONS_TAB_NAME, rows=1000, cols=len(req.REQUISITIONS_HEADERS)
                    )
                    log.info(f"'{req.REQUISITIONS_TAB_NAME}' tab created in org sheet '{org_sheet_name}'.")
                ensure_headers(org_req_ws, req.REQUISITIONS_HEADERS)
            except Exception as e:
                log.error(f"Could not ensure Requisitions tab in org sheet '{org_sheet_name}': {e}")
    except Exception as e:
        log.error(f"Could not scan org sheets for Requisitions tab setup: {e}")

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
            "Type *req* to submit a requisition, or describe an expense "
            "directly. Type *help* to see everything I can do."
        )
    return (
        "Hello! Welcome to the Ledger Bot.\n\n"
        "I help field staff log expenses and track spending — all through WhatsApp.\n\n"
        "It looks like your number is not registered yet. "
        "Please contact your administrator to be added to the system."
    )


HELP_REPLY_STAFF = (
    "*What I can do for you:*\n\n"
    "*Submit a requisition* (recommended)\n"
    "  Req        - Start a new requisition\n"
    "  Follow the prompts: pick account, add items, submit\n"
    "  Your Admin will approve or reject it\n\n"
    "*After approval:*\n"
    "  Send a photo of your receipt with the reference "
    "(e.g. *REQ-2026-0001*) in the caption\n\n"
    "*Log an expense directly* (no approval needed)\n"
    "  \"Spent 300,000 paying school fees for John at ABC Primary School\"\n"
    "  \"Bought soap 20,000 and vaseline 3,000\"\n\n"
    "*After I show you a summary:*\n"
    "  Yes        - Save the transactions\n"
    "  No         - Cancel and discard\n"
    "  Correct    - Fix something before saving\n\n"
    "*Other commands:*\n"
    "  Balance    - See your total income and expenses\n"
    "  Undo       - Delete your last saved entry\n"
    "  Help       - Show this menu"
)

HELP_REPLY_ADMIN = (
    "*Admin commands:*\n\n"
    "*Requisitions:*\n"
    "  Pending           - List requisitions awaiting your approval\n"
    "  Approved          - List approved, not yet receipted\n"
    "  Receipted         - List completed requisitions\n"
    "  Rejected          - List rejected requisitions\n"
    "  Summary           - Spending totals by account\n"
    "  View REQ-XXXX     - See full detail of one requisition\n"
    "  Approve REQ-XXXX  - Approve a pending requisition\n"
    "  Reject REQ-XXXX [reason] - Reject with an optional reason\n\n"
    "*User management:*\n"
    "  adduser <phone> <name> <role>  - Add a new user\n"
    "  removeuser <phone>             - Deactivate a user\n"
    "  listusers                      - See all active users\n\n"
    "Example: adduser 256700123456 Jane Apio staff\n\n"
    "You can also submit your own requisition with *req*, or log an "
    "expense directly the same way staff do."
)

HELP_REPLY_MASTER = (
    "*Master commands:*\n\n"
    "  Invoice       - Create a new invoice/receipt for a client\n"
    "  Switch sheet  - Switch which org's sheet you are logging to\n"
    "  Test mode     - Switch back to the test sheet (NGO_Grant_Ledger)\n\n"
    "*Invoice flow:*\n"
    "  - Enter client name, address, line items\n"
    "  - Type 'done' when finished\n"
    "  - Review, then 'yes' to generate PDF\n"
    "  - 'Edit' to fix something, 'Cancel' to discard\n"
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
    "5": "category", "6": "activity_code",
    "amount":    "amount",  "person":   "who",  "who":      "who",
    "name":      "who",     "location": "location", "district": "location",
    "place":     "location","item":     "line_item", "line item": "line_item",
    "type":      "line_item",
    "category":      "category", "account": "category",
    "account name":  "category", "main account": "category",
    "activity":      "activity_code", "activity code": "activity_code",
    "code":          "activity_code", "sub account": "activity_code",
    "sub-account":   "activity_code",
}

def build_correction_menu(batch: dict) -> str:
    lines = ["What would you like to correct?\n"]
    lines.append("Which field?")
    lines.append("  1 - Amount")
    lines.append("  2 - Person name (who)")
    lines.append("  3 - Location / district")
    lines.append("  4 - Line item / spend type")
    lines.append("  5 - Account Name (category)")
    lines.append("  6 - Activity Code")
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
                "Reply with 1 (amount), 2 (person), 3 (location), 4 (line item), "
                "5 (account name), or 6 (activity code)."
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
            "line_item": "line item / spend type",
            "category":  "Account Name (e.g. '4700 - Social Work & Field')",
            "activity_code": "Activity Code (e.g. '4701 - Home Tracing')",
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

        # --- If the Account Name or Activity Code was corrected, make sure ---
        # --- this classification exists in the Chart of Accounts. If it   ---
        # --- doesn't, add it now so future messages recognise it too.     ---
        # --- For-profit without a CoA: skip this entirely.                ---
        coa_note = ""
        entity_type = user["entity_type"] if user else DEFAULT_ENTITY_TYPE
        if field in ("category", "activity_code") and not (
            entity_type == "for-profit" and not has_coa_tab(entity_type)
        ):
            if not coa_pair_exists(entity_type, t["category"], t["activity_code"]):
                added = add_coa_entry(entity_type, t["category"], t["activity_code"])
                if added:
                    coa_note = (
                        f"\n\nNote: '{t['category']} -> {t['activity_code']}' was not in "
                        "your Chart of Accounts, so I've added it for future use."
                    )
                else:
                    coa_note = (
                        "\n\nNote: I could not automatically add this classification to "
                        "your Chart of Accounts. You may want to add it manually."
                    )

        # Persist correction to Corrections tab for future learning
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
            f"Correction applied.{coa_note}\n\n"
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
    global master_session_state, master_routing_state
    text_lower = raw_text.strip().lower()

    # --- Responding to org-choice prompt ---
    if master_routing_state.get("orgs"):
        orgs = master_routing_state["orgs"]
        test_sheet = accounting_config.get("ngo", {}).get("sheet_name", "NGO_Grant_Ledger")
        # Accept a number
        if text_lower.isdigit():
            idx = int(text_lower) - 1
            if idx == len(orgs):  # last option = test sheet
                chosen_sheet = test_sheet
                chosen_name  = "Test Sheet"
            elif 0 <= idx < len(orgs):
                chosen_sheet = orgs[idx]["sheet_name"]
                chosen_name  = orgs[idx]["name"]
            else:
                send_whatsapp_reply(phone, f"Please reply with a number between 1 and {len(orgs) + 1}.")
                return True

            expires = datetime.datetime.now() + datetime.timedelta(hours=MASTER_SESSION_HOURS)
            master_session_state = {
                "sheet_name": chosen_sheet,
                "org_name":   chosen_name,
                "expires_at": expires,
            }
            master_routing_state = {}
            send_whatsapp_reply(
                phone,
                f"Routing to *{chosen_name}* for the next {MASTER_SESSION_HOURS} hours.\n"
                "Type *switch sheet* to change at any time.\n\n"
                "Now send your transaction message."
            )
            return True
        else:
            # Not a number while in choosing state — re-prompt
            lines = ["Please reply with a number to choose:\n"]
            for i, org in enumerate(orgs, 1):
                lines.append(f"  {i}. {org['name']}")
            lines.append(f"  {len(orgs) + 1}. Test Sheet")
            send_whatsapp_reply(phone, "\n".join(lines))
            return True

    # --- Switch sheet / test mode commands ---
    if text_lower in MASTER_SWITCH_KEYWORDS:
        master_session_state = {}  # clear current session
        master_routing_state = {}
        test_sheet = accounting_config.get("ngo", {}).get("sheet_name", "NGO_Grant_Ledger")

        if "test" in text_lower:
            # Jump straight to test sheet
            expires = datetime.datetime.now() + datetime.timedelta(hours=MASTER_SESSION_HOURS)
            master_session_state = {
                "sheet_name": test_sheet,
                "org_name":   "Test Sheet",
                "expires_at": expires,
            }
            send_whatsapp_reply(phone, f"Switched to *Test Sheet* ({test_sheet}).")
            return True

        # Otherwise trigger the full choice flow
        orgs = fetch_master_orgs()
        if not orgs:
            expires = datetime.datetime.now() + datetime.timedelta(hours=MASTER_SESSION_HOURS)
            master_session_state = {
                "sheet_name": test_sheet,
                "org_name":   "Test Sheet",
                "expires_at": expires,
            }
            send_whatsapp_reply(phone, "You are not registered with any org. Using Test Sheet.")
            return True

        if len(orgs) == 1:
            expires = datetime.datetime.now() + datetime.timedelta(hours=MASTER_SESSION_HOURS)
            master_session_state = {
                "sheet_name": orgs[0]["sheet_name"],
                "org_name":   orgs[0]["name"],
                "expires_at": expires,
            }
            send_whatsapp_reply(phone, f"Switched to *{orgs[0]['name']}*.")
            return True

        # Multiple orgs — show choice
        master_routing_state = {"orgs": orgs}
        lines = ["Which sheet would you like to switch to?\n"]
        for i, org in enumerate(orgs, 1):
            lines.append(f"  {i}. {org['name']}")
        lines.append(f"  {len(orgs) + 1}. Test Sheet")
        send_whatsapp_reply(phone, "\n".join(lines))
        return True

    if text_lower in INVOICE_TRIGGER_WORDS:
        prompt = inv.start_invoice_flow(phone)
        send_whatsapp_reply(phone, prompt)
        return True

    if text_lower in INVOICE_HELP_WORDS:
        send_whatsapp_reply(
            phone,
            "*Master commands:*\n\n"
            "  Invoice  - Create a new invoice/receipt\n\n"
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
                gc_client, local_filename, f"{safe_invoice_num}.pdf",
                creds=google_cloud_creds
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
# 13d. REQUISITION WORKFLOW — structured, no AI required
# =====================================================================

def process_requisition_flow_message(phone: str, raw_text: str) -> bool:
    """Advance an in-progress requisition. On completion, submit + notify admins."""
    reply_text, completed = req.process_requisition_message(phone, raw_text)

    if completed is None:
        send_whatsapp_reply(phone, reply_text)
        return True

    send_whatsapp_reply(phone, reply_text)  # "Processing your requisition..."

    sheet_name  = completed.get("_sheet_name")
    staff_name  = completed.get("_user_name", phone)
    entity_type = completed.get("_entity_type", DEFAULT_ENTITY_TYPE)

    if not sheet_name:
        log.error(f"Requisition completed for {phone} with no sheet_name — cannot submit.")
        send_whatsapp_reply(phone, "Something went wrong submitting your requisition. Please try *req* again.")
        return True

    try:
        req_number = req.submit_requisition(gc_client, sheet_name, phone, staff_name, completed)
    except Exception as e:
        log.error(f"Failed to submit requisition for {phone}: {e}")
        send_whatsapp_reply(phone, "Something went wrong submitting your requisition. Please try *req* again.")
        return True

    total = sum(i["total"] for i in completed["items"])
    send_whatsapp_reply(
        phone,
        f"Requisition submitted!\n\n"
        f"Reference: *{req_number}*\n"
        f"Total: UGX {total:,}\n\n"
        "You will be notified once your Admin reviews it."
    )

    # Notify all admins for this org
    base_sheet_name = accounting_config.get("ngo", {}).get("sheet_name", "NGO_Grant_Ledger")
    admins = req.get_org_admins(gc_client, base_sheet_name, USERS_TAB_NAME, entity_type, sheet_name)
    cat = completed["selected_category"]
    cat_label = cat.split(" - ", 1)[-1] if " - " in cat else cat

    for admin_phone in admins:
        if admin_phone == phone:
            continue
        send_whatsapp_reply(
            admin_phone,
            f"New requisition from {staff_name}:\n"
            f"*{req_number}* — {cat_label}\n"
            f"Total: UGX {total:,}\n\n"
            "Reply:\n"
            f"  approve {req_number}\n"
            f"  reject {req_number} [reason]\n"
            f"  view {req_number}"
        )

    return True


def _require_admin(phone: str, user: dict | None) -> tuple:
    """
    Gate a command to admins only (or the master user).
    Returns (ok, resolved_user, sheet_name). Sends a denial/prompt message if not ok.
    """
    resolved_user, sheet_name, needs_choice = resolve_sender(phone)
    if needs_choice:
        return (False, None, None)   # org-choice prompt already sent
    if not resolved_user:
        send_whatsapp_reply(phone, "You are not registered. Contact your administrator.")
        return (False, None, None)
    is_admin = resolved_user.get("role") == "admin" or (MASTER_PHONE and phone == MASTER_PHONE)
    if not is_admin:
        send_whatsapp_reply(phone, "Sorry, this command is only available to admins.")
        return (False, None, None)
    return (True, resolved_user, sheet_name)


def handle_approve_command(phone: str, user: dict | None, req_number: str) -> bool:
    ok, resolved_user, sheet_name = _require_admin(phone, user)
    if not ok:
        return True
    success, data = req.approve_requisition(gc_client, sheet_name, req_number, resolved_user.get("name", phone))
    if not success:
        send_whatsapp_reply(phone, data["error"])
        return True

    try:
        total_int = int(str(data["total"]).replace(",", ""))
    except (ValueError, TypeError):
        total_int = 0

    send_whatsapp_reply(
        phone,
        f"Approved *{data['req_number']}* for {data['staff_name']}.\n"
        f"Total: UGX {total_int:,}"
    )

    items_lines = "\n".join(
        f"  {it['name']}: {it['qty']} x {it.get('unit_price', 0):,} = UGX {it.get('total', 0):,}"
        for it in data.get("items", [])
    )
    send_whatsapp_reply(
        data["staff_phone"],
        f"Your requisition *{data['req_number']}* has been approved!\n\n"
        f"{items_lines}\n\n"
        f"Total: UGX {total_int:,}\n\n"
        "Please proceed with the purchase. Once done, send a photo of the receipt "
        f"with *{data['req_number']}* in the caption."
    )
    return True


def handle_reject_command(phone: str, user: dict | None, req_number: str, reason: str) -> bool:
    ok, resolved_user, sheet_name = _require_admin(phone, user)
    if not ok:
        return True
    success, data = req.reject_requisition(gc_client, sheet_name, req_number, resolved_user.get("name", phone), reason)
    if not success:
        send_whatsapp_reply(phone, data["error"])
        return True

    send_whatsapp_reply(phone, f"Rejected *{data['req_number']}*.")
    reason_txt = f"\nReason: {reason}" if reason else ""
    send_whatsapp_reply(
        data["staff_phone"],
        f"Your requisition *{data['req_number']}* was not approved.{reason_txt}\n\n"
        "Contact your administrator for details, or submit a new requisition with *req*."
    )
    return True


def handle_req_list_command(phone: str, user: dict | None, status_filter: str, title: str) -> bool:
    ok, resolved_user, sheet_name = _require_admin(phone, user)
    if not ok:
        return True
    results = req.list_requisitions(gc_client, sheet_name, status_filter=status_filter)
    send_whatsapp_reply(phone, req.format_req_list(results, title))
    return True


def handle_summary_command(phone: str, user: dict | None) -> bool:
    ok, resolved_user, sheet_name = _require_admin(phone, user)
    if not ok:
        return True
    send_whatsapp_reply(phone, req.get_spending_summary(gc_client, sheet_name))
    return True


def handle_view_command(phone: str, user: dict | None, req_number: str) -> bool:
    ok, resolved_user, sheet_name = _require_admin(phone, user)
    if not ok:
        return True
    detail = req.get_requisition_detail(gc_client, sheet_name, req_number)
    if not detail:
        send_whatsapp_reply(phone, f"*{req_number}* not found.")
        return True
    send_whatsapp_reply(phone, req.format_req_detail(detail))
    return True


def handle_requisition_command(raw_text: str, phone: str, user: dict | None) -> bool:
    """
    Routes all requisition-related messages. Returns True if handled.
    Called from handle_command() before the general command keywords.
    """
    text_lower    = raw_text.strip().lower()
    text_stripped = raw_text.strip()

    # In-progress requisition flow takes top priority
    if req.is_in_requisition_flow(phone):
        return process_requisition_flow_message(phone, raw_text)

    # Start a new requisition
    if text_lower in REQ_TRIGGER_KEYWORDS:
        resolved_user, sheet_name, needs_choice = resolve_sender(phone)
        if needs_choice:
            return True
        if not resolved_user:
            send_whatsapp_reply(
                phone,
                "Sorry, your number is not registered. Contact your administrator to be added."
            )
            return True
        entity_type = resolved_user["entity_type"]
        coa_rules   = fetch_coa_rules(entity_type)
        prompt      = req.start_requisition(phone, coa_rules)
        req.requisition_state[phone]["_sheet_name"]  = sheet_name
        req.requisition_state[phone]["_user_name"]   = resolved_user.get("name", phone)
        req.requisition_state[phone]["_entity_type"] = entity_type
        send_whatsapp_reply(phone, prompt)
        return True

    # Approve / Reject (admin)
    m = req.APPROVE_PATTERN.match(text_stripped)
    if m:
        return handle_approve_command(phone, user, m.group(1).upper())

    m = req.REJECT_PATTERN.match(text_stripped)
    if m:
        return handle_reject_command(phone, user, m.group(1).upper(), (m.group(2) or "").strip())

    # Listing / summary (admin)
    if text_lower in PENDING_LIST_KEYWORDS:
        return handle_req_list_command(phone, user, req.STATUS_PENDING, "Pending Requisitions")
    if text_lower in APPROVED_LIST_KEYWORDS:
        return handle_req_list_command(phone, user, req.STATUS_APPROVED, "Approved — Awaiting Receipt")
    if text_lower in RECEIPTED_LIST_KEYWORDS:
        return handle_req_list_command(phone, user, req.STATUS_RECEIPTED, "Receipted Requisitions")
    if text_lower in REJECTED_LIST_KEYWORDS:
        return handle_req_list_command(phone, user, req.STATUS_REJECTED, "Rejected Requisitions")
    if text_lower in SUMMARY_KEYWORDS:
        return handle_summary_command(phone, user)

    # View / status (admin)
    m = req.VIEW_PATTERN.match(text_stripped) or req.STATUS_PATTERN.match(text_stripped)
    if m:
        return handle_view_command(phone, user, m.group(1).upper())

    return False


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

    # --- REQUISITION WORKFLOW ---
    # Handles: req (start), in-progress flow, approve/reject, pending/approved/
    # receipted/rejected/summary, view/status. Checked before greeting/help so
    # an in-progress requisition flow always takes priority.
    if handle_requisition_command(raw_text, phone, user):
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
        written, skipped, txn_ref = write_batch_to_sheet(phone, batch, user)
        register_fingerprint(phone, fingerprint)

        reply_lines = [f"Saved! {written} transaction(s) recorded."]
        if skipped:
            reply_lines.append(f"({skipped} personal item(s) skipped.)")

        # Include the reference number so user can attach a receipt photo
        if txn_ref and written > 0:
            reply_lines.append(
                f"\nReference: *{txn_ref}*"
                f"\nTo attach a receipt photo, send it with *{txn_ref}* in the caption."
            )

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
            deleted_row  = all_values[last_row_index - 1]
            entity_type  = user["entity_type"] if user else DEFAULT_ENTITY_TYPE
            target_sheet.delete_rows(last_row_index)

            # Column layout differs between NGO and for-profit
            if entity_type == "for-profit":
                # D=Purpose(3), E=Line Item(4), F=Cash In(5), G=Cash Out(6)
                activity_label = deleted_row[3] if len(deleted_row) > 3 else "N/A"
                item_label     = deleted_row[4] if len(deleted_row) > 4 else "N/A"
                cash_in        = deleted_row[5] if len(deleted_row) > 5 else ""
                cash_out       = deleted_row[6] if len(deleted_row) > 6 else ""
                amount_str     = (
                    f"Cash In: UGX {cash_in}"   if cash_in  else
                    f"Cash Out: UGX {cash_out}"  if cash_out else "N/A"
                )
            else:
                # D=Primary Activity(3), F=Line Item(5), G=Amount(6)
                activity_label = deleted_row[3] if len(deleted_row) > 3 else "N/A"
                item_label     = deleted_row[5] if len(deleted_row) > 5 else "N/A"
                amount_str     = f"UGX {deleted_row[6]}" if len(deleted_row) > 6 else "N/A"

            send_whatsapp_reply(
                phone,
                f"Undone! Deleted your last entry:\n"
                f"  Activity: {activity_label}\n"
                f"  Item: {item_label} - {amount_str}"
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
            all_rows   = target_sheet.get_all_values()[1:]  # skip header
            entity_type = user["entity_type"] if user else DEFAULT_ENTITY_TYPE
            currency   = "UGX"

            def safe_int(val):
                try:
                    return int(str(val).replace(",", "").replace(" ", "")) if val else 0
                except (ValueError, TypeError):
                    return 0

            if entity_type == "for-profit":
                # For-profit layout:
                # B=phone(1), F=cash_in(5), G=cash_out(6), I=type(8)
                my_rows = [r for r in all_rows if len(r) > 8 and r[1] == phone]
                total_in      = sum(safe_int(r[5]) for r in my_rows)
                total_out     = sum(safe_int(r[6]) for r in my_rows)
                drawings_out  = sum(safe_int(r[6]) for r in my_rows if r[8] == "Drawings")
                expense_out   = sum(safe_int(r[6]) for r in my_rows if r[8] == "Expense")
                capital_in    = sum(safe_int(r[5]) for r in my_rows if r[8] == "Capital Investment")
                revenue_in    = sum(safe_int(r[5]) for r in my_rows if r[8] == "Income")
                net           = total_in - total_out

                lines = [f"*Balance Summary*\n"]
                lines.append(f"  Cash In:    {currency} {total_in:,}")
                lines.append(f"  Cash Out:   {currency} {total_out:,}")
                lines.append(f"  Net:        {currency} {net:,}\n")
                lines.append("*Breakdown:*")
                if revenue_in:
                    lines.append(f"  Revenue:             {currency} {revenue_in:,}")
                if capital_in:
                    lines.append(f"  Capital Contributions: {currency} {capital_in:,}")
                if expense_out:
                    lines.append(f"  Business Expenses:   {currency} {expense_out:,}")
                if drawings_out:
                    lines.append(f"  Drawings:            {currency} {drawings_out:,}")
                send_whatsapp_reply(phone, "\n".join(lines))

            else:
                # NGO layout:
                # B=phone(1), G=amount(6), J=category(9), L=type(11)
                my_rows = [r for r in all_rows if len(r) > 11 and r[1] == phone]

                # Group expenses by category
                from collections import defaultdict
                by_category  = defaultdict(int)
                income_total = 0
                expense_total = 0

                for r in my_rows:
                    tx_type  = r[11]
                    amount   = safe_int(r[6])
                    category = r[9] if r[9] else "Uncategorised"

                    if tx_type == "Income":
                        income_total += amount
                    elif tx_type == "Expense":
                        by_category[category] += amount
                        expense_total += amount

                lines = [f"*Balance by Account*\n"]

                if income_total:
                    lines.append(f"  Total Income:  {currency} {income_total:,}\n")

                lines.append("*Expenses by Account:*")
                if by_category:
                    # Sort by amount descending so biggest spend shows first
                    for cat, amt in sorted(by_category.items(), key=lambda x: -x[1]):
                        # Shorten the category label (remove leading code if present)
                        label = cat.split(" - ", 1)[-1] if " - " in cat else cat
                        lines.append(f"  {label}: {currency} {amt:,}")
                    lines.append(f"\n  Total Expenses: {currency} {expense_total:,}")
                else:
                    lines.append("  No expenses logged yet.")

                net = income_total - expense_total
                lines.append(f"  Net:            {currency} {net:,}")
                send_whatsapp_reply(phone, "\n".join(lines))

        except Exception as e:
            log.error(f"Balance check failed: {e}", exc_info=True)
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

    # --- Step 3: Resolve sender + sheet (handles master routing too) ---
    user, sheet_name, needs_choice = resolve_sender(phone)
    if needs_choice:
        # A prompt was already sent asking master to choose an org
        return
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

    # For-profit gets a simpler prompt — no CoA anchoring rules needed
    if entity_type == "for-profit" and not fetch_coa_rules(entity_type):
        full_prompt = f"""
{system_instruction}

{coa_context}

{corrections_context}

================================================================
HOW TO PROCESS A FOR-PROFIT MESSAGE:

STEP 1 - Identify each separate amount in the message.
STEP 2 - For each amount, determine whether it is money coming IN or going OUT:
         Cash IN  → type = "Income" (sales, services) or "Capital Investment" (owner funds in)
         Cash OUT → type = "Expense" (business spending) or "Drawings" (owner takes money out)
STEP 3 - Assign a sensible category and activity_code using best judgment.
STEP 4 - Fill purpose (what it was for), line_item (specific item), who, amount, record_type.
STEP 5 - Set primary_activity to the main business activity described.
================================================================

Now process this message from {user.get("name", "a staff member")}:
"{raw_text}"
"""
    else:
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

Now process this message from {user.get("name", "a staff member")}:
"{raw_text}"
"""

    call_gemini_with_retry(raw_text, phone, user, full_prompt, attempt=1)

# =====================================================================
# 15c. RECEIPT PHOTO HANDLING
# =====================================================================

def download_whatsapp_media(media_id: str) -> bytes | None:
    """
    Download media from WhatsApp using its media ID.
    Two-step: first get the URL, then fetch the bytes.
    Meta deletes media after 30 days so we download immediately.
    """
    try:
        url_resp = requests.get(
            f"https://graph.facebook.com/v19.0/{media_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            verify=False, timeout=10
        )
        url_resp.raise_for_status()
        media_url = url_resp.json().get("url")
        if not media_url:
            log.error("Media URL not returned by Meta.")
            return None

        media_resp = requests.get(
            media_url,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            verify=False, timeout=30
        )
        media_resp.raise_for_status()
        return media_resp.content
    except Exception as e:
        log.error(f"Failed to download WhatsApp media {media_id}: {e}")
        return None


def forward_receipt_to_master(
    image_bytes: bytes,
    filename: str,
    txn_ref: str,
    sender_name: str,
    sender_phone: str,
) -> bool:
    """
    Forward a receipt image to the master phone via WhatsApp.
    This sidesteps the Google Drive service account storage quota issue entirely
    (service accounts have 0 quota on personal Drive).
    Returns True on success.
    """
    if not MASTER_PHONE:
        log.warning("MASTER_PHONE not set — cannot forward receipt.")
        return False

    try:
        # Step 1: Upload image to WhatsApp Media API to get a media_id
        upload_url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/media"
        headers    = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        files      = {"file": (filename, image_bytes, "image/jpeg")}
        data       = {"messaging_product": "whatsapp", "type": "image/jpeg"}

        r = requests.post(
            upload_url, headers=headers, files=files, data=data,
            timeout=30, verify=False
        )
        r.raise_for_status()
        media_id = r.json().get("id")

        if not media_id:
            log.error("Media upload for receipt archive returned no media_id.")
            return False

        # Step 2: Send the image to the master phone with TXN reference in caption
        send_url  = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
        caption   = (
            f"Receipt for *{txn_ref}*\n"
            f"From: {sender_name} ({sender_phone})\n"
            f"Received: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        payload   = {
            "messaging_product": "whatsapp",
            "to":   MASTER_PHONE,
            "type": "image",
            "image": {"id": media_id, "caption": caption}
        }
        r2 = requests.post(
            send_url,
            headers={**headers, "Content-Type": "application/json"},
            json=payload, timeout=15, verify=False
        )
        r2.raise_for_status()
        log.info(f"Receipt {txn_ref} forwarded to master {MASTER_PHONE}.")
        return True

    except Exception as e:
        log.error(f"Failed to forward receipt to master: {e}")
        return False


def upload_receipt_to_drive_org(image_bytes: bytes, filename: str, folder_name: str) -> str | None:
    """
    Upload a receipt image to an org's Google Drive folder.
    NOTE: This requires the folder to be on a Google Shared Drive (Google Workspace)
    because service accounts have 0 storage quota on personal Google Drive.
    If you are on personal Gmail, this will fail with storageQuotaExceeded —
    use forward_receipt_to_master() instead (see handle_receipt_photo).
    Returns the shareable Drive link, or None on failure.
    """
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload

        # gc_client.auth does not exist in gspread 6.x — use module-level creds
        drive_service = build("drive", "v3", credentials=google_cloud_creds)

        # Find pre-existing shared folder
        query   = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
        results = drive_service.files().list(q=query, fields="files(id, name)").execute()
        folders = results.get("files", [])

        if not folders:
            log.warning(f"Drive folder '{folder_name}' not found.")
            return None
        folder_id = folders[0]["id"]

        file_metadata = {"name": filename, "parents": [folder_id]}
        media    = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/jpeg")
        uploaded = drive_service.files().create(
            body=file_metadata, media_body=media, fields="id"
        ).execute()
        file_id = uploaded["id"]

        drive_service.permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"}
        ).execute()

        return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

    except Exception as e:
        log.error(f"Failed to upload receipt to Drive: {e}")
        return None


def update_receipt_link_in_sheet(sheet_name: str, row_numbers: list[int], drive_link: str):
    """Update column O (Receipt Link) for the specified rows in the sheet."""
    try:
        ws = gc_client.open(sheet_name).sheet1
        for row_num in row_numbers:
            ws.update_cell(row_num, 15, drive_link)  # Column O = 15
        log.info(f"Receipt link set on rows {row_numbers} in '{sheet_name}'")
    except Exception as e:
        log.error(f"Failed to update receipt link in '{sheet_name}': {e}")


def handle_requisition_receipt(req_number: str, media_id: str, sender_phone: str):
    """
    Attach a receipt photo to an approved requisition and mark it Receipted.
    Always forwards a copy to the master phone for visibility/audit,
    in addition to (attempted) Drive storage.
    """
    resolved_user, sheet_name, needs_choice = resolve_sender(sender_phone)
    if needs_choice or not sheet_name:
        send_whatsapp_reply(
            sender_phone,
            f"Could not resolve which sheet *{req_number}* belongs to. Please try again."
        )
        return

    detail = req.get_requisition_detail(gc_client, sheet_name, req_number)
    if not detail:
        send_whatsapp_reply(sender_phone, f"I could not find requisition *{req_number}*.")
        return

    if detail["status"] == req.STATUS_PENDING:
        send_whatsapp_reply(
            sender_phone,
            f"*{req_number}* is still pending approval — please wait for it to be "
            "approved before submitting a receipt."
        )
        return
    if detail["status"] == req.STATUS_REJECTED:
        send_whatsapp_reply(sender_phone, f"*{req_number}* was rejected and cannot be receipted.")
        return
    if detail["status"] == req.STATUS_RECEIPTED:
        send_whatsapp_reply(sender_phone, f"*{req_number}* already has a receipt attached.")
        return

    send_whatsapp_reply(sender_phone, f"Got it! Attaching receipt to *{req_number}*...")
    image_bytes = download_whatsapp_media(media_id)
    if not image_bytes:
        send_whatsapp_reply(
            sender_phone,
            "Sorry, I could not download the image from WhatsApp. Please try sending it again."
        )
        return

    filename    = f"{req_number}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
    folder_name = sheet_name
    drive_link  = upload_receipt_to_drive_org(image_bytes, filename, folder_name)

    sender_name = resolved_user.get("name", sender_phone) if resolved_user else sender_phone

    # Always forward a copy to the master phone — receipts should be visible
    # to the Admin/Master for audit purposes, regardless of Drive availability.
    forward_receipt_to_master(image_bytes, filename, req_number, sender_name, sender_phone)

    link_or_note = drive_link or (
        f"Archived to master WhatsApp on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    success, data = req.receipt_requisition(gc_client, sheet_name, req_number, link_or_note)

    if not success:
        send_whatsapp_reply(sender_phone, data.get("error", "Could not mark this requisition as receipted."))
        return

    if drive_link:
        send_whatsapp_reply(
            sender_phone,
            f"Receipt attached to *{req_number}* and marked as Receipted.\n\n"
            f"View: {drive_link}"
        )
    else:
        send_whatsapp_reply(
            sender_phone,
            f"Receipt for *{req_number}* received and marked as Receipted.\n\n"
            "(Saved to the admin's WhatsApp archive — Drive storage is not "
            "available for service accounts on personal Google accounts.)"
        )


def handle_legacy_txn_receipt(txn_ref: str, media_id: str, sender_phone: str):
    """
    Attach a receipt photo to a confirmed batch from the old free-form
    transaction flow (reference format TXN-YYYY-NNNN). Kept for backward
    compatibility with transactions logged before the requisition workflow.
    """
    ref_info = pending_receipt_refs.get(txn_ref)

    if ref_info and datetime.datetime.now() > ref_info["expires_at"]:
        del pending_receipt_refs[txn_ref]
        ref_info = None

    if not ref_info:
        send_whatsapp_reply(
            sender_phone,
            f"I could not find reference *{txn_ref}*.\n\n"
            "This may be because:\n"
            "  - The reference has expired (references are valid for 48 hours)\n"
            "  - The reference number was typed incorrectly\n\n"
            "Please check the reference and try again."
        )
        return

    send_whatsapp_reply(sender_phone, f"Got it! Attaching receipt to *{txn_ref}*...")
    image_bytes = download_whatsapp_media(media_id)

    if not image_bytes:
        send_whatsapp_reply(
            sender_phone,
            "Sorry, I could not download the image from WhatsApp. Please try sending it again."
        )
        return

    sheet_name  = ref_info["sheet_name"]
    folder_name = sheet_name
    filename    = f"{txn_ref}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"

    drive_link = upload_receipt_to_drive_org(image_bytes, filename, folder_name)

    if drive_link:
        update_receipt_link_in_sheet(sheet_name, ref_info["row_numbers"], drive_link)
        del pending_receipt_refs[txn_ref]
        send_whatsapp_reply(
            sender_phone,
            f"Receipt attached to *{txn_ref}*!\n\n"
            f"View: {drive_link}"
        )
    else:
        log.warning(f"Drive upload failed for {txn_ref} — forwarding to master WhatsApp.")
        sender_user  = get_user(sender_phone)
        sender_name  = sender_user.get("name", sender_phone) if sender_user else sender_phone

        archived = forward_receipt_to_master(
            image_bytes, filename, txn_ref, sender_name, sender_phone
        )

        archive_note = f"Archived to master WhatsApp on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
        update_receipt_link_in_sheet(sheet_name, ref_info["row_numbers"], archive_note)
        del pending_receipt_refs[txn_ref]

        if archived:
            send_whatsapp_reply(
                sender_phone,
                f"Receipt for *{txn_ref}* received and saved.\n\n"
                "(Stored in the admin's archive — Drive storage is not available "
                "for service accounts on personal Google accounts.)"
            )
        else:
            send_whatsapp_reply(
                sender_phone,
                f"Receipt for *{txn_ref}* received but could not be stored automatically.\n\n"
                "Please send it directly to your administrator for manual filing."
            )


def handle_receipt_photo(message: dict, sender_phone: str):
    """
    Process an incoming image message as a potential receipt.

    Flow:
      1. Extract media ID and caption from the message
      2. Look for a REQ- reference first (requisition workflow — primary path)
      3. Fall back to a TXN- reference (legacy free-form transaction flow)
      4. If neither found: prompt user to resend with the reference in the caption
    """
    image_data = message.get("image", {})
    media_id   = image_data.get("id", "")
    caption    = image_data.get("caption", "").strip()

    req_matches = req.REQ_REF_PATTERN.findall(caption)
    if req_matches:
        handle_requisition_receipt(req_matches[0].upper(), media_id, sender_phone)
        return

    txn_matches = RECEIPT_REF_PATTERN.findall(caption)
    if txn_matches:
        handle_legacy_txn_receipt(txn_matches[0].upper(), media_id, sender_phone)
        return

    send_whatsapp_reply(
        sender_phone,
        "I received your photo but could not find a requisition reference in the caption.\n\n"
        "To attach a receipt, resend the photo with the reference in the caption.\n"
        "Example caption: *REQ-2026-0001*\n\n"
        "(The reference was sent to you when your requisition was approved.)"
    )


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

            message    = value["messages"][0]
            message_id = message.get("id", "")

            # --- Deduplication safety net ---
            # Meta can redeliver the same notification (slow response, multiple
            # subscribed apps, etc.). If we've already processed this exact
            # message id, acknowledge and stop here — do not process twice.
            if is_duplicate_message_id(message_id):
                log.info(f"Duplicate message id {message_id} ignored.")
                return jsonify({"status": "duplicate_ignored"}), 200

            msg_type     = message.get("type")
            sender_phone = message["from"]

            if msg_type == "text":
                raw_text = message["text"]["body"]
                # Respond to Meta immediately, process in background thread.
                # This prevents Meta's 5-second timeout from triggering a
                # redelivery (which was causing duplicate messages).
                threading.Thread(
                    target=handle_incoming_whatsapp_message,
                    args=(raw_text, sender_phone),
                    daemon=True
                ).start()

            elif msg_type == "image":
                # Receipt photo — download and link to a transaction reference
                threading.Thread(
                    target=handle_receipt_photo,
                    args=(message, sender_phone),
                    daemon=True
                ).start()

            else:
                log.info(f"Non-text/image message type '{msg_type}' received. Ignored.")
                return jsonify({"status": "ignored"}), 200

        except (KeyError, IndexError, TypeError) as e:
            log.info(f"Unhandled payload structure: {e}. Ignored.")

        return jsonify({"status": "success"}), 200

# =====================================================================
# 17. HEALTH CHECK
# =====================================================================
@app.route("/health", methods=["GET"])
def health_check():
    registry = fetch_users_registry()
    session = master_session_state
    active_org = session.get("org_name", "none") if session.get("sheet_name") else "none"
    return jsonify({
        "status":                "running",
        "default_entity":        DEFAULT_ENTITY_TYPE,
        "registered_users":      len(registry),
        "pending_users":         len(pending_store),
        "correcting_users":      len(correction_state),
        "retry_queue_size":      sum(len(v) for v in retry_queue.values()),
        "master_active_org":     active_org,
        "pending_receipt_refs":  len(pending_receipt_refs),
        "timestamp":             datetime.datetime.now().isoformat()
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
