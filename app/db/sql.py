"""SQL dialect glue between the existing SQLite stores and Postgres (Mk13).

The stores are written with sqlite's `?` placeholders. Rather than fork ~1500 lines of
store code per backend, Phase 2 runs the *same* statements on both: `qmark_to_format`
rewrites `?` to psycopg's `%s` (and escapes literal `%`) so one code path serves both.
"""

from __future__ import annotations


def qmark_to_format(sql: str) -> str:
    """Rewrite sqlite `?` placeholders to psycopg `%s`, escaping literal `%` as `%%`.

    Placeholders inside single-quoted strings, double-quoted identifiers and `--` comments
    are left alone (a `?` there is text, not a parameter); a `%` anywhere is doubled because
    psycopg treats it as a format character whenever parameters are passed.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            end = _skip_quoted(sql, i, ch)
            out.append(sql[i:end].replace("%", "%%"))
            i = end
        elif ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(sql[i:end].replace("%", "%%"))
            i = end
        elif ch == "?":
            out.append("%s")
            i += 1
        elif ch == "%":
            out.append("%%")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    """Index just past the quoted run that starts at `start` (doubled quote = escaped quote)."""
    i = start + 1
    n = len(sql)
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n  # unterminated: treat the rest as quoted text
