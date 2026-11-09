"""Conservative scalar category and integer-bound diagnostics for IEC ST."""

import re
from array import array
from bisect import bisect_left
from collections import defaultdict

from .st_lexer import lex

BOOLEAN_TYPES = {"BOOL"}
NUMERIC_TYPES = {
    "BYTE",
    "WORD",
    "DWORD",
    "LWORD",
    "SINT",
    "INT",
    "DINT",
    "LINT",
    "USINT",
    "UINT",
    "UDINT",
    "ULINT",
    "REAL",
    "LREAL",
}
STRING_TYPES = {"STRING", "WSTRING", "CHAR", "WCHAR"}
TEMPORAL_TYPES = {
    "TIME",
    "LTIME",
    "DATE",
    "LDATE",
    "TOD",
    "LTOD",
    "TIME_OF_DAY",
    "DATE_AND_TIME",
    "DT",
    "LDT",
}
TYPE_FAMILIES = {
    **dict.fromkeys(BOOLEAN_TYPES, "boolean"),
    **dict.fromkeys(NUMERIC_TYPES, "numeric"),
    **dict.fromkeys(STRING_TYPES, "string"),
    **dict.fromkeys(TEMPORAL_TYPES, "temporal"),
}
INTEGER_BOUNDS = {
    "SINT": (-(2**7), 2**7 - 1),
    "USINT": (0, 2**8 - 1),
    "BYTE": (0, 2**8 - 1),
    "INT": (-(2**15), 2**15 - 1),
    "UINT": (0, 2**16 - 1),
    "WORD": (0, 2**16 - 1),
    "DINT": (-(2**31), 2**31 - 1),
    "UDINT": (0, 2**32 - 1),
    "DWORD": (0, 2**32 - 1),
    "LINT": (-(2**63), 2**63 - 1),
    "ULINT": (0, 2**64 - 1),
    "LWORD": (0, 2**64 - 1),
}
NUMERIC_LITERAL = re.compile(
    r"(?:\d[\d_]*(?:\.(?!\.)[\d_]+)?(?:[eE][+-]?\d[\d_]*)?|"
    r"(?:16#[0-9A-Fa-f_]+|8#[0-7_]+|2#[01_]+))\Z"
)
SIMPLE_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
MAX_SCALAR_EXPRESSION_TOKENS = 256
MAX_SCALAR_EXPRESSION_PAREN_DEPTH = 32
INTEGER_DECIMAL_DIGITS = re.compile(r"\d(?:_?\d)*\Z")
INTEGER_RADIX_DIGITS = {
    2: re.compile(r"[01](?:_?[01])*\Z"),
    8: re.compile(r"[0-7](?:_?[0-7])*\Z"),
    16: re.compile(r"[0-9A-Fa-f](?:_?[0-9A-Fa-f])*\Z"),
}
EXPRESSION_PRECEDENCE = {
    "OR": 1,
    "OR_ELSE": 1,
    "XOR": 2,
    "AND": 3,
    "AND_THEN": 3,
    "&": 3,
    "=": 4,
    "<>": 4,
    "<": 4,
    "<=": 4,
    ">": 4,
    ">=": 4,
    "+": 5,
    "-": 5,
    "*": 6,
    "/": 6,
    "MOD": 6,
    "**": 7,
}
COMPARISON_OPERATORS = {"=", "<>", "<", "<=", ">", ">="}
NUMERIC_OPERATORS = {"+", "-", "*", "/", "MOD", "**"}
BOOLEAN_OPERATORS = {"AND", "OR", "XOR", "AND_THEN", "OR_ELSE", "&"}


def _literal_family(token):
    value = token.text
    upper = value.upper()
    if upper in {"TRUE", "FALSE", "BOOL#TRUE", "BOOL#FALSE"}:
        return "boolean"
    if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
        return "string"
    if "#" in upper:
        prefix = upper.split("#", 1)[0]
        family = TYPE_FAMILIES.get(prefix)
        if family:
            return family
    if NUMERIC_LITERAL.fullmatch(value):
        return "numeric"
    return None


def _target_family(data_type, controller_id=None, aliases=None):
    name = str(data_type or "").strip()
    family = TYPE_FAMILIES.get(name.upper())
    if family:
        return family
    if aliases is None:
        return None
    return aliases.get(controller_id, {}).get(name.casefold())


def controller_scalar_alias_families(project, variables):
    """Resolve only unambiguous scalar aliases from complete controller tables."""
    controller_ids = {item.get("controller_id") for item in variables}
    families = {}
    controllers_with_tables = 0
    controllers_with_incomplete_tables = 0
    controllers_without_tables = 0
    resolved_aliases = 0
    ambiguous_names = 0

    for controller_id in sorted(controller_ids, key=lambda value: (value is None, str(value))):
        if not isinstance(controller_id, str) or not controller_id:
            controllers_with_incomplete_tables += 1
            continue
        try:
            inventory = project.data_types(controller_id=controller_id)
        except ValueError:
            controllers_with_incomplete_tables += 1
            continue

        tables = inventory.get("tables", [])
        if not tables:
            controllers_without_tables += 1
            continue
        controllers_with_tables += 1
        if inventory.get("completeness") != "observed" or any(
            table.get("completeness") != "observed" for table in tables
        ):
            controllers_with_incomplete_tables += 1
            continue

        definitions = defaultdict(list)
        for item in inventory.get("items", []):
            name = item.get("name")
            if isinstance(name, str) and SIMPLE_TYPE_NAME.fullmatch(name):
                definitions[name.casefold()].append(item)
        ambiguous_names += sum(len(items) > 1 for items in definitions.values())

        def resolve(name, visiting, definitions=definitions):
            builtin_family = TYPE_FAMILIES.get(name.upper())
            if builtin_family:
                return builtin_family
            key = name.casefold()
            if key in visiting:
                return None
            matches = definitions.get(key, ())
            if len(matches) != 1:
                return None
            definition = matches[0]
            if definition.get("child_count") != 0:
                return None
            base_type = str(definition.get("base_type") or "").strip()
            if not SIMPLE_TYPE_NAME.fullmatch(base_type):
                return None
            return resolve(base_type, visiting | {key})

        controller_families = {}
        for key, matches in definitions.items():
            if len(matches) != 1:
                continue
            family = resolve(matches[0]["name"], set())
            if family:
                controller_families[key] = family
        families[controller_id] = controller_families
        resolved_aliases += len(controller_families)

    status = (
        "not_executed"
        if not controller_ids
        else ("partial" if controllers_with_incomplete_tables else "observed")
    )
    return families, {
        "status": status,
        "controllers_with_tables": controllers_with_tables,
        "controllers_with_incomplete_tables": controllers_with_incomplete_tables,
        "controllers_without_tables": controllers_without_tables,
        "resolved_aliases": resolved_aliases,
        "ambiguous_names": ambiguous_names,
    }


def _literal_family_from_source(source):
    """Classify one complete scalar literal; skip expressions and unknown syntax."""
    result = lex(str(source))
    if result.diagnostics or len(result.tokens) != 1:
        return None
    token = result.tokens[0]
    if token.kind == "keyword" and token.text.upper() in {"TRUE", "FALSE"}:
        return "boolean"
    if token.kind != "literal":
        return None
    return _literal_family(token)


def _integer_literal_value(tokens):
    """Parse only a single integral numeric literal with an optional unary sign."""
    if len(tokens) == 1:
        sign, token = 1, tokens[0]
    elif len(tokens) == 2 and tokens[0].text in {"+", "-"}:
        sign, token = (-1 if tokens[0].text == "-" else 1), tokens[1]
    else:
        return None
    if token.kind != "literal" or _literal_family(token) != "numeric":
        return None
    value = token.text
    prefix, separator, body = value.partition("#")
    typed_sign = 1
    if separator and prefix.upper() in TYPE_FAMILIES:
        if prefix.upper() not in INTEGER_BOUNDS:
            return None
        value = body
        if value.startswith(("+", "-")):
            typed_sign = -1 if value[0] == "-" else 1
            value = value[1:]
    if len(value) > 80:
        return None
    if "#" in value:
        radix, digits = value.split("#", 1)
        base = {"2": 2, "8": 8, "16": 16}.get(radix)
        if base is None or not INTEGER_RADIX_DIGITS[base].fullmatch(digits):
            return None
        number = int(digits.replace("_", ""), base)
    elif INTEGER_DECIMAL_DIGITS.fullmatch(value):
        number = int(value.replace("_", ""))
    else:
        return None
    return number * sign * typed_sign


def _integer_range_mismatch(data_type, tokens):
    bounds = INTEGER_BOUNDS.get(str(data_type or "").strip().upper())
    value = _integer_literal_value(tokens)
    if bounds is None or value is None:
        return None
    minimum, maximum = bounds
    return bounds if value < minimum or value > maximum else None


def _incompatible(target_family, literal_family):
    incompatible = {
        "boolean": {"numeric", "string", "temporal"},
        "numeric": {"boolean", "string"},
        "string": {"boolean", "numeric", "temporal"},
        "temporal": {"boolean", "string"},
    }
    return literal_family in incompatible.get(target_family, set())


def _scalar_expression_family(tokens, variable_family):
    """Infer a bounded scalar result family; return None for unsupported syntax."""
    if not tokens or len(tokens) > MAX_SCALAR_EXPRESSION_TOKENS:
        return None
    depth = 0
    for token in tokens:
        if token.text == "(":
            depth += 1
            if depth > MAX_SCALAR_EXPRESSION_PAREN_DEPTH:
                return None
        elif token.text == ")":
            depth -= 1
            if depth < 0:
                return None
    if depth:
        return None

    position = 0

    def parse_primary():
        nonlocal position
        if position >= len(tokens):
            return None
        token = tokens[position]
        if token.text == "(":
            position += 1
            family = parse_expression(1)
            if position >= len(tokens) or tokens[position].text != ")":
                return None
            position += 1
            return family
        position += 1
        if token.kind == "literal":
            return _literal_family(token)
        if token.kind == "keyword" and token.text.upper() in {"TRUE", "FALSE"}:
            return "boolean"
        if token.kind == "identifier":
            return variable_family(token)
        return None

    def parse_unary():
        nonlocal position
        if position >= len(tokens):
            return None
        operator = tokens[position].text.upper()
        if operator in {"+", "-", "NOT"}:
            position += 1
            operand = parse_unary()
            if operator in {"+", "-"}:
                return "numeric" if operand == "numeric" else None
            return "boolean" if operand == "boolean" else None
        return parse_primary()

    def combine(operator, left, right):
        if left is None or right is None:
            return None
        if operator in COMPARISON_OPERATORS:
            if left != right:
                return None
            if operator in {"=", "<>"} and left in TYPE_FAMILIES.values():
                return "boolean"
            return "boolean" if operator in {"<", "<=", ">", ">="} and left == "numeric" else None
        if operator in NUMERIC_OPERATORS:
            return "numeric" if left == right == "numeric" else None
        if operator in BOOLEAN_OPERATORS:
            return "boolean" if left == right == "boolean" else None
        return None

    def parse_expression(minimum_precedence):
        nonlocal position
        left = parse_unary()
        while position < len(tokens):
            operator = tokens[position].text.upper()
            precedence = EXPRESSION_PRECEDENCE.get(operator)
            if precedence is None or precedence < minimum_precedence:
                break
            position += 1
            next_precedence = precedence if operator == "**" else precedence + 1
            right = parse_expression(next_precedence)
            left = combine(operator, left, right)
        return left

    family = parse_expression(1)
    return family if position == len(tokens) else None


def scalar_type_diagnostics(index, variables=(), *, aliases=None, limit=1000):
    """Report definite category and built-in integer-bound mismatches.

    Direct literals/variables and a bounded expression grammar are checked;
    integer bounds apply only to direct numeric literals and initializers.
    Calls, members, arrays, unsupported operators and unknown types are skipped.
    Diagnostics never include source text, variable names, or literal values.
    """
    occurrences = defaultdict(list)
    aliases = aliases or {}
    for item in index.occurrences:
        if item.get("language") != "ST":
            continue
        occurrences[(item.get("pou_id"), item.get("section_id"), item.get("start"))].append(item)

    observed = {
        (section.get("pou_id"), section.get("id"))
        for section in index.sections
        if section.get("language") == "ST" and section.get("completeness") == "observed"
    }
    diagnostics = []
    omitted = 0
    for field in index.fields:
        scope = (field.get("pou_id"), field.get("section_id"))
        if field.get("language") != "ST" or scope not in observed:
            continue
        source = field.get("source", "")
        result = lex(source)
        if result.diagnostics:
            continue
        tokens = result.tokens
        statement_ends = array("I", (i for i, item in enumerate(tokens) if item.text == ";"))
        for position, token in enumerate(tokens):
            if token.text != ":=" or position < 1 or position + 1 >= len(tokens):
                continue
            target = tokens[position - 1]
            if target.kind != "identifier":
                continue
            candidates = occurrences.get((*scope, target.start), ())
            target_ref = next(
                (
                    item
                    for item in candidates
                    if item.get("access") == "write"
                    and item.get("role") == "variable"
                    and item.get("kind") == "variable"
                    and item.get("variable_id")
                ),
                None,
            )
            if target_ref is None:
                continue
            target_family = _target_family(
                target_ref.get("data_type"), target_ref.get("controller_id"), aliases
            )
            if not target_family:
                continue

            semicolon_index = bisect_left(statement_ends, position + 1)
            if semicolon_index >= len(statement_ends):
                continue
            expression_end = statement_ends[semicolon_index]
            expression_tokens = tokens[position + 1 : expression_end]
            integer_bounds = _integer_range_mismatch(target_ref.get("data_type"), expression_tokens)
            if integer_bounds is not None:
                if len(diagnostics) >= limit:
                    omitted += 1
                    continue
                line = source.count("\n", 0, target.start) + 1
                line_start = source.rfind("\n", 0, target.start) + 1
                diagnostics.append(
                    {
                        "code": "assignment_integer_range_mismatch",
                        "pou_id": field["pou_id"],
                        "section_id": field["section_id"],
                        "variable_id": target_ref["variable_id"],
                        "target_type": target_ref["data_type"],
                        "minimum": integer_bounds[0],
                        "maximum": integer_bounds[1],
                        "certainty": "definite_integer_range_mismatch",
                        "start": target.start,
                        "line": line,
                        "column": target.start - line_start + 1,
                    }
                )
                continue
            if len(expression_tokens) == 1:
                value = expression_tokens[0]
            else:
                value = None

            if value is not None and value.kind == "identifier":
                source_ref = next(
                    (
                        item
                        for item in occurrences.get((*scope, value.start), ())
                        if item.get("access") != "write"
                        and item.get("role") == "variable"
                        and item.get("kind") == "variable"
                        and item.get("variable_id")
                    ),
                    None,
                )
                source_family = (
                    _target_family(
                        source_ref.get("data_type"), source_ref.get("controller_id"), aliases
                    )
                    if source_ref
                    else None
                )
                if (
                    not source_ref
                    or not source_family
                    or not _incompatible(target_family, source_family)
                ):
                    continue
                if len(diagnostics) >= limit:
                    omitted += 1
                    continue
                line = source.count("\n", 0, target.start) + 1
                line_start = source.rfind("\n", 0, target.start) + 1
                diagnostics.append(
                    {
                        "code": "assignment_variable_type_mismatch",
                        "pou_id": field["pou_id"],
                        "section_id": field["section_id"],
                        "target_variable_id": target_ref["variable_id"],
                        "source_variable_id": source_ref["variable_id"],
                        "target_type": target_ref["data_type"],
                        "source_type": source_ref["data_type"],
                        "certainty": "definite_type_category_mismatch",
                        "start": target.start,
                        "line": line,
                        "column": target.start - line_start + 1,
                    }
                )
                continue

            if value is not None:
                is_boolean_keyword = value.kind == "keyword" and value.text.upper() in {
                    "TRUE",
                    "FALSE",
                }
                if value.kind != "literal" and not is_boolean_keyword:
                    continue
                value_family = _literal_family(value)
                if not value_family or not _incompatible(target_family, value_family):
                    continue
                diagnostic_code = "assignment_literal_type_mismatch"
                value_key = "literal_category"
            else:

                def variable_family(expression_token, scope=scope):
                    source_ref = next(
                        (
                            item
                            for item in occurrences.get((*scope, expression_token.start), ())
                            if item.get("access") != "write"
                            and item.get("role") == "variable"
                            and item.get("kind") == "variable"
                            and item.get("variable_id")
                        ),
                        None,
                    )
                    if not source_ref:
                        return None
                    return _target_family(
                        source_ref.get("data_type"), source_ref.get("controller_id"), aliases
                    )

                value_family = _scalar_expression_family(expression_tokens, variable_family)
                if not value_family or not _incompatible(target_family, value_family):
                    continue
                diagnostic_code = "assignment_expression_type_mismatch"
                value_key = "expression_category"
            if len(diagnostics) >= limit:
                omitted += 1
                continue
            line = source.count("\n", 0, target.start) + 1
            line_start = source.rfind("\n", 0, target.start) + 1
            diagnostics.append(
                {
                    "code": diagnostic_code,
                    "pou_id": field["pou_id"],
                    "section_id": field["section_id"],
                    "variable_id": target_ref["variable_id"],
                    "target_type": target_ref["data_type"],
                    value_key: value_family,
                    "certainty": "definite_category_mismatch",
                    "start": target.start,
                    "line": line,
                    "column": target.start - line_start + 1,
                }
            )
    for variable in variables:
        target_type = variable.get("data_type")
        target_family = _target_family(target_type, variable.get("controller_id"), aliases)
        initial_value = variable.get("initial_value")
        if target_family is None or initial_value is None:
            continue
        initializer_tokens = lex(str(initial_value))
        integer_bounds = (
            _integer_range_mismatch(target_type, initializer_tokens.tokens)
            if not initializer_tokens.diagnostics
            else None
        )
        if integer_bounds is not None:
            if len(diagnostics) >= limit:
                omitted += 1
                continue
            line_index = variable.get("line_index")
            diagnostics.append(
                {
                    "code": "initializer_integer_range_mismatch",
                    "variable_id": variable["id"],
                    "table_id": variable["table_id"],
                    "target_type": target_type,
                    "minimum": integer_bounds[0],
                    "maximum": integer_bounds[1],
                    "certainty": "definite_integer_range_mismatch",
                    "start": None,
                    "line": line_index + 1 if isinstance(line_index, int) else None,
                    "column": None,
                }
            )
            continue
        literal_family = _literal_family_from_source(initial_value)
        if not literal_family or not _incompatible(target_family, literal_family):
            continue
        if len(diagnostics) >= limit:
            omitted += 1
            continue
        line_index = variable.get("line_index")
        diagnostics.append(
            {
                "code": "initializer_literal_type_mismatch",
                "variable_id": variable["id"],
                "table_id": variable["table_id"],
                "target_type": target_type,
                "literal_category": literal_family,
                "certainty": "definite_category_mismatch",
                "start": None,
                "line": line_index + 1 if isinstance(line_index, int) else None,
                "column": None,
            }
        )
    diagnostics.sort(
        key=lambda item: (
            item.get("pou_id") or item.get("table_id", ""),
            item.get("section_id") or "",
            item.get("start") if item.get("start") is not None else item.get("line", -1),
            item["code"],
        )
    )
    return diagnostics, omitted
