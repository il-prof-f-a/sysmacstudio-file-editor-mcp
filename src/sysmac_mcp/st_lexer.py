"""Bounded IEC ST lexer with exact spans; no execution or compiler claim."""

from __future__ import annotations

import re
from dataclasses import dataclass

TYPES = set(
    "BOOL BYTE WORD DWORD LWORD SINT INT DINT LINT USINT UINT UDINT ULINT REAL LREAL STRING WSTRING TIME LTIME DATE LDATE TIME_OF_DAY TOD LTOD DATE_AND_TIME DT LDT T D".split()
)
KEYWORDS = (TYPES - {"T", "D"}) | set(
    "TRUE FALSE IF THEN ELSIF ELSE END_IF CASE OF END_CASE FOR TO BY DO END_FOR WHILE END_WHILE REPEAT UNTIL END_REPEAT EXIT CONTINUE RETURN AND OR XOR NOT MOD AND_THEN OR_ELSE VAR VAR_INPUT VAR_OUTPUT VAR_IN_OUT VAR_EXTERNAL VAR_GLOBAL VAR_TEMP VAR_ACCESS END_VAR CONSTANT RETAIN NON_RETAIN AT ARRAY STRUCT END_STRUCT TYPE END_TYPE FUNCTION END_FUNCTION FUNCTION_BLOCK END_FUNCTION_BLOCK PROGRAM END_PROGRAM EN ENO NULL REF_TO REFERENCE TO END_NAMESPACE NAMESPACE USING".split()
)
NUMBER = re.compile(
    r"(?:16#[0-9A-Fa-f_]+|8#[0-7_]+|2#[01_]+|\d[\d_]*(?:\.(?!\.)[\d_]+)?(?:[eE][+-]?\d[\d_]*)?)"
)
DURATION = re.compile(r"[+-]?(?:\d[\d_]*(?:\.[\d_]+)?(?:ms|us|ns|d|h|m|s))+", re.I)
DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
CLOCK = re.compile(r"\d{1,2}:\d{1,2}:\d{1,2}(?:\.\d+)?")
DATE_TIME = re.compile(DATE.pattern + "-" + CLOCK.pattern)
MAX_SOURCE = 4 * 1024 * 1024
MAX_TOKENS = 500_000


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(slots=True)
class LexResult:
    tokens: list[Token]
    diagnostics: list[dict]


def lex(source: str) -> LexResult:
    tokens, diagnostics = [], []
    if len(source) > MAX_SOURCE:
        return LexResult([], [{"code": "source_limit", "start": 0}])
    i, length = 0, len(source)
    while i < length:
        if len(tokens) >= MAX_TOKENS:
            diagnostics.append({"code": "token_limit", "start": i})
            break
        start = i
        if source[i].isspace():
            i += 1
            continue
        if source.startswith("//", i):
            end = source.find("\n", i)
            i = length if end < 0 else end + 1
            continue
        if source.startswith("(*", i):
            depth, i = 1, i + 2
            while i < length and depth:
                if source.startswith("(*", i):
                    depth, i = depth + 1, i + 2
                elif source.startswith("*)", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                diagnostics.append({"code": "unterminated_comment", "start": start})
            continue
        kind = "symbol"
        if source[i] in "'\"":
            quote, i, closed = source[i], i + 1, False
            while i < length:
                if source[i] == "$":
                    i += 2
                elif source[i] == quote:
                    if i + 1 < length and source[i + 1] == quote:
                        i += 2
                    else:
                        i, closed = i + 1, True
                        break
                else:
                    i += 1
            i = min(i, length)
            if not closed:
                diagnostics.append({"code": "unterminated_string", "start": start})
            kind = "literal"
        elif source[i] == "%":
            address = re.match(r"%[IQM][XBWDL]?(?:\d+(?:\.\d+)*|\*)?", source[i:], re.I)
            if address:
                i += address.end()
                kind = "literal"
            else:
                i += 1
                diagnostics.append({"code": "unsupported_direct_address", "start": start})
        elif source[i].isalpha() or source[i] == "_":
            i += 1
            while i < length and (source[i].isalnum() or source[i] == "_"):
                i += 1
            word = source[start:i].upper()
            kind = "keyword" if word in KEYWORDS else "identifier"
            if word in TYPES and i < length and source[i] == "#":
                i += 1
                pattern = (
                    DURATION
                    if word in {"T", "TIME", "LTIME"}
                    else DATE
                    if word in {"D", "DATE", "LDATE"}
                    else DATE_TIME
                    if word in {"DT", "LDT", "DATE_AND_TIME"}
                    else CLOCK
                    if word in {"TOD", "LTOD", "TIME_OF_DAY"}
                    else NUMBER
                )
                numeric_start = (
                    i + 1 if pattern is NUMBER and i < length and source[i] in "+-" else i
                )
                match = pattern.match(source, numeric_start)
                boolean = (
                    re.match(r"(?:TRUE|FALSE)\b", source[i:], re.I) if word == "BOOL" else None
                )
                if boolean:
                    i += boolean.end()
                elif match:
                    i = match.end()
                elif word not in {"STRING", "WSTRING"} or i >= length or source[i] not in "'\"":
                    diagnostics.append({"code": "invalid_typed_literal", "start": start})
                kind = "literal"
        elif source[i].isdigit():
            match = NUMBER.match(source, i)
            i = match.end()
            kind = "literal"
            if i < length and (source[i].isalpha() or source[i] == "_"):
                diagnostics.append({"code": "invalid_numeric_literal", "start": start})
        else:
            pair = source[i : i + 2]
            i += 2 if pair in {":=", "=>", "::", "<>", "<=", ">=", "**", ".."} else 1
            if source[start:i] in {"{", "}", "\x00"}:
                diagnostics.append({"code": "unsupported_source_token", "start": start})
        tokens.append(Token(kind, source[start:i], start, i))
    return LexResult(tokens, diagnostics)
