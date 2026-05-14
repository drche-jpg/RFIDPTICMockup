import streamlit as st
import pandas as pd
import json
import qrcode
import io
import os
import zipfile
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
import base64

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="RFID·QR Material Manager",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ─────────────────────────────────────────────
# DATA STORAGE
# ─────────────────────────────────────────────
DATA_FILE = "material_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ─────────────────────────────────────────────
# URL HELPER  — keyed by RFID Tag Code
# ─────────────────────────────────────────────
DEFAULT_APP_URL = "https://rfidmockupv2-ybpjtckxtby4yehfv4mxme.streamlit.app"

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
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
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
    # Top label (bin or tag code)
    bbox = draw.textbbox((0, 0), label_top, font=font_big)
    tw = bbox[2] - bbox[0]
    draw.text(((qr_img.width - tw) // 2, qr_img.height + 5), label_top, fill="black", font=font_big)
    # Bottom label
    bbox2 = draw.textbbox((0, 0), label_bot, font=font_sm)
    tw2 = bbox2[2] - bbox2[0]
    draw.text(((qr_img.width - tw2) // 2, qr_img.height + 26), label_bot, fill="#555555", font=font_sm)
    # RFID tag label
    tag_label = "RFID·QR SYSTEM"
    bbox3 = draw.textbbox((0, 0), tag_label, font=font_sm)
    tw3 = bbox3[2] - bbox3[0]
    draw.text(((qr_img.width - tw3) // 2, qr_img.height + 42), tag_label, fill="#888888", font=font_sm)
    return canvas

def qr_to_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# ─────────────────────────────────────────────
# CSV PARSER & FIELD CONFIG
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

FREETEXT_FIELDS = [
    "RFID Tag Code", "Material Description", "Batch",
    "Total Stock", "SLED/BBD", "GR Date"
]

def get_field_options(data, field):
    vals = set()
    for rec in data.values():
        v = rec.get(field, "").strip()
        if v and not v.startswith("_"):
            vals.add(v)
    return sorted(vals)

def parse_csv(uploaded_file):
    try:
        df = pd.read_csv(uploaded_file, dtype=str).fillna("")
        df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
        # Drop completely empty rows
        df = df[df["RFID Tag Code"].str.strip() != ""]
        return df, None
    except Exception as e:
        return None, str(e)

# ─────────────────────────────────────────────
# PASSWORD HELPERS
# ─────────────────────────────────────────────
DEFAULT_PASSWORD = "RFID123"

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

    pw = st.text_input("Password", type="password",
                        placeholder="Enter password…",
                        key=f"pw_input_{tag_code}",
                        label_visibility="collapsed")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔓 Unlock", use_container_width=True,
                     type="primary", key=f"pw_submit_{tag_code}"):
            if pw == get_password():
                st.session_state[f"auth_ok_{tag_code}"] = True
                st.success("✅ Authenticated!")
                st.rerun()
            else:
                st.error("❌ Incorrect password. Please try again.")
    with col2:
        if st.button("✕ Cancel", use_container_width=True,
                     key=f"pw_cancel_{tag_code}"):
            st.session_state.pop(f"v_mode_{tag_code}", None)
            st.rerun()

# ─────────────────────────────────────────────
# SHARED EDIT FORM
# ─────────────────────────────────────────────
def _show_edit_form(tag_code, rec, data, is_empty=False):
    title = "Register Material" if is_empty else "Edit Material"
    st.markdown(f"""
    <div class="edit-header">
      <div class="edit-header-title">✎  {title}</div>
      <div class="edit-header-sub">
          RFID Tag: {tag_code}<br>
          Select from known values or choose "type custom value" to enter new data.
      </div>
    </div>
    """, unsafe_allow_html=True)

    with st.form(key=f"viewer_form_{tag_code}"):
        new_vals = {}
        # RFID Tag Code is fixed — not editable
        new_vals["RFID Tag Code"] = tag_code
        st.markdown(f"**RFID Tag Code (fixed):** `{tag_code}`")
        st.markdown("---")

        for field in EXPECTED_COLS:
            if field == "RFID Tag Code":
                continue  # already set above, not editable
            current_val = rec.get(field, "")
            if field in DROPDOWN_FIELDS:
                options = get_field_options(data, field)
                CUSTOM = "— type custom value —"
                choices = options + [CUSTOM]
                default_idx = (options.index(current_val)
                               if current_val in options else len(choices) - 1)
                selected = st.selectbox(field, options=choices,
                                        index=default_idx,
                                        key=f"vf_sel_{tag_code}_{field}")
                if selected == CUSTOM:
                    new_vals[field] = st.text_input(
                        f"Custom {field}",
                        value=current_val if current_val not in options else "",
                        key=f"vf_custom_{tag_code}_{field}",
                        placeholder=f"Enter {field}...")
                else:
                    new_vals[field] = selected
            else:
                new_vals[field] = st.text_input(
                    field, value=current_val,
                    key=f"vf_inp_{tag_code}_{field}")

        st.markdown("---")
        st.markdown(
            "<div style='font-size:0.9rem;color:#7a8299;margin-bottom:6px;'>"
            "Type <b style='color:#4f9cf9;font-size:1rem;'>SAVE</b> in the box "
            "below to confirm, then click Save Material.</div>",
            unsafe_allow_html=True
        )
        confirm_text = st.text_input(
            "Type SAVE to confirm",
            key=f"vf_confirm_text_{tag_code}",
            placeholder="Type SAVE here…",
            label_visibility="collapsed"
        )
        confirmed = confirm_text.strip().upper() == "SAVE"

        cs, cc = st.columns(2)
        with cs:
            submitted = st.form_submit_button(
                "💾  Save Material", type="primary", use_container_width=True)
        with cc:
            cancelled = st.form_submit_button("✕  Cancel", use_container_width=True)

        if submitted:
            if not confirmed:
                st.error("⚠️  Please type SAVE in the confirmation box to proceed.")
            else:
                new_vals["_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                data[tag_code] = new_vals
                save_data(data)
                st.session_state.pop(f"v_mode_{tag_code}", None)
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.success(f"✅ Tag {tag_code} saved successfully!")
                st.rerun()
        if cancelled:
            st.session_state.pop(f"v_mode_{tag_code}", None)
            st.session_state.pop(f"auth_ok_{tag_code}", None)
            st.rerun()

# ─────────────────────────────────────────────
# VIEWER MODE — triggered by ?tag=RFIDTAGCODE
# ─────────────────────────────────────────────
def show_viewer(tag_code):
    data = load_data()

    st.markdown("""
    <style>
    #MainMenu, footer, header {visibility: hidden;}
    .block-container {padding-top: 0 !important; padding-bottom: 2rem; max-width: 680px;}
    .stButton > button {font-size: 1.05rem !important; padding: 0.65rem 1rem !important; border-radius: 10px !important;}
    div[data-testid="stForm"] {border: none; padding: 0;}

    .mat-id-box {
        background: linear-gradient(135deg,#0f2a52,#1a1040);
        border: 1px solid #3a5a9a; border-radius: 14px;
        padding: 1.2rem 1.4rem; margin-bottom: 1rem;
    }
    .mat-id-label {
        font-size: 0.72rem; text-transform: uppercase;
        letter-spacing: 2px; color: #7a8299; margin-bottom: 6px;
    }
    .mat-id-value {
        font-family: monospace; font-size: 2.1rem; font-weight: 800;
        color: #4f9cf9; letter-spacing: 2px; line-height: 1.1; word-break: break-all;
    }
    .mat-name-value {
        font-size: 1.2rem; font-weight: 600; color: #111111;
        margin-top: 8px; line-height: 1.4;
    }
    .rfid-tag-badge {
        display: inline-block; margin-top: 10px;
        background: rgba(79,156,249,0.15); border: 1px solid #3a5a9a;
        border-radius: 6px; padding: 4px 10px;
        font-family: monospace; font-size: 0.72rem; color: #7ab8f5;
        letter-spacing: 1px; word-break: break-all;
    }
    .detail-card {
        background: #f5f7fa;
        border: 1px solid #c8d0e0;
        border-radius: 14px; overflow: hidden; margin-bottom: 1rem;
    }
    .detail-card-header {
        background: #dde3ee; padding: 0.6rem 1.1rem;
        border-bottom: 1px solid #c8d0e0;
        font-size: 0.7rem; text-transform: uppercase;
        letter-spacing: 2px; color: #334466; font-weight: 600;
    }
    .detail-row {
        display: flex; align-items: center;
        padding: 0.75rem 1.1rem; border-bottom: 1px solid #dde3ee; gap: 0.8rem;
    }
    .detail-row:last-child { border-bottom: none; }
    .detail-icon { font-size: 1.25rem; width: 30px; text-align: center; flex-shrink: 0; }
    .detail-label {
        font-size: 0.7rem; text-transform: uppercase;
        letter-spacing: 1px; color: #667799; margin-bottom: 3px;
    }
    .detail-value { font-size: 1.05rem; font-weight: 600; color: #111111; word-break: break-word; }
    .detail-value-hi { font-size: 1.15rem; font-weight: 700; color: #0a7a45; word-break: break-word; }
    .confirm-box {
        background: #2a1010; border: 2px solid #f87171;
        border-radius: 12px; padding: 1.2rem 1.4rem; margin-top: 0.8rem;
    }
    .confirm-title { font-size: 1.1rem; font-weight: 700; color: #f87171; margin-bottom: 0.5rem; }
    .confirm-body { color: #e8ecf4; font-size: 1rem; line-height: 1.7; }
    .empty-box {
        background: #1a1d27; border: 2px solid #fbbf24;
        border-radius: 12px; padding: 2rem; text-align: center; margin-bottom: 1.5rem;
    }
    .empty-title { font-size: 1.3rem; font-weight: 700; color: #fbbf24; }
    .empty-sub { color: #7a8299; margin-top: 0.5rem; font-size: 0.95rem; }
    .edit-header {
        background: #0f2240; border: 1px solid #2a4a7a;
        border-radius: 10px; padding: 0.9rem 1.2rem; margin-bottom: 1rem;
    }
    .edit-header-title { font-size: 1rem; font-weight: 700; color: #4f9cf9; }
    .edit-header-sub { font-size: 0.82rem; color: #7a8299; margin-top: 4px; line-height: 1.6; }
    </style>
    """, unsafe_allow_html=True)

    # Top header
    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#0a1628,#15103a);
        padding:1.3rem 1.5rem 1.1rem;margin-bottom:1rem;
        border-bottom:3px solid #4f9cf9;">
      <div style="font-family:monospace;color:#4f9cf9;font-size:0.78rem;
          letter-spacing:3px;margin-bottom:5px;">📦 RFID · QR SYSTEM</div>
      <div style="font-size:0.8rem;color:#7a8299;letter-spacing:1px;
          text-transform:uppercase;">RFID Tag</div>
      <div style="font-family:monospace;font-size:1.1rem;font-weight:700;
          color:#ffffff;letter-spacing:1px;margin-top:4px;word-break:break-all;">
          {tag_code}</div>
    </div>
    """, unsafe_allow_html=True)

    if not data:
        st.warning("⚠️ No materials have been registered yet.")
        st.info("Ask your administrator to import the CSV file in the Register tab.")
        return

    if tag_code not in data:
        st.error(f"❌ Tag **{tag_code}** not registered.")
        st.info("Contact your warehouse administrator.")
        return

    rec = data[tag_code]

    # Empty / cleared tag
    if rec.get("_cleared"):
        st.markdown(f"""
        <div class="empty-box">
          <div style="font-size:3rem;margin-bottom:0.5rem;">📭</div>
          <div class="empty-title">Tag is Empty</div>
          <div class="empty-sub">No material registered<br>
              Cleared: {rec.get("_cleared_at","unknown")}</div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("### Register new material to this tag")
        if not check_viewer_auth(tag_code):
            show_password_gate(tag_code)
        else:
            _show_edit_form(tag_code, rec, data, is_empty=True)
        return

    # Material hero card
    bin_id   = rec.get("Storage Bin","—")
    mat_id   = rec.get("Material","—")
    mat_desc = rec.get("Material Description","—")

    st.markdown(f"""
    <div class="mat-id-box">
      <div class="mat-id-label">Material ID</div>
      <div class="mat-id-value">{mat_id}</div>
      <div class="mat-name-value">{mat_desc}</div>
      <div class="rfid-tag-badge">🏷 RFID: {tag_code}</div>
    </div>
    """, unsafe_allow_html=True)

    # Detail rows
    st.markdown('<div class="detail-card"><div class="detail-card-header">Material Details</div>', unsafe_allow_html=True)

    stock_val = f"{rec.get('Total Stock','')} {rec.get('Base Unit of Measure','')}".strip()

    rows = [
        ("📦", "Storage Bin",      rec.get("Storage Bin",""),      False),
        ("🏭", "Plant",            rec.get("Plant",""),            False),
        ("📍", "Storage Location", rec.get("Storage Location",""), False),
        ("🗂",  "Storage Type",     rec.get("Storage Type",""),     False),
        ("📂", "Storage Section",  rec.get("Storage Section",""),  False),
        ("🏷",  "Batch",            rec.get("Batch",""),            False),
        ("📋", "Stock Category",   rec.get("Stock Category",""),   False),
        ("📊", "Total Stock",      stock_val,                      True),
        ("📅", "SLED / BBD",       rec.get("SLED/BBD",""),         False),
        ("📅", "GR Date",          rec.get("GR Date",""),          False),
    ]

    for icon, label, value, highlight in rows:
        val_css = "detail-value-hi" if highlight else "detail-value"
        st.markdown(f"""<div class="detail-row">
          <div class="detail-icon">{icon}</div>
          <div style="flex:1;min-width:0;">
            <div class="detail-label">{label}</div>
            <div class="{val_css}">{value or "—"}</div>
          </div>
        </div>""", unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)
    st.caption(f"🕐 Last updated: {rec.get('_updated_at','unknown')}  ·  Scan again to refresh")

    # Warehouse action buttons
    st.markdown("---")
    st.markdown("#### Warehouse Actions")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("✎  Edit Material", use_container_width=True, type="primary",
                     key=f"v_edit_{tag_code}"):
            st.session_state[f"v_mode_{tag_code}"] = "edit"
            st.session_state.pop(f"auth_ok_{tag_code}", None)
            st.rerun()
    with col2:
        if st.button("🗑  Clear Tag", use_container_width=True,
                     key=f"v_clear_btn_{tag_code}"):
            st.session_state[f"v_mode_{tag_code}"] = "confirm_clear"
            st.session_state.pop(f"auth_ok_{tag_code}", None)
            st.rerun()

    mode = st.session_state.get(f"v_mode_{tag_code}", "")

    # Password gate
    if mode in ("edit", "confirm_clear"):
        if not check_viewer_auth(tag_code):
            st.markdown("---")
            show_password_gate(tag_code)
            return

    # Confirm clear (after auth)
    if mode == "confirm_clear":
        st.markdown(f"""
        <div class="confirm-box">
          <div class="confirm-title">⚠️  Confirm Clear Tag</div>
          <div class="confirm-body">
            This will remove all material data from RFID tag<br>
            <strong>{tag_code}</strong><br><br>
            The QR code and URL will remain unchanged.<br>
            <strong>Are you sure?</strong>
          </div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown(" ")
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("✅  Yes, Clear Tag", use_container_width=True,
                         type="primary", key=f"v_confirm_clear_{tag_code}"):
                data[tag_code] = {
                    "RFID Tag Code": tag_code,
                    "_cleared": True,
                    "_cleared_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                save_data(data)
                st.session_state.pop(f"v_mode_{tag_code}", None)
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.success("✅ Tag cleared successfully.")
                st.rerun()
        with cc2:
            if st.button("✕  Cancel", use_container_width=True,
                         key=f"v_cancel_clear_{tag_code}"):
                st.session_state.pop(f"v_mode_{tag_code}", None)
                st.session_state.pop(f"auth_ok_{tag_code}", None)
                st.rerun()

    # Edit form (after auth)
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
.qr-card {
    background: #1a1d27; border: 1px solid #2e3347; border-radius: 10px;
    padding: 1rem; text-align: center; margin-bottom: 0.5rem;
}
.bin-badge {
    background: rgba(79,156,249,0.15); color: #4f9cf9;
    padding: 2px 10px; border-radius: 20px; font-family: monospace;
    font-size: 0.78rem; font-weight: 700; display: inline-block; margin-bottom: 4px;
    word-break: break-all;
}
.status-ok   {color: #34d399; font-size: 0.75rem;}
.status-empty {color: #7a8299; font-size: 0.75rem;}
</style>
"""

# ─────────────────────────────────────────────
# ADMIN — SETUP TAB
# ─────────────────────────────────────────────
def tab_setup():
    st.subheader("App Configuration")

    st.markdown("### App URL (for QR codes)")
    st.success(f"**Active URL:** {get_base_url()}")
    st.caption("QR codes will link to this URL with ?tag=RFID_TAG_CODE appended.")

    with st.expander("Change URL (optional)"):
        st.warning("Only change this if you move the app to a different address.")
        url_input = st.text_input(
            "New App URL", value=get_base_url(),
            placeholder="https://your-app-name.streamlit.app")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Save new URL", type="primary", use_container_width=True):
                st.session_state["base_url"] = url_input.strip().rstrip("/")
                st.success(f"URL updated to: {url_input.strip()}")
                st.rerun()
        with col2:
            if st.button("Reset to default", use_container_width=True):
                st.session_state.pop("base_url", None)
                st.success(f"Reset to: {DEFAULT_APP_URL}")
                st.rerun()

    st.markdown("---")
    st.markdown("### 🔒 Warehouse Password")
    current_pw = get_password()
    st.info(f"Current password: **{'*' * len(current_pw)}** ({len(current_pw)} characters)  ·  Default: `RFID123`")

    with st.expander("Change Password (optional)"):
        col_a, col_b = st.columns(2)
        with col_a:
            new_pw1 = st.text_input("New Password", type="password",
                                     key="new_pw1", placeholder="Enter new password")
        with col_b:
            new_pw2 = st.text_input("Confirm Password", type="password",
                                     key="new_pw2", placeholder="Repeat new password")
        pw_cols = st.columns(2)
        with pw_cols[0]:
            if st.button("💾 Save Password", type="primary", use_container_width=True):
                if not new_pw1:
                    st.error("Password cannot be empty.")
                elif new_pw1 != new_pw2:
                    st.error("Passwords do not match.")
                elif len(new_pw1) < 4:
                    st.error("Password must be at least 4 characters.")
                else:
                    st.session_state["app_password"] = new_pw1
                    st.success("✅ Password updated for this session.")
                    st.caption("For a permanent password, set RFID_PASSWORD in Streamlit Cloud secrets.")
        with pw_cols[1]:
            if st.button("↩ Reset to RFID123", use_container_width=True):
                st.session_state.pop("app_password", None)
                st.success("Password reset to default: RFID123")
                st.rerun()

    st.markdown("---")
    st.markdown("### How it works")
    st.info("""
- Each **RFID Tag Code** gets a permanent QR code and URL: `?tag=E2801191...`
- The QR code never changes — it is physically fixed to the RFID tag
- Material data (bin, batch, stock etc.) can be updated or cleared at any time
- Scanning the QR on any phone shows the current material info instantly
- Password required to edit or clear from the mobile scan page
""")

    st.markdown("---")
    st.markdown("### Database status")
    data = load_data()
    col1, col2, col3 = st.columns(3)
    col1.metric("Total tags", len(data))
    col2.metric("Active tags", sum(1 for v in data.values()
                                   if not v.get("_cleared") and v.get("Material")))
    col3.metric("Empty tags", sum(1 for v in data.values()
                                   if v.get("_cleared") or not v.get("Material")))
    if data:
        st.markdown("---")
        if st.button("🗑 Reset ALL data", type="secondary"):
            save_data({})
            st.success("All data cleared.")
            st.rerun()

# ─────────────────────────────────────────────
# ADMIN — REGISTER TAB
# ─────────────────────────────────────────────
def tab_register():
    st.subheader("📋 Import Materials from CSV")
    st.info("CSV must include an **RFID Tag Code** column. Each tag code becomes the permanent key for its QR code and URL.")

    uploaded = st.file_uploader("Upload your CSV file", type=["csv"])

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
        st.error(f"CSV parse error: {err}")
        return

    if "RFID Tag Code" not in df.columns:
        st.error("❌ CSV must contain an 'RFID Tag Code' column.")
        return

    st.success(f"✅ Loaded {len(df)} rows from CSV")
    st.dataframe(df, use_container_width=True, height=250)
    st.markdown("---")
    col1, col2 = st.columns([3, 1])
    with col2:
        overwrite = st.checkbox("Overwrite existing tags", value=True)

    if st.button("☁ Register All to Database", type="primary", use_container_width=True):
        data = load_data()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        skipped = 0
        registered = 0
        prog = st.progress(0, text="Registering...")
        for i, row in df.iterrows():
            tag_code = str(row.get("RFID Tag Code", "")).strip()
            if not tag_code:
                skipped += 1
                continue
            if tag_code in data and not overwrite:
                skipped += 1
                continue
            rec = {col: str(row.get(col, "")).strip() for col in EXPECTED_COLS if col in df.columns}
            rec["RFID Tag Code"] = tag_code
            rec["_updated_at"] = now
            data[tag_code] = rec
            registered += 1
            prog.progress((i + 1) / len(df), text=f"Registering {tag_code[:16]}...")
        save_data(data)
        prog.empty()
        st.success(f"✅ {registered} tags registered · {skipped} skipped")
        st.balloons()

# ─────────────────────────────────────────────
# ADMIN — QR CODES TAB
# ─────────────────────────────────────────────
def tab_qrcodes():
    st.subheader("◻ QR Code Gallery")
    data = load_data()

    if not data:
        st.warning("No tags registered yet. Go to the Register tab to import your CSV.")
        return

    col1, col2 = st.columns([4, 1])
    with col1:
        search = st.text_input("Search", placeholder="Filter by RFID tag, bin, material...",
                               label_visibility="collapsed")
    with col2:
        dl_all = st.button("⬇ Download All", use_container_width=True)

    tags = {k: v for k, v in data.items()
            if search.lower() in k.lower()
            or search.lower() in v.get("Material", "").lower()
            or search.lower() in v.get("Storage Bin", "").lower()
            or search.lower() in v.get("Material Description", "").lower()}

    st.caption(f"Showing {len(tags)} of {len(data)} tags")

    if dl_all:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            for tc, rec in data.items():
                bin_id = rec.get("Storage Bin", tc[:8])
                url    = tag_url(tc)
                label  = f"Bin:{bin_id}"
                img    = make_qr_image(url, label, tc[:16])
                zf.writestr(f"QR_{bin_id}_{tc[:8]}.png", qr_to_bytes(img))
        zip_buf.seek(0)
        st.download_button("📦 Download ZIP (all QR codes)", data=zip_buf,
                           file_name="RFID_QR_Codes.zip", mime="application/zip")

    cols = st.columns(4)
    for i, (tc, rec) in enumerate(tags.items()):
        has_data = bool(rec.get("Material")) and not rec.get("_cleared")
        bin_id   = rec.get("Storage Bin", "—")
        url      = tag_url(tc)
        label    = f"Bin: {bin_id}"
        img      = make_qr_image(url, label, tc[:20])

        with cols[i % 4]:
            st.markdown('<div class="qr-card">', unsafe_allow_html=True)
            st.markdown(f'<div class="bin-badge">Bin: {bin_id}</div>', unsafe_allow_html=True)
            st.markdown(f'<div style="font-size:0.62rem;color:#5a6280;font-family:monospace;margin-bottom:4px;word-break:break-all;">{tc}</div>', unsafe_allow_html=True)
            st.image(img, use_container_width=True)
            if has_data:
                st.markdown(f'<div class="status-ok">● {rec.get("Material","")[:18]}</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div class="status-empty">○ Empty</div>', unsafe_allow_html=True)
            st.download_button("⬇ PNG", data=qr_to_bytes(img),
                               file_name=f"QR_{bin_id}_{tc[:8]}.png", mime="image/png",
                               key=f"dl_{tc}", use_container_width=True)
            st.markdown('</div>', unsafe_allow_html=True)

# ─────────────────────────────────────────────
# ADMIN — MANAGE TAB
# ─────────────────────────────────────────────
def tab_manage():
    st.subheader("🗂 Material Tag Manager")
    data = load_data()

    if not data:
        st.warning("No tags registered yet.")
        return

    search = st.text_input("Search", placeholder="Filter by RFID tag, bin, material...",
                           label_visibility="collapsed")
    tags = {k: v for k, v in data.items()
            if search.lower() in k.lower()
            or search.lower() in v.get("Material", "").lower()
            or search.lower() in v.get("Storage Bin", "").lower()
            or search.lower() in v.get("Material Description", "").lower()}

    st.caption(f"{len(tags)} tags shown")

    for tc, rec in tags.items():
        has_data    = bool(rec.get("Material")) and not rec.get("_cleared")
        status_color = "#34d399" if has_data else "#7a8299"
        status_txt   = "Active" if has_data else "Empty"
        bin_id       = rec.get("Storage Bin", "—")
        mat_desc     = rec.get("Material Description", "(empty)")[:50]

        with st.expander(f"{'●' if has_data else '○'} **Bin {bin_id}** · {mat_desc}"):

            # RFID tag code shown prominently
            st.markdown(f"""
            <div style="background:#0d1a2e;border:1px solid #2a4a7a;border-radius:8px;
                padding:0.6rem 1rem;margin-bottom:0.75rem;">
              <div style="font-size:0.68rem;color:#5a7299;text-transform:uppercase;
                  letter-spacing:1px;">RFID Tag Code (permanent)</div>
              <div style="font-family:monospace;font-size:0.88rem;color:#7ab8f5;
                  word-break:break-all;margin-top:2px;">{tc}</div>
            </div>
            """, unsafe_allow_html=True)

            col1, col2 = st.columns([3, 1])
            with col1:
                st.markdown(f"**Status:** <span style='color:{status_color}'>{status_txt}</span>",
                            unsafe_allow_html=True)
                if has_data:
                    st.markdown(f"**Material:** `{rec.get('Material','—')}`")
                    st.markdown(f"**Stock:** {rec.get('Total Stock','—')} {rec.get('Base Unit of Measure','')}")
                    st.markdown(f"**Batch:** {rec.get('Batch','—')}")
                    st.markdown(f"**GR Date:** {rec.get('GR Date','—')}")
                    st.markdown(f"**SLED/BBD:** {rec.get('SLED/BBD','—')}")
            with col2:
                st.markdown(f"[🔗 View]({tag_url(tc)})")

            st.markdown("---")
            action_cols = st.columns(3)

            with action_cols[0]:
                if st.button("✎ Edit", key=f"edit_{tc}", use_container_width=True):
                    st.session_state[f"editing_{tc}"] = True

            with action_cols[1]:
                if st.button("✕ Clear", key=f"clear_{tc}",
                             use_container_width=True, type="secondary"):
                    data[tc] = {
                        "RFID Tag Code": tc,
                        "_cleared": True,
                        "_cleared_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    save_data(data)
                    st.success(f"Tag cleared. QR code unchanged.")
                    st.rerun()

            with action_cols[2]:
                bin_id_safe = rec.get("Storage Bin", tc[:8])
                qr_img = make_qr_image(tag_url(tc), f"Bin: {bin_id_safe}", tc[:20])
                st.download_button("⬇ QR", data=qr_to_bytes(qr_img),
                                   file_name=f"QR_{bin_id_safe}_{tc[:8]}.png",
                                   mime="image/png",
                                   key=f"qrdl_{tc}", use_container_width=True)

            # Edit form
            if st.session_state.get(f"editing_{tc}"):
                st.markdown("---")
                st.markdown("#### ✎ Edit material data")
                st.caption("RFID Tag Code is fixed. Dropdown fields show all known values.")

                with st.form(key=f"form_{tc}"):
                    new_vals = {"RFID Tag Code": tc}
                    st.markdown(f"**RFID Tag Code (fixed):** `{tc}`")
                    st.markdown("---")
                    col_a, col_b = st.columns(2)
                    field_list = [f for f in EXPECTED_COLS if f != "RFID Tag Code"]
                    for j, field in enumerate(field_list):
                        target = col_a if j % 2 == 0 else col_b
                        current_val = rec.get(field, "")
                        with target:
                            if field in DROPDOWN_FIELDS:
                                options = get_field_options(data, field)
                                CUSTOM = "— type custom value —"
                                choices = options + [CUSTOM]
                                default_idx = (options.index(current_val)
                                               if current_val in options else len(choices) - 1)
                                selected = st.selectbox(field, options=choices,
                                                        index=default_idx,
                                                        key=f"sel_{tc}_{field}")
                                if selected == CUSTOM:
                                    new_vals[field] = st.text_input(
                                        f"Custom {field}",
                                        value=current_val if current_val not in options else "",
                                        key=f"custom_{tc}_{field}",
                                        placeholder=f"Enter {field}...")
                                else:
                                    new_vals[field] = selected
                            else:
                                new_vals[field] = st.text_input(
                                    field, value=current_val, key=f"inp_{tc}_{field}")

                    st.markdown(" ")
                    c_save, c_cancel = st.columns(2)
                    with c_save:
                        submitted = st.form_submit_button(
                            "💾 Save Changes", type="primary", use_container_width=True)
                    with c_cancel:
                        cancelled = st.form_submit_button("✕ Cancel", use_container_width=True)

                    if submitted:
                        new_vals["_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        data[tc] = new_vals
                        save_data(data)
                        st.session_state[f"editing_{tc}"] = False
                        st.success(f"✅ Tag updated!")
                        st.rerun()
                    if cancelled:
                        st.session_state[f"editing_{tc}"] = False
                        st.rerun()

# ─────────────────────────────────────────────
# MAIN ROUTER
# ─────────────────────────────────────────────
def main():
    # Read query params safely
    try:
        tag_param = st.query_params.get("tag", None)
        if tag_param:
            tag_param = str(tag_param).strip()
    except Exception:
        tag_param = None

    # Viewer mode — triggered by ?tag=RFIDTAGCODE
    if tag_param:
        show_viewer(tag_param)
        return

    # Admin mode
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
