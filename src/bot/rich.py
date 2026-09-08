import html
import logging
import re
from typing import Union, Optional, Any
from aiogram import types
from aiogram.methods.base import TelegramMethod
from pydantic import Field

from src.config import settings
from src.bot.core import bot

logger = logging.getLogger("ibkr-bot")

# ---------------------------------------------------------------------------
# Telegram Bot API 10.1 Rich Messages Helper Functions
# ---------------------------------------------------------------------------


def text_plain(t: str) -> dict:
    return {"type": "plain", "text": t}


def text_bold(t) -> dict:
    if isinstance(t, str):
        t = text_plain(t)
    return {"type": "bold", "text": t}


def text_italic(t) -> dict:
    if isinstance(t, str):
        t = text_plain(t)
    return {"type": "italic", "text": t}


def text_code(t) -> dict:
    if isinstance(t, str):
        t = text_plain(t)
    return {"type": "code", "text": t}


def text_url(t, url: str) -> dict:
    if isinstance(t, str):
        t = text_plain(t)
    return {"type": "url", "text": t, "url": url}


def text_concat(*args) -> dict:
    processed = []
    for arg in args:
        if isinstance(arg, str):
            processed.append(text_plain(arg))
        elif isinstance(arg, dict):
            processed.append(arg)
        elif isinstance(arg, list):
            processed.extend([text_plain(x) if isinstance(x, str) else x for x in arg])
    if not processed:
        return text_plain("")
    if len(processed) == 1:
        return processed[0]
    return {"type": "texts", "texts": processed}


def html_to_rich(html_text: str) -> dict:
    pattern = re.compile(r'(<b>.*?</b>|<i>.*?</i>|<code>.*?</code>|[^<]+|<)')
    tokens = pattern.findall(html_text)
    spans = []
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("<b>") and tok.endswith("</b>"):
            spans.append(text_bold(tok[3:-4]))
        elif tok.startswith("<i>") and tok.endswith("</i>"):
            spans.append(text_italic(tok[3:-4]))
        elif tok.startswith("<code>") and tok.endswith("</code>"):
            spans.append(text_code(tok[6:-7]))
        else:
            spans.append(text_plain(tok))
    return text_concat(*spans)


def block_paragraph(text_obj) -> dict:
    if isinstance(text_obj, str):
        text_obj = text_plain(text_obj)
    return {"type": "paragraph", "text": text_obj}


def block_heading(text_obj) -> dict:
    if isinstance(text_obj, str):
        text_obj = text_plain(text_obj)
    return {"type": "sectionHeading", "text": text_obj}


def block_thinking() -> dict:
    return {"type": "thinking"}


def cell(
    text_obj=None,
    is_header: bool = False,
    align: str = None,
    valign: str = None,
    colspan: int = None,
    rowspan: int = None
) -> dict:
    res = {}
    if text_obj is not None:
        if isinstance(text_obj, str):
            text_obj = text_plain(text_obj)
        res["text"] = text_obj
    if is_header:
        res["is_header"] = True
    if align:
        res["align"] = align
    if valign:
        res["valign"] = valign
    if colspan and colspan > 1:
        res["colspan"] = colspan
    if rowspan and rowspan > 1:
        res["rowspan"] = rowspan
    return res


def block_table(
    cells: list[list[dict]],
    is_bordered: bool = False,
    is_striped: bool = False,
    caption=None
) -> dict:
    res = {
        "type": "table",
        "cells": cells
    }
    if is_bordered:
        res["is_bordered"] = True
    if is_striped:
        res["is_striped"] = True
    if caption:
        if isinstance(caption, str):
            caption = text_plain(caption)
        res["caption"] = caption
    return res


def block_details(title, blocks: list[dict], is_open: bool = False) -> dict:
    if isinstance(title, str):
        title = text_plain(title)
    res = {
        "type": "details",
        "title": title,
        "blocks": blocks
    }
    if is_open:
        res["is_open"] = True
    return res


# ---------------------------------------------------------------------------
# Telegram Bot API Custom Method Handlers
# ---------------------------------------------------------------------------

class SendRichMessage(TelegramMethod[types.Message]):
    __returning__ = types.Message
    __api_method__ = "sendRichMessage"

    chat_id: Union[int, str] = Field(..., alias="chat_id")
    rich_message: dict = Field(..., alias="rich_message")
    reply_markup: Optional[Any] = Field(None, alias="reply_markup")


class EditMessageTextRich(TelegramMethod[Union[types.Message, bool]]):
    __returning__ = Union[types.Message, bool]
    __api_method__ = "editMessageText"

    chat_id: Optional[Union[int, str]] = Field(None, alias="chat_id")
    message_id: Optional[int] = Field(None, alias="message_id")
    inline_message_id: Optional[str] = Field(None, alias="inline_message_id")
    rich_message: dict = Field(..., alias="rich_message")
    reply_markup: Optional[Any] = Field(None, alias="reply_markup")


def text_to_html(text_obj) -> str:
    if isinstance(text_obj, str):
        return html.escape(text_obj)
    if isinstance(text_obj, dict):
        t_type = text_obj.get("type")
        if t_type == "plain":
            return html.escape(text_obj.get("text", ""))
        elif t_type == "bold":
            return f"<b>{text_to_html(text_obj.get('text'))}</b>"
        elif t_type == "italic":
            return f"<i>{text_to_html(text_obj.get('text'))}</i>"
        elif t_type == "code":
            return f"<code>{text_to_html(text_obj.get('text'))}</code>"
        elif t_type == "url":
            url = html.escape(text_obj.get("url", ""))
            return f'<a href="{url}">{text_to_html(text_obj.get("text"))}</a>'
        elif t_type == "texts":
            return "".join(text_to_html(x) for x in text_obj.get("texts", []))
    if isinstance(text_obj, list):
        return "".join(text_to_html(x) for x in text_obj)
    return ""


def block_to_html(block_obj) -> str:
    if isinstance(block_obj, dict):
        b_type = block_obj.get("type")
        if b_type == "paragraph":
            return f"<p>{text_to_html(block_obj.get('text'))}</p>"
        elif b_type == "sectionHeading":
            return f"<h1>{text_to_html(block_obj.get('text'))}</h1>"
        elif b_type == "thinking":
            return "<tg-thinking></tg-thinking>"
        elif b_type == "table":
            attrs = []
            if block_obj.get("is_bordered"):
                attrs.append('border="1"')
            attrs_str = " " + " ".join(attrs) if attrs else ""

            caption_html = ""
            if "caption" in block_obj:
                caption_html = f"<caption>{text_to_html(block_obj.get('caption'))}</caption>"

            rows_html = []
            for row in block_obj.get("cells", []):
                row_cells = []
                for c in row:
                    tag = "th" if c.get("is_header") else "td"
                    c_attrs = []
                    if c.get("align"):
                        c_attrs.append(f'align="{c.get("align")}"')
                    if c.get("valign"):
                        c_attrs.append(f'valign="{c.get("valign")}"')
                    if c.get("colspan"):
                        c_attrs.append(f'colspan="{c.get("colspan")}"')
                    if c.get("rowspan"):
                        c_attrs.append(f'rowspan="{c.get("rowspan")}"')
                    c_attrs_str = " " + " ".join(c_attrs) if c_attrs else ""
                    cell_text = text_to_html(c.get("text")) if "text" in c else ""
                    row_cells.append(f"<{tag}{c_attrs_str}>{cell_text}</{tag}>")
                rows_html.append(f"<tr>{''.join(row_cells)}</tr>")

            return f"<table{attrs_str}>{caption_html}{''.join(rows_html)}</table>"
        elif b_type == "details":
            open_attr = " open" if block_obj.get("is_open") else ""
            summary_html = f"<summary>{text_to_html(block_obj.get('title'))}</summary>"
            content_html = "".join(block_to_html(b) for b in block_obj.get("blocks", []))
            return f"<details{open_attr}>{summary_html}{content_html}</details>"
    return ""


# ---------------------------------------------------------------------------
# Message Delivery & Admin Notification Helpers
# ---------------------------------------------------------------------------

async def send_rich_message(chat_id: int, blocks: list[dict], reply_markup=None) -> types.Message:
    html_content = "".join(block_to_html(b) for b in blocks)
    return await bot(
        SendRichMessage(
            chat_id=chat_id,
            rich_message={"html": html_content},
            reply_markup=reply_markup
        )
    )


async def edit_message_to_rich(chat_id: int, message_id: int, blocks: list[dict], reply_markup=None) -> types.Message:
    html_content = "".join(block_to_html(b) for b in blocks)
    return await bot(
        EditMessageTextRich(
            chat_id=chat_id,
            message_id=message_id,
            rich_message={"html": html_content},
            reply_markup=reply_markup
        )
    )


async def notify_admins(text: str, parse_mode: str = "Markdown"):
    for chat_id in settings.allowed_ids_list:
        try:
            await bot.send_message(chat_id, text, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Failed to notify admin {chat_id}: {e}")


async def notify_admins_rich(blocks: list[dict], reply_markup=None):
    for chat_id in settings.allowed_ids_list:
        try:
            await send_rich_message(chat_id, blocks, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Failed to notify admin {chat_id} with rich message: {e}")
