import streamlit as st
import pdfplumber
import fitz  # PyMuPDF
import pandas as pd
import re
import io
import os
import urllib.request

# ── CJK font: use bundled NotoSansCJK if Arial Unicode not present ──────────
_LOCAL_FONT = "/Library/Fonts/Arial Unicode.ttf"
_FALLBACK_FONT_URL = (
    "https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/SimplifiedChinese/"
    "NotoSansCJKsc-Regular.otf"
)
_FALLBACK_FONT_PATH = "/tmp/NotoSansCJKsc-Regular.otf"

def _get_cjk_font():
    if os.path.exists(_LOCAL_FONT):
        return _LOCAL_FONT
    if not os.path.exists(_FALLBACK_FONT_PATH):
        with st.spinner("正在下载中文字体（首次运行）..."):
            urllib.request.urlretrieve(_FALLBACK_FONT_URL, _FALLBACK_FONT_PATH)
    return _FALLBACK_FONT_PATH

CJK_FONT = _get_cjk_font()

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

# Normalize keys — some cells have multiple order IDs separated by newlines
# Explode so each order ID gets its own row
df["_track4"] = df["Tracking ID (Last 4 digital)"].str.strip().str.zfill(4)
df["_order_raw"] = df["Order ID"].str.strip()
# Expand multi-order rows
expanded_rows = []
for _, row in df.iterrows():
    for oid in row["_order_raw"].split("\n"):
        oid = oid.strip()
        if oid:
            r = row.copy()
            r["_order"] = oid
            expanded_rows.append(r)
df = pd.DataFrame(expanded_rows).reset_index(drop=True)

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
    Draw a yellow-highlighted large text block in the blank space
    below all existing content on the page.
    """
    words = pl_page.extract_words()
    if words:
        # Find the lowest content on the page (excluding the tiny footer)
        footer_top = pl_page.height - 35   # "Packing Slip" footer region
        content_words = [w for w in words if w["bottom"] < footer_top]
        last_bottom = max((w["bottom"] for w in content_words), default=160)
    else:
        last_bottom = 160

    pw = pl_page.width
    ph = pl_page.height
    footer_y = ph - 35

    # Available blank height
    available = footer_y - last_bottom - 8
    if available < 20:
        last_bottom = footer_y - len(gift_lines) * 22 - 16

    stamp_x = 6
    stamp_y = last_bottom + 8
    line_h = min(22, max(14, available / max(len(gift_lines), 1) - 2))
    font_size = line_h * 0.78

    box_h = len(gift_lines) * line_h + 10
    box_rect = fitz.Rect(stamp_x - 2, stamp_y - 2,
                         pw - stamp_x + 2, stamp_y + box_h)

    # Yellow highlight background
    shape = fitz_page.new_shape()
    shape.draw_rect(box_rect)
    shape.finish(fill=(1.0, 0.95, 0.2), color=(0.85, 0.7, 0.0), width=1.5)
    shape.commit()

    # Large bold text with CJK font
    for j, line in enumerate(gift_lines):
        y = stamp_y + j * line_h + line_h * 0.8
        fitz_page.insert_text(
            fitz.Point(stamp_x + 2, y),
            line,
            fontsize=font_size,
            fontfile=CJK_FONT,
            color=(0.1, 0.1, 0.1),
        )


# ── Annotate PDF ────────────────────────────────────────────────────────────
with st.spinner("正在匹配并标注..."):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    matched_pages = 0
    highlighted_qty_pages = 0
    preview_entries = []   # list of (page_num_1based, order_id, gift_lines, has_qty_warn)

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as plumber_pdf:
        for page_idx, pl_page in enumerate(plumber_pdf.pages):
            order_id, tracking_last4 = extract_page_meta(pl_page)
            if not order_id or not tracking_last4:
                continue

            # Match: tracking last 4 first, then confirm with order ID
            subset = df[df["_track4"] == tracking_last4]
            if subset.empty:
                continue
            matched = subset[subset["_order"] == order_id]
            if matched.empty:
                matched = subset  # fallback: tracking only

            if matched.empty:
                continue

            matched_pages += 1
            fitz_page = doc[page_idx]
            pw = pl_page.width
            ph = pl_page.height

            # ── 1. Highlight rows where Qty != 1 ────────────────────────
            qty_rects = find_qty_highlight_rects(pl_page)
            has_qty_warn = False
            for rect in qty_rects:
                highlighted_qty_pages += 1
                has_qty_warn = True
                ann = fitz_page.add_rect_annot(rect)
                ann.set_colors(stroke=(1, 0.2, 0.2), fill=(1, 0.85, 0.85))
                ann.set_border(width=1.5)
                ann.set_opacity(0.6)
                ann.update()

            # ── 2. Stamp B4 gift info in blank area (yellow, large) ─────
            gift_lines = build_gift_lines(matched)
            if not gift_lines:
                preview_entries.append((page_idx, order_id, [], has_qty_warn))
                continue

            stamp_gift_label(fitz_page, pl_page, gift_lines)
            preview_entries.append((page_idx, order_id, gift_lines, has_qty_warn))

    output = io.BytesIO()
    doc.save(output)

    # Render preview images AFTER saving (so annotations are baked in)
    preview_images = []
    doc2 = fitz.open(stream=output.getvalue(), filetype="pdf")
    for page_idx, order_id, gift_lines, has_qty_warn in preview_entries:
        page = doc2[page_idx]
        mat = fitz.Matrix(2.5, 2.5)   # 2.5× zoom → ~190 DPI for A5
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_bytes = pix.tobytes("png")
        preview_images.append((page_idx + 1, order_id, gift_lines, has_qty_warn, img_bytes))
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

    for page_num, order_id, gift_lines, has_qty_warn, img_bytes in preview_images:
        tags = []
        if gift_lines:
            tags.append("🎁 赠品已标注")
        if has_qty_warn:
            tags.append("🔴 数量 > 1 警告")
        tag_str = "　".join(tags)

        with st.expander(f"第 {page_num} 页　Order {order_id}　{tag_str}", expanded=True):
            # Summary badges
            if gift_lines:
                st.markdown("**赠品：** " + "　·　".join(gift_lines))
            if has_qty_warn:
                st.warning("该订单有商品数量 > 1，请注意多放！")
            st.image(img_bytes, use_container_width=True)

