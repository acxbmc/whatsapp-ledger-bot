"""
ACX Ledger Bot — Requisition Module v1.0

Structured petty cash / procurement workflow — NO AI REQUIRED in main flow.

  Staff  → submits requisition (guided: category → activity → items → confirm)
  Admin  → approves or rejects immediately via WhatsApp
  Staff  → submits receipt photo after purchase
  System → marks requisition as Receipted — audit trail complete

Arithmetic is validated in Python. Categories come from the CoA tab.
No free-form text parsing. No AI guessing.
"""

import datetime
import json
import re
from collections import defaultdict, OrderedDict


# ── Status constants ──────────────────────────────────────────────────────────
STATUS_PENDING   = "Pending"
STATUS_APPROVED  = "Approved"
STATUS_REJECTED  = "Rejected"
STATUS_RECEIPTED = "Receipted"

# ── Sheet tab ─────────────────────────────────────────────────────────────────
REQUISITIONS_TAB_NAME = "Requisitions"
REQUISITIONS_HEADERS  = [
    "REQ Number",        # A (0)
    "Date Submitted",    # B (1)
    "Staff Phone",       # C (2)
    "Staff Name",        # D (3)
    "Account Category",  # E (4)
    "Activity Code",     # F (5)
    "Items (JSON)",      # G (6)
    "Total Amount",      # H (7)
    "Status",            # I (8): Pending / Approved / Rejected / Receipted
    "Approved By",       # J (9)
    "Approved Date",     # K (10)
    "Rejection Reason",  # L (11)
    "Receipt Link",      # M (12)
    "Notes",             # N (13)
]

# ── Regex patterns ────────────────────────────────────────────────────────────
REQ_TRIGGER_WORDS = {"req", "requisition", "request", "purchase request", "new req"}
APPROVE_PATTERN   = re.compile(r"^approve\s+(REQ-\d{4}-\d{4})$", re.IGNORECASE)
REJECT_PATTERN    = re.compile(r"^reject\s+(REQ-\d{4}-\d{4})(?:\s+(.+))?$", re.IGNORECASE)
STATUS_PATTERN    = re.compile(r"^status\s+(REQ-\d{4}-\d{4})$", re.IGNORECASE)
VIEW_PATTERN      = re.compile(r"^view\s+(REQ-\d{4}-\d{4})$", re.IGNORECASE)
REQ_REF_PATTERN   = re.compile(r"\bREQ-\d{4}-\d{4}\b", re.IGNORECASE)

# ── Requisition state machine ─────────────────────────────────────────────────
# { phone: {
#   "step": "category" | "activity" | "free_category" | "items" | "note" | "preview",
#   "categories":     OrderedDict { main_account: [activity1, activity2, ...] },
#   "category_keys":  [str, ...],
#   "selected_category": str,
#   "selected_activity": str,
#   "items":    [{"name": str, "qty": int, "unit_price": int, "total": int}, ...],
#   "note":     str,
# }}
requisition_state: dict = {}

STEP_CATEGORY      = "category"
STEP_ACTIVITY      = "activity"
STEP_FREE_CATEGORY = "free_category"
STEP_ITEMS         = "items"
STEP_NOTE          = "note"
STEP_PREVIEW       = "preview"


# ── CoA parsing ───────────────────────────────────────────────────────────────

def extract_coa_categories(rules: list) -> OrderedDict:
    """
    Parse CoA rules (format: "4000 - Office -> 4001 - Salary") into
    an OrderedDict: { main_account: [activity1, activity2, ...] }.
    Preserves order as it appears in the CoA.
    """
    categories = OrderedDict()
    for rule in rules:
        parts = rule.split(" -> ", 1)
        if len(parts) == 2:
            main, activity = parts[0].strip(), parts[1].strip()
            if main not in categories:
                categories[main] = []
            if activity:
                categories[main].append(activity)
    return categories


def build_category_menu(categories: OrderedDict) -> str:
    lines = ["Which account category is this requisition for?\n"]
    for i, cat in enumerate(categories.keys(), 1):
        label = cat.split(" - ", 1)[-1] if " - " in cat else cat
        lines.append(f"  {i}. {label}")
    lines.append("\nReply with the number.")
    return "\n".join(lines)


def build_activity_menu(activities: list, main_category: str) -> str:
    label = main_category.split(" - ", 1)[-1] if " - " in main_category else main_category
    lines = [f"*{label}* — Select the specific activity:\n"]
    for i, act in enumerate(activities, 1):
        act_label = act.split(" - ", 1)[-1] if " - " in act else act
        lines.append(f"  {i}. {act_label}")
    lines.append(f"  {len(activities) + 1}. Use main category only")
    lines.append("\nReply with the number.")
    return "\n".join(lines)


# ── Item parsing ──────────────────────────────────────────────────────────────

def parse_item_line(text: str):
    """
    Parse an item line into a dict. Accepts these formats:
      "Pens, 3, 1500"        → name=Pens, qty=3, unit=1500
      "Pens, 3 x 1500"       → same
      "Pens, 3 @ 1,500"      → same (with comma-formatted number)
    Returns None if parsing fails.
    """
    text = text.strip()

    # Format: name, qty, unit_price
    parts = [p.strip() for p in text.split(",")]
    if len(parts) >= 3:
        # Last two parts should be qty and unit_price
        # Name may contain commas so join all but last two
        name       = ", ".join(parts[:-2]).strip()
        qty_str    = parts[-2].replace(",", "").strip()
        price_str  = parts[-1].replace(",", "").strip()
        if qty_str.isdigit() and price_str.isdigit() and name:
            qty   = int(qty_str)
            price = int(price_str)
            return {"name": name, "qty": qty, "unit_price": price, "total": qty * price}

    # Format: name, qty x price  or  name, qty @ price
    match = re.match(r"^(.+),\s*(\d+)\s*[xX×@]\s*([\d,]+)\s*$", text)
    if match:
        name  = match.group(1).strip()
        qty   = int(match.group(2))
        price = int(match.group(3).replace(",", ""))
        return {"name": name, "qty": qty, "unit_price": price, "total": qty * price}

    return None


def build_items_preview(items: list) -> str:
    if not items:
        return "  (no items yet)"
    lines = []
    total = 0
    for i, item in enumerate(items, 1):
        lines.append(
            f"  {i}. {item['name']}: "
            f"{item['qty']} x {item['unit_price']:,} = UGX {item['total']:,}"
        )
        total += item["total"]
    lines.append(f"  {'─' * 38}")
    lines.append(f"  Total: UGX {total:,}")
    return "\n".join(lines)


def build_req_preview(state: dict) -> str:
    cat = state["selected_category"]
    cat_label = cat.split(" - ", 1)[-1] if " - " in cat else cat
    act = state.get("selected_activity", "")
    act_label = act.split(" - ", 1)[-1] if act and " - " in act else act

    lines = ["*Requisition Preview*\n"]
    lines.append(f"Account:  {cat_label}")
    if act_label:
        lines.append(f"Activity: {act_label}")
    if state.get("note"):
        lines.append(f"Note:     {state['note']}")
    lines.append("\nItems:")
    lines.append(build_items_preview(state["items"]))
    lines.append("\nReply:")
    lines.append("  *yes*        - submit for approval")
    lines.append("  *add*        - add another item")
    lines.append("  *remove N*   - remove item number N")
    lines.append("  *note*       - add or edit a note")
    lines.append("  *cancel*     - discard this requisition")
    return "\n".join(lines)


# ── State machine ─────────────────────────────────────────────────────────────

def start_requisition(phone: str, coa_rules: list) -> str:
    """Initialize state and return the first prompt sent to the user."""
    categories = extract_coa_categories(coa_rules) if coa_rules else OrderedDict()
    requisition_state[phone] = {
        "step":               STEP_CATEGORY if categories else STEP_FREE_CATEGORY,
        "categories":         categories,
        "category_keys":      list(categories.keys()),
        "selected_category":  "",
        "selected_activity":  "",
        "items":              [],
        "note":               "",
    }
    if not categories:
        return (
            "No Chart of Accounts found.\n\n"
            "Please type the account category for this requisition:\n"
            "(e.g. Office Expenses, Transport, Staff Costs)"
        )
    return build_category_menu(categories)


def is_in_requisition_flow(phone: str) -> bool:
    return phone in requisition_state


def cancel_requisition(phone: str):
    requisition_state.pop(phone, None)


def process_requisition_message(phone: str, text: str) -> tuple:
    """
    Process one message in the requisition flow.
    Returns (reply_text, completed_state_or_None).
    If completed_state is not None, the caller should call submit_requisition().
    """
    state = requisition_state.get(phone)
    if not state:
        return ("No requisition in progress. Type *req* to start one.", None)

    text_lower = text.strip().lower()

    if text_lower in ("cancel", "abort", "stop", "no"):
        cancel_requisition(phone)
        return ("Requisition cancelled.", None)

    step = state["step"]

    # ── Category selection ────────────────────────────────────────────────────
    if step == STEP_CATEGORY:
        keys = state["category_keys"]
        t    = text.strip()
        if not t.isdigit() or not (1 <= int(t) <= len(keys)):
            return (f"Please reply with a number between 1 and {len(keys)}.", None)
        idx      = int(t) - 1
        selected = keys[idx]
        state["selected_category"] = selected
        activities = state["categories"].get(selected, [])
        cat_label  = selected.split(" - ", 1)[-1] if " - " in selected else selected

        if activities:
            state["step"] = STEP_ACTIVITY
            return (build_activity_menu(activities, selected), None)

        state["selected_activity"] = ""
        state["step"] = STEP_ITEMS
        return (
            f"*{cat_label}* selected.\n\n"
            "Now add your items one by one using the format:\n"
            "*item name, quantity, unit price*\n\n"
            "Example: *Pens, 3, 1500*\n"
            "(means 3 pens at UGX 1,500 each = UGX 4,500)\n\n"
            "Type *done* when you have added everything.",
            None
        )

    # ── Activity selection ────────────────────────────────────────────────────
    if step == STEP_ACTIVITY:
        activities  = state["categories"].get(state["selected_category"], [])
        max_choice  = len(activities) + 1
        t           = text.strip()
        if not t.isdigit() or not (1 <= int(t) <= max_choice):
            return (f"Please reply with a number between 1 and {max_choice}.", None)
        idx = int(t) - 1
        state["selected_activity"] = "" if idx == len(activities) else activities[idx]
        state["step"] = STEP_ITEMS
        cat_label     = state["selected_category"].split(" - ", 1)[-1] if " - " in state["selected_category"] else state["selected_category"]
        return (
            f"*{cat_label}* selected.\n\n"
            "Now add your items one by one using the format:\n"
            "*item name, quantity, unit price*\n\n"
            "Example: *Pens, 3, 1500*\n"
            "Type *done* when finished.",
            None
        )

    # ── Free-text category (no CoA) ───────────────────────────────────────────
    if step == STEP_FREE_CATEGORY:
        state["selected_category"] = text.strip()
        state["selected_activity"] = ""
        state["step"]              = STEP_ITEMS
        return (
            f"*{text.strip()}* set as category.\n\n"
            "Now add items:\n*item name, quantity, unit price*\n\n"
            "Example: *Fuel, 10, 5000*\n"
            "Type *done* when finished.",
            None
        )

    # ── Item entry ────────────────────────────────────────────────────────────
    if step == STEP_ITEMS:
        if text_lower == "done":
            if not state["items"]:
                return ("Please add at least one item before submitting.", None)
            state["step"] = STEP_PREVIEW
            return (build_req_preview(state), None)

        item = parse_item_line(text)
        if not item:
            return (
                "I could not understand that format.\n\n"
                "Please use: *item name, quantity, unit price*\n"
                "Example: *Pens, 3, 1500*\n\n"
                "Type *done* if you are finished.",
                None
            )

        state["items"].append(item)
        running_total = sum(i["total"] for i in state["items"])
        return (
            f"Added: {item['name']} — {item['qty']} x UGX {item['unit_price']:,} = UGX {item['total']:,}\n\n"
            f"Running total: UGX {running_total:,}\n\n"
            "Add another item or type *done*.",
            None
        )

    # ── Preview / confirm ─────────────────────────────────────────────────────
    if step == STEP_PREVIEW:
        if text_lower in ("yes", "confirm", "submit", "ok", "okay"):
            finished = dict(state)
            return ("Processing your requisition...", finished)

        if text_lower == "add":
            state["step"] = STEP_ITEMS
            return ("Send the next item (name, quantity, unit price):", None)

        if text_lower.startswith("remove "):
            parts = text_lower.split()
            if len(parts) == 2 and parts[1].isdigit():
                idx = int(parts[1]) - 1
                if 0 <= idx < len(state["items"]):
                    removed = state["items"].pop(idx)
                    if not state["items"]:
                        state["step"] = STEP_ITEMS
                        return (
                            f"Removed *{removed['name']}*.\n\n"
                            "Add more items or type *done*.",
                            None
                        )
                    return (
                        f"Removed *{removed['name']}*.\n\n{build_req_preview(state)}",
                        None
                    )
            return ("Usage: *remove N* where N is the item number from the list.", None)

        if text_lower == "note":
            state["step"] = STEP_NOTE
            current = state.get("note", "")
            return (
                f"Current note: {current or '(none)'}\n\nType your note:",
                None
            )

        return (
            "Reply *yes* to submit, *add* to add an item, "
            "*remove N* to remove item N, *note* to add a note, or *cancel* to discard.",
            None
        )

    # ── Note entry ────────────────────────────────────────────────────────────
    if step == STEP_NOTE:
        state["note"]  = text.strip()
        state["step"]  = STEP_PREVIEW
        return (f"Note saved.\n\n{build_req_preview(state)}", None)

    cancel_requisition(phone)
    return ("Something went wrong. Type *req* to start again.", None)


# ── REQ number generation ─────────────────────────────────────────────────────

def generate_req_number(gc_client, sheet_name: str) -> str:
    year   = datetime.datetime.now().year
    prefix = f"REQ-{year}-"
    try:
        ws      = gc_client.open(sheet_name).worksheet(REQUISITIONS_TAB_NAME)
        col_a   = ws.col_values(1)[1:]  # skip header
        max_seq = 0
        for cell in col_a:
            if cell.startswith(prefix):
                try:
                    seq = int(cell.split("-")[-1])
                    max_seq = max(max_seq, seq)
                except (ValueError, IndexError):
                    pass
        return f"{prefix}{max_seq + 1:04d}"
    except Exception:
        return f"{prefix}{datetime.datetime.now().strftime('%m%d%H%M%S')}"


# ── Submit requisition ────────────────────────────────────────────────────────

def submit_requisition(gc_client, sheet_name: str, phone: str, name: str, state: dict) -> str:
    """Write requisition to the Requisitions tab. Returns the REQ number."""
    req_number = generate_req_number(gc_client, sheet_name)
    ws         = gc_client.open(sheet_name).worksheet(REQUISITIONS_TAB_NAME)
    total      = sum(i["total"] for i in state["items"])
    row = [
        req_number,
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        phone,
        name,
        state["selected_category"],
        state.get("selected_activity", ""),
        json.dumps(state["items"]),
        total,
        STATUS_PENDING,
        "", "", "",         # Approved By, Approved Date, Rejection Reason
        "",                 # Receipt Link
        state.get("note", ""),
    ]
    ws.append_row(row)
    cancel_requisition(phone)
    return req_number


# ── Find admins for an org ────────────────────────────────────────────────────

def get_org_admins(gc_client, base_sheet_name: str, users_tab: str, entity_type: str, org_sheet_name: str = "") -> list:
    """
    Return list of admin phone numbers for an org.
    Matches by entity_type or by sheet_override equalling org_sheet_name.
    """
    try:
        ws   = gc_client.open(base_sheet_name).worksheet(users_tab)
        rows = ws.get_all_values()[1:]
        admins = []
        for row in rows:
            if len(row) < 4:
                continue
            row_phone       = row[0].strip().replace(" ", "").replace("+", "")
            row_entity_type = row[2].strip().lower() if len(row) > 2 else ""
            row_role        = row[3].strip().lower() if len(row) > 3 else ""
            row_status      = row[7].strip().lower() if len(row) > 7 else "active"
            sheet_override  = row[4].strip()         if len(row) > 4 else ""

            if row_role != "admin" or row_status == "inactive":
                continue
            if (
                (org_sheet_name and sheet_override == org_sheet_name)
                or row_entity_type == entity_type
            ):
                admins.append(row_phone)
        return admins
    except Exception:
        return []


# ── Approve ───────────────────────────────────────────────────────────────────

def approve_requisition(gc_client, sheet_name: str, req_number: str, admin_name: str) -> tuple:
    """
    Approve a Pending requisition.
    Returns (success: bool, data: dict).
    data contains req details on success, or {"error": str} on failure.
    """
    try:
        ws   = gc_client.open(sheet_name).worksheet(REQUISITIONS_TAB_NAME)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if not row or row[0].strip().upper() != req_number.upper():
                continue
            if row[8].strip() != STATUS_PENDING:
                return (False, {"error": f"This requisition is already *{row[8]}* and cannot be approved."})
            ws.update_cell(i, 9,  STATUS_APPROVED)
            ws.update_cell(i, 10, admin_name)
            ws.update_cell(i, 11, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            items = []
            try:
                items = json.loads(row[6]) if row[6] else []
            except Exception:
                pass
            return (True, {
                "req_number":  row[0],
                "staff_phone": row[2].strip(),
                "staff_name":  row[3],
                "category":    row[4].split(" - ", 1)[-1] if " - " in row[4] else row[4],
                "activity":    row[5].split(" - ", 1)[-1] if row[5] and " - " in row[5] else row[5],
                "items":       items,
                "total":       row[7],
                "note":        row[13] if len(row) > 13 else "",
            })
        return (False, {"error": f"*{req_number}* not found in this organisation's records."})
    except Exception as e:
        return (False, {"error": str(e)})


# ── Reject ────────────────────────────────────────────────────────────────────

def reject_requisition(gc_client, sheet_name: str, req_number: str, admin_name: str, reason: str) -> tuple:
    """Reject a Pending requisition with a reason."""
    try:
        ws   = gc_client.open(sheet_name).worksheet(REQUISITIONS_TAB_NAME)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if not row or row[0].strip().upper() != req_number.upper():
                continue
            if row[8].strip() != STATUS_PENDING:
                return (False, {"error": f"This requisition is already *{row[8]}*."})
            ws.update_cell(i, 9,  STATUS_REJECTED)
            ws.update_cell(i, 10, admin_name)
            ws.update_cell(i, 12, reason or "No reason given")
            return (True, {
                "req_number":  row[0],
                "staff_phone": row[2].strip(),
                "staff_name":  row[3],
                "total":       row[7],
            })
        return (False, {"error": f"*{req_number}* not found."})
    except Exception as e:
        return (False, {"error": str(e)})


# ── Mark receipted ────────────────────────────────────────────────────────────

def receipt_requisition(gc_client, sheet_name: str, req_number: str, receipt_link: str) -> tuple:
    """Attach a receipt and mark the requisition as Receipted."""
    try:
        ws   = gc_client.open(sheet_name).worksheet(REQUISITIONS_TAB_NAME)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if not row or row[0].strip().upper() != req_number.upper():
                continue
            status = row[8].strip()
            if status == STATUS_REJECTED:
                return (False, {"error": "Cannot receipt a rejected requisition."})
            if status == STATUS_RECEIPTED:
                return (False, {"error": "This requisition has already been receipted."})
            ws.update_cell(i, 9,  STATUS_RECEIPTED)
            ws.update_cell(i, 13, receipt_link)
            return (True, {
                "req_number":  row[0],
                "staff_phone": row[2].strip(),
                "staff_name":  row[3],
                "total":       row[7],
            })
        return (False, {"error": f"*{req_number}* not found."})
    except Exception as e:
        return (False, {"error": str(e)})


# ── List requisitions ─────────────────────────────────────────────────────────

def list_requisitions(
    gc_client, sheet_name: str,
    status_filter: str  = None,
    phone_filter:  str  = None,
    limit:         int  = 15,
) -> list:
    """
    Fetch requisitions filtered by status and/or phone.
    Returns most-recent-first, capped at limit.
    """
    try:
        ws   = gc_client.open(sheet_name).worksheet(REQUISITIONS_TAB_NAME)
        rows = ws.get_all_values()[1:]
        results = []
        for row in rows:
            if len(row) < 9:
                continue
            if status_filter and row[8].strip() != status_filter:
                continue
            if phone_filter and row[2].strip() != phone_filter:
                continue
            try:
                total = int(str(row[7]).replace(",", "")) if row[7] else 0
            except ValueError:
                total = 0
            results.append({
                "req_number":  row[0],
                "date":        row[1][:10] if row[1] else "",
                "staff_phone": row[2].strip(),
                "staff_name":  row[3],
                "category":    row[4].split(" - ", 1)[-1] if " - " in row[4] else row[4],
                "total":       total,
                "status":      row[8],
            })
        return list(reversed(results))[:limit]
    except Exception:
        return []


def format_req_list(reqs: list, title: str) -> str:
    if not reqs:
        return f"*{title}*\n\nNone found."
    lines = [f"*{title}* ({len(reqs)} shown)\n"]
    for r in reqs:
        cat = r["category"][:28] + "…" if len(r["category"]) > 28 else r["category"]
        lines.append(
            f"*{r['req_number']}* — {r['staff_name'] or r['staff_phone']}\n"
            f"  {cat} | UGX {r['total']:,} | {r['date']}"
        )
    return "\n".join(lines)


# ── Single requisition detail ─────────────────────────────────────────────────

def get_requisition_detail(gc_client, sheet_name: str, req_number: str):
    """Return full details of a single requisition, or None if not found."""
    try:
        ws   = gc_client.open(sheet_name).worksheet(REQUISITIONS_TAB_NAME)
        rows = ws.get_all_values()
        for row in rows[1:]:
            if not row or row[0].strip().upper() != req_number.upper():
                continue
            items = []
            try:
                items = json.loads(row[6]) if row[6] else []
            except Exception:
                pass
            return {
                "req_number":    row[0],
                "date":          row[1],
                "staff_phone":   row[2].strip(),
                "staff_name":    row[3],
                "category":      row[4],
                "activity":      row[5],
                "items":         items,
                "total":         row[7],
                "status":        row[8],
                "approved_by":   row[9]  if len(row) > 9  else "",
                "approved_date": row[10] if len(row) > 10 else "",
                "reason":        row[11] if len(row) > 11 else "",
                "receipt_link":  row[12] if len(row) > 12 else "",
                "note":          row[13] if len(row) > 13 else "",
            }
        return None
    except Exception:
        return None


def format_req_detail(req: dict) -> str:
    cat_label = req["category"].split(" - ", 1)[-1] if " - " in req["category"] else req["category"]
    act_label = req["activity"].split(" - ", 1)[-1] if req.get("activity") and " - " in req["activity"] else req.get("activity", "")
    try:
        total = int(str(req["total"]).replace(",", ""))
    except (ValueError, TypeError):
        total = 0

    lines = [f"*{req['req_number']}*  [{req['status']}]\n"]
    lines.append(f"Date:     {req['date']}")
    lines.append(f"By:       {req['staff_name']} ({req['staff_phone']})")
    lines.append(f"Account:  {cat_label}")
    if act_label:
        lines.append(f"Activity: {act_label}")
    if req.get("note"):
        lines.append(f"Note:     {req['note']}")
    lines.append("\nItems:")
    if req["items"]:
        for item in req["items"]:
            try:
                unit  = int(item.get("unit_price", 0))
                itot  = int(item.get("total", 0))
                lines.append(f"  {item['name']}: {item['qty']} x {unit:,} = UGX {itot:,}")
            except (ValueError, TypeError):
                lines.append(f"  {item['name']}")
    lines.append(f"\nTotal: UGX {total:,}")
    if req.get("approved_by"):
        lines.append(f"Approved by: {req['approved_by']}  ({req.get('approved_date', '')})")
    if req.get("reason"):
        lines.append(f"Rejected: {req['reason']}")
    if req.get("receipt_link"):
        lines.append(f"Receipt: {req['receipt_link']}")
    return "\n".join(lines)


# ── Spending summary ──────────────────────────────────────────────────────────

def get_spending_summary(gc_client, sheet_name: str) -> str:
    """Spending grouped by category for approved + receipted requisitions."""
    try:
        ws   = gc_client.open(sheet_name).worksheet(REQUISITIONS_TAB_NAME)
        rows = ws.get_all_values()[1:]
        by_cat    = defaultdict(int)
        total_out = 0
        pending   = 0
        count_unreceipted = 0

        for row in rows:
            if len(row) < 9:
                continue
            status = row[8].strip()
            try:
                amount = int(str(row[7]).replace(",", "")) if row[7] else 0
            except ValueError:
                amount = 0
            cat = row[4].split(" - ", 1)[-1] if " - " in row[4] else row[4]

            if status == STATUS_PENDING:
                pending += amount
            elif status == STATUS_APPROVED:
                by_cat[cat]   += amount
                total_out     += amount
                count_unreceipted += 1
            elif status == STATUS_RECEIPTED:
                by_cat[cat] += amount
                total_out   += amount

        lines = ["*Spending Summary*\n"]
        if by_cat:
            lines.append("By Account (approved + receipted):")
            for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
                lines.append(f"  {cat}: UGX {amt:,}")
            lines.append(f"\n  Total: UGX {total_out:,}")
        else:
            lines.append("  No approved spending recorded yet.")
        if pending:
            lines.append(f"\n  Pending approval: UGX {pending:,}")
        if count_unreceipted:
            lines.append(f"  Approved but unreceipted: {count_unreceipted} requisition(s)")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not generate summary: {e}"
