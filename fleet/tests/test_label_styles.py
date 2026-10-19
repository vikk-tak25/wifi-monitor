"""Print-label styles must not leak into the dashboard's KPI captions (#13)."""

import re

from fleet.app import STATIC_DIR


def selectors(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    found = []
    for block in re.findall(r"([^{}]+)\{", css):
        found.extend(s.strip() for s in block.split(","))
    return found


def test_no_rule_styles_the_bare_label_class():
    # KPI captions are <div class="label"> inside .kpi; only ".kpi .label" may style them.
    offenders = [s for s in selectors((STATIC_DIR / "styles.css").read_text())
                 if re.search(r"(^|[\s>+~])\.label(?![\w-])", s) and s != ".kpi .label"]
    assert offenders == []


def test_printable_labels_use_their_own_class():
    js = (STATIC_DIR / "labels.js").read_text()
    assert 'label.className = "unit-label"' in js
    assert ".unit-label" in (STATIC_DIR / "styles.css").read_text()
