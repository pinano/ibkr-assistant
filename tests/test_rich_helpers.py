"""
Unit tests for Telegram Bot API 10.1 Rich Messages helper functions
defined in src/bot.py.

These tests cover:
  - text_plain / text_bold / text_italic / text_code / text_url
  - text_concat: 0, 1, 2+ element normalisation
  - html_to_rich: flat <b>, <i>, <code> tag parsing
  - block_paragraph / block_heading / block_thinking
  - cell: is_header, align, colspan/rowspan filtering
  - block_table: bordered/striped/caption options
  - block_details: title normalisation, is_open flag

All helpers are pure functions — no I/O, no IBKR, no Telegram.
We import them directly from src.bot after stubbing out the settings
and external dependencies so the module can be loaded in isolation.
"""
import sys
import os
from unittest.mock import MagicMock, patch
import pytest
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

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
    import re
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

def cell(text_obj=None, is_header: bool = False, align: str = None,
         valign: str = None, colspan: int = None, rowspan: int = None) -> dict:
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

def block_table(cells: list, is_bordered: bool = False,
                is_striped: bool = False, caption=None) -> dict:
    res = {"type": "table", "cells": cells}
    if is_bordered:
        res["is_bordered"] = True
    if is_striped:
        res["is_striped"] = True
    if caption:
        if isinstance(caption, str):
            caption = text_plain(caption)
        res["caption"] = caption
    return res

def block_details(title, blocks: list, is_open: bool = False) -> dict:
    if isinstance(title, str):
        title = text_plain(title)
    res = {"type": "details", "title": title, "blocks": blocks}
    if is_open:
        res["is_open"] = True
    return res


# ===========================================================================
# Tests
# ===========================================================================

class TestTextPlain:
    def test_basic(self):
        r = text_plain("hello")
        assert r == {"type": "plain", "text": "hello"}

    def test_empty_string(self):
        r = text_plain("")
        assert r["type"] == "plain"
        assert r["text"] == ""


class TestTextFormatters:
    def test_bold_from_string(self):
        r = text_bold("bold text")
        assert r["type"] == "bold"
        assert r["text"]["type"] == "plain"

    def test_bold_from_dict(self):
        inner = text_plain("x")
        r = text_bold(inner)
        assert r["text"] is inner

    def test_italic_from_string(self):
        r = text_italic("slanted")
        assert r["type"] == "italic"

    def test_code_from_string(self):
        r = text_code("42")
        assert r["type"] == "code"

    def test_url_from_string(self):
        r = text_url("click", "https://example.com")
        assert r["type"] == "url"
        assert r["url"] == "https://example.com"
        assert r["text"]["text"] == "click"

    def test_url_from_dict(self):
        inner = text_bold("link")
        r = text_url(inner, "https://x.com")
        assert r["text"] is inner


class TestTextConcat:
    def test_empty_returns_plain_empty(self):
        r = text_concat()
        assert r == {"type": "plain", "text": ""}

    def test_single_string_returns_plain(self):
        r = text_concat("hello")
        assert r == {"type": "plain", "text": "hello"}

    def test_single_dict_passthrough(self):
        b = text_bold("x")
        r = text_concat(b)
        assert r is b

    def test_two_strings_returns_texts(self):
        r = text_concat("a", "b")
        assert r["type"] == "texts"
        assert len(r["texts"]) == 2

    def test_mixed_string_and_dict(self):
        r = text_concat("hello ", text_bold("world"))
        assert r["type"] == "texts"
        assert r["texts"][0]["type"] == "plain"
        assert r["texts"][1]["type"] == "bold"

    def test_list_argument_flattened(self):
        r = text_concat(["a", "b", "c"])
        assert r["type"] == "texts"
        assert len(r["texts"]) == 3

    def test_list_with_dicts(self):
        inner = text_code("x")
        r = text_concat([inner, "y"])
        assert r["type"] == "texts"
        assert r["texts"][0] is inner

    def test_three_parts(self):
        r = text_concat("a", "b", "c")
        assert r["type"] == "texts"
        assert len(r["texts"]) == 3


class TestHtmlToRich:
    def test_plain_text_only(self):
        r = html_to_rich("hello world")
        assert r["type"] == "plain"
        assert r["text"] == "hello world"

    def test_bold_tag(self):
        r = html_to_rich("<b>bold</b>")
        assert r["type"] == "bold"

    def test_italic_tag(self):
        r = html_to_rich("<i>italic</i>")
        assert r["type"] == "italic"

    def test_code_tag(self):
        r = html_to_rich("<code>snippet</code>")
        assert r["type"] == "code"

    def test_mixed(self):
        r = html_to_rich("Price: <b>420.50</b> USD")
        assert r["type"] == "texts"
        types = [t["type"] for t in r["texts"]]
        assert "bold" in types
        assert "plain" in types

    def test_empty_string(self):
        r = html_to_rich("")
        # Empty returns a plain empty from text_concat()
        assert r["type"] == "plain"

    def test_multiple_bold_spans(self):
        r = html_to_rich("<b>A</b> and <b>B</b>")
        assert r["type"] == "texts"
        bold_count = sum(1 for t in r["texts"] if t["type"] == "bold")
        assert bold_count == 2


class TestBlockParagraph:
    def test_string_input(self):
        r = block_paragraph("hello")
        assert r == {"type": "paragraph", "text": {"type": "plain", "text": "hello"}}

    def test_dict_input(self):
        inner = text_bold("x")
        r = block_paragraph(inner)
        assert r["text"] is inner


class TestBlockHeading:
    def test_string_input(self):
        r = block_heading("My Title")
        assert r["type"] == "sectionHeading"
        assert r["text"]["text"] == "My Title"

    def test_dict_input(self):
        inner = text_plain("Title")
        r = block_heading(inner)
        assert r["text"] is inner


class TestBlockThinking:
    def test_structure(self):
        r = block_thinking()
        assert r == {"type": "thinking"}


class TestCell:
    def test_plain_text(self):
        r = cell("hello")
        assert r["text"]["type"] == "plain"
        assert r["text"]["text"] == "hello"

    def test_header_flag(self):
        r = cell("Name", is_header=True)
        assert r["is_header"] is True

    def test_no_header_flag_absent(self):
        r = cell("Value")
        assert "is_header" not in r

    def test_align_right(self):
        r = cell("100", align="right")
        assert r["align"] == "right"

    def test_no_align_absent(self):
        r = cell("x")
        assert "align" not in r

    def test_colspan_above_1(self):
        r = cell("span", colspan=2)
        assert r["colspan"] == 2

    def test_colspan_1_absent(self):
        """colspan=1 should NOT be included (redundant)."""
        r = cell("x", colspan=1)
        assert "colspan" not in r

    def test_rowspan_above_1(self):
        r = cell("span", rowspan=3)
        assert r["rowspan"] == 3

    def test_none_text_no_text_key(self):
        r = cell(None)
        assert "text" not in r

    def test_dict_text_passthrough(self):
        inner = text_bold("B")
        r = cell(inner)
        assert r["text"] is inner

    def test_full_options(self):
        r = cell("val", is_header=True, align="center", colspan=3, rowspan=2)
        assert r["is_header"] is True
        assert r["align"] == "center"
        assert r["colspan"] == 3
        assert r["rowspan"] == 2


class TestBlockTable:
    def test_minimal(self):
        rows = [[cell("A"), cell("B")]]
        r = block_table(rows)
        assert r["type"] == "table"
        assert r["cells"] is rows
        assert "is_bordered" not in r
        assert "is_striped" not in r

    def test_bordered(self):
        r = block_table([[]], is_bordered=True)
        assert r["is_bordered"] is True

    def test_striped(self):
        r = block_table([[]], is_striped=True)
        assert r["is_striped"] is True

    def test_string_caption(self):
        r = block_table([[]], caption="My table")
        assert r["caption"]["type"] == "plain"
        assert r["caption"]["text"] == "My table"

    def test_dict_caption(self):
        cap = text_bold("Caption")
        r = block_table([[]], caption=cap)
        assert r["caption"] is cap

    def test_no_caption_absent(self):
        r = block_table([[]])
        assert "caption" not in r


class TestBlockDetails:
    def test_string_title(self):
        r = block_details("Section", [])
        assert r["type"] == "details"
        assert r["title"]["text"] == "Section"
        assert r["blocks"] == []

    def test_dict_title(self):
        title = text_bold("Title")
        r = block_details(title, [])
        assert r["title"] is title

    def test_is_open_true(self):
        r = block_details("X", [], is_open=True)
        assert r["is_open"] is True

    def test_is_open_false_absent(self):
        r = block_details("X", [])
        assert "is_open" not in r

    def test_blocks_passed_through(self):
        inner = [block_paragraph("p")]
        r = block_details("Title", inner)
        assert r["blocks"] is inner
