import streamlit as st
import pandas as pd
import json
import qrcode
import io
import os
import zipfile
import smtplib
import re as _re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_OK = True
except ImportError:
    GSPREAD_OK = False

st.set_page_config(
    page_title="RFID·QR Material Manager",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed"
)

DATA_FILE = "material_data.json"

# ─────────────────────────────────────────────
# DATA STORAGE
# ─────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_master_df():
    """Get master material list from Google Sheets (single source of truth).
    Returns (DataFrame, error_message). DataFrame is None on failure."""
    df, err = get_inventory_df()
    if err:
        return None, err
    if df is None or df.empty:
        return None, "Google Sheets inventory is empty or not connected"
    return df, None

# ─────────────────────────────────────────────
# URL HELPER
# ─────────────────────────────────────────────
DEFAULT_APP_URL = "https://rfidpticmockup-qfn4gve2qgohbwaechw7qh.streamlit.app"

def get_base_url():
    if st.session_state.get("base_url"):
        return st.session_state["base_url"].rstrip("/")
    try:
        url = st.secrets.get("base_url", "")
        if url:
            return url.rstrip("/")
    except:
        pass
    return DEFAULT_APP_URL

def tag_url(rfid_tag_code):
    return f"{get_base_url()}?tag={rfid_tag_code}"

# ─────────────────────────────────────────────
# QR CODE GENERATOR
# ─────────────────────────────────────────────
def make_qr_image(url, label_top, label_bot="Scan for material info"):
    qr = qrcode.QRCode(version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    label_h = 56
    canvas = Image.new("RGB", (qr_img.width, qr_img.height + label_h), "white")
    canvas.paste(qr_img, (0, 0))
    draw = ImageDraw.Draw(canvas)
    try:
        font_big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
        font_sm  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
    except:
        font_big = ImageFont.load_default()
        font_sm  = font_big
    bbox = draw.textbbox((0,0), label_top, font=font_big)
    draw.text(((qr_img.width-(bbox[2]-bbox[0]))//2, qr_img.height+5), label_top, fill="black", font=font_big)
    bbox2 = draw.textbbox((0,0), label_bot, font=font_sm)
    draw.text(((qr_img.width-(bbox2[2]-bbox2[0]))//2, qr_img.height+26), label_bot, fill="#555555", font=font_sm)
    tag_lbl = "RFID·QR SYSTEM"
    bbox3 = draw.textbbox((0,0), tag_lbl, font=font_sm)
    draw.text(((qr_img.width-(bbox3[2]-bbox3[0]))//2, qr_img.height+42), tag_lbl, fill="#888888", font=font_sm)
    return canvas

def qr_to_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# ─────────────────────────────────────────────
# FIELD CONFIG
# ─────────────────────────────────────────────
EXPECTED_COLS = [
    "Material", "Plant", "Storage Location", "Storage Type",
    "Storage Section", "Storage Bin", "Material Description",
    "Batch", "Stock Category", "Total Stock",
    "Base Unit of Measure", "SLED/BBD", "GR Date", "RFID Tag Code"
]

DROPDOWN_FIELDS = [
    "Material", "Plant", "Storage Location", "Storage Type",
    "Storage Section", "Stock Category", "Base Unit of Measure", "Storage Bin"
]

def get_field_options(data, field):
    vals = set()
    for rec in data.values():
        v = rec.get(field, "").strip()
        if v and not v.startswith("_"):
            vals.add(v)
    return sorted(vals)

def parse_csv_generic(uploaded_file):
    try:
        df = pd.read_csv(uploaded_file, dtype=str).fillna("")
        df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
        return df, None
    except Exception as e:
        return None, str(e)

def parse_csv(uploaded_file):
    df, err = parse_csv_generic(uploaded_file)
    if err:
        return None, err
    if "RFID Tag Code" not in df.columns:
        return None, "CSV missing 'RFID Tag Code' column"
    df = df[df["RFID Tag Code"].str.strip() != ""]
    return df, None

# ─────────────────────────────────────────────
# MASTER LIST HELPERS
# ─────────────────────────────────────────────
def master_display_opts(master_df):
    """Build display list 'MaterialID | Description' for selectbox."""
    mat_col   = next((c for c in master_df.columns if "Material Description" in c), None)
    matid_col = next((c for c in master_df.columns if c.strip() == "Material"), None)
    if matid_col and mat_col:
        return ["— choose material —"] + [
            f"{row[matid_col]} | {row[mat_col]}" for _, row in master_df.iterrows()
        ]
    elif matid_col:
        return ["— choose material —"] + master_df[matid_col].tolist()
    return ["— choose material —"] + [f"Row {i+1}" for i in range(len(master_df))]

def prefill_from_master(master_df, chosen, display_opts):
    """Return dict of field values from the chosen master row."""
    if chosen == "— choose material —":
        return {}
    idx = display_opts.index(chosen) - 1
    row = master_df.iloc[idx]
    return {col: str(row.get(col, "")).strip()
            for col in EXPECTED_COLS if col in master_df.columns}

# ─────────────────────────────────────────────
# PASSWORD HELPERS
# ─────────────────────────────────────────────
DEFAULT_PASSWORD = "RFID123"

# ─────────────────────────────────────────────
# CHECKOUT / EMAIL CONFIG
# ─────────────────────────────────────────────
DEFAULT_ALLOWED_DOMAIN = "@gmail.com"   # change in Setup
DEFAULT_ADMIN_EMAIL    = "geoworkingstation@gmail.com"
SMTP_CONFIG_FILE       = "smtp_config.json"

def load_smtp_config():
    if os.path.exists(SMTP_CONFIG_FILE):
        with open(SMTP_CONFIG_FILE) as f:
            return json.load(f)
    return {
        "smtp_host":      "smtp.gmail.com",
        "smtp_port":      587,
        "smtp_user":      "geoworkingstation@gmail.com",
        "smtp_password":  "ogrrsusoowdyhsea",
        "admin_email":    DEFAULT_ADMIN_EMAIL,
        "allowed_domain": DEFAULT_ALLOWED_DOMAIN,
    }

def save_smtp_config(cfg):
    with open(SMTP_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ─────────────────────────────────────────────
# GOOGLE SHEETS HELPERS
# ─────────────────────────────────────────────
GSHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_gsheets_client():
    """Return (client, error_message). client is None if failed."""
    if not GSPREAD_OK:
        return None, "gspread not installed — add gspread and google-auth to requirements.txt"
    try:
        raw = st.secrets.get("gsheets", {})
        if not raw:
            return None, "No [gsheets] section in Streamlit secrets"
        creds_raw = raw.get("credentials", {})
        if not creds_raw:
            return None, "No [gsheets.credentials] section in Streamlit secrets"
        creds_dict = dict(creds_raw)
        # Fix escaped newlines in private_key (common in TOML secrets)
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        creds = Credentials.from_service_account_info(creds_dict, scopes=GSHEETS_SCOPES)
        client = gspread.authorize(creds)
        return client, None
    except KeyError as e:
        return None, f"Missing key in secrets: {e}"
    except Exception as e:
        return None, f"Auth error: {e}"

def get_inventory_sheet():
    """Return (worksheet, error_message)."""
    client, err = get_gsheets_client()
    if client is None:
        return None, err
    try:
        raw = st.secrets.get("gsheets", {})
        spreadsheet_id = raw.get("spreadsheet_id", "")
        sheet_name     = raw.get("sheet_name", "inventory")
        if not spreadsheet_id:
            return None, "spreadsheet_id not set in [gsheets] secrets"
        sh = client.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name)
        return ws, None
    except gspread.exceptions.SpreadsheetNotFound:
        return None, "Spreadsheet not found — check spreadsheet_id and that service account has access"
    except gspread.exceptions.WorksheetNotFound:
        return None, f"Worksheet tab not found — create a tab named '{raw.get('sheet_name','inventory')}'"
    except Exception as e:
        return None, f"Sheet error: {e}"

def get_inventory_df():
    """Return (DataFrame or None, error_message)."""
    ws, err = get_inventory_sheet()
    if ws is None:
        return None, err
    try:
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(), None
        return pd.DataFrame(records), None
    except Exception as e:
        return None, f"Could not read sheet: {e}"

def clear_rfid_in_sheet(old_tag_code):
    """Remove RFID Tag Code from the old row in Google Sheets when tag is cleared.
    Returns (success, message)."""
    ws, err = get_inventory_sheet()
    if ws is None:
        return False, f"Sheet not connected: {err}"
    try:
        headers = ws.row_values(1)
        col_tag = headers.index("RFID Tag Code") + 1 if "RFID Tag Code" in headers else None
        if col_tag is None:
            return False, "RFID Tag Code column not found"
        tag_vals = ws.col_values(col_tag)
        if old_tag_code not in tag_vals:
            return True, "Tag not found in sheet (already cleared or never registered)"
        row_idx = tag_vals.index(old_tag_code) + 1
        ws.update_cell(row_idx, col_tag, "")
        return True, f"Cleared RFID Tag Code from row {row_idx}"
    except Exception as e:
        return False, str(e)


def register_new_item_in_sheet(new_vals):
    """Register a newly saved tag into Google Sheets.
    - If a row with matching Material already exists (no RFID tag), update that row.
    - Otherwise append a new row.
    Returns (success, message)."""
    ws, err = get_inventory_sheet()
    if ws is None:
        return False, f"Sheet not connected: {err}"
    try:
        headers = ws.row_values(1)

        def col_of(name):
            return headers.index(name) + 1 if name in headers else None

        col_tag = col_of("RFID Tag Code")
        col_mat = col_of("Material")
        if col_tag is None:
            return False, "RFID Tag Code column not found in sheet"

        tag_code   = new_vals.get("RFID Tag Code", "")
        material   = new_vals.get("Material", "")
        now_str    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Try to find a matching row: same Material, empty RFID Tag Code
        tag_col_vals = ws.col_values(col_tag)
        mat_col_vals = ws.col_values(col_mat) if col_mat else []

        target_row = None
        # First priority: find row with same tag_code (re-registration of same tag)
        if tag_code in tag_col_vals:
            target_row = tag_col_vals.index(tag_code) + 1
        # Second priority: find row with same material but empty RFID Tag Code
        elif material and col_mat:
            for i, (mat_v, tag_v) in enumerate(zip(mat_col_vals, tag_col_vals), start=1):
                if str(mat_v).strip() == str(material).strip() and str(tag_v).strip() == "":
                    target_row = i
                    break

        if target_row:
            # Update existing row
            updates = []
            for field in EXPECTED_COLS:
                c = col_of(field)
                if c and field in new_vals:
                    updates.append({
                        "range": gspread.utils.rowcol_to_a1(target_row, c),
                        "values": [[str(new_vals.get(field, ""))]]
                    })
            # Also update Last Updated
            col_lu = col_of("Last Updated")
            if col_lu:
                updates.append({
                    "range": gspread.utils.rowcol_to_a1(target_row, col_lu),
                    "values": [[now_str]]
                })
            if updates:
                ws.batch_update(updates)
            return True, f"Updated existing row {target_row} in Google Sheets"
        else:
            # Append new row
            new_row = [""] * len(headers)
            for field in EXPECTED_COLS:
                c = col_of(field)
                if c and field in new_vals:
                    new_row[c - 1] = str(new_vals.get(field, ""))
            col_lu = col_of("Last Updated")
            if col_lu:
                new_row[col_lu - 1] = now_str
            ws.append_row(new_row, value_input_option="USER_ENTERED")
            return True, "Appended new row to Google Sheets"

    except gspread.exceptions.APIError as e:
        return False, f"API error: {e}"
    except Exception as e:
        return False, f"Error: {type(e).__name__}: {e}"


def update_stock_after_checkout(tag_code, qty_approved, checkout_info):
    """
    Deduct qty_approved from Total Stock for the row matching RFID Tag Code.
    Also updates Last Updated, Last Checkout By, Last Checkout Date.
    Returns (success: bool, message: str)
    """
    ws, err = get_inventory_sheet()
    if ws is None:
        return False, f"Google Sheets not connected: {err}"
    try:
        headers = ws.row_values(1)

        def col_of(name):
            return headers.index(name) + 1 if name in headers else None

        col_tag     = col_of("RFID Tag Code")
        col_stock   = col_of("Total Stock")
        col_updated = col_of("Last Updated")
        col_by      = col_of("Last Checkout By")
        col_date    = col_of("Last Checkout Date")
        col_qty     = col_of("Last Checkout Qty")

        if col_tag is None:
            return False, "Column 'RFID Tag Code' not found in sheet header row"
        if col_stock is None:
            return False, "Column 'Total Stock' not found in sheet header row"

        tag_col_vals = ws.col_values(col_tag)
        if tag_code not in tag_col_vals:
            return False, f"RFID Tag Code '{tag_code}' not found in sheet"
        row_idx = tag_col_vals.index(tag_code) + 1  # 1-based

        current_stock_str = ws.cell(row_idx, col_stock).value
        try:
            current_stock = float(str(current_stock_str).replace(",","").strip())
        except (ValueError, TypeError):
            return False, f"Cannot parse stock value '{current_stock_str}' in row {row_idx}"

        new_stock = max(0.0, current_stock - float(qty_approved))
        now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Use batch_update for efficiency and reliability
        cell_updates = []
        cell_updates.append({"range": gspread.utils.rowcol_to_a1(row_idx, col_stock),
                              "values": [[new_stock]]})
        if col_updated:
            cell_updates.append({"range": gspread.utils.rowcol_to_a1(row_idx, col_updated),
                                  "values": [[now_str]]})
        if col_by:
            cell_updates.append({"range": gspread.utils.rowcol_to_a1(row_idx, col_by),
                                  "values": [[checkout_info.get("req_name","")]]})
        if col_date:
            cell_updates.append({"range": gspread.utils.rowcol_to_a1(row_idx, col_date),
                                  "values": [[now_str]]})
        if col_qty:
            cell_updates.append({"range": gspread.utils.rowcol_to_a1(row_idx, col_qty),
                                  "values": [[float(qty_approved)]]})

        ws.batch_update(cell_updates)
        return True, f"Stock updated: {current_stock} → {new_stock} (row {row_idx})"

    except gspread.exceptions.APIError as e:
        return False, f"Google Sheets API error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {type(e).__name__}: {e}"

def get_stock_for_tag(tag_code):
    """Return (stock_float_or_None, error_message)."""
    ws, err = get_inventory_sheet()
    if ws is None:
        return None, err
    try:
        headers  = ws.row_values(1)
        col_tag  = headers.index("RFID Tag Code") + 1 if "RFID Tag Code" in headers else None
        col_stk  = headers.index("Total Stock")    + 1 if "Total Stock"    in headers else None
        if col_tag is None or col_stk is None:
            return None, "RFID Tag Code or Total Stock column not found"
        tag_vals = ws.col_values(col_tag)
        if tag_code not in tag_vals:
            return None, f"Tag {tag_code} not in sheet"
        row_idx  = tag_vals.index(tag_code) + 1
        val      = ws.cell(row_idx, col_stk).value
        if not val:
            return None, "Empty stock cell"
        return float(str(val).replace(",","").strip()), None
    except Exception as e:
        return None, str(e)


def send_email(subject, body_html, to_email):
    cfg = load_smtp_config()
    if not cfg.get("smtp_user") or not cfg.get("smtp_password"):
        return False, "SMTP not configured"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg["smtp_user"]
        msg["To"]      = to_email
        msg.attach(MIMEText(body_html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as s:
            s.starttls()
            s.login(cfg["smtp_user"], cfg["smtp_password"])
            s.sendmail(cfg["smtp_user"], to_email, msg.as_string())
        return True, "OK"
    except Exception as e:
        return False, str(e)

def is_allowed_email(email):
    cfg = load_smtp_config()
    domain = cfg.get("allowed_domain", DEFAULT_ALLOWED_DOMAIN).strip().lower()
    return email.strip().lower().endswith(domain)

def checkout_email_body(tag_code, rec, req):
    mat  = rec.get("Material Description", rec.get("Material","—"))
    return f"""
    <div style="font-family:sans-serif;max-width:600px;">
      <h2 style="color:#1a3a6b;">📦 Material Checkout Request</h2>
      <table style="width:100%;border-collapse:collapse;">
        <tr><td style="padding:6px;color:#666;width:35%;">RFID Tag</td>
            <td style="padding:6px;font-family:monospace;">{tag_code}</td></tr>
        <tr style="background:#f5f7fa;"><td style="padding:6px;color:#666;">Material</td>
            <td style="padding:6px;font-weight:bold;">{mat}</td></tr>
        <tr><td style="padding:6px;color:#666;">Storage Bin</td>
            <td style="padding:6px;">{rec.get("Storage Bin","—")}</td></tr>
        <tr style="background:#f5f7fa;"><td style="padding:6px;color:#666;">Quantity Requested</td>
            <td style="padding:6px;font-weight:bold;color:#1a3a6b;">{req.get("req_quantity","?")} {req.get("req_uom","")}</td></tr>
        <tr><td style="padding:6px;color:#666;">Requested by</td>
            <td style="padding:6px;">{req.get("req_name","—")}</td></tr>
        <tr><td style="padding:6px;color:#666;">Email</td>
            <td style="padding:6px;">{req.get("req_email","—")}</td></tr>
        <tr style="background:#f5f7fa;"><td style="padding:6px;color:#666;">Purpose / Location</td>
            <td style="padding:6px;">{req.get("req_purpose","—")}</td></tr>
        <tr><td style="padding:6px;color:#666;">From date</td>
            <td style="padding:6px;">{req.get("req_date_from","—")}</td></tr>
        <tr style="background:#f5f7fa;"><td style="padding:6px;color:#666;">To date</td>
            <td style="padding:6px;">{req.get("req_date_to","—")}</td></tr>
        <tr><td style="padding:6px;color:#666;">Requested at</td>
            <td style="padding:6px;">{req.get("req_timestamp","—")}</td></tr>
      </table>
      <br>
      <p style="color:#666;">Scan the QR code or open the link below to approve:</p>
      <a href="{req.get("req_url","")}" style="background:#1a3a6b;color:#fff;
         padding:10px 20px;border-radius:6px;text-decoration:none;">
         View & Approve Request
      </a>
    </div>
    """

def approve_email_body(tag_code, rec, req):
    mat = rec.get("Material Description", rec.get("Material","—"))
    return f"""
    <div style="font-family:sans-serif;max-width:600px;">
      <h2 style="color:#0a7a45;">✅ Checkout Approved</h2>
      <p>Your request to check out <b>{mat}</b> has been approved.</p>
      <table style="width:100%;border-collapse:collapse;">
        <tr><td style="padding:6px;color:#666;width:35%;">Material</td>
            <td style="padding:6px;font-weight:bold;">{mat}</td></tr>
        <tr style="background:#f5f7fa;"><td style="padding:6px;color:#666;">Storage Bin</td>
            <td style="padding:6px;">{rec.get("Storage Bin","—")}</td></tr>
        <tr><td style="padding:6px;color:#666;">Approved for</td>
            <td style="padding:6px;">{req.get("req_date_from","—")} → {req.get("req_date_to","—")}</td></tr>
        <tr style="background:#f5f7fa;"><td style="padding:6px;color:#666;">Location / Purpose</td>
            <td style="padding:6px;">{req.get("req_purpose","—")}</td></tr>
        <tr><td style="padding:6px;color:#666;">Approved at</td>
            <td style="padding:6px;">{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</td></tr>
      </table>
      <p style="color:#666;margin-top:1rem;">Please collect the item from the warehouse.</p>
    </div>
    """

def get_password():
    return os.environ.get("RFID_PASSWORD",
           st.session_state.get("app_password", DEFAULT_PASSWORD))

def check_viewer_auth(tag_code):
    return st.session_state.get(f"auth_ok_{tag_code}", False)

def show_password_gate(tag_code):
    st.markdown("""
    <div style="background:#0f1a2e;border:1.5px solid #2a4a7a;border-radius:14px;
        padding:1.5rem 1.5rem 1.2rem;margin-top:0.5rem;">
      <div style="font-size:1rem;font-weight:700;color:#4f9cf9;margin-bottom:0.4rem;">
          🔒 Warehouse Authentication</div>
      <div style="font-size:0.88rem;color:#7a8299;margin-bottom:1rem;">
          Enter the warehouse password to edit or clear this tag.</div>
    </div>
    """, unsafe_allow_html=True)
    pw = st.text_input("Password", type="password", placeholder="Enter password…",

                       key=f"pw_input_{tag_code}", label_visibility="collapsed")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔓 Unlock", use_container_width=True, type="primary",
                     key=f"pw_submit_{tag_code}"):
            if pw == get_password():
                st.session_state[f"auth_ok_{tag_code}"] = True
                st.success("✅ Authenticated!")
                st.rerun()
            else:
                st.error("❌ Incorrect password.")
    with c2:
        if st.button("✕ Cancel", use_container_width=True, key=f"pw_cancel_{tag_code}"):
            st.session_state.pop(f"v_mode_{tag_code}", None)
            st.rerun()

# ─────────────────────────────────────────────
# SHARED EDIT / REGISTER FORM
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# CHECKOUT REQUEST PAGE  (?page=checkout)
# A single permanent URL/QR for all staff
# ─────────────────────────────────────────────
def show_checkout_page():
    data = load_data()
    cfg  = load_smtp_config()
    allowed_domain = cfg.get("allowed_domain", DEFAULT_ALLOWED_DOMAIN).strip().lower()

    st.markdown("""
    <style>
    #MainMenu, footer, header {visibility: hidden;}
    .block-container {padding-top: 0 !important; padding-bottom: 2rem; max-width: 620px;}
    .stButton > button {font-size: 1rem !important; padding: 0.6rem 1rem !important; border-radius: 10px !important;}
    </style>
    """, unsafe_allow_html=True)

    # Header
    st.markdown("""
    <div style="background:linear-gradient(135deg,#0a1628,#15103a);
        padding:1.2rem 1.5rem 1rem;margin-bottom:1.2rem;border-bottom:3px solid #4f9cf9;">
      <div style="font-family:monospace;color:#4f9cf9;font-size:0.78rem;
          letter-spacing:3px;margin-bottom:4px;">📦 RFID · QR SYSTEM</div>
      <div style="font-size:1.4rem;font-weight:800;color:#fff;">Material Checkout Request</div>
      <div style="font-size:0.8rem;color:#7a8299;margin-top:2px;">
          Fill in all fields to submit a checkout request</div>
    </div>
    """, unsafe_allow_html=True)

    step_key   = "co_step"     # "email" | "form" | "done"
    email_key  = "co_email"
    mat_key    = "co_mat"      # selected tag_code

    step = st.session_state.get(step_key, "email")

    # ══════════════════════════════════════════
    # STEP 1 — Email verification
    # ══════════════════════════════════════════
    if step == "email":
        st.markdown("### Step 1 — Verify your email")
        st.info(f"Only **{allowed_domain}** email addresses are allowed.")

        email_input = st.text_input(

            "Your email address",
            placeholder=f"yourname{allowed_domain}",
            key="co_email_input"
        )

        if st.button("Continue →", type="primary",
                     use_container_width=True, key="co_email_btn"):
            em = email_input.strip().lower()
            if not em:
                st.error("Please enter your email address.")
            elif not _re.match(r"[^@]+@[^@]+[.][^@]+", em):
                st.error("Please enter a valid email address.")
            elif not em.endswith(allowed_domain):
                st.error(f"Only {allowed_domain} email addresses are allowed.")
            else:
                st.session_state[email_key] = em
                st.session_state[step_key]  = "form"
                st.rerun()
        return

    # ══════════════════════════════════════════
    # STEP 2 — Fill checkout form
    # ══════════════════════════════════════════
    if step == "form":
        user_email = st.session_state.get(email_key, "")
        st.markdown(f"**Logged in as:** `{user_email}`")
        st.markdown("---")

        # ── Search for material to checkout ──
        st.markdown("### Step 2 — Select material to checkout")

        search_q = st.text_input(

            "Search material",
            placeholder="Type material ID, description, bin...",
            key="co_search"
        )

        active_tags = {
            k: v for k, v in data.items()
            if v.get("Material") and not v.get("_cleared")
            and not v.get("_checkout_request")   # not already pending
            and not v.get("_checked_out")         # not already checked out
        }

        if search_q.strip():
            q = search_q.strip().lower()
            filtered = {
                k: v for k, v in active_tags.items()
                if q in k.lower()
                or q in v.get("Material","").lower()
                or q in v.get("Material Description","").lower()
                or q in v.get("Storage Bin","").lower()
            }
        else:
            filtered = active_tags

        if not filtered:
            if data:
                st.warning("No available materials found (or all are pending/checked out).")
            else:
                st.warning("No materials registered in the system yet.")
            if st.button("← Back", key="co_back1"):
                st.session_state.pop(step_key, None)
                st.rerun()
            return

        st.caption(f"{len(filtered)} available materials")

        # Build options
        mat_options = ["— select material —"] + [
            f"{v.get('Storage Bin','?')}  |  {v.get('Material','')}  |  {v.get('Material Description','')[:40]}"
            for k, v in filtered.items()
        ]
        tag_keys = list(filtered.keys())

        chosen_opt = st.selectbox(

            "Material", options=mat_options,
            key="co_mat_sel", label_visibility="collapsed"
        )

        if chosen_opt != "— select material —":
            idx     = mat_options.index(chosen_opt) - 1
            sel_tag = tag_keys[idx]
            sel_rec = filtered[sel_tag]

            # Show material detail card
            st.markdown(f"""
            <div style="background:#f5f7fa;border:1px solid #c8d0e0;
                border-radius:10px;padding:1rem 1.2rem;margin:0.5rem 0;">
              <div style="font-size:0.7rem;color:#667799;text-transform:uppercase;
                  letter-spacing:1px;margin-bottom:4px;">Selected Material</div>
              <div style="font-size:1.1rem;font-weight:700;color:#111;">
                  {sel_rec.get("Material Description","—")}</div>
              <div style="font-family:monospace;font-size:0.82rem;color:#4f9cf9;">
                  {sel_rec.get("Material","—")}</div>
              <div style="font-size:0.85rem;color:#444;margin-top:4px;">
                  Bin: <b>{sel_rec.get("Storage Bin","—")}</b> &nbsp;|&nbsp;
                  Stock: <b>{sel_rec.get("Total Stock","—")} {sel_rec.get("Base Unit of Measure","")}</b>
              </div>
            </div>
            """, unsafe_allow_html=True)
            st.session_state[mat_key] = sel_tag
        else:
            st.session_state.pop(mat_key, None)

        # ── Checkout form fields ──────────────
        if st.session_state.get(mat_key):
            st.markdown("---")
            st.markdown("### Step 3 — Fill in checkout details")

            # Show live stock from Google Sheets
            sel_tag_now = st.session_state.get(mat_key)
            if sel_tag_now:
                live_stock, _gs_err = get_stock_for_tag(sel_tag_now)
                sel_rec_now = filtered.get(sel_tag_now, {})
                uom_now = sel_rec_now.get("Base Unit of Measure","")
                if live_stock is not None:
                    st.info(f"📦 Available stock (Google Sheets): **{live_stock:.2f} {uom_now}**")
                else:
                    fallback_stock = sel_rec_now.get("Total Stock","?")
                    st.info(f"📦 Available stock (local data): **{fallback_stock} {uom_now}**")

            name_input = st.text_input(

                "Your full name *", placeholder="First Last",
                key="co_name")

            cq1, cq2 = st.columns([3, 1])
            with cq1:
                qty_input = st.number_input(

                    "Quantity requested *",
                    min_value=0.01, step=1.0, format="%.2f",
                    key="co_qty")
            with cq2:
                uom_show = filtered.get(st.session_state.get(mat_key,""), {}).get("Base Unit of Measure","")
                st.text_input("Unit", value=uom_show, disabled=True, key="co_uom_disp", label_visibility="visible")

            col1, col2 = st.columns(2)
            with col1:
                date_from = st.date_input("Date from *", key="co_date_from")
            with col2:
                date_to   = st.date_input("Date to *", key="co_date_to")
            purpose   = st.text_area(

                "Purpose / Location to use *",
                placeholder="e.g. Maintenance at Building A, Floor 3",
                key="co_purpose", height=80)

            st.markdown("---")
            c1, c2 = st.columns([2,1])
            with c1:
                submit = st.button("📤 Submit Request", type="primary",

                                   use_container_width=True, key="co_submit")
            with c2:
                if st.button("← Back", use_container_width=True, key="co_back2"):
                    st.session_state.pop(step_key, None)
                    st.rerun()

            if submit:
                # Validate
                errors = []
                if not name_input.strip():
                    errors.append("Full name is required.")
                if date_to < date_from:
                    errors.append("'Date to' must be after 'Date from'.")
                if not purpose.strip():
                    errors.append("Purpose / Location is required.")
                qty_val = float(st.session_state.get("co_qty", 0) or 0)
                if qty_val <= 0:
                    errors.append("Quantity must be greater than 0.")
                else:
                    # Check stock availability
                    live_chk, _ = get_stock_for_tag(st.session_state.get(mat_key,""))
                    if live_chk is not None and qty_val > live_chk:
                        errors.append(f"Requested quantity ({qty_val:.2f}) exceeds available stock ({live_chk:.2f}).")

                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    sel_tag = st.session_state[mat_key]
                    sel_rec = data[sel_tag]
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    req_url = f"{get_base_url()}?tag={sel_tag}"

                    req_data = {
                        "req_name":      name_input.strip(),
                        "req_email":     user_email,
                        "req_purpose":   purpose.strip(),
                        "req_date_from": str(date_from),
                        "req_date_to":   str(date_to),
                        "req_timestamp": now_str,
                        "req_url":       req_url,
                        "req_quantity":  qty_val,
                        "req_uom":       sel_rec.get("Base Unit of Measure",""),
                    }

                    # Save pending request to tag record
                    data[sel_tag]["_checkout_request"] = req_data
                    save_data(data)

                    # Send email to admin
                    admin_email = cfg.get("admin_email", DEFAULT_ADMIN_EMAIL)
                    subj = f"[Checkout Request] {sel_rec.get('Material Description','')[:40]} — {name_input.strip()}"
                    body = checkout_email_body(sel_tag, sel_rec, req_data)
                    ok, msg = send_email(subj, body, admin_email)

                    # Also notify requester
                    send_email(
                        f"[Checkout Request Submitted] {sel_rec.get('Material Description','')[:40]}",
                        f"<p>Your checkout request has been submitted and is pending approval.</p>{body}",
                        user_email
                    )

                    # Clear form state
                    for k in ["co_step","co_email","co_mat","co_search",
                              "co_mat_sel","co_name","co_purpose","co_email_input","co_qty","co_uom_disp"]:
                        st.session_state.pop(k, None)
                    st.session_state["co_step"] = "done"
                    st.session_state["co_done_mat"] = sel_rec.get("Material Description","—")
                    st.session_state["co_done_email"] = ok
                    st.rerun()
        return

    # ══════════════════════════════════════════
    # DONE
    # ══════════════════════════════════════════
    if step == "done":
        mat_name  = st.session_state.get("co_done_mat","")
        email_ok  = st.session_state.get("co_done_email", False)
        st.markdown("""
        <div style="background:#0f2e1a;border:2px solid #34d399;border-radius:14px;
            padding:2rem;text-align:center;margin-top:1rem;">
          <div style="font-size:3rem;margin-bottom:0.5rem;">✅</div>
          <div style="font-size:1.3rem;font-weight:700;color:#34d399;">
              Request Submitted!</div>
          <div style="color:#e8ecf4;margin-top:0.5rem;font-size:0.95rem;">
              Your checkout request has been sent to the warehouse admin for approval.
          </div>
        </div>
        """, unsafe_allow_html=True)
        if email_ok:
            st.success("📧 Confirmation email sent to you and the admin.")
        else:
            st.warning("⚠️ Email notification could not be sent — please inform the admin directly.")

        st.markdown(f"**Material requested:** {mat_name}")
        st.markdown("Scan the QR again to submit another request.")

        if st.button("Submit another request", use_container_width=True, key="co_again"):
            for k in list(st.session_state.keys()):
                if k.startswith("co_"):
                    st.session_state.pop(k, None)
            st.rerun()


def _show_edit_form(tag_code, rec, data, is_empty=False):
    """
    Step-by-step edit/register form:
    Step 1 — Choose mode: Master DB or Manual
    Step 2 (Master) — Search & select material → load form
    Step 3 — Edit fields → Save
    """
    title = "Register Material" if is_empty else "Edit Material"
    st.markdown(f"""
    <div class="edit-header">
      <div class="edit-header-title">✎  {title} — {tag_code}</div>
      <div class="edit-header-sub">Follow the steps below to fill in material information.</div>
    </div>
    """, unsafe_allow_html=True)

    # ── Session state keys ────────────────────────────────────
    mode_key   = f"ef_mode_{tag_code}"      # "master" or "manual"
    loaded_key = f"ef_loaded_{tag_code}"    # dict of loaded field values (after selecting)
    form_key   = f"ef_form_{tag_code}"      # True when form should show

    master_df, _mdf_err = get_master_df()

    # ══════════════════════════════════════════════════════════
    # STEP 1 — Choose mode
    # ══════════════════════════════════════════════════════════
    if mode_key not in st.session_state:
        st.markdown("### Step 1 — How would you like to fill in the information?")
        st.markdown(" ")
        c1, c2 = st.columns(2)
        with c1:
            master_disabled = master_df is None
            hint = "" if not master_disabled else " *(no master list uploaded)*"
            if st.button(
                f"📋 From Master Database{hint}",
                use_container_width=True,
                type="primary",
                disabled=master_disabled,
                key=f"ef_btn_master_{tag_code}"
            ):
                st.session_state[mode_key] = "master"
                st.rerun()
        with c2:
            if st.button(
                "✏️ Fill Manually",
                use_container_width=True,
                key=f"ef_btn_manual_{tag_code}"
            ):
                # Load existing rec values for manual edit
                st.session_state[mode_key]   = "manual"
                st.session_state[loaded_key] = {f: rec.get(f,"") for f in EXPECTED_COLS}
                st.session_state[form_key]   = True
                st.rerun()

        if master_disabled:
            st.info("💡 Upload a Master Material CSV in **Setup** tab to enable database selection.")
        return

    mode = st.session_state[mode_key]

    # ══════════════════════════════════════════════════════════
    # STEP 2 (Master mode) — Search & select material
    # ══════════════════════════════════════════════════════════
    if mode == "master" and not st.session_state.get(form_key):
        st.markdown("### Step 2 — Search & select material from Master Database")
        st.caption(f"{len(master_df)} materials in master list")

        # Search box
        search_q = st.text_input(

            "Search material",
            placeholder="Type material ID, description, plant...",
            key=f"ef_search_{tag_code}"
        )

        # Filter master_df
        if search_q.strip():
            q = search_q.strip().lower()
            mask = master_df.apply(
                lambda row: any(q in str(v).lower() for v in row.values), axis=1
            )
            filtered = master_df[mask]
        else:
            filtered = master_df

        st.caption(f"{len(filtered)} results")

        if len(filtered) == 0:
            st.warning("No materials match your search.")
        else:
            # Build display list
            mat_col   = next((c for c in filtered.columns if "Material Description" in c), None)
            matid_col = next((c for c in filtered.columns if c.strip() == "Material"), None)

            if matid_col and mat_col:
                options = ["— select material —"] + [
                    f"{row[matid_col]}  |  {row[mat_col]}"
                    for _, row in filtered.iterrows()
                ]
            elif matid_col:
                options = ["— select material —"] + filtered[matid_col].tolist()
            else:
                options = ["— select material —"] + [f"Row {i+1}" for i in range(len(filtered))]

            chosen = st.selectbox(

                "Select material",
                options=options,
                key=f"ef_chosen_{tag_code}",
                label_visibility="collapsed"
            )

            st.markdown(" ")
            c_load, c_back = st.columns([2, 1])
            with c_load:
                if st.button("✅ Load this material into form",
                             use_container_width=True, type="primary",
                             key=f"ef_load_{tag_code}",
                             disabled=(chosen == "— select material —")):
                    # Find actual row and load values
                    idx = options.index(chosen) - 1
                    row = filtered.iloc[idx]
                    loaded = {f: str(row.get(f,"")).strip()
                              for f in EXPECTED_COLS if f in filtered.columns}
                    # Keep RFID Tag Code from tag_code
                    loaded["RFID Tag Code"] = tag_code
                    st.session_state[loaded_key] = loaded
                    st.session_state[form_key]   = True
                    st.rerun()
            with c_back:
                if st.button("← Back", use_container_width=True,
                             key=f"ef_back_step2_{tag_code}"):
                    st.session_state.pop(mode_key, None)
                    st.rerun()
        return

    # ══════════════════════════════════════════════════════════
    # STEP 3 — Edit form + Save
    # ══════════════════════════════════════════════════════════
    loaded = st.session_state.get(loaded_key, {f: rec.get(f,"") for f in EXPECTED_COLS})

    if mode == "master":
        mat_name = loaded.get("Material Description", loaded.get("Material",""))
        st.markdown(f"### Step 3 — Review & Edit")
        st.success(f"📋 Loaded from Master: **{mat_name}**")
    else:
        st.markdown("### Fill in material information")

    st.markdown(f"**RFID Tag Code (fixed):** `{tag_code}`")
    st.markdown("---")

    # ── Widget keys (use wk_ prefix, populated from loaded) ──
    # On first render of step 3, clear widget keys so loaded values take effect
    first_render_key = f"ef_first_{tag_code}"
    if first_render_key not in st.session_state:
        for field in EXPECTED_COLS:
            if field == "RFID Tag Code": continue
            for sfx in ["_sel","_custom","_txt"]:
                st.session_state.pop(f"wk_{tag_code}_{field}{sfx}", None)
        st.session_state[first_render_key] = True

    col_a, col_b = st.columns(2)
    for j, field in enumerate(EXPECTED_COLS):
        if field == "RFID Tag Code": continue
        wk = f"wk_{tag_code}_{field}"
        val = loaded.get(field, "")
        target = col_a if j % 2 == 0 else col_b
        with target:
            if field in DROPDOWN_FIELDS:
                options = get_field_options(data, field)
                if val and val not in options:
                    options = sorted(options + [val])
                CUSTOM = "— type custom value —"
                choices = options + [CUSTOM]
                idx = options.index(val) if val in options else len(choices)-1
                sel = st.selectbox(field, choices, index=idx, key=f"{wk}_sel")
                if sel == CUSTOM:
                    st.text_input(f"Custom {field}",

                        value=val, key=f"{wk}_custom",
                        placeholder=f"Enter {field}...")

            else:
                st.text_input(field, value=val, key=f"{wk}_txt")

    st.markdown("---")
    st.markdown(
        "<div style='font-size:0.9rem;color:#7a8299;margin-bottom:6px;'>"
        "Type <b style='color:#4f9cf9;font-size:1rem;'>SAVE</b> to confirm.</div>",
        unsafe_allow_html=True)
    confirm_txt = st.text_input("Confirm",

        placeholder="Type SAVE here…",
        key=f"ef_confirm_{tag_code}", label_visibility="collapsed")

    btn_c1, btn_c2, btn_c3 = st.columns([2, 2, 1])
    with btn_c1:
        save_clicked = st.button("💾 Save Material", type="primary",

            use_container_width=True, key=f"ef_save_{tag_code}")
    with btn_c2:
        # Allow going back to re-search (master mode only)
        if mode == "master":
            if st.button("🔍 Change material", use_container_width=True,
                         key=f"ef_change_{tag_code}"):
                st.session_state.pop(loaded_key, None)
                st.session_state.pop(form_key, None)
                st.session_state.pop(first_render_key, None)
                st.rerun()
    with btn_c3:
        cancel_clicked = st.button("✕ Cancel", use_container_width=True,

            key=f"ef_cancel_{tag_code}")

    def _cleanup():
        for field in EXPECTED_COLS:
            if field == "RFID Tag Code": continue
            wk = f"wk_{tag_code}_{field}"
            for sfx in ["_sel","_custom","_txt"]:
                st.session_state.pop(f"{wk}{sfx}", None)
        for k in [mode_key, loaded_key, form_key, first_render_key,
                  f"ef_confirm_{tag_code}", f"ef_search_{tag_code}",
                  f"ef_chosen_{tag_code}", f"v_mode_{tag_code}",
                  f"auth_ok_{tag_code}", f"v_register_{tag_code}"]:
            st.session_state.pop(k, None)

    if save_clicked:
        if confirm_txt.strip().upper() != "SAVE":
            st.error("⚠️ Type SAVE in the confirmation box to proceed.")
        else:
            new_vals = {"RFID Tag Code": tag_code}
            for field in EXPECTED_COLS:
                if field == "RFID Tag Code": continue
                wk = f"wk_{tag_code}_{field}"
                if field in DROPDOWN_FIELDS:
                    sv = st.session_state.get(f"{wk}_sel", "")
                    new_vals[field] = (st.session_state.get(f"{wk}_custom","")
                                      if sv == "— type custom value —" else sv)
                else:
                    new_vals[field] = st.session_state.get(f"{wk}_txt", "")
            new_vals["_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data[tag_code] = new_vals
            save_data(data)
            _cleanup()

            # Sync to Google Sheets
            gs_ok2, gs_msg2 = register_new_item_in_sheet(new_vals)
            if gs_ok2:
                st.success(f"✅ Tag {tag_code} saved! Google Sheets updated: {gs_msg2}")
            else:
                st.success(f"✅ Tag {tag_code} saved locally.")
                st.warning(f"⚠️ Google Sheets sync failed: {gs_msg2}")
            st.rerun()

    if cancel_clicked:
        _cleanup()
        st.rerun()


def show_viewer(tag_code):
    data = load_data()

    st.markdown("""
    <style>
    #MainMenu, footer, header {visibility: hidden;}
    .block-container {padding-top: 0 !important; padding-bottom: 2rem; max-width: 680px;}
    .stButton > button {font-size: 1.05rem !important; padding: 0.65rem 1rem !important; border-radius: 10px !important;}
    div[data-testid="stForm"] {border: none; padding: 0;}
    .mat-id-box { background: linear-gradient(135deg,#0f2a52,#1a1040); border: 1px solid #3a5a9a; border-radius: 14px; padding: 1.2rem 1.4rem; margin-bottom: 1rem; }
    .mat-id-label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 2px; color: #7a8299; margin-bottom: 6px; }
    .mat-id-value { font-family: monospace; font-size: 2.1rem; font-weight: 800; color: #4f9cf9; letter-spacing: 2px; line-height: 1.1; word-break: break-all; }
    .mat-name-value { font-size: 1.2rem; font-weight: 600; color: #111111; margin-top: 8px; line-height: 1.4; }
    .rfid-tag-badge { display: inline-block; margin-top: 10px; background: rgba(79,156,249,0.15); border: 1px solid #3a5a9a; border-radius: 6px; padding: 4px 10px; font-family: monospace; font-size: 0.72rem; color: #7ab8f5; letter-spacing: 1px; word-break: break-all; }
    .detail-card { background: #f5f7fa; border: 1px solid #c8d0e0; border-radius: 14px; overflow: hidden; margin-bottom: 1rem; }
    .detail-card-header { background: #dde3ee; padding: 0.6rem 1.1rem; border-bottom: 1px solid #c8d0e0; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 2px; color: #334466; font-weight: 600; }
    .detail-row { display: flex; align-items: center; padding: 0.75rem 1.1rem; border-bottom: 1px solid #dde3ee; gap: 0.8rem; }
    .detail-row:last-child { border-bottom: none; }
    .detail-icon { font-size: 1.25rem; width: 30px; text-align: center; flex-shrink: 0; }
    .detail-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: #667799; margin-bottom: 3px; }
    .detail-value { font-size: 1.05rem; font-weight: 600; color: #111111; word-break: break-word; }
    .detail-value-hi { font-size: 1.15rem; font-weight: 700; color: #0a7a45; word-break: break-word; }
    .confirm-box { background: #2a1010; border: 2px solid #f87171; border-radius: 12px; padding: 1.2rem 1.4rem; margin-top: 0.8rem; }
    .confirm-title { font-size: 1.1rem; font-weight: 700; color: #f87171; margin-bottom: 0.5rem; }
    .confirm-body { color: #e8ecf4; font-size: 1rem; line-height: 1.7; }
    .empty-box { background: #1a1d27; border: 2px solid #fbbf24; border-radius: 12px; padding: 2rem; text-align: center; margin-bottom: 1.5rem; }
    .empty-title { font-size: 1.3rem; font-weight: 700; color: #fbbf24; }
    .empty-sub { color: #7a8299; margin-top: 0.5rem; font-size: 0.95rem; }
    .edit-header { background: #0f2240; border: 1px solid #2a4a7a; border-radius: 10px; padding: 0.9rem 1.2rem; margin-bottom: 1rem; }
    .edit-header-title { font-size: 1rem; font-weight: 700; color: #4f9cf9; }
    .edit-header-sub { font-size: 0.82rem; color: #7a8299; margin-top: 4px; line-height: 1.6; }
    </style>
    """, unsafe_allow_html=True)

    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#0a1628,#15103a);
        padding:1.3rem 1.5rem 1.1rem;margin-bottom:1rem;border-bottom:3px solid #4f9cf9;">
      <div style="font-family:monospace;color:#4f9cf9;font-size:0.78rem;letter-spacing:3px;margin-bottom:5px;">📦 RFID · QR SYSTEM</div>
      <div style="font-size:0.8rem;color:#7a8299;letter-spacing:1px;text-transform:uppercase;">RFID Tag</div>
      <div style="font-family:monospace;font-size:1.1rem;font-weight:700;color:#ffffff;letter-spacing:1px;margin-top:4px;word-break:break-all;">{tag_code}</div>
    </div>
    """, unsafe_allow_html=True)

    if not data:
        st.warning("⚠️ No materials registered yet.")
        st.info("Ask your administrator to import data in the Register tab.")
        return

    if tag_code not in data:
        st.error(f"❌ Tag **{tag_code}** not registered.")
        st.info("Contact your warehouse administrator.")
        return

    rec = data[tag_code]

    # ── Empty / cleared ───────────────────────────────────────
    if rec.get("_cleared"):
        st.markdown(f"""
        <div class="empty-box">
          <div style="font-size:3rem;margin-bottom:0.5rem;">📭</div>
          <div class="empty-title">Tag is Empty</div>
          <div class="empty-sub">No material registered<br>Cleared: {rec.get("_cleared_at","unknown")}</div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("### Register new material to this tag")
        if not st.session_state.get(f"v_register_{tag_code}", False):
            if st.button("✎  Register Material", use_container_width=True,
                         type="primary", key=f"v_reg_btn_{tag_code}"):
                st.session_state[f"v_register_{tag_code}"] = True
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.rerun()
        else:
            if not check_viewer_auth(tag_code):
                show_password_gate(tag_code)
            else:
                _show_edit_form(tag_code, rec, data, is_empty=True)
        return

    # ── Material hero ─────────────────────────────────────────
    st.markdown(f"""
    <div class="mat-id-box">
      <div class="mat-id-label">Material ID</div>
      <div class="mat-id-value">{rec.get("Material","—")}</div>
      <div class="mat-name-value">{rec.get("Material Description","—")}</div>
      <div class="rfid-tag-badge">🏷 RFID: {tag_code}</div>
    </div>
    """, unsafe_allow_html=True)

    # ── Detail rows ───────────────────────────────────────────
    st.markdown('<div class="detail-card"><div class="detail-card-header">Material Details</div>', unsafe_allow_html=True)
    stock_val = f"{rec.get('Total Stock','')} {rec.get('Base Unit of Measure','')}".strip()
    rows = [
        ("📦","Storage Bin",     rec.get("Storage Bin",""),     False),
        ("🏭","Plant",           rec.get("Plant",""),           False),
        ("📍","Storage Location",rec.get("Storage Location",""),False),
        ("🗂", "Storage Type",    rec.get("Storage Type",""),    False),
        ("📂","Storage Section", rec.get("Storage Section",""), False),
        ("🏷", "Batch",           rec.get("Batch",""),           False),
        ("📋","Stock Category",  rec.get("Stock Category",""),  False),
        ("📊","Total Stock",     stock_val,                     True),
        ("📅","SLED / BBD",      rec.get("SLED/BBD",""),        False),
        ("📅","GR Date",         rec.get("GR Date",""),         False),
    ]
    for icon, label, value, hi in rows:
        css = "detail-value-hi" if hi else "detail-value"
        st.markdown(f"""<div class="detail-row">
          <div class="detail-icon">{icon}</div>
          <div style="flex:1;min-width:0;">
            <div class="detail-label">{label}</div>
            <div class="{css}">{value or "—"}</div>
          </div>
        </div>""", unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
    st.caption(f"🕐 Last updated: {rec.get('_updated_at','unknown')}  ·  Scan again to refresh")

    # ── Warehouse actions ─────────────────────────────────────
    # ── Checkout request pending banner ──────────────────────
    req = rec.get("_checkout_request")
    checked_out = rec.get("_checked_out")

    if checked_out:
        st.markdown(f"""
        <div style="background:#0f2e1a;border:2px solid #34d399;border-radius:12px;
            padding:1.2rem 1.4rem;margin-bottom:1rem;">
          <div style="font-size:1rem;font-weight:700;color:#34d399;margin-bottom:6px;">
              📤 CHECKED OUT</div>
          <div style="color:#e8ecf4;font-size:0.95rem;line-height:1.8;">
            <b>By:</b> {checked_out.get("req_name","—")} ({checked_out.get("req_email","—")})<br>
            <b>Purpose:</b> {checked_out.get("req_purpose","—")}<br>
            <b>Period:</b> {checked_out.get("req_date_from","—")} → {checked_out.get("req_date_to","—")}<br>
            <b>Quantity:</b> {checked_out.get("approved_qty", checked_out.get("req_quantity","?"))} {checked_out.get("req_uom","")}<br>
            <b>Approved at:</b> {checked_out.get("approved_at","—")}
          </div>
        </div>
        """, unsafe_allow_html=True)

    elif req:
        st.markdown(f"""
        <div style="background:#2a1f0a;border:2px solid #fbbf24;border-radius:12px;
            padding:1.2rem 1.4rem;margin-bottom:1rem;">
          <div style="font-size:1rem;font-weight:700;color:#fbbf24;margin-bottom:6px;">
              ⏳ PENDING CHECKOUT REQUEST</div>
          <div style="color:#e8ecf4;font-size:0.95rem;line-height:1.8;">
            <b>Requested by:</b> {req.get("req_name","—")} ({req.get("req_email","—")})<br>
            <b>Purpose:</b> {req.get("req_purpose","—")}<br>
            <b>Period:</b> {req.get("req_date_from","—")} → {req.get("req_date_to","—")}<br>
            <b>Quantity:</b> {req.get("req_quantity","?")} {req.get("req_uom","")}<br>
            <b>Submitted:</b> {req.get("req_timestamp","—")}
          </div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("#### Warehouse Actions")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✎  Edit Material", use_container_width=True, type="primary",
                     key=f"v_edit_{tag_code}"):
            st.session_state[f"v_mode_{tag_code}"] = "edit"
            st.session_state.pop(f"auth_ok_{tag_code}", None)
            st.rerun()
    with c2:
        if st.button("🗑  Clear Tag", use_container_width=True,
                     key=f"v_clear_btn_{tag_code}"):
            st.session_state[f"v_mode_{tag_code}"] = "confirm_clear"
            st.session_state.pop(f"auth_ok_{tag_code}", None)
            st.rerun()

    # ── Approve / Return actions (password protected) ─────────
    if req or checked_out:
        st.markdown("#### Checkout Actions *(warehouse staff only)*")
        if req and not checked_out:
            if st.button("✅  Approve Checkout", use_container_width=True,
                         key=f"v_approve_{tag_code}", type="primary"):
                st.session_state[f"v_mode_{tag_code}"] = "confirm_approve"
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.rerun()
        if checked_out:
            if st.button("🔄  Mark as Returned", use_container_width=True,
                         key=f"v_return_{tag_code}"):
                st.session_state[f"v_mode_{tag_code}"] = "confirm_return"
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.rerun()

    mode = st.session_state.get(f"v_mode_{tag_code}", "")

    if mode in ("edit", "confirm_clear", "confirm_approve", "confirm_return", "ask_clear_after_approve"):
        if not check_viewer_auth(tag_code):
            st.markdown("---")
            show_password_gate(tag_code)
            return

    if mode == "confirm_clear":
        st.markdown(f"""
        <div class="confirm-box">
          <div class="confirm-title">⚠️  Confirm Clear Tag</div>
          <div class="confirm-body">
            Remove all data from RFID tag <strong>{tag_code}</strong><br><br>
            QR code and URL will remain unchanged.<br><strong>Are you sure?</strong>
          </div>
        </div>""", unsafe_allow_html=True)
        st.markdown(" ")
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("✅  Yes, Clear Tag", use_container_width=True, type="primary",
                         key=f"v_confirm_clear_{tag_code}"):
                data[tag_code] = {"RFID Tag Code": tag_code, "_cleared": True,
                    "_cleared_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                save_data(data)
                st.session_state.pop(f"v_mode_{tag_code}", None)
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.success("✅ Tag cleared.")
                st.rerun()
        with cc2:
            if st.button("✕  Cancel", use_container_width=True,
                         key=f"v_cancel_clear_{tag_code}"):
                st.session_state.pop(f"v_mode_{tag_code}", None)
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.rerun()

    # ── Confirm Approve ───────────────────────────────────────
    if mode == "confirm_approve":
        req = rec.get("_checkout_request", {})
        req_qty  = req.get("req_quantity", 1)
        req_uom  = req.get("req_uom", rec.get("Base Unit of Measure",""))

        # Show live stock from Google Sheets
        live_stk, _gs_stk_err = get_stock_for_tag(tag_code)
        stock_display = f"{live_stk:.2f}" if live_stk is not None else rec.get("Total Stock","?")

        st.markdown(f"""
        <div style="background:#0f2a1a;border:2px solid #34d399;border-radius:12px;
            padding:1.2rem 1.4rem;margin-top:0.8rem;">
          <div style="font-size:1.1rem;font-weight:700;color:#34d399;margin-bottom:0.5rem;">
              ✅ Approve Checkout Request</div>
          <div style="color:#e8ecf4;font-size:0.95rem;line-height:1.9;">
            <b>Requested by:</b> {req.get("req_name","—")} ({req.get("req_email","—")})<br>
            <b>Purpose:</b> {req.get("req_purpose","—")}<br>
            <b>Period:</b> {req.get("req_date_from","—")} → {req.get("req_date_to","—")}<br>
            <b>Quantity requested:</b> {req_qty} {req_uom}<br>
            <b>Current stock:</b> {stock_display} {req_uom}
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("**Confirm quantity to approve:**")
        approve_qty = st.number_input(

            "Quantity to approve",
            min_value=0.01,
            max_value=float(live_stk) if live_stk is not None else float(req_qty) * 10,
            value=float(req_qty),
            step=1.0, format="%.2f",
            key=f"v_approve_qty_{tag_code}"
        )

        ap1, ap2 = st.columns(2)
        with ap1:
            if st.button("✅ Approve", use_container_width=True,
                         type="primary", key=f"v_do_approve_{tag_code}"):
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                req["approved_at"]  = now_str
                req["approved_qty"] = approve_qty
                req["approved_uom"] = req_uom

                # 1. Update Google Sheets stock
                gs_ok, gs_msg = update_stock_after_checkout(tag_code, approve_qty, req)

                # 2. Calculate new stock after deduction
                new_stock_after = None
                if live_stk is not None:
                    new_stock_after = max(0.0, live_stk - float(approve_qty))

                # 3. Save checkout history
                history = data[tag_code].get("_checkout_history", [])
                history.append({**req, "returned_at": None})

                if new_stock_after is not None and new_stock_after <= 0:
                    # Stock reached 0 — ask whether to clear QR
                    st.session_state[f"v_approve_done_{tag_code}"] = {
                        "req": req, "history": history, "gs_ok": gs_ok,
                        "gs_msg": gs_msg, "now_str": now_str,
                        "new_stock": new_stock_after,
                    }
                    st.session_state[f"v_mode_{tag_code}"] = "ask_clear_after_approve"
                    st.rerun()
                else:
                    # Stock still > 0 — update tag data, do NOT clear
                    data[tag_code].pop("_checkout_request", None)
                    data[tag_code]["_checkout_history"] = history
                    if new_stock_after is not None:
                        data[tag_code]["Total Stock"] = str(new_stock_after)
                    save_data(data)

                    # Email requester
                    send_email(
                        f"[Approved] Checkout: {rec.get('Material Description','')} ({approve_qty} {req_uom})",
                        approve_email_body(tag_code, rec, req),
                        req.get("req_email","")
                    )
                    st.session_state.pop(f"v_mode_{tag_code}", None)
                    st.session_state.pop(f"auth_ok_{tag_code}", None)
                    if gs_ok:
                        remaining = f"{new_stock_after:.2f}" if new_stock_after is not None else "?"
                        st.success(f"✅ Approved! Stock: -{approve_qty:.2f} → remaining {remaining} {req_uom}")
                    else:
                        st.error(f"⚠️ Approved & email sent, BUT Google Sheets update FAILED: {gs_msg}")
                    st.rerun()

        with ap2:
            if st.button("✕ Cancel", use_container_width=True,
                         key=f"v_cancel_approve_{tag_code}"):
                st.session_state.pop(f"v_mode_{tag_code}", None)
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.rerun()

    # ── Ask clear after approve (stock = 0) ───────────────────
    if mode == "ask_clear_after_approve":
        done = st.session_state.get(f"v_approve_done_{tag_code}", {})
        req_d    = done.get("req", {})
        history  = done.get("history", [])
        gs_ok    = done.get("gs_ok", False)
        gs_msg   = done.get("gs_msg", "")
        now_str  = done.get("now_str", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        req_uom2 = req_d.get("req_uom", "")
        approve_qty2 = req_d.get("approved_qty", 0)

        if gs_ok:
            st.success(f"✅ Stock updated in Google Sheets: -{approve_qty2:.2f} {req_uom2}")
        else:
            st.error(f"⚠️ Google Sheets update FAILED: {gs_msg}")

        st.markdown(f"""
        <div style="background:#2a1a0a;border:2px solid #fbbf24;border-radius:12px;
            padding:1.2rem 1.4rem;margin-top:0.8rem;">
          <div style="font-size:1.1rem;font-weight:700;color:#fbbf24;margin-bottom:0.5rem;">
              ⚠️ Stock is now 0</div>
          <div style="color:#e8ecf4;font-size:0.95rem;line-height:1.8;">
            After approving <b>{approve_qty2} {req_uom2}</b>, the remaining stock is <b>0</b>.<br><br>
            Do you want to <b>clear this QR tag</b> (ready for new item registration)?<br>
            Or <b>keep the tag</b> with 0 stock?
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown(" ")
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("🗑 Yes, Clear QR Tag", use_container_width=True,
                         type="primary", key=f"v_clear_after_approve_{tag_code}"):
                data[tag_code] = {
                    "RFID Tag Code":     tag_code,
                    "_cleared":          True,
                    "_cleared_at":       now_str,
                    "_checkout_history": history,
                }
                save_data(data)
                # Clear RFID Tag Code from Google Sheets old row
                gs_clr_ok, gs_clr_msg = clear_rfid_in_sheet(tag_code)
                send_email(
                    f"[Approved] Checkout: {rec.get('Material Description','')} ({approve_qty2} {req_uom2})",
                    approve_email_body(tag_code, rec, req_d),
                    req_d.get("req_email","")
                )
                st.session_state.pop(f"v_mode_{tag_code}", None)
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.session_state.pop(f"v_approve_done_{tag_code}", None)
                if gs_clr_ok:
                    st.success(f"✅ Approved & QR cleared. Sheets updated: {gs_clr_msg}")
                else:
                    st.success("✅ Approved & QR tag cleared.")
                    st.warning(f"⚠️ Sheets clear failed: {gs_clr_msg}")
                st.rerun()
        with cc2:
            if st.button("📦 No, Keep Tag (stock = 0)", use_container_width=True,
                         key=f"v_keep_after_approve_{tag_code}"):
                # Keep tag, update stock to 0
                data[tag_code].pop("_checkout_request", None)
                data[tag_code]["_checkout_history"] = history
                data[tag_code]["Total Stock"] = "0"
                save_data(data)
                send_email(
                    f"[Approved] Checkout: {rec.get('Material Description','')} ({approve_qty2} {req_uom2})",
                    approve_email_body(tag_code, rec, req_d),
                    req_d.get("req_email","")
                )
                st.session_state.pop(f"v_mode_{tag_code}", None)
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.session_state.pop(f"v_approve_done_{tag_code}", None)
                st.success("✅ Approved. Tag kept with stock = 0.")
                st.rerun()

    # ── Confirm Return ────────────────────────────────────────
    if mode == "confirm_return":
        checked_out = rec.get("_checked_out", {})
        st.markdown(f"""
        <div style="background:#1a1010;border:2px solid #a78bfa;border-radius:12px;
            padding:1.2rem 1.4rem;margin-top:0.8rem;">
          <div style="font-size:1.1rem;font-weight:700;color:#a78bfa;margin-bottom:0.5rem;">
              🔄 Confirm Item Returned</div>
          <div style="color:#e8ecf4;font-size:0.95rem;line-height:1.7;">
            Mark item as returned from <b>{checked_out.get("req_name","—")}</b><br>
            The tag will be cleared and ready for new registration.
          </div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown(" ")
        rt1, rt2 = st.columns(2)
        with rt1:
            if st.button("🔄 Yes, Mark Returned", use_container_width=True,
                         type="primary", key=f"v_do_return_{tag_code}"):
                # Archive checkout history then clear
                history = data[tag_code].get("_checkout_history", [])
                co = data[tag_code].pop("_checked_out", {})
                co["returned_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                history.append(co)
                data[tag_code]["_checkout_history"] = history
                data[tag_code].pop("_checkout_request", None)
                data[tag_code].pop("_cleared", None)
                save_data(data)
                st.session_state.pop(f"v_mode_{tag_code}", None)
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.success("✅ Item marked as returned. Tag is active again.")
                st.rerun()
        with rt2:
            if st.button("✕ Cancel", use_container_width=True,

                         key=f"v_cancel_return_{tag_code}"):
                st.session_state.pop(f"v_mode_{tag_code}", None)
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.rerun()

    if mode == "edit":
        st.markdown("---")
        _show_edit_form(tag_code, rec, data, is_empty=False)

# ─────────────────────────────────────────────
# ADMIN CSS
# ─────────────────────────────────────────────
ADMIN_CSS = """
<style>
#MainMenu, footer {visibility: hidden;}
.stTabs [data-baseweb="tab-list"] {gap: 8px;}
.stTabs [data-baseweb="tab"] {padding: 6px 20px; border-radius: 6px; font-size: 0.82rem;}
.stTabs [aria-selected="true"] {background: #1a3a6b !important; color: #4f9cf9 !important;}
div[data-testid="stMetricValue"] {font-size: 1.8rem !important;}
.qr-card { background: #1a1d27; border: 1px solid #2e3347; border-radius: 10px; padding: 1rem; text-align: center; margin-bottom: 0.5rem; }
.bin-badge { background: rgba(79,156,249,0.15); color: #4f9cf9; padding: 2px 10px; border-radius: 20px; font-family: monospace; font-size: 0.78rem; font-weight: 700; display: inline-block; margin-bottom: 4px; word-break: break-all; }
.status-ok   { color: #34d399; font-size: 0.75rem; }
.status-empty { color: #7a8299; font-size: 0.75rem; }
</style>
"""

# ─────────────────────────────────────────────
# SETUP TAB
# ─────────────────────────────────────────────
def tab_setup():
    st.subheader("App Configuration")

    # ── App URL ───────────────────────────────────────────────
    st.markdown("### App URL (for QR codes)")
    st.success(f"**Active URL:** {get_base_url()}")
    st.caption("QR codes link to this URL with ?tag=RFID_TAG_CODE appended.")

    with st.expander("Change URL (optional)"):
        st.warning("Only change if you move the app to a different address.")
        url_input = st.text_input("New App URL", value=get_base_url(),

            placeholder="https://your-app-name.streamlit.app", key="setup_url_input")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Save new URL", type="primary", use_container_width=True, key="setup_save_url"):
                st.session_state["base_url"] = url_input.strip().rstrip("/")
                st.success(f"URL updated.")
                st.rerun()
        with c2:
            if st.button("Reset to default", use_container_width=True, key="setup_reset_url"):
                st.session_state.pop("base_url", None)
                st.success(f"Reset to default.")
                st.rerun()

    st.markdown("---")

    st.markdown("---")

    # ── Password ──────────────────────────────────────────────
    st.markdown("### 🔒 Warehouse Password")
    current_pw = get_password()
    st.info(f"Current password: **{'*' * len(current_pw)}** ({len(current_pw)} chars)  ·  Default: `RFID123`")

    with st.expander("Change Password (optional)"):
        ca, cb = st.columns(2)
        with ca:
            new_pw1 = st.text_input("New Password", type="password", key="new_pw1",
                                     placeholder="Enter new password")
        with cb:
            new_pw2 = st.text_input("Confirm Password", type="password", key="new_pw2",
                                     placeholder="Repeat new password")
        pc1, pc2 = st.columns(2)
        with pc1:
            if st.button("💾 Save Password", type="primary", use_container_width=True, key="setup_save_pw"):
                if not new_pw1:
                    st.error("Password cannot be empty.")
                elif new_pw1 != new_pw2:
                    st.error("Passwords do not match.")
                elif len(new_pw1) < 4:
                    st.error("Minimum 4 characters.")
                else:
                    st.session_state["app_password"] = new_pw1
                    st.success("✅ Password updated.")
        with pc2:
            if st.button("↩ Reset to RFID123", use_container_width=True, key="setup_reset_pw"):
                st.session_state.pop("app_password", None)
                st.success("Reset to RFID123")
                st.rerun()

    st.markdown("---")

    # ── Checkout & Email Config ───────────────────────────────
    st.markdown("### 📤 Checkout & Email Settings")
    cfg = load_smtp_config()

    with st.expander("Configure checkout settings", expanded=False):
        st.markdown("**Allowed email domain** (only this domain can submit checkout requests)")
        domain_input = st.text_input("Allowed domain",

            value=cfg.get("allowed_domain", DEFAULT_ALLOWED_DOMAIN),
            placeholder="@company.com", key="cfg_domain")

        st.markdown("**Admin email** (receives checkout request notifications)")
        admin_input = st.text_input("Admin email",

            value=cfg.get("admin_email", DEFAULT_ADMIN_EMAIL),
            placeholder="admin@company.com", key="cfg_admin_email")

        st.markdown("**SMTP Settings** (for sending email notifications)")
        sc1, sc2 = st.columns(2)
        with sc1:
            smtp_host = st.text_input("SMTP Host",

                value=cfg.get("smtp_host","smtp.gmail.com"), key="cfg_smtp_host")
            smtp_user = st.text_input("SMTP Email (sender)",

                value=cfg.get("smtp_user",""), key="cfg_smtp_user")
        with sc2:
            smtp_port = st.text_input("SMTP Port",

                value=str(cfg.get("smtp_port",587)), key="cfg_smtp_port")
            smtp_pass = st.text_input("SMTP Password / App Password",

                value=cfg.get("smtp_password",""),
                type="password", key="cfg_smtp_pass")

        st.caption("For Gmail: use an App Password (not your account password). "
                   "Enable 2FA → Google Account → Security → App Passwords.")

        if st.button("💾 Save Email Settings", type="primary",
                     use_container_width=True, key="cfg_save_email"):
            new_cfg = {
                "allowed_domain": domain_input.strip().lower(),
                "admin_email":    admin_input.strip(),
                "smtp_host":      smtp_host.strip(),
                "smtp_port":      int(smtp_port.strip() or 587),
                "smtp_user":      smtp_user.strip(),
                "smtp_password":  smtp_pass,
            }
            save_smtp_config(new_cfg)
            st.success("✅ Email settings saved.")

        if st.button("🧪 Test GSheets connection", key="cfg_test_sheets"):
            ws_t, err_t = get_inventory_sheet()
            if ws_t:
                st.success(f"✅ Google Sheets OK — sheet '{ws_t.title}' accessible")
            else:
                st.error(f"❌ Google Sheets error: {err_t}")

        if st.button("🧪 Test email (send to admin)", key="cfg_test_email"):
            ok, msg = send_email(
                "RFID QR System — Test Email",
                "<p>Email configuration is working correctly.</p>",
                cfg.get("admin_email", DEFAULT_ADMIN_EMAIL)
            )
            if ok:
                st.success("✅ Test email sent successfully.")
            else:
                st.error(f"❌ Failed: {msg}")

    # ── Google Sheets status ──────────────────────────────────
    st.markdown("---")
    st.markdown("### 📊 Google Sheets (Master Inventory)")
    if not GSPREAD_OK:
        st.error("gspread library not installed. Add `gspread` and `google-auth` to requirements.txt")
    else:
        ws_test, ws_err = get_inventory_sheet()
        if ws_test:
            st.success(f"✅ Connected to Google Sheets inventory")
            try:
                inv_df, inv_df_err = get_inventory_df()
                if inv_df is not None and not inv_df.empty:
                    st.caption(f"{len(inv_df)} rows in inventory sheet")
                    with st.expander("Preview inventory (first 10 rows)"):
                        st.dataframe(inv_df.head(10), use_container_width=True)
                elif inv_df_err:
                    st.warning(f"Could not load preview: {inv_df_err}")
            except Exception as e:
                st.warning(f"Could not load preview: {e}")
        else:
            st.warning(f"⚠️ Google Sheets not connected: {ws_err}")
            st.markdown("""
**Required in Streamlit Secrets:**
```toml
[gsheets]
spreadsheet_id = "your-sheet-id"
sheet_name = "inventory"

[gsheets.credentials]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n"
client_email = "rfid@project.iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
```
            """)

    # ── Checkout QR ───────────────────────────────────────────
    st.markdown("**Checkout Request QR Code**")
    st.caption("Print this QR and place it at the warehouse entrance. "
               "Staff scan this to submit a checkout request.")
    checkout_url = f"{get_base_url()}?page=checkout"
    st.code(checkout_url, language=None)

    co_qr = make_qr_image(checkout_url, "CHECKOUT REQUEST", "Scan to request item")
    st.image(co_qr, width=200)
    st.download_button("⬇ Download Checkout QR",
        data=qr_to_bytes(co_qr),
        file_name="QR_Checkout_Request.png",
        mime="image/png", key="dl_checkout_qr")

    st.markdown("---")
    st.markdown("### Database status")
    data = load_data()
    c1, c2, c3 = st.columns(3)
    c1.metric("Total tags", len(data))
    c2.metric("Active tags", sum(1 for v in data.values() if not v.get("_cleared") and v.get("Material")))
    c3.metric("Empty tags",  sum(1 for v in data.values() if v.get("_cleared") or not v.get("Material")))
    if data:
        st.markdown("---")
        if st.button("🗑 Reset ALL tag data", type="secondary", key="setup_reset_all"):
            save_data({})
            st.success("All tag data cleared.")
            st.rerun()

# ─────────────────────────────────────────────
# REGISTER TAB
# ─────────────────────────────────────────────
def tab_register():
    st.subheader("📋 Register Material to RFID Tag")

    reg_mode = st.radio(

        "Registration method",
        ["🗂 Quick: Select from Master List", "📄 Bulk: Upload CSV file"],
        horizontal=True, key="reg_mode"
    )
    st.markdown("---")

    # ════════════════════════════════════════════════════════
    # QUICK MODE
    # ════════════════════════════════════════════════════════
    if reg_mode == "🗂 Quick: Select from Master List":

        master_df, _mdf_err_reg = get_master_df()
        if master_df is None:
            st.warning(f"⚠️ Cannot load master list: {_mdf_err_reg}")
            st.info("Check Google Sheets connection in the **Setup tab**.")
            return

        st.success(f"✅ Connected to Google Sheets — {len(master_df)} materials available")

        # ── RFID Tag input ────────────────────────────────────
        st.markdown("### Step 1 — RFID Tag Code")
        tag_file = st.file_uploader(

            "Upload RFID Tag List CSV (optional)", type=["csv"], key="tag_list_csv",
            help="CSV with RFID Tag Code column — enables dropdown tag selection"
        )
        tag_options = []
        if tag_file:
            df_t, err_t = parse_csv_generic(tag_file)
            if not err_t and "RFID Tag Code" in df_t.columns:
                tag_options = df_t["RFID Tag Code"].str.strip().dropna().unique().tolist()
                st.success(f"✅ {len(tag_options)} tags loaded")

        if tag_options:
            inp_mode = st.radio("Tag input", ["Select from list", "Type manually"],

                                horizontal=True, key="tag_input_mode")
            if inp_mode == "Select from list":
                sel_tag = st.selectbox("Select RFID Tag",

                    options=["— choose tag —"] + tag_options, key="tag_select")
                tag_code = "" if sel_tag == "— choose tag —" else sel_tag
            else:
                tag_code = st.text_input("Type RFID Tag Code",

                    placeholder="E2801191A504...", key="tag_manual_input").strip()
        else:
            tag_code = st.text_input("Type RFID Tag Code",


                placeholder="E2801191A504...", key="tag_manual_input2").strip()

        # ── Material selector ─────────────────────────────────
        st.markdown("### Step 2 — Select Material")
        disp = master_display_opts(master_df)
        selected_mat = st.selectbox("Material from master list",

            options=disp, key="master_mat_select")

        if not tag_code:
            st.warning("⚠️ Enter an RFID Tag Code above.")

        st.markdown("---")
        st.markdown("### Step 3 — Fill in material information")

        rmode_key   = "rq_mode"
        rloaded_key = "rq_loaded"
        rform_key   = "rq_form"
        rfirst_key  = "rq_first"

        def _rcleanup():
            for field in EXPECTED_COLS:
                if field == "RFID Tag Code": continue
                for sfx in ["_sel","_custom","_txt"]:
                    st.session_state.pop(f"rwk_{field}{sfx}", None)
            for k in [rmode_key, rloaded_key, rform_key, rfirst_key,
                      "rq_search","rq_chosen"]:
                st.session_state.pop(k, None)

        # ── Step 3a: Choose mode ──────────────────────────────
        if rmode_key not in st.session_state:
            rc1, rc2 = st.columns(2)
            with rc1:
                md = master_df is None
                if st.button("📋 From Master Database",
                             use_container_width=True, type="primary",
                             disabled=md, key="rq_btn_master"):
                    st.session_state[rmode_key] = "master"
                    st.rerun()
            with rc2:
                if st.button("✏️ Fill Manually",
                             use_container_width=True, key="rq_btn_manual"):
                    st.session_state[rmode_key]   = "manual"
                    st.session_state[rloaded_key] = {f:"" for f in EXPECTED_COLS}
                    st.session_state[rform_key]   = True
                    st.rerun()

        elif st.session_state[rmode_key] == "master" and not st.session_state.get(rform_key):
            # ── Step 3b: Search ───────────────────────────────
            st.markdown("**Search material from Master Database**")
            rq = st.text_input("Search", placeholder="Material ID, description...",

                key="rq_search")
            if rq.strip():
                rmask = master_df.apply(
                    lambda row: any(rq.lower() in str(v).lower() for v in row.values), axis=1)
                rfiltered = master_df[rmask]
            else:
                rfiltered = master_df
            st.caption(f"{len(rfiltered)} results")

            if len(rfiltered) > 0:
                rmc  = next((c for c in rfiltered.columns if "Material Description" in c), None)
                rmic = next((c for c in rfiltered.columns if c.strip() == "Material"), None)
                if rmic and rmc:
                    ropts = ["— select —"] + [f"{r[rmic]}  |  {r[rmc]}" for _,r in rfiltered.iterrows()]
                elif rmic:
                    ropts = ["— select —"] + rfiltered[rmic].tolist()
                else:
                    ropts = ["— select —"] + [f"Row {i+1}" for i in range(len(rfiltered))]

                rchosen = st.selectbox("Material", ropts, key="rq_chosen",
                    label_visibility="collapsed")
                rl1, rl2 = st.columns([2,1])
                with rl1:
                    if st.button("✅ Load into form", use_container_width=True,
                                 type="primary", key="rq_load",
                                 disabled=(rchosen=="— select —")):

                        ridx = ropts.index(rchosen) - 1
                        rrow = rfiltered.iloc[ridx]
                        rl = {f: str(rrow.get(f,"")).strip()
                              for f in EXPECTED_COLS if f in rfiltered.columns}
                        st.session_state[rloaded_key] = rl
                        st.session_state[rform_key]   = True
                        st.rerun()
                with rl2:
                    if st.button("← Back", use_container_width=True, key="rq_back"):
                        st.session_state.pop(rmode_key, None)
                        st.rerun()
            else:
                st.warning("No results.")

        else:
            # ── Step 3c: Edit form ────────────────────────────
            rloaded = st.session_state.get(rloaded_key, {f:"" for f in EXPECTED_COLS})
            rmode   = st.session_state.get(rmode_key, "manual")

            if rmode == "master":
                rn = rloaded.get("Material Description", rloaded.get("Material",""))
                st.success(f"📋 Loaded: **{rn}**")

            if not tag_code:
                st.warning("⚠️ Enter RFID Tag Code in Step 2 above.")

            st.markdown(f"**RFID Tag Code:** `{tag_code or '(not set)'}`")
            st.markdown("---")

            if rfirst_key not in st.session_state:
                for field in EXPECTED_COLS:
                    if field == "RFID Tag Code": continue
                    for sfx in ["_sel","_custom","_txt"]:
                        st.session_state.pop(f"rwk_{field}{sfx}", None)
                st.session_state[rfirst_key] = True

            rca, rcb = st.columns(2)
            for j, field in enumerate(EXPECTED_COLS):
                if field == "RFID Tag Code": continue
                rwk = f"rwk_{field}"
                rv  = rloaded.get(field,"")
                target = rca if j % 2 == 0 else rcb
                with target:
                    if field in DROPDOWN_FIELDS:
                        db2 = load_data()
                        ropts2 = get_field_options(db2, field)
                        if field in master_df.columns:
                            for v in master_df[field].dropna().unique():
                                if str(v).strip(): ropts2 = sorted(set(ropts2)|{str(v).strip()})
                        if rv and rv not in ropts2:
                            ropts2 = sorted(ropts2 + [rv])
                        CUSTOM = "— type custom value —"
                        rch = ropts2 + [CUSTOM]
                        ridx2 = ropts2.index(rv) if rv in ropts2 else len(rch)-1
                        rsel = st.selectbox(field, rch, index=ridx2, key=f"{rwk}_sel")
                        if rsel == CUSTOM:
                            st.text_input(f"Custom {field}", value=rv,


                                key=f"{rwk}_custom", placeholder=f"Enter {field}...")
                    else:
                        st.text_input(field, value=rv, key=f"{rwk}_txt")

            st.markdown("---")
            rs1, rs2, rs3 = st.columns([2,2,1])
            with rs1:
                rsave = st.button("💾 Save to Tag", type="primary",

                    use_container_width=True, key="rq_save")
            with rs2:
                if rmode == "master":
                    if st.button("🔍 Change material", use_container_width=True,

                                 key="rq_change"):
                        st.session_state.pop(rloaded_key, None)
                        st.session_state.pop(rform_key, None)
                        st.session_state.pop(rfirst_key, None)
                        st.rerun()
            with rs3:
                rcancel = st.button("✕", use_container_width=True, key="rq_cancel")

            if rsave:
                if not tag_code:
                    st.error("⚠️ Enter RFID Tag Code first.")
                else:
                    db3 = load_data()
                    new_vals = {"RFID Tag Code": tag_code}
                    for field in EXPECTED_COLS:
                        if field == "RFID Tag Code": continue
                        rwk = f"rwk_{field}"
                        if field in DROPDOWN_FIELDS:
                            sv = st.session_state.get(f"{rwk}_sel","")
                            new_vals[field] = (st.session_state.get(f"{rwk}_custom","")
                                if sv == "— type custom value —" else sv)
                        else:
                            new_vals[field] = st.session_state.get(f"{rwk}_txt","")
                    new_vals["_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    db3[tag_code] = new_vals
                    save_data(db3)
                    _rcleanup()
                    # Sync to Google Sheets
                    gs_r_ok, gs_r_msg = register_new_item_in_sheet(new_vals)
                    if gs_r_ok:
                        st.success(f"✅ Tag **{tag_code}** registered! Sheets: {gs_r_msg}")
                    else:
                        st.success(f"✅ Tag **{tag_code}** registered locally.")
                        st.warning(f"⚠️ Sheets sync failed: {gs_r_msg}")
                    st.balloons()

            if rcancel:
                _rcleanup()
                st.rerun()


    # ════════════════════════════════════════════════════════
    # BULK MODE
    # ════════════════════════════════════════════════════════
    else:
        st.info("Upload a CSV with both **RFID Tag Code** and material columns filled.")
        uploaded = st.file_uploader("Upload CSV file", type=["csv"], key="reg_csv")

        if not uploaded:
            st.markdown("""
**Required columns:**
`Material` · `Plant` · `Storage Location` · `Storage Type` · `Storage Section` ·
`Storage Bin` · `Material Description` · `Batch` · `Stock Category` ·
`Total Stock` · `Base Unit of Measure` · `SLED/BBD` · `GR Date` · **`RFID Tag Code`**
""")
            return

        df, err = parse_csv(uploaded)
        if err:
            st.error(f"CSV error: {err}")
            return

        st.success(f"✅ Loaded {len(df)} rows")
        st.dataframe(df, use_container_width=True, height=250)
        st.markdown("---")
        _, col2 = st.columns([3, 1])
        with col2:
            overwrite = st.checkbox("Overwrite existing", value=True, key="reg_overwrite")

        if st.button("☁ Register All", type="primary", use_container_width=True, key="reg_submit"):
            data = load_data()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            skipped = registered = 0
            prog = st.progress(0, text="Registering...")
            for i, row in df.iterrows():
                tc = str(row.get("RFID Tag Code","")).strip()
                if not tc:
                    skipped += 1; continue
                if tc in data and not overwrite:
                    skipped += 1; continue
                rec = {col: str(row.get(col,"")).strip()
                       for col in EXPECTED_COLS if col in df.columns}
                rec["RFID Tag Code"] = tc
                rec["_updated_at"]   = now
                data[tc] = rec
                registered += 1
                prog.progress((i+1)/len(df), text=f"Registering {tc[:16]}...")
            save_data(data)
            prog.empty()
            st.success(f"✅ {registered} registered · {skipped} skipped")
            st.balloons()

# ─────────────────────────────────────────────
# QR CODES TAB
# ─────────────────────────────────────────────
def tab_qrcodes():
    st.subheader("◻ QR Code Gallery")
    data = load_data()
    if not data:
        st.warning("No tags registered yet.")
        return

    c1, c2 = st.columns([4,1])
    with c1:
        search = st.text_input("Search", placeholder="Filter by tag, bin, material...",


            label_visibility="collapsed", key="qr_search")
    with c2:
        dl_all = st.button("⬇ Download All", use_container_width=True, key="qr_dl_all")

    tags = {k: v for k,v in data.items()
            if search.lower() in k.lower()
            or search.lower() in v.get("Material","").lower()
            or search.lower() in v.get("Storage Bin","").lower()
            or search.lower() in v.get("Material Description","").lower()}

    st.caption(f"Showing {len(tags)} of {len(data)} tags")

    if dl_all:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf,"w") as zf:
            for tc, rec in data.items():
                bid = rec.get("Storage Bin", tc[:8])
                img = make_qr_image(tag_url(tc), f"Bin:{bid}", tc[:16])
                zf.writestr(f"QR_{bid}_{tc[:8]}.png", qr_to_bytes(img))
        zip_buf.seek(0)
        st.download_button("📦 Download ZIP", data=zip_buf,
            file_name="RFID_QR_Codes.zip", mime="application/zip")

    cols = st.columns(4)
    for i, (tc, rec) in enumerate(tags.items()):
        has_data = bool(rec.get("Material")) and not rec.get("_cleared")
        bid  = rec.get("Storage Bin","—")
        img  = make_qr_image(tag_url(tc), f"Bin: {bid}", tc[:20])
        with cols[i%4]:
            st.markdown('<div class="qr-card">', unsafe_allow_html=True)
            st.markdown(f'<div class="bin-badge">Bin: {bid}</div>', unsafe_allow_html=True)
            st.markdown(f'<div style="font-size:0.62rem;color:#5a6280;font-family:monospace;margin-bottom:4px;word-break:break-all;">{tc}</div>', unsafe_allow_html=True)
            st.image(img, use_container_width=True)
            if has_data:
                st.markdown(f'<div class="status-ok">● {rec.get("Material","")[:18]}</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div class="status-empty">○ Empty</div>', unsafe_allow_html=True)
            st.download_button("⬇ PNG", data=qr_to_bytes(img),
                file_name=f"QR_{bid}_{tc[:8]}.png", mime="image/png",
                key=f"dl_{tc}", use_container_width=True)
            st.markdown('</div>', unsafe_allow_html=True)

# ─────────────────────────────────────────────
# MANAGE TAB
# ─────────────────────────────────────────────
def tab_manage():
    st.subheader("🗂 Material Tag Manager")
    data = load_data()
    if not data:
        st.warning("No tags registered yet.")
        return

    search = st.text_input("Search", placeholder="Filter by tag, bin, material...",


        label_visibility="collapsed", key="manage_search")
    tags = {k: v for k,v in data.items()
            if search.lower() in k.lower()
            or search.lower() in v.get("Material","").lower()
            or search.lower() in v.get("Storage Bin","").lower()
            or search.lower() in v.get("Material Description","").lower()}

    st.caption(f"{len(tags)} tags shown")

    for tc, rec in tags.items():
        has_data     = bool(rec.get("Material")) and not rec.get("_cleared")
        status_color = "#34d399" if has_data else "#7a8299"
        status_txt   = "Active" if has_data else "Empty"
        bid          = rec.get("Storage Bin","—")
        mat_desc     = rec.get("Material Description","(empty)")[:50]

        with st.expander(f"{'●' if has_data else '○'} **Bin {bid}** · {mat_desc}"):
            st.markdown(f"""
            <div style="background:#0d1a2e;border:1px solid #2a4a7a;border-radius:8px;
                padding:0.6rem 1rem;margin-bottom:0.75rem;">
              <div style="font-size:0.68rem;color:#5a7299;text-transform:uppercase;letter-spacing:1px;">RFID Tag Code (permanent)</div>
              <div style="font-family:monospace;font-size:0.88rem;color:#7ab8f5;word-break:break-all;margin-top:2px;">{tc}</div>
            </div>""", unsafe_allow_html=True)

            c1, c2 = st.columns([3,1])
            with c1:
                st.markdown(f"**Status:** <span style='color:{status_color}'>{status_txt}</span>",
                    unsafe_allow_html=True)
                if has_data:
                    st.markdown(f"**Material:** `{rec.get('Material','—')}`")
                    st.markdown(f"**Stock:** {rec.get('Total Stock','—')} {rec.get('Base Unit of Measure','')}")
                    st.markdown(f"**Batch:** {rec.get('Batch','—')}")
                    st.markdown(f"**GR Date:** {rec.get('GR Date','—')}")
                    st.markdown(f"**SLED/BBD:** {rec.get('SLED/BBD','—')}")
            with c2:
                st.markdown(f"[🔗 View]({tag_url(tc)})")

            st.markdown("---")
            ac = st.columns(3)
            with ac[0]:
                if st.button("✎ Edit", key=f"edit_{tc}", use_container_width=True):
                    st.session_state[f"editing_{tc}"] = True
            with ac[1]:
                if st.button("✕ Clear", key=f"clear_{tc}",
                             use_container_width=True, type="secondary"):
                    data[tc] = {"RFID Tag Code": tc, "_cleared": True,
                        "_cleared_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    save_data(data)
                    st.success("Tag cleared.")
                    st.rerun()
            with ac[2]:
                qr_img = make_qr_image(tag_url(tc), f"Bin: {bid}", tc[:20])
                st.download_button("⬇ QR", data=qr_to_bytes(qr_img),
                    file_name=f"QR_{bid}_{tc[:8]}.png", mime="image/png",
                    key=f"qrdl_{tc}", use_container_width=True)

            # ── Edit form with master list ────────────────────
            if st.session_state.get(f"editing_{tc}"):
                st.markdown("---")
                st.markdown("#### ✎ Edit material data")

                mmode_key   = f"mm_mode_{tc}"
                mloaded_key = f"mm_loaded_{tc}"
                mform_key   = f"mm_form_{tc}"
                mfirst_key  = f"mm_first_{tc}"
                master_df, _mdf_err3 = get_master_df()

                def _mcleanup():
                    for field in EXPECTED_COLS:
                        if field == "RFID Tag Code": continue
                        for sfx in ["_sel","_custom","_txt"]:
                            st.session_state.pop(f"mwk_{tc}_{field}{sfx}", None)
                    for k in [mmode_key, mloaded_key, mform_key, mfirst_key]:
                        st.session_state.pop(k, None)
                    st.session_state[f"editing_{tc}"] = False

                # ── Step 1: Choose mode ───────────────────────
                if mmode_key not in st.session_state:
                    st.markdown("**How would you like to fill in the information?**")
                    mc1, mc2 = st.columns(2)
                    with mc1:
                        md = master_df is None
                        if st.button("📋 From Master Database",

                                     use_container_width=True, type="primary",
                                     disabled=md, key=f"mm_btn_master_{tc}"):
                            st.session_state[mmode_key] = "master"
                            st.rerun()
                        if md: st.caption("Upload Master CSV in Setup first")
                    with mc2:
                        if st.button("✏️ Fill Manually",

                                     use_container_width=True, key=f"mm_btn_manual_{tc}"):
                            st.session_state[mmode_key]   = "manual"
                            st.session_state[mloaded_key] = {f: rec.get(f,"") for f in EXPECTED_COLS}
                            st.session_state[mform_key]   = True
                            st.rerun()
                    if st.button("✕ Cancel edit", key=f"mm_cancel_mode_{tc}"):
                        st.session_state[f"editing_{tc}"] = False
                        st.rerun()

                elif st.session_state[mmode_key] == "master" and not st.session_state.get(mform_key):
                    # ── Step 2: Search ────────────────────────
                    st.markdown("**Search & select material**")
                    mq = st.text_input("Search material",

                        placeholder="Material ID, description...",
                        key=f"mm_search_{tc}")
                    if mq.strip():
                        mask = master_df.apply(
                            lambda row: any(mq.lower() in str(v).lower() for v in row.values), axis=1)
                        mfiltered = master_df[mask]
                    else:
                        mfiltered = master_df
                    st.caption(f"{len(mfiltered)} results")

                    if len(mfiltered) > 0:
                        mc = next((c for c in mfiltered.columns if "Material Description" in c), None)
                        mic = next((c for c in mfiltered.columns if c.strip() == "Material"), None)
                        if mic and mc:
                            mopts = ["— select —"] + [f"{r[mic]}  |  {r[mc]}" for _,r in mfiltered.iterrows()]
                        elif mic:
                            mopts = ["— select —"] + mfiltered[mic].tolist()
                        else:
                            mopts = ["— select —"] + [f"Row {i+1}" for i in range(len(mfiltered))]

                        mchosen = st.selectbox("Material", options=mopts,

                            key=f"mm_chosen_{tc}", label_visibility="collapsed")
                        ml1, ml2 = st.columns([2,1])
                        with ml1:
                            if st.button("✅ Load into form", use_container_width=True,

                                         type="primary", key=f"mm_load_{tc}",
                                         disabled=(mchosen=="— select —")):

                                midx = mopts.index(mchosen) - 1
                                mrow = mfiltered.iloc[midx]
                                ml = {f: str(mrow.get(f,"")).strip()
                                      for f in EXPECTED_COLS if f in mfiltered.columns}
                                ml["RFID Tag Code"] = tc
                                st.session_state[mloaded_key] = ml
                                st.session_state[mform_key]   = True
                                st.rerun()
                        with ml2:
                            if st.button("← Back", use_container_width=True,

                                         key=f"mm_back_{tc}"):
                                st.session_state.pop(mmode_key, None)
                                st.rerun()
                    else:
                        st.warning("No results.")
                        if st.button("← Back", key=f"mm_back2_{tc}"):
                            st.session_state.pop(mmode_key, None)
                            st.rerun()

                else:
                    # ── Step 3: Edit form ─────────────────────
                    mloaded = st.session_state.get(mloaded_key,
                        {f: rec.get(f,"") for f in EXPECTED_COLS})
                    mmode = st.session_state[mmode_key]
                    if mmode == "master":
                        mn = mloaded.get("Material Description", mloaded.get("Material",""))
                        st.success(f"📋 Loaded: **{mn}**")

                    st.markdown(f"**RFID Tag Code (fixed):** `{tc}`")
                    st.markdown("---")

                    if mfirst_key not in st.session_state:
                        for field in EXPECTED_COLS:
                            if field == "RFID Tag Code": continue
                            for sfx in ["_sel","_custom","_txt"]:
                                st.session_state.pop(f"mwk_{tc}_{field}{sfx}", None)
                        st.session_state[mfirst_key] = True

                    mca, mcb = st.columns(2)
                    for j, field in enumerate(EXPECTED_COLS):
                        if field == "RFID Tag Code": continue
                        mwk = f"mwk_{tc}_{field}"
                        mv  = mloaded.get(field,"")
                        tgt = mca if j % 2 == 0 else mcb
                        with tgt:
                            if field in DROPDOWN_FIELDS:
                                mopts2 = get_field_options(data, field)
                                if mv and mv not in mopts2:
                                    mopts2 = sorted(mopts2 + [mv])
                                CUSTOM = "— type custom value —"
                                mch = mopts2 + [CUSTOM]
                                midx2 = mopts2.index(mv) if mv in mopts2 else len(mch)-1
                                msel = st.selectbox(field, mch, index=midx2, key=f"{mwk}_sel")
                                if msel == CUSTOM:
                                    st.text_input(f"Custom {field}", value=mv,


                                        key=f"{mwk}_custom", placeholder=f"Enter {field}...")
                            else:
                                st.text_input(field, value=mv, key=f"{mwk}_txt")

                    st.markdown("---")
                    ms1, ms2, ms3 = st.columns([2,2,1])
                    with ms1:
                        msave = st.button("💾 Save Changes", type="primary",

                            use_container_width=True, key=f"mm_save_{tc}")
                    with ms2:
                        if mmode == "master":
                            if st.button("🔍 Change material", use_container_width=True,

                                         key=f"mm_change_{tc}"):
                                st.session_state.pop(mloaded_key, None)
                                st.session_state.pop(mform_key, None)
                                st.session_state.pop(mfirst_key, None)
                                st.rerun()
                    with ms3:
                        mcancel = st.button("✕", use_container_width=True,

                            key=f"mm_cancel_{tc}")

                    if msave:
                        new_vals = {"RFID Tag Code": tc}
                        for field in EXPECTED_COLS:
                            if field == "RFID Tag Code": continue
                            mwk = f"mwk_{tc}_{field}"
                            if field in DROPDOWN_FIELDS:
                                sv = st.session_state.get(f"{mwk}_sel","")
                                new_vals[field] = (st.session_state.get(f"{mwk}_custom","")
                                    if sv == "— type custom value —" else sv)
                            else:
                                new_vals[field] = st.session_state.get(f"{mwk}_txt","")
                        new_vals["_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        data[tc] = new_vals
                        save_data(data)
                        _mcleanup()
                        # Sync to Google Sheets
                        gs_m_ok, gs_m_msg = register_new_item_in_sheet(new_vals)
                        if gs_m_ok:
                            st.success(f"✅ Tag updated! Sheets: {gs_m_msg}")
                        else:
                            st.success("✅ Tag updated locally.")
                            st.warning(f"⚠️ Sheets sync failed: {gs_m_msg}")
                        st.rerun()

                    if mcancel:
                        _mcleanup()
                        st.rerun()


def main():
    try:
        params    = st.query_params
        tag_param = params.get("tag", None)
        page_param = params.get("page", None)
        if tag_param:  tag_param  = str(tag_param).strip()
        if page_param: page_param = str(page_param).strip()
    except Exception:
        tag_param  = None
        page_param = None

    # Checkout request page — single permanent URL for all staff
    if page_param == "checkout":
        show_checkout_page()
        return

    if tag_param:
        show_viewer(tag_param)
        return

    st.markdown(ADMIN_CSS, unsafe_allow_html=True)
    st.markdown("""
    <div style="background:linear-gradient(135deg,#0d1b3e,#1a1040);
        padding:1rem 1.5rem;border-radius:10px;margin-bottom:1rem;">
      <div style="font-family:monospace;color:#4f9cf9;font-size:1rem;
          letter-spacing:2px;font-weight:700;">
          RFID<span style="color:#7c5cbf">·QR</span> MANAGER</div>
      <div style="font-size:0.72rem;color:#7a8299;margin-top:2px;letter-spacing:1px;">
          MATERIAL TRACKING SYSTEM</div>
    </div>
    """, unsafe_allow_html=True)

    tab1, tab2, tab3, tab4 = st.tabs(["⚙ Setup", "📋 Register", "◻ QR Codes", "🗂 Manage"])
    with tab1: tab_setup()
    with tab2: tab_register()
    with tab3: tab_qrcodes()
    with tab4: tab_manage()

if __name__ == "__main__":
    main()
