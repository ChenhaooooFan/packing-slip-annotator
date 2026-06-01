import streamlit as st
import pdfplumber
import fitz  # PyMuPDF
import pandas as pd
import re
import io
import os
import glob
import urllib.request
from PIL import Image, ImageDraw, ImageFont

# ── Locate a CJK-capable TTF/TTC font ───────────────────────────────────────
@st.cache_resource(show_spinner="加载字体中…")
def get_cjk_font_path() -> str:
    # macOS
    for p in ["/Library/Fonts/Arial Unicode.ttf",
               "/System/Library/Fonts/STHeiti Medium.ttc"]:
        if os.path.exists(p):
            return p
    # Streamlit Cloud / Ubuntu  (packages.txt installs fonts-noto-cjk)
    hits = glob.glob("/usr/share/fonts/**/Noto*CJK*.ttc", recursive=True)
    if hits:
        return hits[0]
    hits = glob.glob("/usr/share/fonts/**/Noto*CJK*.otf", recursive=True)
    if hits:
        return hits[0]
    # Last resort: download a small subset
    dest = "/tmp/NotoSansSC.otf"
    if not os.path.exists(dest):
        urllib.request.urlretrieve(
            "https://github.com/googlefonts/noto-cjk/raw/main"
            "/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf", dest)
    return dest

CJK_FONT_PATH = get_cjk_font_path()


def make_label_image(gift_lines: list[str], page_width_pt: float,
                     line_h_pt: float = 18, font_pt: float = 13) -> bytes:
    """
    Render gift_lines as a yellow PIL image and return PNG bytes.
    Uses a real CJK font → guaranteed correct rendering on all platforms.
    """
    DPI = 150
    SCALE = DPI / 72          # 1 pt → pixels
    LINE_H_PX = max(1, int(line_h_pt * SCALE))
    FONT_PX   = max(1, int(font_pt * SCALE))
    PAD = int(3 * SCALE)

    img_w = int((page_width_pt - 8) * SCALE)
    img_h = len(gift_lines) * LINE_H_PX + PAD * 2

    img = Image.new("RGB", (img_w, img_h), (255, 243, 51))   # yellow
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype(CJK_FONT_PATH, FONT_PX)
    except Exception:
        font = ImageFont.load_default()

    for j, line in enumerate(gift_lines):
        draw.text((PAD, PAD + j * LINE_H_PX), line, font=font, fill=(13, 13, 13))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_warn_image(text: str, page_width_pt: float) -> bytes:
    """Render a single-line orange warning banner as PNG."""
    DPI = 150
    SCALE = DPI / 72
    FONT_PX = int(7 * SCALE)
    PAD = int(2 * SCALE)
    img_w = int(page_width_pt * SCALE)
    img_h = int(14 * SCALE)

    img = Image.new("RGB", (img_w, img_h), (255, 153, 26))   # orange
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(CJK_FONT_PATH, FONT_PX)
    except Exception:
        font = ImageFont.load_default()
    draw.text((PAD, PAD), text, font=font, fill=(60, 20, 0))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

st.set_page_config(page_title="Packing Slip B4 Annotator", layout="centered")
st.title("📦 Packing Slip B4 Annotator")
st.markdown("上传 Packing Slip PDF 和 B4 赠品 CSV，系统自动匹配并标注赠品信息及多件商品提醒。")

col1, col2 = st.columns(2)
with col1:
    pdf_file = st.file_uploader("上传 Packing Slip PDF", type="pdf")
with col2:
    csv_file = st.file_uploader("上传 B4 赠品 CSV", type="csv")

if not (pdf_file and csv_file):
    st.info("请上传 PDF 和 CSV 文件后开始处理。")
    st.stop()

# ── Parse CSV ──────────────────────────────────────────────────────────────
df = pd.read_csv(csv_file, dtype=str).fillna("")
df.columns = [c.lstrip("﻿").strip() for c in df.columns]
# Drop completely empty rows
df = df[df["Order ID"].str.strip() != ""].reset_index(drop=True)

# Each CSV row = 1 gift item (never explode into duplicates).
# Store tracking and all order IDs per row.
df["_track4"] = df["Tracking ID (Last 4 digital)"].str.strip()
df["_has_track"] = df["_track4"] != ""
df["_track4"] = df["_track4"].str.zfill(4).where(df["_has_track"], "")

# Store ALL order IDs for this row as a set (handles multi-ID cells)
df["_order_ids"] = df["Order ID"].str.strip().apply(
    lambda x: {oid.strip() for oid in x.split("\n") if oid.strip()}
)

# Deduplicate: same tracking + same style content = accidental duplicate entry
df = df.drop_duplicates(
    subset=["Tracking ID (Last 4 digital)", "Order ID", "款式 + 库位"]
).reset_index(drop=True)


def match_rows(page_order_id: str, page_track4: str):
    """
    Returns (matched_df, order_id_mismatch: bool).

    Logic:
    1. Match by tracking last-4 (primary, always used when tracking exists in CSV).
    2. If no tracking match → fallback to order_id match (rows without tracking).
    After matching by tracking, check whether page's order_id appears in any
    matched row → if not, flag as mismatch (still annotate, just warn).
    """
    # Priority 1: tracking match
    if page_track4:
        by_track = df[df["_has_track"] & (df["_track4"] == page_track4)]
        if not by_track.empty:
            matched = by_track.reset_index(drop=True)
            # Secondary check: does this page's order_id appear in matched rows?
            order_ok = any(page_order_id in row["_order_ids"]
                           for _, row in matched.iterrows())
            return matched, not order_ok   # mismatch = True when order not found
    # Priority 2: order_id fallback (rows that have no tracking)
    by_order = df[df["_order_ids"].apply(lambda s: page_order_id in s)]
    return by_order.reset_index(drop=True), False

# ── Parse PDF pages ─────────────────────────────────────────────────────────
pdf_bytes = pdf_file.read()

def extract_page_meta(page):
    """Return (order_id, tracking_last4) from a pdfplumber page."""
    text = page.extract_text() or ""
    order_id = None
    tracking_last4 = None

    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "Order ID:" in line:
            m = re.search(r"Order ID:\s*(\d+)", line)
            if m and order_id is None:          # take first occurrence
                order_id = m.group(1).strip()
        if "Tracking number:" in line:
            # The last 4 digits are on the NEXT line
            if i + 1 < len(lines):
                tracking_last4 = lines[i + 1].strip().zfill(4)

    return order_id, tracking_last4


def find_qty_highlight_rects(page):
    """
    Returns a list of fitz.Rect objects for product rows whose Qty != 1.
    Coordinates are in pdfplumber's top-left system (compatible with fitz).
    """
    words = page.extract_words()
    if not words:
        return []

    page_width = page.width

    # Find Qty header position
    qty_header = next((w for w in words if w["text"] == "Qty" and w["top"] < 100), None)
    if qty_header is None:
        return []
    qty_col_x = qty_header["x0"]

    # Find Qty Total: row top → marks end of product rows
    qty_total = next(
        (w for w in words if w["text"] == "Qty" and w["top"] > qty_header["top"] + 5),
        None,
    )
    qty_total_top = qty_total["top"] if qty_total else page.height

    # Collect product-table words (between header row and Qty Total row)
    table_words = [
        w for w in words
        if w["top"] > qty_header["top"] + 2 and w["top"] < qty_total_top - 2
    ]
    if not table_words:
        return []

    # Find Qty column values (words roughly in the Qty column area)
    qty_words = [
        w for w in table_words
        if w["x0"] >= qty_col_x - 10
        and re.fullmatch(r"\d+", w["text"])
    ]

    highlight_rects = []
    for i, qw in enumerate(qty_words):
        if qw["text"] == "1":
            continue

        row_top = qw["top"]
        # Row bottom = top of next qty value, or qty_total_top
        if i + 1 < len(qty_words):
            row_bottom = qty_words[i + 1]["top"]
        else:
            row_bottom = qty_total_top

        # Extend a little so the highlight covers the full text height
        rect = fitz.Rect(0, row_top - 1, page_width, row_bottom - 1)
        highlight_rects.append(rect)

    return highlight_rects


def build_gift_lines(rows):
    """Return list of gift label strings from matched B4 rows."""
    lines = []
    for _, r in rows.iterrows():
        style_loc = r.get("款式 + 库位", "").strip()
        size      = r.get("尺码 (size)", "").strip()
        if style_loc:
            for part in style_loc.split(","):
                part = part.strip()
                if part:
                    lines.append(f"{part}  {size}")
    return lines


def stamp_gift_label(fitz_page, pl_page, gift_lines):
    """
    Render gift text as a PIL image (yellow background) and insert into the
    blank area below existing content. Font size scales to fit available space.
    """
    words = pl_page.extract_words()
    pw  = pl_page.width
    ph  = pl_page.height

    # Footer "Packing Slip" sits at the very bottom ~28 pt
    footer_y = ph - 28
    if words:
        # Bottom of the lowest word above the footer
        content_words = [w for w in words if w["bottom"] < footer_y - 4]
        last_bottom = max((w["bottom"] for w in content_words), default=160)
    else:
        last_bottom = 160

    MARGIN  = 6
    GAP     = 4                    # gap between content and label
    stamp_y = last_bottom + GAP
    available_h = footer_y - stamp_y - 4   # pts of blank space

    n = len(gift_lines)
    # Fit: line_h * n + padding <= available_h
    # line_h is capped: max 20 pt, min 9 pt
    line_h = min(20, max(9, (available_h - 6) / n))
    font_pt = line_h * 0.72        # font size in points
    box_h   = n * line_h + 6

    png = make_label_image(gift_lines, pw - MARGIN * 2,
                           line_h_pt=line_h, font_pt=font_pt)
    img_rect = fitz.Rect(MARGIN, stamp_y, pw - MARGIN, stamp_y + box_h)
    fitz_page.insert_image(img_rect, stream=png)


# ── Annotate PDF ────────────────────────────────────────────────────────────
with st.spinner("正在匹配并标注..."):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    matched_pages = 0
    highlighted_qty_pages = 0
    preview_entries = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as plumber_pdf:

        # ── Pass 1: Qty != 1 highlight on EVERY page ────────────────────
        for page_idx, pl_page in enumerate(plumber_pdf.pages):
            qty_rects = find_qty_highlight_rects(pl_page)
            if qty_rects:
                fitz_page = doc[page_idx]
                for rect in qty_rects:
                    highlighted_qty_pages += 1
                    ann = fitz_page.add_rect_annot(rect)
                    ann.set_colors(stroke=(1, 0.2, 0.2), fill=(1, 0.85, 0.85))
                    ann.set_border(width=1.5)
                    ann.set_opacity(0.6)
                    ann.update()

        # ── Pass 2: B4 gift label + order_id mismatch warn ──────────────
        for page_idx, pl_page in enumerate(plumber_pdf.pages):
            order_id, tracking_last4 = extract_page_meta(pl_page)
            if not order_id or not tracking_last4:
                continue

            matched, order_id_mismatch = match_rows(order_id, tracking_last4)
            if matched.empty:
                continue

            matched_pages += 1
            fitz_page = doc[page_idx]
            pw = pl_page.width
            ph = pl_page.height

            # Check if this page had any Qty warn
            has_qty_warn = bool(find_qty_highlight_rects(pl_page))

            # ── 0. Warn if order_id doesn't align ───────────────────────
            if order_id_mismatch:
                all_b4_ids = set()
                for _, row in matched.iterrows():
                    all_b4_ids |= row["_order_ids"]
                warn_text = (
                    f"⚠ 请核对! Tracking对上但Order ID不一致: "
                    f"PDF={order_id}  B4表={', '.join(sorted(all_b4_ids))}"
                )
                warn_png  = make_warn_image(warn_text, pw)
                warn_rect = fitz.Rect(0, 0, pw, 14)
                fitz_page.insert_image(warn_rect, stream=warn_png)

            # ── 2. Stamp B4 gift info in blank area (yellow, large) ─────
            gift_lines = build_gift_lines(matched)
            if not gift_lines:
                preview_entries.append((page_idx, order_id, [], has_qty_warn, order_id_mismatch))
                continue

            stamp_gift_label(fitz_page, pl_page, gift_lines)
            preview_entries.append((page_idx, order_id, gift_lines, has_qty_warn, order_id_mismatch))

    output = io.BytesIO()
    doc.save(output)

    # Render preview images AFTER saving (so annotations are baked in)
    preview_images = []
    doc2 = fitz.open(stream=output.getvalue(), filetype="pdf")
    for page_idx, order_id, gift_lines, has_qty_warn, order_id_mismatch in preview_entries:
        page = doc2[page_idx]
        mat = fitz.Matrix(2.5, 2.5)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_bytes = pix.tobytes("png")
        preview_images.append((page_idx + 1, order_id, gift_lines, has_qty_warn, order_id_mismatch, img_bytes))
    doc2.close()
    doc.close()

# ── Results ─────────────────────────────────────────────────────────────────
st.success(
    f"✅ 完成！共匹配 **{matched_pages}** 页，其中 **{highlighted_qty_pages}** 处数量 > 1 已高亮提醒。"
)

st.download_button(
    label="⬇️ 下载标注后的 PDF",
    data=output.getvalue(),
    file_name="packing_slip_annotated.pdf",
    mime="application/pdf",
)

# ── Page Previews ────────────────────────────────────────────────────────────
if preview_images:
    st.markdown("---")
    st.subheader(f"📄 已标注页面预览（共 {len(preview_images)} 页）")

    for page_num, order_id, gift_lines, has_qty_warn, order_id_mismatch, img_bytes in preview_images:
        tags = []
        if gift_lines:
            tags.append("🎁 赠品已标注")
        if has_qty_warn:
            tags.append("🔴 数量 > 1 警告")
        if order_id_mismatch:
            tags.append("⚠️ Order ID 不一致")
        tag_str = "　".join(tags)

        with st.expander(f"第 {page_num} 页　Order {order_id}　{tag_str}", expanded=True):
            if order_id_mismatch:
                st.error("⚠️ Order ID 与 B4 表不一致，已按 Tracking 尾号匹配，请人工核对！")
            if gift_lines:
                st.markdown("**赠品：** " + "　·　".join(gift_lines))
            if has_qty_warn:
                st.warning("该订单有商品数量 > 1，请注意多放！")
            st.image(img_bytes, use_container_width=True)

