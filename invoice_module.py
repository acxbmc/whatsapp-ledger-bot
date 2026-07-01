"""
ACX Ledger Bot — Invoice / Receipt Generation Module

This module is imported by acx_bot.py. It provides:
- A conversational step-by-step invoice editor for the master user
- PDF generation using reportlab, branded with the business profile
- Saving the PDF to Google Drive and returning a shareable link
- Logging every invoice to the Invoices tab in the Master_Users sheet

The flow lives entirely in-memory via `invoice_state`, mirroring the
correction_state pattern already used elsewhere in the bot.
"""

import datetime
import json
import os
import re

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER


# =====================================================================
# STATE — tracks where the master user is in the invoice flow
# =====================================================================
# Structure: { phone: {
#   "step": "client_name" | "client_address" | "items" | "preview" | "edit_choose" | "edit_value",
#   "client_name": str,
#   "client_address": str,
#   "items": [ {"description": str, "amount": int}, ... ],
#   "edit_field": str | None,
#   "edit_index": int | None,
# }}
invoice_state: dict[str, dict] = {}


INVOICE_STEPS_HELP = (
    "Let's create a new invoice/receipt.\n\n"
    "What is the client's name?\n"
    "(Type *cancel* anytime to stop.)"
)


def start_invoice_flow(phone: str) -> str:
    """Begin the invoice creation flow. Returns the first prompt to send."""
    invoice_state[phone] = {
        "step": "client_name",
        "client_name": "",
        "client_address": "",
        "items": [],
        "invoice_date": datetime.datetime.now().strftime("%Y-%m-%d"),
        "payment_status": "Pending",
        "edit_field": None,
        "edit_index": None,
    }
    return INVOICE_STEPS_HELP


def is_in_invoice_flow(phone: str) -> bool:
    return phone in invoice_state


def cancel_invoice_flow(phone: str):
    invoice_state.pop(phone, None)


def _parse_item_line(text: str) -> tuple[str, int] | None:
    """
    Parse a line like 'Monthly bot subscription, 350000' or
    'Monthly bot subscription - 350,000' into (description, amount).
    Returns None if it can't find a trailing number.

    Strategy: find the LAST number in the string (which may itself contain
    commas as thousands separators, e.g. "150,000"), treat everything
    before it as the description, and strip any trailing separator
    characters (",", "-", "—") and whitespace from the description.
    """
    text = text.strip()

    # Match a trailing number that may contain comma thousands separators,
    # e.g. "150,000" or "350000" or "1,250,000"
    match = re.search(r"([\d]{1,3}(?:,\d{3})*|\d+)\s*$", text)
    if not match:
        return None

    amount_str = match.group(1).replace(",", "")
    if not amount_str.isdigit():
        return None

    desc = text[:match.start()].strip()
    # Strip trailing separator characters left over (",", "-", "—", ":")
    desc = desc.rstrip(",-—: ").strip()

    if not desc:
        return None

    return desc, int(amount_str)


def _parse_date_input(text: str) -> str | None:
    """
    Parse a user-entered date into YYYY-MM-DD format.
    Accepts:
      - 2026-06-13
      - 13/06/2026 or 13-06-2026 (day/month/year, standard Uganda format)
      - 06/13/2026 (month/day/year, only used if day > 12 makes d/m/y invalid)
      - "today"
    Returns None if the input can't be parsed.
    """
    text = text.strip().lower()
    if text == "today":
        return datetime.datetime.now().strftime("%Y-%m-%d")

    # ISO format: YYYY-MM-DD
    iso_match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if iso_match:
        try:
            y, m, d = int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))
            return datetime.date(y, m, d).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # DD/MM/YYYY or DD-MM-YYYY (preferred — Ugandan convention)
    dmy_match = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", text)
    if dmy_match:
        a, b, year = int(dmy_match.group(1)), int(dmy_match.group(2)), int(dmy_match.group(3))
        # Try day/month first
        try:
            return datetime.date(year, b, a).strftime("%Y-%m-%d")
        except ValueError:
            pass
        # Fall back to month/day if day/month was invalid
        try:
            return datetime.date(year, a, b).strftime("%Y-%m-%d")
        except ValueError:
            return None

    return None


def build_items_summary(items: list[dict]) -> str:
    if not items:
        return "(no items added yet)"
    lines = []
    total = 0
    for i, item in enumerate(items, 1):
        lines.append(f"  {i}. {item['description']} - UGX {item['amount']:,}")
        total += item["amount"]
    lines.append(f"\nSubtotal: UGX {total:,}")
    return "\n".join(lines)


def build_invoice_preview(state: dict) -> str:
    total = sum(item["amount"] for item in state["items"])
    lines = ["*Invoice preview*\n"]
    lines.append(f"Date: {state.get('invoice_date', '')}")
    lines.append(f"Status: {state.get('payment_status', 'Pending')}")
    lines.append(f"Client: {state['client_name']}")
    if state.get("client_address"):
        lines.append(f"Address: {state['client_address']}")
    lines.append("")
    lines.append("Items:")
    for i, item in enumerate(state["items"], 1):
        lines.append(f"  {i}. {item['description']} - UGX {item['amount']:,}")
    lines.append(f"\nTotal: UGX {total:,}")
    lines.append("\nReply:")
    lines.append("  *yes* to generate the PDF")
    lines.append("  *edit* to change something")
    lines.append("  *no* / *cancel* to discard")
    return "\n".join(lines)


EDITABLE_INVOICE_FIELDS = {
    "1": "client_name",
    "2": "client_address",
    "3": "items",
    "4": "invoice_date",
    "5": "payment_status",
    "client name": "client_name",
    "name": "client_name",
    "address": "client_address",
    "items": "items",
    "date": "invoice_date",
    "invoice date": "invoice_date",
    "status": "payment_status",
    "payment status": "payment_status",
    "payment": "payment_status",
}


def build_invoice_edit_menu() -> str:
    return (
        "What would you like to edit?\n\n"
        "  1 - Client name\n"
        "  2 - Client address\n"
        "  3 - Items (re-enter all items)\n"
        "  4 - Date\n"
        "  5 - Payment status (Paid / Pending)\n\n"
        "Reply with the number."
    )


def process_invoice_message(phone: str, raw_text: str) -> tuple[str, dict | None]:
    """
    Process one message from the master user while they are in the invoice flow.

    Returns (reply_text, finished_invoice_state_or_None).
    If finished_invoice_state is not None, the caller should generate the PDF
    using that state, then call cancel_invoice_flow(phone) to clear it.
    """
    state = invoice_state.get(phone)
    if not state:
        return ("No invoice in progress. Type *invoice* to start one.", None)

    text = raw_text.strip()
    text_lower = text.lower()

    if text_lower in ("cancel", "no", "stop", "abort"):
        cancel_invoice_flow(phone)
        return ("Invoice cancelled.", None)

    step = state["step"]

    # --- Step 1: client name ---
    if step == "client_name":
        if not text:
            return ("Client name cannot be empty. Please type the client's name:", None)
        state["client_name"] = text
        state["step"] = "client_address"
        return (
            "Got it. What is the client's address?\n"
            "(Type *skip* if you don't want to include one.)",
            None
        )

    # --- Step 2: client address ---
    if step == "client_address":
        if text_lower != "skip":
            state["client_address"] = text
        state["step"] = "items"
        return (
            "Now add line items. Send each one as:\n"
            "  description, amount\n\n"
            "Example:\n"
            "  Monthly bot subscription, 350000\n\n"
            "Type *done* when you have added everything.",
            None
        )

    # --- Step 3: items ---
    if step == "items":
        if text_lower == "done":
            if not state["items"]:
                return ("You haven't added any items yet. Add at least one item, or type *cancel*.", None)
            state["step"] = "preview"
            return (build_invoice_preview(state), None)

        parsed = _parse_item_line(text)
        if not parsed:
            return (
                "I couldn't understand that. Please use the format:\n"
                "  description, amount\n\n"
                "Example:\n"
                "  Monthly bot subscription, 350000\n\n"
                "Or type *done* if you're finished.",
                None
            )

        desc, amount = parsed
        state["items"].append({"description": desc, "amount": amount})
        return (
            f"Added: {desc} - UGX {amount:,}\n\n"
            f"{build_items_summary(state['items'])}\n\n"
            "Add another item or type *done*.",
            None
        )

    # --- Step 4: preview / confirm ---
    if step == "preview":
        if text_lower in ("yes", "confirm", "ok", "okay", "generate"):
            finished = dict(state)  # copy before clearing
            return ("Generating your invoice PDF...", finished)

        if text_lower in ("edit", "fix", "change"):
            state["step"] = "edit_choose"
            return (build_invoice_edit_menu(), None)

        return (
            "Please reply *yes* to generate the PDF, *edit* to change something, "
            "or *cancel* to discard.",
            None
        )

    # --- Step 5: choose what to edit ---
    if step == "edit_choose":
        field = EDITABLE_INVOICE_FIELDS.get(text_lower)
        if not field:
            return (
                "Please reply with 1 (client name), 2 (address), 3 (items), "
                "4 (date), or 5 (payment status).",
                None
            )

        if field == "items":
            state["items"] = []
            state["step"] = "items"
            return (
                "Okay, let's re-enter the items.\n\n"
                "Send each one as: description, amount\n"
                "Type *done* when finished.",
                None
            )

        if field == "payment_status":
            state["edit_field"] = field
            state["step"] = "edit_value"
            current = state.get("payment_status", "Pending")
            return (
                f"Current status: {current}\n"
                "Reply *paid* or *pending*:",
                None
            )

        if field == "invoice_date":
            state["edit_field"] = field
            state["step"] = "edit_value"
            current = state.get("invoice_date", "")
            return (
                f"Current date: {current}\n"
                "Enter the new date (e.g. 2026-06-13 or 13/06/2026), "
                "or type *today*:",
                None
            )

        state["edit_field"] = field
        state["step"] = "edit_value"
        current = state.get(field, "")
        field_label = "client name" if field == "client_name" else "client address"
        return (f"Current {field_label}: {current or '(none)'}\nPlease type the new value:", None)

    # --- Step 6: enter new value for edited field ---
    if step == "edit_value":
        field = state["edit_field"]

        if field == "payment_status":
            if text_lower in ("paid", "pay", "1"):
                state["payment_status"] = "Paid"
            elif text_lower in ("pending", "unpaid", "2"):
                state["payment_status"] = "Pending"
            else:
                return ("Please reply *paid* or *pending*:", None)
            state["step"] = "preview"
            state["edit_field"] = None
            return (f"Updated.\n\n{build_invoice_preview(state)}", None)

        if field == "invoice_date":
            parsed = _parse_date_input(text)
            if not parsed:
                return (
                    "I couldn't understand that date. "
                    "Please use YYYY-MM-DD (e.g. 2026-06-13), DD/MM/YYYY (e.g. 13/06/2026), "
                    "or type *today*:",
                    None
                )
            state["invoice_date"] = parsed
            state["step"] = "preview"
            state["edit_field"] = None
            return (f"Updated.\n\n{build_invoice_preview(state)}", None)

        state[field] = text
        state["step"] = "preview"
        state["edit_field"] = None
        return (f"Updated.\n\n{build_invoice_preview(state)}", None)

    # Fallback — shouldn't happen
    cancel_invoice_flow(phone)
    return ("Something went wrong with the invoice flow. Please type *invoice* to start again.", None)


# =====================================================================
# INVOICE NUMBER GENERATION
# =====================================================================
def get_next_invoice_number(gc_client, master_sheet_name: str, invoices_tab_name: str, prefix: str) -> str:
    """
    Generate the next invoice number in the format PREFIX-YEAR-NNNN.
    Looks at existing invoice numbers for the current year and increments.
    """
    current_year = datetime.datetime.now().year
    try:
        sheet = gc_client.open(master_sheet_name).worksheet(invoices_tab_name)
        all_rows = sheet.get_all_values()[1:]  # skip header

        max_seq = 0
        year_prefix = f"{prefix}-{current_year}-"
        for row in all_rows:
            if row and row[0].startswith(year_prefix):
                try:
                    seq = int(row[0].split("-")[-1])
                    max_seq = max(max_seq, seq)
                except (ValueError, IndexError):
                    continue

        next_seq = max_seq + 1
        return f"{prefix}-{current_year}-{next_seq:04d}"

    except Exception:
        # If anything goes wrong, fall back to a timestamp-based number
        return f"{prefix}-{current_year}-{datetime.datetime.now().strftime('%m%d%H%M')}"


# =====================================================================
# PDF GENERATION
# =====================================================================
def generate_invoice_pdf(
    output_path: str,
    invoice_number: str,
    invoice_date: str,
    business_profile: dict,
    client_name: str,
    client_address: str,
    items: list[dict],
    payment_status: str = "Pending",
):
    """
    Generate a branded invoice/receipt PDF using reportlab.
    Saves to output_path. Returns the total amount.
    """
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "InvoiceTitle", parent=styles["Title"], fontSize=20, spaceAfter=2, alignment=TA_LEFT
    )
    label_style = ParagraphStyle(
        "Label", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#666666")
    )
    normal_style = ParagraphStyle(
        "NormalLeft", parent=styles["Normal"], fontSize=10, leading=14
    )
    right_style = ParagraphStyle(
        "NormalRight", parent=styles["Normal"], fontSize=10, leading=14, alignment=TA_RIGHT
    )
    business_name_style = ParagraphStyle(
        "BusinessName", parent=styles["Normal"], fontSize=13, leading=16,
        textColor=colors.HexColor("#1a1a1a")
    )

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=20 * mm, bottomMargin=20 * mm,
        leftMargin=20 * mm, rightMargin=20 * mm,
    )

    story = []

    # --- Header: logo + business details, invoice title + number ---
    logo_path = business_profile.get("logo_path", "")
    if logo_path and os.path.exists(logo_path):
        logo = Image(logo_path, width=40 * mm, height=40 * mm, kind="proportional")
    else:
        logo = Paragraph("", normal_style)

    business_block = Paragraph(
        f"<b>{business_profile['name']}</b><br/>"
        f"{business_profile['address']}<br/>"
        f"TIN: {business_profile['tin']}<br/>"
        f"{business_profile['email']}<br/>"
        f"{business_profile['phone']}",
        normal_style
    )

    # Payment status badge — green for Paid, amber for Pending
    if payment_status.strip().lower() == "paid":
        status_color = "#1a7f37"   # green
    else:
        status_color = "#b35900"   # amber/orange

    invoice_block = Paragraph(
        f"<b>RECEIPT / INVOICE</b><br/>"
        f"No: {invoice_number}<br/>"
        f"Date: {invoice_date}<br/>"
        f'<font color="{status_color}"><b>{payment_status.upper()}</b></font>',
        right_style
    )

    header_table = Table(
        [[logo, business_block, invoice_block]],
        colWidths=[45 * mm, 80 * mm, 55 * mm]
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 8 * mm))
    story.append(HRFlowable(width="100%", thickness=0.75, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 6 * mm))

    # --- Bill To ---
    bill_to_lines = f"<b>Bill To:</b><br/>{client_name}"
    if client_address:
        bill_to_lines += f"<br/>{client_address}"
    story.append(Paragraph(bill_to_lines, normal_style))
    story.append(Spacer(1, 8 * mm))

    # --- Items table ---
    currency = business_profile.get("currency", "UGX")
    table_data = [["#", "Description", f"Amount ({currency})"]]
    total = 0
    for i, item in enumerate(items, 1):
        table_data.append([str(i), item["description"], f"{item['amount']:,}"])
        total += item["amount"]

    table_data.append(["Total", "", f"{total:,}"])

    items_table = Table(table_data, colWidths=[12 * mm, 108 * mm, 50 * mm])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (0, -2), "CENTER"),
        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
        ("ALIGN", (0, -1), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#cccccc")),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.HexColor("#cccccc")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, -1), (1, -1), 12),
        ("SPAN", (0, -1), (1, -1)),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10 * mm))

    # --- Payment details ---
    story.append(Paragraph("<b>Payment Details</b>", normal_style))
    story.append(Paragraph(
        f"Mobile Money: {business_profile['phone']} ({business_profile['name']})<br/>"
        f"Currency: {currency}",
        normal_style
    ))
    story.append(Spacer(1, 10 * mm))

    # --- Footer ---
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        "Thank you for your business.",
        ParagraphStyle("Footer", parent=styles["Normal"], fontSize=9,
                       textColor=colors.HexColor("#888888"), alignment=TA_CENTER)
    ))

    doc.build(story)
    return total


# =====================================================================
# GOOGLE DRIVE UPLOAD
# =====================================================================
class DriveFolderNotFound(Exception):
    """Raised when the expected Drive folder doesn't exist or isn't shared with the service account."""
    pass


def upload_pdf_to_drive(gc_client, local_path: str, drive_filename: str, folder_name: str = "ACX_Invoices", creds=None) -> str:
    """
    Upload a PDF to Google Drive using the same service account credentials
    as gspread, share it as 'anyone with link can view', and return the link.

    IMPORTANT: Service accounts have 0 storage quota of their own. The folder
    named `folder_name` must already exist in YOUR personal Google Drive and
    be shared with the service account email (from acx.json) as Editor.
    This function will NOT attempt to create the folder — it raises
    DriveFolderNotFound if it can't find it, so the caller can fall back
    to sending the PDF directly via WhatsApp instead.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    # creds must be a valid google.oauth2 Credentials object,
    # passed in from acx_bot.py's module-level google_cloud_creds
    drive_service = build("drive", "v3", credentials=creds)

    # Find the folder — do NOT create it (service account has no quota to do so)
    query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    folders = results.get("files", [])

    if not folders:
        raise DriveFolderNotFound(
            f"Folder '{folder_name}' not found or not shared with the service account."
        )

    folder_id = folders[0]["id"]

    # Upload file into the existing, pre-shared folder
    file_metadata = {"name": drive_filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype="application/pdf")
    uploaded = drive_service.files().create(
        body=file_metadata, media_body=media, fields="id"
    ).execute()
    file_id = uploaded["id"]

    # Make shareable
    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


# =====================================================================
# LOG INVOICE TO MASTER SHEET
# =====================================================================
def log_invoice_to_sheet(
    gc_client, master_sheet_name: str, invoices_tab_name: str,
    invoice_number: str, invoice_date: str, client_name: str,
    client_address: str, items: list[dict], total: int, currency: str,
    payment_status: str, pdf_link: str
):
    try:
        sheet = gc_client.open(master_sheet_name).worksheet(invoices_tab_name)
        row = [
            invoice_number,
            invoice_date,
            client_name,
            client_address,
            json.dumps(items),
            total,
            currency,
            payment_status,
            pdf_link,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ]
        sheet.append_row(row)
    except Exception as e:
        # Logging failure shouldn't block sending the PDF to the user
        print(f"Could not log invoice to sheet: {e}")