"""Synthetic GradebookDetails footers shaped like the two real Aeries layouts.

Category names and weights only. Points, percents, and marks are invented, and the
banner name and number are fake, so the probe tests can prove none of them is logged.
"""

import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scraper  # noqa: E402

FAKE_STUDENT = {"name": "Quill Fixturekid", "sn": "9900112", "school_code": "0"}

BANNER = (
    '<table id="ctl00_MainContent_tblStudentInfo"><tr>'
    "<td>Student</td><td>Fixturekid, Quill</td><td>Student #</td><td>9900112</td>"
    "</tr></table>"
)

ASSIGNMENT_LIST = (
    '<table id="ctl00_MainContent_subGBS_tblEverything"><tr>'
    "<th>#</th><th>Description</th><th>Category</th><th>Score</th>"
    "<th>Perc</th><th>Comment</th><th>Due Date</th><th>Grading Complete</th>"
    "</tr></table>"
)

POG_HEADERS = ["Category", "Perc of Grade", "Points", "Max", "Perc", "Mark"]


def _cells(tag, values):
    return "".join(f"<{tag}>{v}</{tag}>" for v in values)


def pog_rows(weights, filled=None):
    """(name, weight) pairs → footer rows with invented scores; names in `filled` have points."""
    filled = set(filled if filled is not None else [n for n, _ in weights])
    rows = []
    for n, (name, weight) in enumerate(weights):
        if name in filled:
            rows.append([name, f"{weight}%", f"{43.25 + n}", "51", "84.8%", "B"])
        else:
            rows.append([name, f"{weight}%", "0", "0", "", ""])
    rows.append(["Total", "", "", "", "86.7%", "B"])
    return rows


def pog_table(weights, filled=None, header="thead", ident='id="ctl00_MainContent_subGBS_tblTotals"'):
    """Percent-of-grade footer. header: thead (th), tbody_th, or td (a td header row)."""
    body = "".join(f"<tr>{_cells('td', r)}</tr>" for r in pog_rows(weights, filled))
    if header == "thead":
        return f"<table {ident}><thead><tr>{_cells('th', POG_HEADERS)}</tr></thead><tbody>{body}</tbody></table>"
    tag = "th" if header == "tbody_th" else "td"
    return f"<table {ident}><tbody><tr>{_cells(tag, POG_HEADERS)}</tr>{body}</tbody></table>"


def sf_headers(summative_weight, formative_weight):
    return [
        "Category",
        "Summative Pts", "Summative Max", f"Summative Perc ({summative_weight}%)",
        "Formative Pts", "Formative Max", f"Formative Perc ({formative_weight}%)",
        "Overall Perc", "Mark",
    ]


def sf_rows(sides):
    """(name, side) pairs with side in summative / formative / both / none."""
    rows = []
    for name, side in sides:
        s = ["43.25", "51", "84.8%"] if side in ("summative", "both") else ["", "", ""]
        f = ["7.5", "9", "83.3%"] if side in ("formative", "both") else ["", "", ""]
        rows.append([name, *s, *f, "", ""])
    rows.append(["Total", "43.25", "51", "84.8%", "7.5", "9", "83.3%", "84.7%", "B"])
    return rows


def sf_table(summative_weight, formative_weight, sides):
    head = _cells("th", sf_headers(summative_weight, formative_weight))
    body = "".join(f"<tr>{_cells('td', r)}</tr>" for r in sf_rows(sides))
    return (
        f'<table id="ctl00_MainContent_subGBS_tblTotals"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def sf_two_row_header_table(summative_weight, formative_weight, sides):
    """Bucket headers span Pts / Max / Perc sub-headers on a second row."""
    top = (
        '<tr><th rowspan="2">Category</th>'
        f'<th colspan="3">Summative ({summative_weight}%)</th>'
        f'<th colspan="3">Formative ({formative_weight}%)</th>'
        '<th rowspan="2">Overall Perc</th><th rowspan="2">Mark</th></tr>'
    )
    sub = "<tr>" + _cells("th", ["Pts", "Max", "Perc"] * 2) + "</tr>"
    body = "".join(f"<tr>{_cells('td', r)}</tr>" for r in sf_rows(sides))
    return f'<table class="GradebookTotals"><thead>{top}{sub}</thead><tbody>{body}</tbody></table>'


def grid_div(headers, rows, ident='id="ctl00_MainContent_subGBS_divTotals" class="k-grid"'):
    """The same footer drawn with role=grid / role=row divs instead of a table."""
    def row(values, cell_class):
        cells = "".join(f'<div class="{cell_class}">{v}</div>' for v in values)
        return f'<div role="row" class="k-grid-row">{cells}</div>'
    head = row(headers, "k-header")
    body = "".join(row(r, "k-cell") for r in rows)
    return f'<div role="grid" {ident}>{head}{body}</div>'


def page(*parts):
    return f"<html><body>{BANNER}{ASSIGNMENT_LIST}{''.join(parts)}</body></html>"


def async_delta(html, panel_id="ctl00_MainContent_subGBS_upEverything"):
    """An ASP.NET UpdatePanel delta response carrying html, like the class-switch postback."""
    return (
        f"1|#||4|{len(html)}|updatePanel|{panel_id}|{html}|"
        "12|hiddenField|__VIEWSTATE|abcdefghijkl|"
    )


def soup(html):
    return BeautifulSoup(html, "html.parser")


def totals_for(html, diagnostics=None):
    return scraper.parse_gradebook_totals(soup(html), diagnostics)


def insight_for(html):
    return scraper.build_counted_insight(totals_for(html))
