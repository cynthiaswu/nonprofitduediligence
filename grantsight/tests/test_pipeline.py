"""Offline tests. Every network call is stubbed.

The most important assertions here are the negative ones: an unresolved field
must stay None and must surface as "not determined", never as a zero and never
as a finding against the organization.
"""

from __future__ import annotations

import io
import json
import random
import sys
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diligence import brief as brief_mod, irs_status, metrics, render  # noqa: E402
from diligence.normalize import coverage_report, introspect, normalize_filings  # noqa: E402
from diligence.propublica import clean_ein, format_ein  # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "diligence/fixtures/sample_org.json").read_text()
)


def _filings():
    return normalize_filings(FIXTURE["filings_with_data"], "521693387")


def test_ein_cleaning():
    assert clean_ein("52-1693387") == "521693387"
    assert clean_ein(" 52 1693387 ") == "521693387"
    assert format_ein("521693387") == "52-1693387"
    for bad in ("123", "", "not-an-ein"):
        try:
            clean_ein(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse as an EIN")


def test_filings_sorted_newest_first():
    years = [f.fiscal_year for f in _filings()]
    assert years == sorted(years, reverse=True), years


def test_resolved_fields_carry_provenance():
    latest = _filings()[0]
    assert latest.get("total_revenue") == 2140000
    assert latest.source_of("total_revenue") == "totrevenue"
    assert latest.form_type == "990"


def test_unresolved_field_is_none_not_zero():
    """A field absent from the filing must be None, never 0.0."""
    latest = _filings()[0]
    assert latest.get("grants_paid") is None
    assert latest.get("public_support_170") is None
    computed = metrics.compute(_filings())
    assert computed.public_support_ratio is None


def test_ratios_computed_where_inputs_exist():
    computed = metrics.compute(_filings(), today=date(2026, 9, 10))
    assert computed.net_assets == 370000
    assert computed.months_of_reserve is not None
    assert 1.7 < computed.months_of_reserve < 1.9
    assert computed.revenue_change is not None
    assert computed.revenue_change < -0.35


def test_flags_fire_on_real_conditions():
    computed = metrics.compute(_filings(), today=date(2026, 9, 10))
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    flags = metrics.evaluate(_filings(), computed, status, today=date(2026, 9, 10))
    titles = [f.title for f in flags]
    assert any("reserve" in t.lower() for t in titles), titles
    assert any("revenue decline" in t.lower() for t in titles), titles
    assert any("consecutive" in t.lower() for t in titles), titles
    # No program-ratio flag, because the ratio never resolved.
    assert not any("program expense" in t.lower() for t in titles), titles


def test_revocation_is_critical_and_leads():
    computed = metrics.compute(_filings())
    status = irs_status.ExemptStatus(
        state=irs_status.REVOKED,
        revocation_date="05/15/2024",
        detail="Exemption auto-revoked effective 05/15/2024.",
        in_pub78=False,
    )
    flags = metrics.evaluate(_filings(), computed, status)
    assert flags[0].severity == metrics.CRITICAL
    assert "revoked" in flags[0].title.lower()


def test_unknown_status_never_reads_as_clean():
    computed = metrics.compute(_filings())
    status = irs_status.ExemptStatus()  # index not built
    flags = metrics.evaluate(_filings(), computed, status)
    assert any("not checked" in f.title.lower() for f in flags)
    assert any("good standing" in g for g in metrics.gaps(_filings(), computed, status))


def test_990n_only_org_is_context_not_a_finding():
    status = irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, epostcard_years=[2024, 2023], detail=""
    )
    computed = metrics.compute([])
    flags = metrics.evaluate([], computed, status)
    assert len(flags) == 1
    assert flags[0].severity == metrics.CONTEXT
    assert "not a finding against it" in flags[0].detail


def test_org_with_no_data_at_all():
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    flags = metrics.evaluate([], metrics.compute([]), status)
    assert flags[0].severity == metrics.CONTEXT
    assert "fiscally sponsored" in flags[0].detail


def test_coverage_and_introspection():
    coverage = coverage_report(_filings())
    assert coverage["total_revenue"] is True
    report = introspect(FIXTURE["filings_with_data"][0])
    assert "totrevenue" in report["matched"]
    assert "noemplyeesw3cnt" in report["matched"]


def test_functional_expense_split_is_not_a_mappable_field():
    """It is absent from the SOI extract entirely -- every Part IX element is
    the total column. Claiming it 'did not resolve' would imply a fixable bug."""
    from diligence.normalize import FIELD_MAP, NOT_IN_EXTRACT
    assert "program_expenses" not in FIELD_MAP
    assert "management_expenses" not in FIELD_MAP
    assert "column (A)" in NOT_IN_EXTRACT["program_expenses"]
    computed = metrics.compute(_filings())
    assert computed.program_expense_ratio is None


def test_brief_and_html_render(monkeypatch):
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: FIXTURE)
    monkeypatch.setattr(
        brief_mod.irs_status,
        "check",
        lambda ein: irs_status.ExemptStatus(
            state=irs_status.CLEAR, in_pub78=True,
            detail="Not on the IRS Auto-Revocation List.",
            index_built_at="2026-09-01T00:00:00",
        ),
    )
    result = brief_mod.build("52-1693387", include_narrative=True)

    assert result.name.startswith("HARBOR STREET")
    assert result.ein == "52-1693387"
    assert result.severity == metrics.WATCH
    assert "Exempt status current" in result.verdict
    assert result.gaps and any("Schedule B" in g for g in result.gaps)

    html = render.brief_page(result)
    assert "Harbor" in html or "HARBOR" in html
    assert "What this brief could not determine" in html
    assert "ProPublica" in html
    assert "n/d" in html  # program expenses render as not-determined
    assert json.dumps(result.to_dict())  # JSON route stays serializable


def test_search_page_renders():
    html = render.search_page("feeding", candidates=[
        {"ein": 363673599, "name": "FEEDING AMERICA", "city": "CHICAGO", "state": "IL"}
    ])
    assert "FEEDING AMERICA" in html
    assert "36-3673599" in html


# ---------------------------------------------------------------------------
# Exemption status. These are the tests that matter most: a failure in this
# module is the difference between "verified in good standing" and a guess.
# ---------------------------------------------------------------------------

def test_date_parser_accepts_every_documented_layout():
    """The IRS data dictionary says MM/DD/YYYY; a widely used community loader
    implies DD-Mon-YYYY. Both must parse, or revoked orgs read as clean."""
    from datetime import date as _date
    for text, expected in [
        ("05/15/2024", _date(2024, 5, 15)),
        ("15-May-2024", _date(2024, 5, 15)),
        ("2024-05-15", _date(2024, 5, 15)),
        ("15-May-2024 ", _date(2024, 5, 15)),
    ]:
        assert irs_status.parse_date(text) == expected, text
    for junk in ("", None, "   ", "MARCH THE 5TH 2020", "not a date"):
        assert irs_status.parse_date(junk) is None, junk


def test_date_layout_labels_unrecognized_input():
    assert irs_status.date_layout("05/15/2024") == "MM/DD/YYYY"
    assert irs_status.date_layout("15-May-2024") == "DD-Mon-YYYY"
    assert irs_status.date_layout("MARCH THE 5TH 2020") == "UNRECOGNIZED"
    assert irs_status.date_layout("") is None


def _offline_irs(tmp_path, monkeypatch):
    """Point irs_status at a temp dir and make every download fail."""
    monkeypatch.setattr(irs_status, "DATA_DIR", tmp_path)
    monkeypatch.setattr(irs_status, "DB_PATH", tmp_path / "irs.sqlite3")
    monkeypatch.setattr(irs_status, "_fetch", lambda key, url, local: local)
    monkeypatch.setattr(irs_status, "discover_urls", lambda: {})


def _build_tiny_index(tmp_path, rows, monkeypatch):
    """Index a handful of rows in a temp dir and return the module."""
    src = tmp_path / "data-download-revocation.txt"
    src.write_text("\n".join(rows), encoding="latin-1")
    _offline_irs(tmp_path, monkeypatch)
    irs_status.build_index({"revocation": src})
    return irs_status


def test_revoked_reinstated_and_clear_round_trip(tmp_path, monkeypatch):
    rows = [
        "521693387|REVOKED ORG|D|1 ST|BALTIMORE|MD|21230|US|03|05/15/2024|08/12/2024|",
        "999888777|REINSTATED ORG|D|1 ST|ALBANY|NY|12201|US|03|01/15/2019|04/10/2019|11/01/2020",
    ]
    s = _build_tiny_index(tmp_path, rows, monkeypatch)

    revoked = s.check("521693387")
    assert revoked.state == s.REVOKED
    assert revoked.revocation_date == "05/15/2024"

    reinstated = s.check("999888777")
    assert reinstated.state == s.REINSTATED
    assert reinstated.reinstatement_date == "11/01/2020"

    assert s.check("111111111").state == s.CLEAR


def test_unparseable_date_reports_unknown_not_clear(tmp_path, monkeypatch):
    """Schema drift must degrade to 'cannot verify', never to 'in good standing'."""
    rows = ["521693387|ORG|D|1 ST|X|IL|62701|US|03|MARCH THE 5TH 2020|same||"]
    s = _build_tiny_index(tmp_path, rows, monkeypatch)
    result = s.check("521693387")
    assert result.state == s.UNKNOWN
    assert "could not be parsed" in result.detail


def test_empty_revocation_table_reports_unknown(tmp_path, monkeypatch):
    """A failed download must not make every organization look clean."""
    _offline_irs(tmp_path, monkeypatch)
    report = irs_status.build_index({})
    assert report["counts"].get("revocation", 0) == 0
    result = irs_status.check("521693387")
    assert result.state == irs_status.UNKNOWN
    assert "empty" in result.detail.lower()


def test_double_quotes_in_names_do_not_swallow_following_rows(tmp_path, monkeypatch):
    """The live e-Postcard file has `"` inside names. Default csv quoting
    treats one as an opening quote and merges every following row into a
    single field until the limit blows -- the build crashed and the revoked
    organizations after it were never indexed."""
    rows = [
        '111111111|PONTIAN SOCIETY "PANAGIA SOUMELA" INC|D|1 ST|X|MA|02118|US|03|05/15/2024|08/12/2024|',
        "222222222|PLAIN ORG|D|1 ST|X|MD|21230|US|03|05/15/2024|08/12/2024|",
    ]
    s = _build_tiny_index(tmp_path, rows, monkeypatch)
    assert s.check("111111111").state == s.REVOKED
    assert s.check("222222222").state == s.REVOKED

    epostcard = tmp_path / "data-download-epostcard.txt"
    epostcard.write_text(
        '333333333|2021|ORG|T|F|01-01-2021|12-31-2021|ORG "NICK" INC|X|1 ST\n'
        "444444444|2022|ORG|T|F|01-01-2022|12-31-2022||X|1 ST\n",
        encoding="latin-1",
    )
    report = s.build_index({"revocation": tmp_path / "data-download-revocation.txt",
                            "epostcard": epostcard})
    assert report["counts"]["epostcard"] == 2
    assert s.check("444444444").epostcard_years == [2022]


def test_missing_index_reports_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(irs_status, "DB_PATH", tmp_path / "does-not-exist.sqlite3")
    assert irs_status.check("521693387").state == irs_status.UNKNOWN


def test_verify_rejects_a_truncated_list(tmp_path, monkeypatch):
    rows = [
        f"{100000000+i}|ORG {i}|D|1 ST|X|IL|62701|US|03|05/15/2024|05/15/2024|"
        for i in range(50)
    ]
    s = _build_tiny_index(tmp_path, rows, monkeypatch)
    ok, messages = s.verify()
    assert ok is False
    assert any("truncated" in m for m in messages)


def test_stale_index_surfaces_as_a_finding():
    status = irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail="", stale=True,
        index_age_days=97, index_built_at="2026-06-01T00:00:00",
    )
    flags = metrics.evaluate(_filings(), metrics.compute(_filings()), status)
    assert any("not current" in f.title for f in flags)


def test_deductibility_label_maps_pub78_codes():
    assert irs_status.ExemptStatus(pub78_deductibility="PC").deductibility_label == "public charity"
    assert irs_status.ExemptStatus(pub78_deductibility="PF").deductibility_label == "private foundation"
    assert irs_status.ExemptStatus(pub78_deductibility="ZZZ").deductibility_label == "ZZZ"
    assert irs_status.ExemptStatus().deductibility_label is None


# ---------------------------------------------------------------------------
# 990-N filers. ProPublica does not index organizations under $50,000 in
# receipts, which is a large share of what a small funder looks at. Before
# these tests existed, the 990-N handling was unreachable: fetch_organization
# raised NotFound before the IRS data was ever consulted.
# ---------------------------------------------------------------------------

def _postcard_status(**kwargs):
    base = dict(
        state=irs_status.CLEAR, in_pub78=True, pub78_deductibility="PC",
        epostcard_years=[2024, 2023, 2022],
        detail="Not on the IRS Auto-Revocation List.",
        index_built_at="2026-09-01T00:00:00", index_age_days=10,
    )
    base.update(kwargs)
    return irs_status.ExemptStatus(**base)


def test_990n_filer_gets_a_brief_not_a_404(monkeypatch):
    from diligence.propublica import NotFound

    def absent(ein):
        raise NotFound("not in ProPublica's index")

    monkeypatch.setattr(brief_mod, "fetch_organization", absent)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda e: _postcard_status())
    monkeypatch.setattr(brief_mod.irs_status, "lookup_name",
                        lambda e: "RIVERBEND MUTUAL AID")

    result = brief_mod.build("30-0112233")
    assert result.source == "irs-only"
    assert result.name == "RIVERBEND MUTUAL AID"
    assert "Exempt status current" in result.verdict
    assert "990-N postcard" in result.verdict
    assert result.severity == metrics.CONTEXT      # small is not a red flag
    assert any("Any financial figure" in g for g in result.gaps)

    html = render.brief_page(result)
    assert "IRS Tax Exempt Organization Search" in html
    assert "projects.propublica.org/nonprofits/organizations" not in html
    assert "Reported financials" not in html


def test_990n_filer_that_was_revoked_still_reads_critical(monkeypatch):
    from diligence.propublica import NotFound

    monkeypatch.setattr(brief_mod, "fetch_organization",
                        lambda e: (_ for _ in ()).throw(NotFound("absent")))
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda e: _postcard_status(
        state=irs_status.REVOKED, revocation_date="15-May-2024", in_pub78=False,
        detail="Exemption auto-revoked effective 15-May-2024 with no reinstatement date on file.",
    ))
    monkeypatch.setattr(brief_mod.irs_status, "lookup_name", lambda e: "LAPSED ORG")

    result = brief_mod.build("30-0112233")
    assert result.severity == metrics.CRITICAL
    assert "revoked" in result.verdict.lower()


def test_absent_everywhere_still_raises_not_found(monkeypatch):
    """No ProPublica record and no IRS record means no brief to build."""
    from diligence.propublica import NotFound

    monkeypatch.setattr(brief_mod, "fetch_organization",
                        lambda e: (_ for _ in ()).throw(NotFound("absent")))
    monkeypatch.setattr(brief_mod.irs_status, "check",
                        lambda e: irs_status.ExemptStatus())
    monkeypatch.setattr(brief_mod.irs_status, "lookup_name", lambda e: None)
    try:
        brief_mod.build("30-0112233")
    except NotFound as exc:
        assert "IRS exemption files" in str(exc)
    else:
        raise AssertionError("should have raised NotFound")


def test_irs_only_brief_survives_a_missing_name(monkeypatch):
    from diligence.propublica import NotFound

    monkeypatch.setattr(brief_mod, "fetch_organization",
                        lambda e: (_ for _ in ()).throw(NotFound("absent")))
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda e: _postcard_status())
    monkeypatch.setattr(brief_mod.irs_status, "lookup_name", lambda e: None)
    result = brief_mod.build("30-0112233")
    assert "30-0112233" in result.name
    assert any("legal name" in g for g in result.gaps)
    render.brief_page(result)   # must not raise


def test_lookup_name_reads_pub78_then_revocation(tmp_path, monkeypatch):
    rows = ["521693387|REVOKED ORG INC|D|1 ST|X|MD|21230|US|03|05/15/2024|08/12/2024|"]
    s = _build_tiny_index(tmp_path, rows, monkeypatch)
    assert s.lookup_name("521693387") == "REVOKED ORG INC"
    assert s.lookup_name("111111111") is None
    assert s.known_to_irs("521693387") is True
    assert s.known_to_irs("111111111") is False


# ---------------------------------------------------------------------------
# Cross-form normalization. Every element name below comes from the published
# IRS SOI extract data dictionaries. These tests exist because the first
# version of this map was guessed and was wrong for 990-EZ and 990-PF.
# ---------------------------------------------------------------------------

def _load(name):
    return json.loads(
        (Path(__file__).parent.parent / f"diligence/fixtures/{name}.json").read_text()
    )


EZ = _load("sample_ez")
PF = _load("sample_pf")


def test_990ez_uses_its_own_element_names():
    f = normalize_filings(EZ["filings_with_data"], "843311902")[0]
    assert f.form_type == "990-EZ"
    assert f.get("total_revenue") == 184000
    assert f.source_of("total_revenue") == "totrevnue"      # not totrevenue
    assert f.source_of("total_expenses") == "totexpns"      # not totfuncexpns
    assert f.source_of("contributions") == "totcntrbs"      # not totcntrbgfts
    assert f.source_of("net_assets") == "totnetassetsend"
    assert f.get("program_revenue") == 31000


def test_990pf_uses_its_own_element_names():
    f = normalize_filings(PF["filings_with_data"], "136164271")[0]
    assert f.form_type == "990-PF"
    assert f.is_private_foundation
    assert f.source_of("total_revenue") == "totrcptperbks"
    assert f.source_of("total_expenses") == "totexpnspbks"
    assert f.source_of("net_assets") == "tfundnworth"
    assert f.source_of("grants_paid") == "contrpdpbks"
    assert f.get("grants_approved_future") == 1250000


def test_summed_candidates_add_their_members():
    """990-PF has no single investment income element; it is interest plus
    dividends, and provenance must record both."""
    f = normalize_filings(PF["filings_with_data"], "136164271")[0]
    assert f.get("investment_income") == 310000 + 1640000
    assert f.source_of("investment_income") == "intrstrvnue + dividndsamt"


def test_a_real_zero_stays_zero():
    """grscontrgifts is 0 on this foundation. Zero is data, not absence."""
    f = normalize_filings(PF["filings_with_data"], "136164271")[0]
    assert f.get("contributions") == 0.0
    assert f.source_of("contributions") == "grscontrgifts"


def test_same_metric_across_all_three_forms():
    forms = {
        normalize_filings(FIXTURE["filings_with_data"], "1")[0].form_type,
        normalize_filings(EZ["filings_with_data"], "2")[0].form_type,
        normalize_filings(PF["filings_with_data"], "3")[0].form_type,
    }
    assert forms == {"990", "990-EZ", "990-PF"}
    for raw, ein in ((FIXTURE, "1"), (EZ, "2"), (PF, "3")):
        f = normalize_filings(raw["filings_with_data"], ein)[0]
        for metric in ("total_revenue", "total_expenses", "total_assets",
                       "total_liabilities", "net_assets"):
            assert f.get(metric) is not None, f"{f.form_type} missing {metric}"


def test_introspect_finds_nothing_unmapped_in_the_fixtures():
    """A non-empty unmapped list on a real filing is now a regression signal."""
    for raw in (FIXTURE, EZ, PF):
        report = introspect(raw["filings_with_data"][0])
        assert report["unmapped"] == [], report["unmapped"]


# --- derived measures that the corrected map unlocked ----------------------

def test_public_support_test_measures_concentration():
    filings = normalize_filings(EZ["filings_with_data"], "843311902")
    m = metrics.compute(filings)
    assert m.public_support_ratio is not None
    assert abs(m.public_support_ratio - 548000 / 612000) < 1e-9
    assert m.public_support_basis == "170(b)(1)(A)(vi)"
    assert m.donor_concentration is not None
    assert abs(m.donor_concentration - 41000 / 612000) < 1e-9


def test_low_public_support_raises_a_finding():
    raw = json.loads(json.dumps(EZ["filings_with_data"]))
    raw[0]["pubsupplesspct170"] = 120000      # ~20% of 612000
    filings = normalize_filings(raw, "843311902")
    m = metrics.compute(filings)
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    titles = [f.title for f in metrics.evaluate(filings, m, status)]
    assert any("one-third test" in t for t in titles), titles


def test_revenue_trend_uses_the_whole_window():
    filings = normalize_filings(EZ["filings_with_data"], "843311902")
    m = metrics.compute(filings)
    assert m.revenue_trend == "growing"
    assert m.trend_span == 3
    assert m.revenue_cagr is not None and m.revenue_cagr > 0.15


def test_declining_trend_is_a_watch_not_just_context():
    filings = normalize_filings(FIXTURE["filings_with_data"], "521693387")
    m = metrics.compute(filings)
    assert m.revenue_trend == "declining"
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    flags = {f.title: f.severity for f in metrics.evaluate(filings, m, status)}
    declining = [t for t in flags if "declining across" in t]
    assert declining and flags[declining[0]] == metrics.WATCH


def test_termination_answer_is_critical():
    """The filing asks directly whether the organization ceased operations.
    Better liveness evidence than a stale filing date, with no name-collision risk."""
    raw = json.loads(json.dumps(FIXTURE["filings_with_data"]))
    raw[0]["ceaseoperationscd"] = "Y"
    filings = normalize_filings(raw, "521693387")
    assert filings[0].flag("terminated") is True
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    flags = metrics.evaluate(filings, metrics.compute(filings), status)
    assert flags[0].severity == metrics.CRITICAL
    assert "termination" in flags[0].title.lower()


def test_no_answer_on_termination_is_not_a_finding():
    filings = normalize_filings(FIXTURE["filings_with_data"], "521693387")
    assert filings[0].flag("terminated") is None
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    titles = [f.title for f in metrics.evaluate(filings, metrics.compute(filings), status)]
    assert not any("termination" in t.lower() for t in titles)


def test_church_classification_explains_absent_filings():
    raw = json.loads(json.dumps(EZ["filings_with_data"]))
    raw[0]["nonpfrea"] = 1
    filings = normalize_filings(raw, "843311902")
    assert filings[0].nonpf_reason == "church"
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    flags = metrics.evaluate(filings, metrics.compute(filings), status)
    church = [f for f in flags if "church" in f.title.lower()]
    assert church and "not evidence of dormancy" in church[0].detail


def test_gaps_distinguish_absent_from_unresolved():
    filings = normalize_filings(FIXTURE["filings_with_data"], "521693387")
    m = metrics.compute(filings)
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    text = " ".join(metrics.gaps(filings, m, status))
    assert "column (A)" in text            # permanent absence, explained
    assert "Schedule J" in text


# ---------------------------------------------------------------------------
# Peer percentiles. The previous implementation sampled a dozen organizations
# from one page of search results, called it a percentile, and had no tests.
# This builds the population locally and counts.
# ---------------------------------------------------------------------------

def _peer_sources(tmp_path, n=400):
    """Synthesize an SOI extract (space-delimited, header row) and a BMF CSV."""
    import random
    random.seed(11)

    soi = tmp_path / "soi.dat"
    with soi.open("w") as fh:
        fh.write("elf EIN tax_pd totrevenue totfuncexpns totassetsend\n")
        for i in range(n):
            revenue = 10_000 * (i + 1)
            fh.write(f"E {100000000 + i} 202312 {revenue} {revenue - 500} {revenue * 2}\n")

    bmf = tmp_path / "bmf.csv"
    with bmf.open("w") as fh:
        fh.write("EIN,NAME,STATE,NTEE_CD\n")
        for i in range(n):
            state = "MD" if i % 2 == 0 else "NY"
            fh.write(f"{100000000 + i},ORG {i},{state},O{20 + i % 5}\n")
    return soi, bmf


def _build_peers(tmp_path, monkeypatch, n=400):
    from diligence import peers
    soi, bmf = _peer_sources(tmp_path, n)
    monkeypatch.setattr(peers, "DATA_DIR", tmp_path)
    monkeypatch.setattr(peers, "DB_PATH", tmp_path / "peers.sqlite3")
    # Production expects a six-figure population; these fixtures are small.
    monkeypatch.setattr(peers, "MIN_POPULATION_FOR_VERIFY", 100)
    report = peers.build_index([str(soi)], [str(bmf)])
    return peers, report


def test_peer_index_joins_soi_to_bmf(tmp_path, monkeypatch):
    peers, report = _build_peers(tmp_path, monkeypatch)
    assert report["soi"] == 400
    assert report["bmf"] == 400
    assert report["joined"] == 400          # every EIN appears in both
    ok, messages = peers.verify()
    assert ok, messages


def test_percentile_is_counted_not_sampled(tmp_path, monkeypatch):
    peers, _ = _build_peers(tmp_path, monkeypatch)
    org = {"ntee_code": "O20", "state": "MD"}

    # Revenues are 10k, 20k ... 4,000k across 400 orgs, half in MD.
    low = peers.compare(org, 20_000)
    high = peers.compare(org, 3_900_000)
    assert low and high
    assert low.percentile < 10
    assert high.percentile > 90
    assert low.population == high.population == 200      # MD only
    assert low.state == "MD"
    assert low.median_revenue is not None
    assert low.p25 < low.median_revenue < low.p75


def test_thin_state_falls_back_to_national(tmp_path, monkeypatch):
    peers, _ = _build_peers(tmp_path, monkeypatch)
    result = peers.compare({"ntee_code": "O20", "state": "WY"}, 500_000)
    assert result is not None
    assert result.state is None
    assert "nationally" in result.scope
    assert result.population == 400


def test_peer_comparison_unavailable_without_an_index(tmp_path, monkeypatch):
    from diligence import peers
    monkeypatch.setattr(peers, "DB_PATH", tmp_path / "absent.sqlite3")
    assert peers.compare({"ntee_code": "O20", "state": "MD"}, 100_000) is None


def test_peer_comparison_needs_an_ntee_code_and_a_revenue(tmp_path, monkeypatch):
    peers, _ = _build_peers(tmp_path, monkeypatch)
    assert peers.compare({"ntee_code": "", "state": "MD"}, 100_000) is None
    assert peers.compare({"ntee_code": "O20", "state": "MD"}, None) is None


def test_missing_propublica_ntee_falls_back_to_bmf_sector(tmp_path, monkeypatch):
    peers, _ = _build_peers(tmp_path, monkeypatch)
    result = peers.compare({"ein": "10-0000007", "ntee_code": None, "state": "NY"}, 500_000)
    assert result is not None and result.sector == "O"


def test_why_missing_names_the_reason(tmp_path, monkeypatch):
    peers, _ = _build_peers(tmp_path, monkeypatch)
    assert peers.why_missing({"ntee_code": "O20"}, None) is None
    no_sector = peers.why_missing({"ein": "999999999", "ntee_code": ""}, 1.0)
    assert "NTEE" in no_sector
    thin = peers.why_missing({"ntee_code": "Z99", "state": "MD"}, 1.0)
    assert "Fewer than" in thin
    monkeypatch.setattr(peers, "DB_PATH", tmp_path / "absent.sqlite3")
    assert "not built" in peers.why_missing({"ntee_code": "O20"}, 1.0)


def test_tiny_population_is_refused_not_reported(tmp_path, monkeypatch):
    """Below the minimum, say nothing rather than quote a percentile of five."""
    peers, _ = _build_peers(tmp_path, monkeypatch, n=8)
    assert peers.compare({"ntee_code": "O20", "state": "MD"}, 40_000) is None
    ok, messages = peers.verify()
    assert ok is False
    assert any("too small" in m or "not large enough" in m for m in messages)


# ---------------------------------------------------------------------------
# Form 990 XML. This is where the functional expense split, named officers,
# Schedule J detail and Schedule I grants actually live -- none of them are in
# the SOI extract.
# ---------------------------------------------------------------------------

from diligence import xml990  # noqa: E402

XML_DIR = Path(__file__).parent.parent / "diligence/fixtures"


def _xml(name="sample_990"):
    return xml990.parse(XML_DIR / f"{name}.xml")


def test_functional_expense_split_comes_out_of_the_xml():
    """The ratio the SOI extract cannot give at any effort."""
    exp = _xml().expenses
    assert exp.complete
    assert exp.total == 2480000
    assert exp.program == 1810000
    assert exp.management == 452000
    assert exp.fundraising == 218000
    assert abs(exp.ratio - 1810000 / 2480000) < 1e-9
    assert abs(exp.overhead_ratio - (452000 + 218000) / 2480000) < 1e-9


def test_named_officers_with_titles_hours_and_pay():
    people = _xml().people
    assert [p.name for p in people][:2] == ["Dana Whitfield", "Marcus Oyelaran"]
    ed = people[0]
    assert ed.title == "EXECUTIVE DIRECTOR"
    assert ed.hours == 40.0
    assert ed.roles == ["officer"]
    assert ed.reportable_comp == 142000
    assert ed.other_comp == 18500
    assert ed.total_comp == 160500


def test_people_are_sorted_by_compensation():
    comps = [p.total_comp for p in _xml().people]
    assert comps == sorted(comps, reverse=True)


def test_schedule_j_detail_merges_onto_the_right_person():
    ed = _xml().people[0]
    assert ed.base_comp == 132000
    assert ed.bonus_comp == 10000
    assert ed.deferred_comp == 6500
    assert ed.benefits == 12000
    assert ed.total_comp_schedule_j == 160500
    # Someone absent from Schedule J keeps None rather than zero.
    assert _xml().people[1].total_comp_schedule_j is None


def test_unpaid_board_members_are_represented_as_unpaid():
    chair = next(p for p in _xml().people if p.title == "BOARD CHAIR")
    assert chair.total_comp == 0
    assert chair.is_unpaid
    assert chair.roles == ["trustee or director"]


def test_institutional_trustee_reads_as_an_organization():
    trustee = next(p for p in _xml().people if p.is_institution)
    assert trustee.name == "CHESAPEAKE TRUST COMPANY"
    assert "institutional trustee" in trustee.roles


def test_schedule_i_grants_with_recipient_eins():
    grants = _xml().grants
    assert len(grants) == 2
    assert grants[0].recipient == "PATTERSON PARK YOUTH SPORTS"   # largest first
    assert grants[0].ein == "522345678"
    assert grants[0].total == 135000
    assert grants[0].purpose == "Equipment and coaching stipends"
    assert grants[1].cash == 75000 and grants[1].non_cash is None


def test_header_fields_and_provenance():
    x = _xml()
    assert x.ein == "521693387"
    assert x.tax_year == 2023
    assert x.form_type == "990"
    assert x.website == "www.harborstreetyouth.org"
    assert x.principal_officer == "Dana Whitfield"
    assert x.employee_count == 24
    assert x.provenance["functional_expenses"] == "TotalFunctionalExpensesGrp"
    assert x.provenance["people"] == "Form990PartVIISectionAGrp"


def test_legacy_schema_names_still_parse():
    """Pre-2013 filings use different element names throughout. Candidate
    fallback is the reason this works without a second parser."""
    x = _xml("sample_990_legacy")
    assert x.expenses.complete
    assert x.expenses.program == 640000
    assert x.provenance["functional_expenses"] == "TotalFunctionalExpenses"
    assert len(x.people) == 1
    person = x.people[0]
    assert person.name == "Eleanor Prybylski"
    assert person.title == "PRESIDENT"
    assert person.total_comp == 97000
    assert person.roles == ["officer"]


def test_namespaces_do_not_matter():
    raw = (XML_DIR / "sample_990.xml").read_text()
    rehomed = raw.replace('xmlns="http://www.irs.gov/efile"', 'xmlns="urn:other"')
    assert xml990.parse(rehomed).expenses.program == 1810000
    stripped = raw.replace(' xmlns="http://www.irs.gov/efile"', "")
    assert xml990.parse(stripped).expenses.program == 1810000


def test_missing_schedules_are_absent_not_zero():
    x = _xml("sample_990_legacy")
    assert x.grants == []
    assert x.has_schedule_i is False
    assert x.has_schedule_j is False
    assert x.people[0].base_comp is None


def test_malformed_xml_raises_rather_than_returning_empty():
    import pytest
    with pytest.raises(Exception):
        xml990.parse("<Return><unclosed>")


def test_xml_fills_the_program_ratio_and_closes_that_gap():
    filings = normalize_filings(FIXTURE["filings_with_data"], "521693387")
    x = _xml()
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")

    without = metrics.compute(filings)
    assert without.program_expense_ratio is None
    assert any("column (A)" in g for g in metrics.gaps(filings, without, status))

    with_xml = metrics.compute(filings, xml=x)
    assert with_xml.program_expense_ratio is not None
    text = metrics.gaps(filings, with_xml, status, xml=x)
    assert not any("column (A)" in g for g in text)
    assert not any("Named officers" in g for g in text)


def test_low_program_ratio_fires_only_with_the_xml():
    filings = normalize_filings(FIXTURE["filings_with_data"], "521693387")
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    raw = (XML_DIR / "sample_990.xml").read_text().replace(
        "<ProgramServicesAmt>1810000</ProgramServicesAmt>",
        "<ProgramServicesAmt>1200000</ProgramServicesAmt>")
    lean = xml990.parse(raw)
    m = metrics.compute(filings, xml=lean)
    titles = [f.title for f in metrics.evaluate(filings, m, status)]
    assert any("Program expense ratio" in t for t in titles)


def test_brief_renders_people_and_grants(monkeypatch):
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: FIXTURE)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail="", index_built_at="2026-09-01T00:00:00"))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: _xml())

    result = brief_mod.build("52-1693387")
    assert result.xml is not None
    assert result.metrics.program_expense_ratio is not None

    html = render.brief_page(result)
    assert "Officers, directors and key employees" in html
    assert "Dana Whitfield" in html
    assert "EXECUTIVE DIRECTOR" in html
    assert "Schedule J" in html
    assert "Grants made" in html
    assert "PATTERSON PARK YOUTH SPORTS" in html
    assert "/brief?ein=522345678" in html      # recipients link to their own brief
    assert json.dumps(result.to_dict(), default=str)


def test_brief_without_xml_still_builds(monkeypatch):
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: FIXTURE)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: None)
    result = brief_mod.build("52-1693387")
    assert result.xml is None
    assert result.metrics.program_expense_ratio is None
    html = render.brief_page(result)
    assert "Officers, directors and key employees" not in html
    assert "column (A)" in html      # gap stated, with the reason


def test_xml_failure_degrades_to_a_stated_error(monkeypatch):
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: FIXTURE)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for",
                        lambda ein: (_ for _ in ()).throw(OSError("corpus offline")))
    result = brief_mod.build("52-1693387")
    assert result.xml is None
    assert any("XML unavailable" in err for err in result.errors)


def test_filer_name_wins_over_preparer_firm_name():
    """The preparer firm's BusinessName comes first in the return header."""
    xml = (
        '<Return xmlns="http://www.irs.gov/efile"><ReturnHeader>'
        "<PreparerFirmGrp><PreparerFirmEIN>814234542</PreparerFirmEIN>"
        "<PreparerFirmName><BusinessNameLine1Txt>BPM LLP</BusinessNameLine1Txt>"
        "</PreparerFirmName></PreparerFirmGrp>"
        "<Filer><EIN>510187791</EIN><BusinessName>"
        "<BusinessNameLine1Txt>MISSION ECONOMIC DEVELOPMENT AGENCY</BusinessNameLine1Txt>"
        "</BusinessName></Filer></ReturnHeader><ReturnData><IRS990/></ReturnData></Return>"
    )
    parsed = xml990.parse(xml)
    assert parsed.name == "MISSION ECONOMIC DEVELOPMENT AGENCY"
    assert parsed.ein == "510187791"


# --- index build and batch-zip retrieval ------------------------------------

INDEX_HEADER = "RETURN_ID,FILING_TYPE,EIN,TAX_PERIOD,SUB_DATE,TAXPAYER_NAME,RETURN_TYPE,DLN,OBJECT_ID,XML_BATCH_ID\n"


def _xml_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(xml990, "DATA_DIR", tmp_path)
    monkeypatch.setattr(xml990, "XML_DB", tmp_path / "xml_index.sqlite3")
    monkeypatch.setattr(xml990, "BATCH_DB", tmp_path / "xml_batches.sqlite3")
    monkeypatch.setattr(xml990, "XML_CORPUS", tmp_path / "corpus")
    monkeypatch.setattr(xml990, "XML_CACHE", tmp_path / "cache")
    monkeypatch.setattr(xml990, "OBJECT_URL", None)


def test_build_index_keeps_batch_id_and_takes_several_sources(monkeypatch, tmp_path):
    _xml_paths(monkeypatch, tmp_path)
    a = tmp_path / "index_2025.csv"
    a.write_text(INDEX_HEADER
                 + "1,EFILE,510187791,202412,2025-05-01,MEDA,990,x,202513219349325391,2025_TEOS_XML_11D\n"
                 + "2,EFILE,510187791,202412,2025-03-01,MEDA,990T,x,202503219339305105,2025_TEOS_XML_11A\n")
    b = tmp_path / "index_2024.csv"
    b.write_text(INDEX_HEADER
                 + "3,EFILE,510187791,202312,2024-11-01,MEDA,990,x,202443209349328364,2024_TEOS_XML_11A\n")
    monkeypatch.setattr(xml990, "get_bytes",
                        lambda url, dest, **kw: (_ for _ in ()).throw(
                            xml990.FetchError(f"{url}: 404")))

    result = xml990.build_index([str(a), "https://apps.irs.gov/wrong/2024/index_2025.csv", str(b)])
    assert result["rows"] == 3, "a bad URL is skipped, not fatal"

    filings = xml990.indexed_filings("51-0187791")
    assert [f.object_id for f in filings] == [
        "202513219349325391", "202443209349328364", "202503219339305105"]
    assert filings[0].batch == "2025_TEOS_XML_11D"
    assert filings[0].form_type == "990"
    # 990-T carries no Part VII / Part IX content, so it sorts last even
    # though it is the same tax year as the 990.
    assert filings[-1].form_type == "990T"


def test_build_index_with_nothing_loaded_writes_no_db(monkeypatch, tmp_path):
    _xml_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(xml990, "get_bytes",
                        lambda url, dest, **kw: (_ for _ in ()).throw(
                            xml990.FetchError(f"{url}: 404")))
    result = xml990.build_index(["https://apps.irs.gov/wrong.csv", str(tmp_path / "nope")])
    assert result["rows"] == 0 and result["db"] is None
    assert not xml990.XML_DB.exists()
    assert xml990.indexed_filings("510187791") == []


def _fake_remote(monkeypatch, blob: bytes):
    """Serve `blob` through the two HTTP primitives the zip reader uses."""
    calls = []

    def head(url):
        return len(blob)

    def rng(url, start, end):
        calls.append((start, end))
        return blob[start:end + 1]

    monkeypatch.setattr(xml990, "content_length", head)
    monkeypatch.setattr(xml990, "get_range", rng)
    return calls


def _batch_zip(members: dict[str, bytes], force_zip64=False, stored=False) -> bytes:
    buf = io.BytesIO()
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w", method) as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = method
            with zf.open(info, "w", force_zip64=force_zip64) as handle:
                handle.write(data)
    return buf.getvalue()


SAMPLE_XML = (XML_DIR / "sample_990.xml").read_bytes()


def test_remote_zip_member_reads_only_directory_and_target(monkeypatch, tmp_path):
    _xml_paths(monkeypatch, tmp_path)
    blob = _batch_zip({
        # Incompressible padding so the zip is well past the 64 KiB tail read.
        "111_public.xml": random.Random(7).randbytes(300_000),
        "202513219349325391_public.xml": SAMPLE_XML,
        "333_public.xml": b"<x/>" * 2000,
    })
    calls = _fake_remote(monkeypatch, blob)

    data = xml990._remote_zip_member("https://irs/2025_TEOS_XML_11D.zip",
                                     "2025_TEOS_XML_11D", "202513219349325391_public.xml")
    assert data == SAMPLE_XML
    fetched = sum(end - start + 1 for start, end in calls)
    assert fetched < len(blob), "the whole batch zip must not be downloaded"

    # Second member from the same batch uses the cached directory: no tail read.
    calls.clear()
    assert xml990._remote_zip_member("https://irs/2025_TEOS_XML_11D.zip",
                                     "2025_TEOS_XML_11D", "333_public.xml") == b"<x/>" * 2000
    assert all(start > 0 for start, _ in calls)
    assert len(calls) == 2  # local header + compressed data

    assert xml990._remote_zip_member("https://irs/2025_TEOS_XML_11D.zip",
                                     "2025_TEOS_XML_11D", "missing.xml") is None


def test_remote_zip_member_handles_zip64_and_stored(monkeypatch, tmp_path):
    _xml_paths(monkeypatch, tmp_path)
    blob = _batch_zip({"a_public.xml": SAMPLE_XML, "b_public.xml": b"<b/>"},
                      force_zip64=True, stored=True)
    _fake_remote(monkeypatch, blob)
    assert xml990._remote_zip_member("https://irs/z.zip", "z", "a_public.xml") == SAMPLE_XML
    assert xml990._remote_zip_member("https://irs/z.zip", "z", "b_public.xml") == b"<b/>"


def test_load_for_pulls_filing_from_irs_batch_zip_and_caches_it(monkeypatch, tmp_path):
    _xml_paths(monkeypatch, tmp_path)
    idx = tmp_path / "index_2025.csv"
    idx.write_text(INDEX_HEADER
                   + "1,EFILE,521693387,202312,2025-05-01,HARBOR,990,x,2025A,2025_TEOS_XML_11D\n")
    xml990.build_index([str(idx)])
    blob = _batch_zip({"2025A_public.xml": SAMPLE_XML})
    urls = []

    def head(url):
        urls.append(url)
        return len(blob)

    monkeypatch.setattr(xml990, "content_length", head)
    monkeypatch.setattr(xml990, "get_range", lambda url, s, e: blob[s:e + 1])

    parsed = xml990.load_for("52-1693387")
    assert parsed is not None and parsed.expenses.complete
    assert parsed.people[0].name == "Dana Whitfield"
    assert urls == ["https://apps.irs.gov/pub/epostcard/990/xml/2025/2025_TEOS_XML_11D.zip"]
    assert (xml990.XML_CACHE / "2025A.xml").read_bytes() == SAMPLE_XML

    # Cached: the network is not consulted again.
    monkeypatch.setattr(xml990, "get_range",
                        lambda *a: (_ for _ in ()).throw(AssertionError("network used")))
    assert xml990.load_for("521693387").name == parsed.name


def test_load_for_is_none_when_batch_is_unreachable(monkeypatch, tmp_path):
    _xml_paths(monkeypatch, tmp_path)
    idx = tmp_path / "index_2025.csv"
    idx.write_text(INDEX_HEADER
                   + "1,EFILE,521693387,202312,2025-05-01,HARBOR,990,x,2025A,2025_TEOS_XML_11D\n")
    xml990.build_index([str(idx)])
    monkeypatch.setattr(xml990, "content_length",
                        lambda url: (_ for _ in ()).throw(xml990.FetchError("503")))
    assert xml990.load_for("521693387") is None


# ---------------------------------------------------------------------------
# Donor-restricted net assets. Part X lines 27-29 ARE in the SOI extract for
# Form 990 filers; an earlier version of this code asserted they were not and
# printed that claim into the reserve finding.
# ---------------------------------------------------------------------------

def test_part_x_net_asset_lines_resolve():
    f = _filings()[0]
    assert f.get("unrestricted_net_assets") == 95000
    assert f.source_of("unrestricted_net_assets") == "unrstrctnetasstsend"
    assert f.get("temp_restricted_net_assets") == 215000
    assert f.get("perm_restricted_net_assets") == 60000


def test_unrestricted_reserve_is_separated_from_restricted():
    m = metrics.compute(_filings())
    assert m.unrestricted_net_assets == 95000
    assert m.restricted_net_assets == 215000 + 60000
    assert m.net_assets == 370000
    # 95k unrestricted against 2.48M expenses is well under half a month,
    # versus 1.8 months on the total.
    assert m.months_of_unrestricted_reserve < m.months_of_reserve
    assert abs(m.months_of_unrestricted_reserve - (95000 / 2480000) * 12) < 1e-9
    assert m.reserve_basis == "net assets without donor restrictions"
    assert abs(m.restricted_share - 275000 / 370000) < 1e-9


def test_reserve_finding_uses_the_unrestricted_figure_and_says_so():
    filings = _filings()
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    reserve = next(
        f for f in metrics.evaluate(filings, metrics.compute(filings), status)
        if f.title == "Thin operating reserve"
    )
    assert "without donor restrictions" in reserve.detail
    assert "cannot spend on general operations" in reserve.detail
    # The old, incorrect claim must be gone.
    assert "extract does not" not in reserve.detail


def test_healthy_total_reserve_that_is_mostly_restricted_is_flagged():
    raw = json.loads(json.dumps(FIXTURE["filings_with_data"]))
    raw[0].update({
        "totfuncexpns": 1000000, "totnetassetend": 900000,
        "unrstrctnetasstsend": 150000, "temprstrctnetasstsend": 600000,
        "permrstrctnetasstsend": 150000,
    })
    filings = normalize_filings(raw, "521693387")
    m = metrics.compute(filings)
    assert m.months_of_reserve > 10          # looks comfortable on the total
    assert m.months_of_unrestricted_reserve < 2   # is not
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    titles = [f.title for f in metrics.evaluate(filings, m, status)]
    assert "Reserve is mostly donor-restricted" in titles


def test_filing_without_the_split_says_so_rather_than_assuming():
    raw = json.loads(json.dumps(FIXTURE["filings_with_data"]))
    for f in raw:
        for key in ("unrstrctnetasstsend", "temprstrctnetasstsend",
                    "permrstrctnetasstsend"):
            f.pop(key, None)
    filings = normalize_filings(raw, "521693387")
    m = metrics.compute(filings)
    assert m.months_of_unrestricted_reserve is None
    assert m.reserve_basis == "total net assets"
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    assert any("donor-restricted" in g for g in metrics.gaps(filings, m, status))


def test_990ez_has_no_part_x_split_and_does_not_pretend_to():
    filings = normalize_filings(EZ["filings_with_data"], "843311902")
    m = metrics.compute(filings)
    assert m.unrestricted_net_assets is None
    assert m.months_of_reserve is not None       # total still works
    assert m.reserve_basis == "total net assets"


# ---------------------------------------------------------------------------
# Summary, highlights, and press. The identity-confirmation tests are the
# important ones: a confident news result about the wrong "Hope House" is
# worse than no result at all.
# ---------------------------------------------------------------------------

from diligence import profile as profile_mod  # noqa: E402

HARBOR = {
    "name": "HARBOR STREET YOUTH COALITION", "city": "Baltimore", "state": "MD",
    "ein": "52-1693387", "website": "https://www.harborstreetyouth.org",
}
GENERIC = {
    "name": "HOPE HOUSE", "city": "Springfield", "state": "IL",
    "ein": "12-3456789", "website": None,
}


def test_highlights_describe_the_organization(monkeypatch):
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: FIXTURE)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: _xml())
    monkeypatch.setattr(profile_mod, "check_website", lambda url, timeout=6.0: "reachable")

    result = brief_mod.build("52-1693387")
    h = result.highlights
    assert "youth development" in h.headline.lower()
    assert "Baltimore, MD" in h.headline
    assert h.mission.startswith("Afterschool tutoring")
    joined = " ".join(h.facts)
    assert "FY2023" in joined
    assert "24 employees and 180 volunteers" in joined
    assert "73% of spending on programs" in joined
    assert "led by Dana Whitfield" in joined
    assert h.website == "https://www.harborstreetyouth.org"


def test_summary_renders_above_the_findings(monkeypatch):
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: FIXTURE)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: _xml())
    monkeypatch.setattr(profile_mod, "check_website", lambda url, timeout=6.0: "reachable")
    html = render.brief_page(brief_mod.build("52-1693387"))
    assert "<h2>Summary</h2>" in html
    assert html.index("<h2>Summary</h2>") < html.index("<h2>Findings</h2>")
    assert "Afterschool tutoring" in html


def test_website_check_uses_only_the_filed_domain():
    assert profile_mod.check_website(None) is None
    assert profile_mod._normalize_url("www.example.org") == "https://www.example.org"
    assert profile_mod._normalize_url("N/A") is None


def test_press_is_inert_without_a_provider():
    assert profile_mod.find_press(HARBOR) == []


# --- identity confirmation -------------------------------------------------

def test_name_alone_is_never_enough():
    item = {"title": "Harbor Street Youth Coalition expands tutoring",
            "url": "https://news.example.com/a", "snippet": "The group said."}
    ok, why = profile_mod.confirm_identity(item, HARBOR)
    assert ok is False and why == []


def test_name_plus_city_confirms():
    item = {"title": "Harbor Street Youth Coalition expands tutoring",
            "url": "https://news.example.com/a",
            "snippet": "The Baltimore group said it would add two sites."}
    ok, why = profile_mod.confirm_identity(item, HARBOR)
    assert ok is True
    assert any("Baltimore" in w for w in why)


def test_name_plus_filed_domain_confirms():
    item = {"title": "Harbor Street Youth Coalition names new director",
            "url": "https://news.example.com/b",
            "snippet": "More at harborstreetyouth.org."}
    ok, why = profile_mod.confirm_identity(item, HARBOR)
    assert ok is True and any("domain" in w for w in why)


def test_name_plus_ein_confirms():
    item = {"title": "Harbor Street Youth Coalition audit",
            "url": "https://news.example.com/c", "snippet": "EIN 52-1693387 filed."}
    ok, why = profile_mod.confirm_identity(item, HARBOR)
    assert ok is True and "EIN" in why


def test_a_different_organization_with_the_same_name_is_rejected():
    """The collision this whole mechanism exists for."""
    item = {"title": "Harbor Street Youth Coalition opens shelter",
            "url": "https://news.example.com/d",
            "snippet": "The Portland, Oregon nonprofit announced."}
    ok, _ = profile_mod.confirm_identity(item, HARBOR)
    assert ok is False


def test_generic_names_require_two_corroborators():
    one = {"title": "Hope House expands", "url": "https://x.example/1",
           "snippet": "The Springfield charity said."}
    assert profile_mod.confirm_identity(one, GENERIC)[0] is False

    two = {"title": "Hope House expands", "url": "https://x.example/2",
           "snippet": "The Springfield, Illinois charity, EIN 12-3456789, said."}
    ok, why = profile_mod.confirm_identity(two, GENERIC)
    assert ok is True and len(why) >= 2


def test_unrelated_article_is_rejected():
    item = {"title": "Baltimore budget passes", "url": "https://x.example/3",
            "snippet": "Maryland officials approved the plan."}
    assert profile_mod.confirm_identity(item, HARBOR)[0] is False


def test_find_press_drops_unconfirmed_items_silently():
    def fake_search(query):
        return [
            {"title": "Harbor Street Youth Coalition opens shelter",
             "url": "https://x.example/wrong", "snippet": "The Portland nonprofit."},
            {"title": "Harbor Street Youth Coalition wins grant",
             "url": "https://x.example/right",
             "snippet": "The Baltimore organization received funding.",
             "source": "Baltimore Banner", "published": "2026-04-02"},
        ]
    items = profile_mod.find_press(HARBOR, fake_search)
    assert len(items) == 1
    assert items[0].url == "https://x.example/right"
    assert items[0].source == "Baltimore Banner"


def test_gnews_provider_maps_articles_and_sends_only_the_quoted_name(monkeypatch):
    import httpx

    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params)
        return httpx.Response(200, json={"totalArticles": 1, "articles": [
            {"title": "Harbor Street Youth Coalition wins grant",
             "description": "The Baltimore organization received funding.",
             "content": "Full text... [1200 chars]",
             "url": "https://x.example/right",
             "publishedAt": "2026-04-02T14:00:00Z",
             "source": {"name": "Baltimore Banner", "url": "https://banner.example"}},
            "not-a-dict",
        ]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    search = profile_mod.gnews_provider("k")
    items = list(search(profile_mod.build_query(HARBOR)))

    assert seen["url"] == profile_mod.GNEWS_ENDPOINT
    assert seen["params"]["q"] == f'"{HARBOR["name"]}"'
    assert seen["params"]["apikey"] == "k"
    assert items == [{
        "title": "Harbor Street Youth Coalition wins grant",
        "url": "https://x.example/right",
        "snippet": "The Baltimore organization received funding. Full text... [1200 chars]",
        "source": "Baltimore Banner",
        "published": "2026-04-02",
    }]
    assert len(profile_mod.find_press(HARBOR, search)) == 1


def test_provider_selection(monkeypatch):
    monkeypatch.delenv("GRANTSIGHT_NEWS_URL", raising=False)
    monkeypatch.delenv("GNEWS_API_KEY", raising=False)
    monkeypatch.delenv("GRANTSIGHT_NEWS_PROVIDER", raising=False)
    assert profile_mod._provider_from_env(HARBOR) is not None, "GDELT needs no key"
    monkeypatch.setenv("GRANTSIGHT_NEWS_PROVIDER", "gnews")
    assert profile_mod._provider_from_env(HARBOR) is None
    monkeypatch.setenv("GNEWS_API_KEY", "k")
    assert profile_mod._provider_from_env(HARBOR) is not None
    monkeypatch.setenv("GRANTSIGHT_NEWS_PROVIDER", "url")
    assert profile_mod._provider_from_env(HARBOR) is None
    monkeypatch.setenv("GRANTSIGHT_NEWS_URL", "https://s.example/?q={query}")
    assert profile_mod._provider_from_env(HARBOR) is not None


def test_gdelt_provider_queries_name_with_place_and_marks_the_corroboration(monkeypatch):
    import httpx

    seen = {}
    monkeypatch.setattr(profile_mod, "GDELT_MIN_INTERVAL_S", 0.0)

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params)
        return httpx.Response(200, json={"articles": [
            {"url": "https://x.example/right", "url_mobile": "",
             "title": "Harbor Street Youth Coalition wins grant",
             "seendate": "20260402T140000Z", "socialimage": "",
             "domain": "banner.example", "language": "English",
             "sourcecountry": "United States"},
            "not-a-dict",
        ]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    search = profile_mod.gdelt_provider(org=HARBOR)
    items = list(search(profile_mod.build_query(HARBOR)))

    assert seen["url"] == profile_mod.GDELT_ENDPOINT
    assert seen["params"]["query"] == (
        f'"{HARBOR["name"]}" ("Baltimore" OR "Maryland") sourcelang:english')
    assert seen["params"]["mode"] == "artlist" and seen["params"]["format"] == "json"
    assert items == [{
        "title": "Harbor Street Youth Coalition wins grant",
        "url": "https://x.example/right",
        "snippet": None,
        "source": "banner.example",
        "published": "2026-04-02",
        "corroborated_by": ["full-text match (Baltimore or Maryland)"],
    }]
    press = profile_mod.find_press(HARBOR, search)
    assert len(press) == 1
    assert press[0].corroborated_by == ["full-text match (Baltimore or Maryland)"]


def test_gdelt_empty_body_and_title_only_results_without_place_do_not_confirm(monkeypatch):
    """GDELT answers an empty body for no matches; and a title-only result
    that GDELT did not filter by place is still held to the normal rule."""
    import httpx

    monkeypatch.setattr(profile_mod, "GDELT_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        200, text="", request=httpx.Request("GET", url)))
    assert list(profile_mod.gdelt_provider(org=HARBOR)("q")) == []

    no_place = {"title": "Harbor Street Youth Coalition wins grant",
                "url": "https://x.example/x", "corroborated_by": []}
    ok, _ = profile_mod.confirm_identity(no_place, HARBOR)
    assert not ok


def test_press_search_failure_is_not_fatal():
    def broken(query):
        raise RuntimeError("provider down")
    assert profile_mod.find_press(HARBOR, broken) == []


def test_press_items_never_become_findings(monkeypatch):
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: FIXTURE)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: _xml())
    monkeypatch.setattr(profile_mod, "check_website", lambda url, timeout=6.0: "reachable")

    def fake_search(query):
        return [{"title": "Harbor Street Youth Coalition under investigation",
                 "url": "https://x.example/probe",
                 "snippet": "The Baltimore group faces scrutiny."}]

    with_press = brief_mod.build("52-1693387", search=fake_search)
    without = brief_mod.build("52-1693387")
    assert len(with_press.highlights.press) == 1
    assert with_press.severity == without.severity
    assert [f.title for f in with_press.flags] == [f.title for f in without.flags]
    html = render.brief_page(with_press)
    assert "Press mentions" in html
    assert "treated as a finding" in html   # explicitly disclaimed


def test_query_is_scoped_to_the_organization():
    query = profile_mod.build_query(HARBOR)
    assert '"HARBOR STREET YOUTH COALITION"' in query
    assert "Baltimore" in query and "MD" in query



# ---------------------------------------------------------------------------
# Peer build diagnostics. A population of 0 used to report only "too small",
# which says nothing about which of the two sources failed.
# ---------------------------------------------------------------------------

def test_zip_member_without_a_known_extension_is_still_read(tmp_path):
    """IRS SOI extracts have shipped as .dat, .dat.dat and with no extension."""
    import sqlite3 as _sqlite3
    import zipfile as _zip

    from diligence import peers

    inner = tmp_path / "extract990"          # no extension at all
    inner.write_text("\n".join(
        ["elf EIN tax_pd totrevenue"]
        + [f"E {100000000 + i} 202312 {5000 * (i + 1)}" for i in range(40)]
    ))
    archive = tmp_path / "soi.zip"
    with _zip.ZipFile(archive, "w") as z:
        z.write(inner, "extract990")

    conn = _sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE soi (ein TEXT PRIMARY KEY, revenue REAL, fiscal_year INTEGER)")
    assert peers._load_soi(conn, archive) == 40


def test_an_html_error_page_is_not_treated_as_data(tmp_path, monkeypatch):
    """A wrong URL often returns a 200 HTML page, which must not load as rows."""
    from diligence import peers

    fake = tmp_path / "fake.bin"
    fake.write_bytes(b"<!DOCTYPE html><html><body>Page not found</body></html>")
    monkeypatch.setattr(peers, "DATA_DIR", tmp_path)
    monkeypatch.setattr(peers, "get_bytes", lambda url, dest, **kw: fake)
    assert peers._resolve_source("https://example.invalid/x.zip", "soi") is None


def test_directory_or_empty_source_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    from diligence import peers

    monkeypatch.setattr(peers, "DATA_DIR", tmp_path)
    assert peers._resolve_source(str(tmp_path), "soi") is None
    assert peers._resolve_source("", "soi") is None
    assert peers._resolve_source(str(tmp_path / "missing.zip"), "soi") is None
    err = capsys.readouterr().err
    assert "is a directory" in err and "empty --soi" in err and "does not exist" in err


def test_empty_join_names_which_source_failed(tmp_path, monkeypatch):
    from diligence import peers

    bmf = tmp_path / "bmf.csv"
    bmf.write_text("EIN,NAME,STATE,NTEE_CD\n"
                   + "\n".join(f"{100000000 + i},ORG,MD,O20" for i in range(40)))
    monkeypatch.setattr(peers, "DATA_DIR", tmp_path)
    monkeypatch.setattr(peers, "DB_PATH", tmp_path / "peers.sqlite3")

    report = peers.build_index([], [str(bmf)])
    assert report["bmf"] == 40
    assert report["soi"] == 0
    assert report["joined"] == 0
    problem = " ".join(report["problems"])
    assert "SOI extract loaded 0 rows" in problem
    assert "--soi URL" in problem


def test_non_overlapping_eins_are_distinguished_from_a_failed_download(tmp_path, monkeypatch):
    """Both files fine but no shared EINs is a different problem, worded differently."""
    from diligence import peers

    soi = tmp_path / "soi.dat"
    soi.write_text("\n".join(["elf EIN tax_pd totrevenue"]
                             + [f"E {100000000 + i} 202312 5000" for i in range(40)]))
    bmf = tmp_path / "bmf.csv"
    bmf.write_text("EIN,NAME,STATE,NTEE_CD\n"
                   + "\n".join(f"{900000000 + i},ORG,MD,O20" for i in range(40)))
    monkeypatch.setattr(peers, "DATA_DIR", tmp_path)
    monkeypatch.setattr(peers, "DB_PATH", tmp_path / "peers.sqlite3")

    report = peers.build_index([str(soi)], [str(bmf)])
    assert report["soi"] == 40 and report["bmf"] == 40 and report["joined"] == 0
    assert "no EINs matched" in " ".join(report["problems"])


# ---------------------------------------------------------------------------
# SOI delimiter handling. Pre-2018 extracts are space-delimited ASCII; the
# 2018+ files are CSV. Assuming one format parsed the other to zero rows and
# reported it as "population too small", which pointed nowhere useful.
# ---------------------------------------------------------------------------

def _load_soi_text(tmp_path, name, body):
    import sqlite3 as _sqlite3

    from diligence import peers
    path = tmp_path / name
    path.write_text(body)
    conn = _sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE soi (ein TEXT PRIMARY KEY, revenue REAL, fiscal_year INTEGER)")
    count = peers._load_soi(conn, path)
    rows = conn.execute("SELECT ein, revenue, fiscal_year FROM soi ORDER BY ein").fetchall()
    return count, rows


def test_space_delimited_extract_loads():
    from diligence import peers
    assert peers._sniff_delimiter("elf EIN tax_pd totrevenue") == peers.WHITESPACE


def test_comma_delimited_extract_loads():
    from diligence import peers
    assert peers._sniff_delimiter("elf,EIN,tax_pd,totrevenue") == ","


def test_whitespace_is_distinguishable_from_failure():
    """Whitespace used to be represented as None, the same value returned for
    'no delimiter found', so a correctly parsed header read as a failure."""
    from diligence import peers
    assert peers._sniff_delimiter("elf EIN tax_pd totrevenue") is not None
    assert peers._sniff_delimiter("nothing useful here at all") is None


def test_every_delimiter_variant_yields_the_same_rows(tmp_path):
    variants = {
        "space.dat": "\n".join(["elf EIN tax_pd totrevenue"]
                               + [f"E {100000000 + i} 202312 {5000 * (i + 1)}" for i in range(30)]),
        "comma.csv": "\n".join(["elf,EIN,tax_pd,totrevenue"]
                               + [f"E,{100000000 + i},202312,{5000 * (i + 1)}" for i in range(30)]),
        "quoted.csv": "\n".join(['"elf","EIN","tax_pd","totrevenue"']
                                + [f'"E","{100000000 + i}","202312","{5000 * (i + 1)}"' for i in range(30)]),
        "tab.txt": "\n".join(["elf\tEIN\ttax_pd\ttotrevenue"]
                             + [f"E\t{100000000 + i}\t202312\t{5000 * (i + 1)}" for i in range(30)]),
    }
    reference = None
    for name, body in variants.items():
        count, rows = _load_soi_text(tmp_path, name, body)
        assert count == 30, name
        if reference is None:
            reference = rows
        assert rows == reference, f"{name} parsed differently"


def test_990pf_revenue_column_is_recognised(tmp_path):
    """990-PF uses totrcptperbks, not totrevenue."""
    count, rows = _load_soi_text(
        tmp_path, "pf.csv",
        "\n".join(["EIN,tax_pd,totrcptperbks"]
                  + [f"{100000000 + i},202312,{9000 * (i + 1)}" for i in range(30)]),
    )
    assert count == 30
    assert rows[0][1] == 9000.0


def test_unrecognisable_header_reports_what_it_saw(tmp_path, capsys):
    count, _ = _load_soi_text(
        tmp_path, "wrong.csv", "alpha,beta,gamma\n1,2,3\n")
    assert count == 0
    err = capsys.readouterr().err
    assert "could not find EIN" in err
    assert "alpha,beta,gamma" in err      # shows the real header, not a guess
