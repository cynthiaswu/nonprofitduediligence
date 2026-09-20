"""Offline tests. Every network call is stubbed.

The most important assertions here are the negative ones: an unresolved field
must stay None and must surface as "not determined", never as a zero and never
as a finding against the organization.
"""

from __future__ import annotations

import json
import sys
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
    return normalize_filings(FIXTURE["filings_with_data"], "991693387")


def test_ein_cleaning():
    assert clean_ein("36-3673599") == "363673599"
    assert clean_ein(" 36 3673599 ") == "363673599"
    assert format_ein("363673599") == "36-3673599"
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
    result = brief_mod.build("99-1693387", include_narrative=True)

    assert result.name.startswith("HARBOR STREET")
    assert result.ein == "99-1693387"
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


def _build_tiny_index(tmp_path, rows, monkeypatch):
    """Index a handful of rows in a temp dir and return the module."""
    src = tmp_path / "data-download-revocation.txt"
    src.write_text("\n".join(rows), encoding="latin-1")
    monkeypatch.setattr(irs_status, "DATA_DIR", tmp_path)
    monkeypatch.setattr(irs_status, "DB_PATH", tmp_path / "irs.sqlite3")
    irs_status.build_index({"revocation": src})
    return irs_status


def test_revoked_reinstated_and_clear_round_trip(tmp_path, monkeypatch):
    rows = [
        "991693387|REVOKED ORG|D|1 ST|BALTIMORE|MD|21230|US|03|05/15/2024|08/12/2024|",
        "999888777|REINSTATED ORG|D|1 ST|ALBANY|NY|12201|US|03|01/15/2019|04/10/2019|11/01/2020",
    ]
    s = _build_tiny_index(tmp_path, rows, monkeypatch)

    revoked = s.check("991693387")
    assert revoked.state == s.REVOKED
    assert revoked.revocation_date == "05/15/2024"

    reinstated = s.check("999888777")
    assert reinstated.state == s.REINSTATED
    assert reinstated.reinstatement_date == "11/01/2020"

    assert s.check("111111111").state == s.CLEAR


def test_unparseable_date_reports_unknown_not_clear(tmp_path, monkeypatch):
    """Schema drift must degrade to 'cannot verify', never to 'in good standing'."""
    rows = ["991693387|ORG|D|1 ST|X|IL|62701|US|03|MARCH THE 5TH 2020|same||"]
    s = _build_tiny_index(tmp_path, rows, monkeypatch)
    result = s.check("991693387")
    assert result.state == s.UNKNOWN
    assert "could not be parsed" in result.detail


def test_empty_revocation_table_reports_unknown(tmp_path, monkeypatch):
    """A failed download must not make every organization look clean."""
    monkeypatch.setattr(irs_status, "DATA_DIR", tmp_path)
    monkeypatch.setattr(irs_status, "DB_PATH", tmp_path / "irs.sqlite3")
    report = irs_status.build_index({})
    assert report["counts"].get("revocation", 0) == 0
    result = irs_status.check("991693387")
    assert result.state == irs_status.UNKNOWN
    assert "empty" in result.detail.lower()


def test_missing_index_reports_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(irs_status, "DB_PATH", tmp_path / "does-not-exist.sqlite3")
    assert irs_status.check("991693387").state == irs_status.UNKNOWN


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

    result = brief_mod.build("99-0112233")
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

    result = brief_mod.build("99-0112233")
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
        brief_mod.build("99-0112233")
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
    result = brief_mod.build("99-0112233")
    assert "99-0112233" in result.name
    assert any("legal name" in g for g in result.gaps)
    render.brief_page(result)   # must not raise


def test_lookup_name_reads_pub78_then_revocation(tmp_path, monkeypatch):
    rows = ["991693387|REVOKED ORG INC|D|1 ST|X|MD|21230|US|03|05/15/2024|08/12/2024|"]
    s = _build_tiny_index(tmp_path, rows, monkeypatch)
    assert s.lookup_name("991693387") == "REVOKED ORG INC"
    assert s.lookup_name("111111111") is None
    assert s.known_to_irs("991693387") is True
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
    f = normalize_filings(EZ["filings_with_data"], "993311902")[0]
    assert f.form_type == "990-EZ"
    assert f.get("total_revenue") == 184000
    assert f.source_of("total_revenue") == "totrevnue"      # not totrevenue
    assert f.source_of("total_expenses") == "totexpns"      # not totfuncexpns
    assert f.source_of("contributions") == "totcntrbs"      # not totcntrbgfts
    assert f.source_of("net_assets") == "totnetassetsend"
    assert f.get("program_revenue") == 31000


def test_990pf_uses_its_own_element_names():
    f = normalize_filings(PF["filings_with_data"], "996164271")[0]
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
    f = normalize_filings(PF["filings_with_data"], "996164271")[0]
    assert f.get("investment_income") == 310000 + 1640000
    assert f.source_of("investment_income") == "intrstrvnue + dividndsamt"


def test_a_real_zero_stays_zero():
    """grscontrgifts is 0 on this foundation. Zero is data, not absence."""
    f = normalize_filings(PF["filings_with_data"], "996164271")[0]
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
    filings = normalize_filings(EZ["filings_with_data"], "993311902")
    m = metrics.compute(filings)
    assert m.public_support_ratio is not None
    assert abs(m.public_support_ratio - 548000 / 612000) < 1e-9
    assert m.public_support_basis == "170(b)(1)(A)(vi)"
    assert m.donor_concentration is not None
    assert abs(m.donor_concentration - 41000 / 612000) < 1e-9


def test_low_public_support_raises_a_finding():
    raw = json.loads(json.dumps(EZ["filings_with_data"]))
    raw[0]["pubsupplesspct170"] = 120000      # ~20% of 612000
    filings = normalize_filings(raw, "993311902")
    m = metrics.compute(filings)
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    titles = [f.title for f in metrics.evaluate(filings, m, status)]
    assert any("one-third test" in t for t in titles), titles


def test_revenue_trend_uses_the_whole_window():
    filings = normalize_filings(EZ["filings_with_data"], "993311902")
    m = metrics.compute(filings)
    assert m.revenue_trend == "growing"
    assert m.trend_span == 3
    assert m.revenue_cagr is not None and m.revenue_cagr > 0.15


def test_declining_trend_is_a_watch_not_just_context():
    filings = normalize_filings(FIXTURE["filings_with_data"], "991693387")
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
    filings = normalize_filings(raw, "991693387")
    assert filings[0].flag("terminated") is True
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    flags = metrics.evaluate(filings, metrics.compute(filings), status)
    assert flags[0].severity == metrics.CRITICAL
    assert "termination" in flags[0].title.lower()


def test_no_answer_on_termination_is_not_a_finding():
    filings = normalize_filings(FIXTURE["filings_with_data"], "991693387")
    assert filings[0].flag("terminated") is None
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    titles = [f.title for f in metrics.evaluate(filings, metrics.compute(filings), status)]
    assert not any("termination" in t.lower() for t in titles)


def test_church_classification_explains_absent_filings():
    raw = json.loads(json.dumps(EZ["filings_with_data"]))
    raw[0]["nonpfrea"] = 1
    filings = normalize_filings(raw, "993311902")
    assert filings[0].nonpf_reason == "church"
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    flags = metrics.evaluate(filings, metrics.compute(filings), status)
    church = [f for f in flags if "church" in f.title.lower()]
    assert church and "not evidence of dormancy" in church[0].detail


def test_gaps_distinguish_absent_from_unresolved():
    filings = normalize_filings(FIXTURE["filings_with_data"], "991693387")
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
    assert x.ein == "991693387"
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
    filings = normalize_filings(FIXTURE["filings_with_data"], "991693387")
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
    filings = normalize_filings(FIXTURE["filings_with_data"], "991693387")
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

    result = brief_mod.build("99-1693387")
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
    result = brief_mod.build("99-1693387")
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
    result = brief_mod.build("99-1693387")
    assert result.xml is None
    assert any("XML unavailable" in err for err in result.errors)


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
    filings = normalize_filings(raw, "991693387")
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
    filings = normalize_filings(raw, "991693387")
    m = metrics.compute(filings)
    assert m.months_of_unrestricted_reserve is None
    assert m.reserve_basis == "total net assets"
    status = irs_status.ExemptStatus(state=irs_status.CLEAR, in_pub78=True, detail="")
    assert any("donor-restricted" in g for g in metrics.gaps(filings, m, status))


def test_990ez_has_no_part_x_split_and_does_not_pretend_to():
    filings = normalize_filings(EZ["filings_with_data"], "993311902")
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
    "ein": "99-1693387", "website": "https://www.harborstreetyouth.org",
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

    result = brief_mod.build("99-1693387")
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
    html = render.brief_page(brief_mod.build("99-1693387"))
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
            "url": "https://news.example.com/c", "snippet": "EIN 99-1693387 filed."}
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

    with_press = brief_mod.build("99-1693387", search=fake_search)
    without = brief_mod.build("99-1693387")
    assert len(with_press.highlights.press) == 1
    assert with_press.severity == without.severity
    assert [f.title for f in with_press.flags] == [f.title for f in without.flags]
    html = render.brief_page(with_press)
    assert "Press mentions" in html
    assert "treated as a finding" in html   # explicitly disclaimed


def test_query_uses_the_press_form_of_the_name():
    """The IRS legal name is not what a newsroom prints. Quoting it exactly,
    with INC and in caps, matches nothing -- which is why local coverage was
    coming back empty."""
    query = profile_mod.build_query(
        {"name": "HARBOR STREET YOUTH COALITION, INC.", "city": "Baltimore", "state": "MD"}
    )
    assert '"HARBOR STREET YOUTH COALITION"' in query
    assert "Baltimore" in query
    assert "INC" not in query.upper()
    assert "nonprofit" not in query      # narrows to articles using the word


def test_press_name_strips_legal_suffixes_without_touching_case():
    cases = {
        "HARBOR STREET YOUTH COALITION, INC.": "HARBOR STREET YOUTH COALITION",
        "CEDAR CREEK LITERACY PROJECT INC": "CEDAR CREEK LITERACY PROJECT",
        "THE MARLOWE FAMILY FOUNDATION": "THE MARLOWE FAMILY FOUNDATION",
        "Hope House Ltd.": "Hope House",
        # Case is preserved, so acronyms cannot be mangled.
        "ACLU FOUNDATION OF MARYLAND": "ACLU FOUNDATION OF MARYLAND",
        "NAACP LEGAL DEFENSE FUND": "NAACP LEGAL DEFENSE FUND",
        "ST JUDE CHILDRENS RESEARCH HOSPITAL": "ST JUDE CHILDRENS RESEARCH HOSPITAL",
    }
    for raw, expected in cases.items():
        assert profile_mod.press_name(raw) == expected, raw


def test_identity_confirms_from_the_press_form_of_the_name():
    item = {"title": "Harbor Street Youth Coalition wins grant",
            "url": "https://x/1", "snippet": "The Baltimore group received funding."}
    org = {"name": "HARBOR STREET YOUTH COALITION, INC.", "city": "Baltimore",
           "state": "MD", "ein": "99-1693387", "website": None}
    ok, why = profile_mod.confirm_identity(item, org)
    assert ok is True and why



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


# ---------------------------------------------------------------------------
# Press query relaxation. Exact-phrase on a full registered name returns
# nothing even for organizations covered daily -- "PLANNED PARENTHOOD
# FEDERATION OF AMERICA" is not what a headline says.
# ---------------------------------------------------------------------------

def test_core_name_shortens_to_what_gets_printed():
    assert profile_mod.core_name("PLANNED PARENTHOOD FEDERATION OF AMERICA INC") \
        == "PLANNED PARENTHOOD FEDERATION"
    assert profile_mod.core_name("MISSION ECONOMIC DEVELOPMENT AGENCY") \
        == "MISSION ECONOMIC DEVELOPMENT"


def test_queries_go_from_precise_to_loose():
    queries = profile_mod.build_queries(
        {"name": "PLANNED PARENTHOOD FEDERATION OF AMERICA INC", "city": "San Francisco"}
    )
    assert len(queries) >= 2
    assert queries[0].startswith('"PLANNED PARENTHOOD FEDERATION OF AMERICA"')
    assert len(queries[-1]) < len(queries[0])
    assert all("San Francisco" in q for q in queries)


def test_dba_is_tried_before_the_registered_name():
    queries = profile_mod.build_queries(
        {"name": "MISSION ECONOMIC DEVELOPMENT AGENCY", "dba": "MEDA", "city": "San Francisco"}
    )
    assert queries[0].startswith('"MEDA"')


def test_a_long_distinctive_name_still_needs_a_corroborator():
    """A tier requiring zero corroborators for long names was tried and
    reverted: it admitted a same-named organization in another city."""
    org = {"name": "HARBOR STREET YOUTH COALITION", "city": "Baltimore",
           "state": "MD", "ein": "99-1693387", "website": None}
    wrong_city = {"title": "Harbor Street Youth Coalition opens shelter",
                  "url": "https://x/d", "snippet": "The Portland, Oregon nonprofit."}
    assert profile_mod.confirm_identity(wrong_city, org)[0] is False


def test_headline_only_providers_cannot_confirm_and_say_so():
    """GDELT's article list carries no description, so nothing corroborates."""
    org = {"name": "FRIENDS OF THE URBAN FOREST", "city": "San Francisco",
           "state": "CA", "ein": "94-2801737", "website": None}
    headline_only = {"title": "Friends of the Urban Forest plants 500 trees",
                     "url": "https://sfexaminer.com/a", "snippet": None}
    assert profile_mod.confirm_identity(headline_only, org)[0] is False
    reason = profile_mod._reject_reason(headline_only, org)
    assert "no description text" in reason


def test_trace_records_every_query_and_rejection():
    org = {"name": "HARBOR STREET YOUTH COALITION", "city": "Baltimore",
           "state": "MD", "ein": "99-1693387", "website": None}

    def search(query):
        return [{"title": "Harbor Street Youth Coalition opens shelter",
                 "url": "https://x/d", "snippet": "The Portland, Oregon nonprofit."}]

    trace = []
    assert profile_mod.find_press(org, search, trace=trace) == []
    assert len(trace) >= 1
    item = trace[0]["items"][0]
    assert item["confirmed"] is False
    assert "corroborate" in item["reason"]


def test_later_query_is_tried_when_the_first_returns_nothing():
    org = {"name": "PLANNED PARENTHOOD FEDERATION OF AMERICA INC",
           "city": "San Francisco", "state": "CA", "ein": "13-1644147", "website": None}
    calls = []

    def search(query):
        calls.append(query)
        if "OF AMERICA" in query:
            return []
        return [{"title": "Planned Parenthood Federation responds",
                 "url": "https://x/1",
                 "snippet": "The San Francisco clinic said."}]

    found = profile_mod.find_press(org, search)
    assert len(calls) >= 2
    assert len(found) == 1


# ---------------------------------------------------------------------------
# Narrative and press. The paragraph may point at the press section; it must
# never characterise what the coverage says or let it shift severity.
# ---------------------------------------------------------------------------

from diligence import narrative as narrative_mod  # noqa: E402


class _Highlights:
    def __init__(self, press=(), mission=None, facts=()):
        self.press = list(press)
        self.mission = mission
        self.facts = list(facts)


class _FakeBrief:
    def __init__(self, flags=(), gaps=(), highlights=None):
        self.name = "HARBOR STREET YOUTH COALITION"
        self.location = "Baltimore, MD"
        self.subsection = "501(c)(3)"
        self.ntee = "O20 — Youth development"
        self.status = irs_status.ExemptStatus(state=irs_status.CLEAR, detail="Clear.")
        self.metrics = metrics.Metrics(latest_fiscal_year=2023)
        self.flags = list(flags)
        self.gaps = list(gaps)
        self.highlights = highlights


def test_fallback_mentions_press_when_present():
    press = [profile_mod.PressItem(title="A", url="https://x/a"),
             profile_mod.PressItem(title="B", url="https://x/b")]
    brief = _FakeBrief(gaps=["one"], highlights=_Highlights(press=press))
    text = narrative_mod._fallback(brief)
    assert "2 press mentions" in text
    assert "listed above" in text


def test_fallback_is_silent_about_press_when_there_is_none():
    brief = _FakeBrief(gaps=["one"], highlights=_Highlights())
    assert "press" not in narrative_mod._fallback(brief).lower()


def test_fallback_handles_a_brief_with_no_highlights_at_all():
    assert narrative_mod._fallback(_FakeBrief(gaps=["one"]))


def test_press_reaches_the_model_labelled_and_uncharacterised(monkeypatch):
    """Headlines may be passed; the instruction not to describe them must go
    with them, and they must be labelled unverified."""
    captured = {}

    class _Response:
        def raise_for_status(self): pass
        def json(self): return {"content": [{"type": "text", "text": "Fine."}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _Response()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(narrative_mod.httpx, "post", fake_post)

    press = [profile_mod.PressItem(title="Coalition wins grant", url="https://x/a")]
    brief = _FakeBrief(gaps=["one"],
                       highlights=_Highlights(press=press, mission="Tutoring."))
    assert narrative_mod.summarize(brief) == "Fine."

    sent = captured["messages"][0]["content"]
    assert "press_mentions_unverified" in sent
    assert "Do not describe their content" in sent
    assert "Tutoring." in sent                      # mission anchors the paragraph
    assert "Press mentions" in captured["system"]
    assert "never let its presence change your assessment" in captured["system"]


def test_a_failed_api_call_degrades_to_the_template(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    calls = []

    def always_fails(url, headers=None, json=None, timeout=None):
        calls.append(1)
        raise RuntimeError("upstream down")

    monkeypatch.setattr(narrative_mod.httpx, "post", always_fails)
    brief = _FakeBrief(gaps=["one"], highlights=_Highlights())
    text = narrative_mod.summarize(brief)
    assert len(calls) == 2                          # one retry, then give up
    assert "Nothing in the public record" in text


def test_press_never_changes_severity_via_the_narrative(monkeypatch):
    """Belt and braces on the invariant: an alarming headline must not alter
    the flags or the severity the brief reports."""
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: FIXTURE)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: None)
    monkeypatch.setattr(profile_mod, "check_website", lambda url, timeout=6.0: None)

    def alarming(query):
        return [{"title": "Coalition under federal investigation",
                 "url": "https://x/probe",
                 "snippet": "The Baltimore organization faces scrutiny."}]

    with_press = brief_mod.build("99-1693387", search=alarming)
    without = brief_mod.build("99-1693387")
    assert with_press.severity == without.severity
    assert [f.title for f in with_press.flags] == [f.title for f in without.flags]


# ---------------------------------------------------------------------------
# Concrete news providers, and the trust boundary around provider-asserted
# corroboration. GDELT returns headlines only, so it corroborates through its
# own query instead of its payload -- real evidence this code cannot inspect,
# which is accepted but tracked as weaker.
# ---------------------------------------------------------------------------

def test_quoted_term_extracts_the_name_from_a_query():
    assert profile_mod.quoted_term('"FRIENDS OF THE URBAN FOREST" San Francisco') \
        == "FRIENDS OF THE URBAN FOREST"
    assert profile_mod.quoted_term("no quotes here") == "no quotes here"


def test_gnews_adapter_maps_the_response_shape(monkeypatch):
    captured = {}

    class _R:
        def raise_for_status(self): pass
        def json(self):
            return {"totalArticles": 1, "articles": [{
                "title": "Coalition wins grant",
                "description": "The Baltimore organization received funding.",
                "content": "Longer body text.",
                "url": "https://banner.example/a",
                "publishedAt": "2026-04-02T10:00:00Z",
                "source": {"name": "Baltimore Banner", "url": "https://banner.example"},
            }]}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(params or {})
        return _R()

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    search = profile_mod.gnews_provider("test-key")
    items = list(search('"HARBOR STREET YOUTH COALITION" Baltimore'))

    # GNews ANDs every term, so only the quoted name is sent.
    assert captured["q"] == '"HARBOR STREET YOUTH COALITION"'
    assert "Baltimore" not in captured["q"]
    assert captured["apikey"] == "test-key"

    item = items[0]
    assert item["published"] == "2026-04-02"          # trimmed from publishedAt
    assert item["source"] == "Baltimore Banner"        # unwrapped from the dict
    assert "Baltimore organization" in item["snippet"]
    assert "Longer body text." in item["snippet"]      # description + content


def test_gdelt_adapter_asks_for_the_place_term_in_full_text(monkeypatch):
    captured = {}

    class _R:
        text = '{"articles": []}'
        def raise_for_status(self): pass
        def json(self):
            return {"articles": [{
                "title": "Friends of the Urban Forest plants trees",
                "url": "https://sfexaminer.example/a",
                "domain": "sfexaminer.example",
                "seendate": "20260402T083000Z",
            }]}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(params or {})
        return _R()

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(profile_mod, "GDELT_MIN_INTERVAL_S", 0.0)

    org = {"name": "FRIENDS OF THE URBAN FOREST", "city": "San Francisco", "state": "CA"}
    search = profile_mod.gdelt_provider(org=org)
    items = list(search('"FRIENDS OF THE URBAN FOREST" San Francisco'))

    assert '"San Francisco"' in captured["query"]
    assert "California" in captured["query"]
    assert "sourcelang:english" in captured["query"]

    item = items[0]
    assert item["published"] == "2026-04-02"          # parsed from seendate
    assert item["snippet"] is None                    # GDELT carries no excerpt
    assert item["corroborated_by"]                    # asserted by the query


def test_provider_assertion_requires_the_name_in_the_headline():
    """Otherwise a provider could assert past the check on an article that
    only mentions the organization in a URL slug or the source domain."""
    org = {"name": "FRIENDS OF THE URBAN FOREST", "city": "San Francisco",
           "state": "CA", "ein": "94-2801737", "website": None}

    in_headline = {"title": "Friends of the Urban Forest plants 500 trees",
                   "url": "https://x/a", "snippet": None,
                   "corroborated_by": ["full-text match (San Francisco)"]}
    assert profile_mod.confirm_identity(in_headline, org)[0] is True

    not_in_headline = {"title": "City plants 500 trees",
                       "url": "https://x/friends-of-the-urban-forest", "snippet": None,
                       "corroborated_by": ["full-text match (San Francisco)"]}
    assert profile_mod.confirm_identity(not_in_headline, org)[0] is False


def test_provider_asserted_items_are_marked_as_such():
    org = {"name": "FRIENDS OF THE URBAN FOREST", "city": "San Francisco",
           "state": "CA", "ein": "94-2801737", "website": None}

    def gdelt_like(query):
        return [{"title": "Friends of the Urban Forest plants trees",
                 "url": "https://x/a", "snippet": None,
                 "corroborated_by": ["full-text match (San Francisco)"]}]

    items = profile_mod.find_press(org, gdelt_like)
    assert len(items) == 1
    assert items[0].provider_asserted is True


def test_verified_corroboration_is_not_marked_as_asserted():
    org = {"name": "HARBOR STREET YOUTH COALITION", "city": "Baltimore",
           "state": "MD", "ein": "99-1693387", "website": None}

    def gnews_like(query):
        return [{"title": "Harbor Street Youth Coalition wins grant",
                 "url": "https://x/a",
                 "snippet": "The Baltimore organization received funding."}]

    items = profile_mod.find_press(org, gnews_like)
    assert len(items) == 1
    assert items[0].provider_asserted is False


def test_same_name_other_city_still_rejected_even_with_an_assertion():
    """The collision guard must survive the new pathway."""
    org = {"name": "HARBOR STREET YOUTH COALITION", "city": "Baltimore",
           "state": "MD", "ein": "99-1693387", "website": None}
    item = {"title": "Harbor Street Youth Coalition opens Portland shelter",
            "url": "https://x/d",
            "snippet": "The Portland, Oregon nonprofit announced."}
    assert profile_mod.confirm_identity(item, org)[0] is False


# ---------------------------------------------------------------------------
# NewsData.io. Returns description and content, so corroboration is verified
# here rather than asserted by the provider -- the property that matters.
# ---------------------------------------------------------------------------

def _newsdata_stub(monkeypatch, payload, status=200, capture=None):
    class _R:
        status_code = status
        def raise_for_status(self):
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")
        def json(self): return payload

    def fake_get(url, params=None, headers=None, timeout=None):
        if capture is not None:
            capture["url"] = url
            capture.update(params or {})
        return _R()

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)


def test_newsdata_uses_archive_not_latest(monkeypatch):
    """/latest covers 48 hours, which is useless for diligence."""
    captured = {}
    _newsdata_stub(monkeypatch, {"status": "success", "results": []}, capture=captured)
    list(profile_mod.newsdata_provider("k")('"FRIENDS OF THE URBAN FOREST" San Francisco'))
    assert captured["url"].endswith("/archive")
    assert "latest" not in captured["url"]
    assert captured["from_date"]          # a lookback window is always sent


def test_newsdata_sends_only_the_quoted_name(monkeypatch):
    captured = {}
    _newsdata_stub(monkeypatch, {"status": "success", "results": []}, capture=captured)
    list(profile_mod.newsdata_provider("k")('"FRIENDS OF THE URBAN FOREST" San Francisco'))
    assert captured["q"] == '"FRIENDS OF THE URBAN FOREST"'
    assert "San Francisco" not in captured["q"]
    assert captured["apikey"] == "k"


def test_newsdata_maps_its_field_names(monkeypatch):
    _newsdata_stub(monkeypatch, {"status": "success", "results": [{
        "title": "Friends of the Urban Forest plants 500 trees",
        "link": "https://sfexaminer.example/a",       # "link", not "url"
        "description": "The San Francisco nonprofit said.",
        "content": "Full body text.",
        "pubDate": "2026-04-02 10:00:00",
        "source_name": "SF Examiner",
    }]})
    item = list(profile_mod.newsdata_provider("k")('"FRIENDS OF THE URBAN FOREST"'))[0]
    assert item["url"] == "https://sfexaminer.example/a"
    assert item["published"] == "2026-04-02"
    assert item["source"] == "SF Examiner"
    assert "San Francisco nonprofit" in item["snippet"]
    assert "Full body text." in item["snippet"]


def test_newsdata_paywalled_content_placeholder_is_dropped(monkeypatch):
    """Free plans return a literal placeholder in `content`; it must not become
    text the identity check searches."""
    _newsdata_stub(monkeypatch, {"status": "success", "results": [{
        "title": "Coalition wins grant", "link": "https://x/a",
        "description": "The Baltimore group.",
        "content": "ONLY AVAILABLE IN PAID PLANS",
        "pubDate": "2026-04-02 10:00:00", "source_name": "Banner",
    }]})
    item = list(profile_mod.newsdata_provider("k")('"HARBOR STREET"'))[0]
    assert item["snippet"] == "The Baltimore group."


def test_newsdata_error_payload_raises_rather_than_returning_nothing(monkeypatch):
    """A 200 with status:error would otherwise look like 'no coverage'."""
    _newsdata_stub(monkeypatch, {"status": "error",
                                 "results": {"message": "Invalid API key"}})
    import pytest
    with pytest.raises(RuntimeError, match="Invalid API key"):
        list(profile_mod.newsdata_provider("bad")('"X"'))


def test_newsdata_rate_limit_is_named(monkeypatch):
    _newsdata_stub(monkeypatch, {}, status=429)
    import pytest
    with pytest.raises(RuntimeError, match="rate limit"):
        list(profile_mod.newsdata_provider("k")('"X"'))


def test_newsdata_results_corroborate_from_returned_text():
    """End to end: verified, not provider-asserted."""
    org = {"name": "FRIENDS OF THE URBAN FOREST", "city": "San Francisco",
           "state": "CA", "ein": "94-2801737", "website": None}

    def search(query):
        return [{"title": "Friends of the Urban Forest plants 500 trees",
                 "url": "https://sfexaminer.example/a",
                 "snippet": "The San Francisco nonprofit said.",
                 "source": "SF Examiner", "published": "2026-04-02"}]

    items = profile_mod.find_press(org, search)
    assert len(items) == 1
    assert items[0].provider_asserted is False
    assert any("San Francisco" in c for c in items[0].corroborated_by)


# ---------------------------------------------------------------------------
# Press status. "No provider configured", "searched and found nothing" and
# "searched but nothing confirmed" are three different states. Rendering them
# identically makes a misconfiguration look like an organization with no
# coverage, which is backwards for a diligence tool.
# ---------------------------------------------------------------------------

class _MinimalBrief:
    name = "RIVERBEND MUTUAL AID"
    subtitle = None
    city = "Oakland"
    state = "CA"
    ein = "93-4116092"
    ntee = "P20 — Human services"
    subsection = "501(c)(3)"
    location = "Oakland, CA"
    metrics = None
    filings: list = []
    xml = None


def test_no_provider_is_distinct_from_no_coverage():
    h = profile_mod.build(_MinimalBrief(), search=None, check_site=False)
    assert h.press_status == "not_configured"
    assert h.press_searched == 0


def test_provider_returning_nothing_is_recorded_as_searched():
    h = profile_mod.build(_MinimalBrief(), search=lambda q: [], check_site=False)
    assert h.press_status == "searched_nothing_returned"


def test_articles_found_but_unconfirmed_is_its_own_state():
    def search(query):
        return [{"title": "Unrelated story", "url": "https://x/1",
                 "snippet": "Something else entirely."}]
    h = profile_mod.build(_MinimalBrief(), search=search, check_site=False)
    assert h.press_status == "searched_no_match"
    assert h.press_searched >= 1


def test_provider_failure_is_not_reported_as_no_coverage():
    def broken(query):
        raise RuntimeError("Invalid API key")
    h = profile_mod.build(_MinimalBrief(), search=broken, check_site=False)
    assert h.press_status == "error"
    assert h.press == []


def test_brief_states_what_the_search_actually_did(monkeypatch):
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: FIXTURE)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: None)
    monkeypatch.setattr(profile_mod, "check_website", lambda url, timeout=6.0: None)

    result = brief_mod.build("99-1693387", search=lambda q: [])
    html = render.brief_page(result)
    assert "Press mentions" in html
    assert "never been covered by the press" in html

    # With no provider at all, the section stays absent rather than implying
    # a search happened.
    quiet = brief_mod.build("99-1693387")
    assert "Press mentions" not in render.brief_page(quiet)


# ---------------------------------------------------------------------------
# Portfolio intake. Grantee lists arrive as board-report PDFs, accounting
# exports and hand-maintained CSVs; the EIN pattern is distinctive enough to
# find without relying on structure.
# ---------------------------------------------------------------------------

from diligence import intake as intake_mod  # noqa: E402
from diligence import portfolio as portfolio_mod  # noqa: E402


def test_csv_with_headers():
    data = b"Grantee,EIN,Award\nHarbor Street,99-1693387,$50000\nCedar Creek,99-3311902,$40000\n"
    result = intake_mod.from_csv(data)
    assert result.eins == ["991693387", "993311902"]
    assert result.rows[0].name == "Harbor Street"


def test_csv_without_headers_or_consistent_columns():
    """The EIN may be in any column, hyphenated or not, with no header row."""
    data = (b"991693387,Harbor Street Youth Coalition,2024\n"
            b"Cedar Creek,993311902,\n")
    result = intake_mod.from_csv(data)
    assert result.eins == ["991693387", "993311902"]


def test_numbers_that_are_not_eins_are_rejected():
    data = b"Org,Value\nAlpha,555-12-3456\nBeta,1234567890\nGamma,99-1693387\n"
    result = intake_mod.from_csv(data)
    assert result.eins == ["991693387"]
    assert len(result.unmatched) == 2


def test_rows_without_an_ein_are_reported_not_dropped():
    """A silently shorter portfolio is the failure mode to avoid."""
    data = b"Name,EIN\nHarbor Street,99-1693387\nMissing Org,\n"
    result = intake_mod.from_csv(data)
    assert len(result.rows) == 1
    assert len(result.unmatched) == 1
    assert "Missing Org" in result.unmatched[0].raw


def test_duplicate_eins_are_counted_once():
    data = b"Name,EIN\nA,99-1693387\nB,99-1693387\n"
    result = intake_mod.from_csv(data)
    assert result.eins == ["991693387"]
    assert result.duplicate_count == 1


def test_xlsx_round_trip(tmp_path):
    import openpyxl
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Organization Name", "Tax ID", "Program"])
    sheet.append(["Harbor Street Youth Coalition", "99-1693387", "Youth"])
    sheet.append(["No EIN here", None, "Arts"])
    path = tmp_path / "grantees.xlsx"
    workbook.save(path)

    result = intake_mod.parse_upload("grantees.xlsx", path.read_bytes())
    assert result.file_type == "xlsx"
    assert result.eins == ["991693387"]
    assert len(result.unmatched) == 1


def test_pasted_text():
    result = intake_mod.from_text("99-1693387\n99-3311902\nnot an org\n")
    assert result.eins == ["991693387", "993311902"]
    assert len(result.unmatched) == 1


def test_name_extraction_strips_labels_and_amounts():
    assert intake_mod._name_from_line(
        "Harbor Street Youth Coalition   EIN 99-1693387   $75,000"
    ) == "Harbor Street Youth Coalition"
    assert intake_mod._name_from_line("99-1693387") is None


def test_mislabelled_extension_is_sniffed(tmp_path):
    """Export tools mislabel constantly; content wins over the extension."""
    import openpyxl
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Name", "EIN"])
    sheet.append(["Harbor Street", "99-1693387"])
    path = tmp_path / "actually_xlsx.csv"
    workbook.save(path)
    result = intake_mod.parse_upload("actually_xlsx.csv", path.read_bytes())
    # Sniffed as a zip container, so read as xlsx rather than mangled as csv.
    assert result.file_type == "xlsx"
    assert result.eins == ["991693387"]


def test_unreadable_upload_raises_a_usable_message():
    import pytest
    with pytest.raises(intake_mod.UnsupportedFile):
        intake_mod.parse_upload("mystery.bin", b"\x00\x00\x00\x00\x00\x00\x00\x00")


# ---------------------------------------------------------------------------
# Portfolio signals.
# ---------------------------------------------------------------------------

def _portfolio_stubs(monkeypatch, *, status=None, record=None, xml_years=()):
    from diligence.propublica import NotFound

    monkeypatch.setattr(
        portfolio_mod, "fetch_organization",
        (lambda ein: record) if record else
        (lambda ein: (_ for _ in ()).throw(NotFound("absent"))),
    )
    monkeypatch.setattr(
        portfolio_mod.irs_status, "check",
        lambda ein: status or irs_status.ExemptStatus(
            state=irs_status.CLEAR, in_pub78=True, detail="Not on the list."),
    )
    monkeypatch.setattr(portfolio_mod.irs_status, "lookup_name", lambda ein: "TEST ORG")
    monkeypatch.setattr(portfolio_mod.xml990, "load_years",
                        lambda ein, limit=3: list(xml_years))
    monkeypatch.setattr(portfolio_mod.peers_mod, "compare", lambda org, rev: None)


def test_revoked_grantee_is_critical(monkeypatch):
    _portfolio_stubs(monkeypatch, record=FIXTURE, status=irs_status.ExemptStatus(
        state=irs_status.REVOKED, revocation_date="15-May-2024", in_pub78=False,
        detail="Exemption auto-revoked effective 15-May-2024."))
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 1))
    assert result.severity == portfolio_mod.CRITICAL
    assert any(s.kind == "revoked" for s in result.signals)


def test_xml_financials_win_over_the_extract(monkeypatch):
    """The whole point of XML-first: the extract lags 12-24 months."""
    _portfolio_stubs(monkeypatch, record=FIXTURE, xml_years=[_xml()])
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 1))
    assert result.source == "xml"
    assert result.revenue == 2610000        # XML Part I
    assert FIXTURE["filings_with_data"][0]["totrevenue"] == 2140000   # older


def test_extract_is_used_when_no_xml(monkeypatch):
    _portfolio_stubs(monkeypatch, record=FIXTURE, xml_years=[])
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 1))
    assert result.source == "extract"
    assert result.revenue == 2140000


def test_revenue_drop_uses_the_prior_year_column(monkeypatch):
    """One XML filing carries both years, so a drop is detectable from a
    single return rather than needing two on record."""
    raw = (XML_DIR / "sample_990.xml").read_text().replace(
        "<CYTotalRevenueAmt>2610000</CYTotalRevenueAmt>",
        "<CYTotalRevenueAmt>1200000</CYTotalRevenueAmt>")
    _portfolio_stubs(monkeypatch, record=FIXTURE, xml_years=[xml990.parse(raw)])
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 1))
    drops = [s for s in result.signals if s.kind == "revenue_drop"]
    assert drops and "44%" in drops[0].headline


def test_leadership_turnover_detected(monkeypatch):
    current = _xml()
    prior = xml990.parse(
        (XML_DIR / "sample_990.xml").read_text()
        .replace("<TaxYr>2023</TaxYr>", "<TaxYr>2022</TaxYr>")
        .replace("Dana Whitfield", "Priya Raghunathan"))
    _portfolio_stubs(monkeypatch, record=FIXTURE, xml_years=[current, prior])
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 1))
    leadership = [s for s in result.signals if s.kind == "leadership"]
    assert any("Highest-paid officer changed" in s.headline for s in leadership)


def test_no_turnover_when_the_roster_is_unchanged(monkeypatch):
    _portfolio_stubs(monkeypatch, record=FIXTURE, xml_years=[_xml(), _xml()])
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 1))
    assert not [s for s in result.signals if s.kind == "leadership"]


def test_funder_dependence_from_the_public_support_test(monkeypatch):
    _portfolio_stubs(monkeypatch, record=FIXTURE, xml_years=[_xml()])
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 1))
    dependence = [s for s in result.signals if s.kind == "funder_dependence"]
    assert dependence and "25%" in dependence[0].headline


def test_990n_filer_is_context_not_a_problem(monkeypatch):
    _portfolio_stubs(monkeypatch, status=irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail="Not on the list.",
        epostcard_years=[2024]))
    result = portfolio_mod.evaluate_one("99-0112233", today=date(2026, 9, 1))
    assert result.severity == portfolio_mod.CONTEXT
    assert not result.needs_attention


def test_one_bad_ein_does_not_stop_the_sweep(monkeypatch):
    calls = []

    def flaky(ein, listed_as=None, today=None, use_xml=True, award=None):
        calls.append(ein)
        if ein == "993311902":
            raise RuntimeError("upstream exploded")
        return portfolio_mod.OrgResult(ein=ein, name="OK")

    monkeypatch.setattr(portfolio_mod, "evaluate_one", flaky)
    monkeypatch.setattr(portfolio_mod, "_size_distribution", lambda orgs: {})
    monkeypatch.setattr(portfolio_mod.irs_status, "check",
                        lambda e: irs_status.ExemptStatus(state=irs_status.CLEAR))
    report = portfolio_mod.evaluate(["991693387", "993311902", "996164271"])
    assert len(calls) == 3
    assert len(report.organizations) == 2
    assert len(report.failed) == 1
    assert any("could not be evaluated" in n for n in report.notes)


def test_size_bands_cover_the_range():
    assert portfolio_mod._band(10_000) == "under $50K"
    assert portfolio_mod._band(2_000_000) == "$1M–5M"
    assert portfolio_mod._band(900_000_000) == "over $25M"


# ---------------------------------------------------------------------------
# Typing one organization instead of uploading a list, and resolving names to
# EINs. Never guesses: the wrong organization in a portfolio report is worse
# than a gap in one.
# ---------------------------------------------------------------------------

SEARCH_INDEX = {
    "harbor street youth coalition": [
        {"ein": 991693387, "name": "HARBOR STREET YOUTH COALITION",
         "city": "BALTIMORE", "state": "MD"}],
    "hope house": [
        {"ein": 111111111, "name": "HOPE HOUSE", "city": "SPRINGFIELD", "state": "IL"},
        {"ein": 222222222, "name": "HOPE HOUSE INC", "city": "PORTLAND", "state": "OR"},
        {"ein": 333333333, "name": "HOPE HOUSE OF BOSTON", "city": "BOSTON", "state": "MA"}],
    "cedar creek literacy project": [
        {"ein": 993311902, "name": "CEDAR CREEK LITERACY PROJECT INC",
         "city": "BURLINGTON", "state": "VT"},
        {"ein": 999999999, "name": "CEDAR CREEK CONSERVANCY",
         "city": "AUSTIN", "state": "TX"}],
}


def _stub_search(monkeypatch):
    monkeypatch.setattr(
        portfolio_mod, "search_orgs",
        lambda q, state=None, ntee_major=None, c_code=3, page=0: {
            "organizations": SEARCH_INDEX.get((q or "").strip().lower(), [])},
    )


def test_unique_name_resolves_to_an_ein(monkeypatch):
    _stub_search(monkeypatch)
    match = portfolio_mod.resolve_name("Harbor Street Youth Coalition")
    assert match.resolved
    assert match.ein == "991693387"


def test_legal_suffix_does_not_create_a_false_exact_match(monkeypatch):
    """"HOPE HOUSE" and "HOPE HOUSE INC" are the same name. Treating only the
    first as an exact match silently picked one of three organizations."""
    _stub_search(monkeypatch)
    match = portfolio_mod.resolve_name("Hope House")
    assert match.resolved is False
    assert len(match.candidates) == 2
    assert "exact name" in match.reason


def test_suffix_stripping_still_distinguishes_genuinely_different_names(monkeypatch):
    _stub_search(monkeypatch)
    match = portfolio_mod.resolve_name("Cedar Creek Literacy Project")
    assert match.resolved
    assert match.ein == "993311902"      # not the Conservancy


def test_unknown_name_reports_rather_than_guessing(monkeypatch):
    _stub_search(monkeypatch)
    match = portfolio_mod.resolve_name("Nonexistent Org")
    assert match.resolved is False
    assert match.candidates == []
    assert "no organization of that name" in match.reason


def test_resolve_names_splits_resolved_from_unresolved(monkeypatch):
    _stub_search(monkeypatch)
    resolved, unresolved = portfolio_mod.resolve_names(
        ["Harbor Street Youth Coalition", "Hope House", "Nonexistent Org"]
    )
    assert resolved == {"991693387": "HARBOR STREET YOUTH COALITION"}
    assert [m.query for m in unresolved] == ["Hope House", "Nonexistent Org"]


def test_search_failure_is_reported_not_swallowed(monkeypatch):
    from diligence.http import FetchError
    monkeypatch.setattr(
        portfolio_mod, "search_orgs",
        lambda *a, **k: (_ for _ in ()).throw(FetchError("upstream down")),
    )
    match = portfolio_mod.resolve_name("Anything")
    assert match.resolved is False
    assert "search failed" in match.reason


def test_single_entry_accepts_an_ein():
    result = intake_mod.from_text("99-1693387")
    assert result.eins == ["991693387"]


def test_single_entry_accepts_a_name_for_resolution():
    """A typed name has no EIN, so it must land in unmatched for the route to
    hand to resolve_names rather than being discarded."""
    result = intake_mod.from_text("Friends of the Urban Forest")
    assert result.eins == []
    assert len(result.unmatched) == 1
    assert result.unmatched[0].name == "Friends of the Urban Forest"


def test_mixed_list_of_eins_and_names():
    result = intake_mod.from_text(
        "99-1693387\nFriends of the Urban Forest\n99-3311902"
    )
    assert result.eins == ["991693387", "993311902"]
    assert [u.name for u in result.unmatched] == ["Friends of the Urban Forest"]


# ---------------------------------------------------------------------------
# Award exposure. The grant list carries amounts, which nothing public has —
# so "your grant as a share of their revenue" is the one number a funder can
# get here and nowhere else.
# ---------------------------------------------------------------------------

def test_award_column_is_read():
    data = (b"Grantee,EIN,Award Amount\n"
            b"Harbor Street,99-1693387,\"$275,000\"\n"
            b"Cedar Creek,99-3311902,90000\n")
    result = intake_mod.from_csv(data)
    assert result.awards == {"991693387": 275000.0, "993311902": 90000.0}
    assert result.total_awarded == 365000.0


def test_award_is_found_without_a_header():
    result = intake_mod.from_csv(b"Harbor Street,99-1693387,$275,000\n")
    assert result.awards["991693387"] == 275000.0


def test_the_ein_is_not_mistaken_for_an_award():
    """"EIN 99-1693387" parsed as an award of $1,693,387 before the EIN was
    stripped from the line first."""
    result = intake_mod.from_text("Harbor Street   EIN 99-1693387   $75,000")
    assert result.rows[0].award == 75000.0
    assert intake_mod.from_text("99-1693387").rows[0].award is None


def test_years_are_not_mistaken_for_awards():
    assert intake_mod.parse_money("2024") is None
    assert intake_mod.parse_money("$2,024") == 2024.0      # explicit currency


def test_repeat_grants_to_one_organization_are_summed():
    data = b"Name,EIN,Amount\nA,99-1693387,$50,000\nA again,99-1693387,$25,000\n"
    result = intake_mod.from_csv(data)
    assert result.awards["991693387"] == 75000.0


def test_grant_share_of_revenue(monkeypatch):
    _portfolio_stubs(monkeypatch, record=FIXTURE, xml_years=[_xml()])
    result = portfolio_mod.evaluate_one(
        "99-1693387", today=date(2026, 9, 1), award=1_305_000)
    assert abs(result.grant_share - 0.5) < 0.01
    exposure = [s for s in result.signals if s.kind == "exposure"]
    assert exposure and "principal funder" in exposure[0].detail


def test_small_grant_share_is_not_flagged(monkeypatch):
    _portfolio_stubs(monkeypatch, record=FIXTURE, xml_years=[_xml()])
    result = portfolio_mod.evaluate_one(
        "99-1693387", today=date(2026, 9, 1), award=20_000)
    assert result.grant_share < 0.25
    assert not [s for s in result.signals if s.kind == "exposure"]


def test_no_award_means_no_exposure_signal(monkeypatch):
    _portfolio_stubs(monkeypatch, record=FIXTURE, xml_years=[_xml()])
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 1))
    assert result.grant_share is None
    assert not [s for s in result.signals if s.kind == "exposure"]


def test_dollars_at_risk_weights_by_award(monkeypatch):
    """A count treats a $5,000 grant like a $500,000 one; dollars do not."""
    big = portfolio_mod.OrgResult(ein="99-1693387", award=500_000)
    big.signals.append(portfolio_mod.Signal(
        "revoked", portfolio_mod.CRITICAL, "Revoked", "detail"))
    small = portfolio_mod.OrgResult(ein="99-3311902", award=5_000)

    report = portfolio_mod.PortfolioReport(organizations=[big, small])
    assert report.total_awarded == 505_000
    assert report.dollars_at_risk == 500_000
    assert report.has_awards is True


def test_foundation_reserve_is_not_computed(monkeypatch):
    """An endowed foundation holds decades of expenses by design; showing that
    beside a public charity's 2.4 months is a category error."""
    _portfolio_stubs(monkeypatch, record=PF)
    result = portfolio_mod.evaluate_one("99-6164271", today=date(2026, 9, 1))
    assert result.is_foundation is True
    assert result.months_reserve is None


def test_public_charity_reserve_is_computed(monkeypatch):
    _portfolio_stubs(monkeypatch, record=FIXTURE)
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 1))
    assert result.is_foundation is False
    assert result.months_reserve is not None


# ---------------------------------------------------------------------------
# Presentation fixes.
# ---------------------------------------------------------------------------

def test_ordinal_handles_the_teens():
    """The naive rule gives 93th, and also 11st, 12nd, 13rd."""
    assert render.ordinal(93) == "93rd"
    assert render.ordinal(1) == "1st"
    assert render.ordinal(2) == "2nd"
    assert render.ordinal(3) == "3rd"
    assert render.ordinal(11) == "11th"
    assert render.ordinal(12) == "12th"
    assert render.ordinal(13) == "13th"
    assert render.ordinal(21) == "21st"
    assert render.ordinal(101) == "101st"
    assert render.ordinal(111) == "111th"
    assert render.ordinal(None) == ""


def test_report_header_and_ein_disambiguation():
    big = portfolio_mod.OrgResult(ein="99-1693387", name="HOPE HOUSE", revenue=1_000_000)
    other = portfolio_mod.OrgResult(ein="99-3311902", name="HOPE HOUSE", revenue=500_000)
    report = portfolio_mod.PortfolioReport(
        generated_at="September 14, 2026", organizations=[big, other],
        size_distribution={"bands": {}, "placed": 0, "benchmarked": False},
    )
    html = render.portfolio_report_page(report)
    assert "Nonprofit Portfolio Watch" in html
    # Two identically named organizations must remain tellable apart.
    assert "99-1693387" in html and "99-3311902" in html


def test_new_sf_fixtures_trigger_distinct_signals(monkeypatch):
    arts = _load("sample_sf_arts")
    sailing = _load("sample_sf_sailing")

    _portfolio_stubs(monkeypatch, record=arts)
    result = portfolio_mod.evaluate_one("99-1556677", today=date(2026, 9, 1))
    kinds = {s.kind for s in result.signals}
    assert "revenue_drop" in kinds        # 1.62M -> 890K
    assert "deficit" in kinds             # expenses over revenue two years running

    _portfolio_stubs(monkeypatch, record=sailing)
    result = portfolio_mod.evaluate_one(
        "99-2088341", today=date(2026, 9, 1), award=115_000)
    kinds = {s.kind for s in result.signals}
    assert "funder_dependence" in kinds   # public support under one third
    assert "exposure" in kinds            # the award is half their revenue
    assert result.grant_share > 0.5


# ---------------------------------------------------------------------------
# Sampling real organizations. A demo on invented organizations proves
# nothing; inventing alerts against real names is worse than proving nothing.
# The revocation list is the IRS's own published record, so anything drawn
# from it is really on it.
# ---------------------------------------------------------------------------

def _revocation_index(tmp_path, monkeypatch):
    import sqlite3 as _sqlite3
    db = tmp_path / "irs.sqlite3"
    conn = _sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE revocation (ein TEXT PRIMARY KEY, name TEXT, subsection TEXT,
          revocation_date TEXT, posting_date TEXT, reinstatement_date TEXT);
        CREATE TABLE pub78 (ein TEXT PRIMARY KEY, name TEXT, deductibility TEXT);
        CREATE TABLE epostcard (ein TEXT, tax_year INTEGER);
        CREATE TABLE meta (schema_version INTEGER, built_at TEXT, revocation_rows INTEGER,
          pub78_rows INTEGER, epostcard_rows INTEGER, date_layout TEXT, notes TEXT);
    """)
    conn.executemany("INSERT INTO revocation VALUES (?,?,?,?,?,?)", [
        ("941111111", "ORG ONE", "03", "15-May-2024", "12-Aug-2024", ""),
        ("942222222", "ORG TWO", "03", "10-Feb-2019", "01-Apr-2019", ""),
        ("943333333", "ORG THREE", "03", "05-Jan-2019", "01-Mar-2019", "11-Nov-2021"),
    ])
    conn.execute("INSERT INTO meta VALUES (2,'2026-09-01T00:00:00',3,0,0,'DD-Mon-YYYY','')")
    conn.commit(); conn.close()
    monkeypatch.setattr(portfolio_mod.irs_status, "DB_PATH", db)
    return db


def test_sample_returns_only_genuinely_revoked_organizations(tmp_path, monkeypatch):
    _revocation_index(tmp_path, monkeypatch)
    found = portfolio_mod.sample_real("revoked", 5)
    eins = {ein for ein, _ in found}
    assert eins == {"941111111", "942222222"}
    assert "943333333" not in eins          # reinstated, so not currently revoked


def test_sample_can_select_reinstated(tmp_path, monkeypatch):
    _revocation_index(tmp_path, monkeypatch)
    found = portfolio_mod.sample_real("reinstated", 5)
    assert [ein for ein, _ in found] == ["943333333"]


def test_sample_filters_by_revocation_year(tmp_path, monkeypatch):
    _revocation_index(tmp_path, monkeypatch)
    found = portfolio_mod.sample_real("revoked", 5, since_year=2024)
    assert [ein for ein, _ in found] == ["941111111"]


def test_sample_is_empty_without_an_index(tmp_path, monkeypatch):
    """Never invent an example when the real source is unavailable."""
    monkeypatch.setattr(portfolio_mod.irs_status, "DB_PATH",
                        tmp_path / "absent.sqlite3")
    assert portfolio_mod.sample_real("revoked", 5) == []


# ---------------------------------------------------------------------------
# Filed vs published. ProPublica returns filings_with_data (the lagging SOI
# extract) and filings_without_data (returns it holds with no extracted
# numbers). Reading only the first made a current filer read as years
# delinquent — a false positive that destroys trust in a monitoring tool.
# ---------------------------------------------------------------------------

def test_recent_filing_without_figures_is_not_a_late_filing(monkeypatch):
    record = json.loads(json.dumps(FIXTURE))
    record["filings_without_data"] = [
        {"tax_prd": 202512, "tax_prd_yr": 2025, "formtype": 0, "pdf_url": "x"}]
    _portfolio_stubs(monkeypatch, record=record)

    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 14))
    assert result.latest_filing_year == 2025      # what they filed
    assert result.latest_fiscal_year == 2023      # what has published figures
    late = [s for s in result.signals if s.kind == "late"]
    assert late and late[0].severity == portfolio_mod.CONTEXT
    assert "not yet published" in late[0].headline
    assert not any(s.severity == portfolio_mod.WATCH and "filed since" in s.headline
                   for s in result.signals)


def test_a_genuinely_late_filer_is_still_flagged(monkeypatch):
    record = json.loads(json.dumps(FIXTURE))
    record["filings_without_data"] = []
    _portfolio_stubs(monkeypatch, record=record)
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 14))
    assert result.latest_filing_year == 2023
    late = [s for s in result.signals if s.kind == "late"]
    assert late and late[0].severity == portfolio_mod.WATCH
    assert "No return filed since FY2023" in late[0].headline


def test_brief_uses_filings_without_data_for_timeliness(monkeypatch):
    record = json.loads(json.dumps(FIXTURE))
    record["filings_without_data"] = [
        {"tax_prd": 202512, "tax_prd_yr": 2025, "formtype": 0, "pdf_url": "x"}]
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: record)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: None)

    result = brief_mod.build("99-1693387", include_profile=False)
    titles = [f.title for f in result.flags]
    assert not any("No return filed recently" in t for t in titles)
    assert any("figures not yet published" in t for t in titles)


def test_upload_page_content():
    html = render.portfolio_upload_page()
    assert "Nonprofit Portfolio Watch" in html
    assert "Watch your whole grantee list" not in html
    assert "Or check one organization" not in html      # redundant with the list box
    import re
    headings = re.findall(r"<h2[^>]*>(.*?)</h2>", html)
    assert headings == ["Upload your grant list", "Or type a list"]
    assert "auto-revocation, late filings" in html      # merged into the intro


# ---------------------------------------------------------------------------
# The brief must be XML-first too. It was not: metrics.compute read financials
# from the lagging SOI extract and used the XML only for the expense split, so
# a brief showed FY2023 while the organization's own FY2025 return was public.
# ---------------------------------------------------------------------------

def _brief_with(monkeypatch, xml_obj, filed_year=2025):
    record = json.loads(json.dumps(FIXTURE))
    record["filings_without_data"] = [
        {"tax_prd": filed_year * 100 + 12, "tax_prd_yr": filed_year,
         "formtype": 0, "pdf_url": "x"}]
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: record)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: xml_obj)
    return brief_mod.build("99-1693387", include_profile=False)


def _xml_for_year(year):
    raw = (XML_DIR / "sample_990.xml").read_text().replace(
        "<TaxYr>2023</TaxYr>", f"<TaxYr>{year}</TaxYr>")
    return xml990.parse(raw)


def test_brief_financials_come_from_the_xml_when_available(monkeypatch):
    result = _brief_with(monkeypatch, _xml_for_year(2025))
    assert result.metrics.latest_fiscal_year == 2025
    # XML Part I, not the extract's older figures
    assert result.filings[0].get("total_revenue") == 2140000
    assert result.xml.financials.total_revenue == 2610000
    assert "current through FY2025" in result.verdict


def test_brief_falls_back_to_the_extract_without_xml(monkeypatch):
    result = _brief_with(monkeypatch, None)
    assert result.metrics.latest_fiscal_year == 2023
    assert "filed through FY2025" in result.verdict
    assert "published figures are FY2023" in result.verdict


def test_verdict_never_calls_a_stale_figure_the_last_filing(monkeypatch):
    """The old wording said "last filing on record is FY2023" when a FY2025
    return was already on file."""
    result = _brief_with(monkeypatch, None, filed_year=2025)
    assert "last filing on record" not in result.verdict
    assert "no return on record since" not in result.verdict


def test_genuinely_stale_filer_still_reads_as_stale(monkeypatch):
    """FY2023 in late 2026 is 2.75 years and under the threshold, which is
    correct. Drop the newest filings so the lag genuinely exceeds three."""
    record = json.loads(json.dumps(FIXTURE))
    record["filings_without_data"] = []
    record["filings_with_data"] = [
        f for f in record["filings_with_data"] if f["tax_prd_yr"] <= 2021]
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: record)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: None)
    result = brief_mod.build("99-1693387", include_profile=False)
    assert result.latest_filing_year == 2021
    assert any("No return filed recently" in f.title for f in result.flags)


def test_xml_prior_year_column_drives_revenue_change(monkeypatch):
    result = _brief_with(monkeypatch, _xml_for_year(2025))
    # XML: 2,610,000 against a prior year of 2,140,000 -> up, not down
    assert result.metrics.revenue_change > 0


# ---------------------------------------------------------------------------
# /healthz. It reported only the revocation index, so a deployment with no XML
# and no peer data looked entirely healthy while two of three features were
# silently off.
# ---------------------------------------------------------------------------

def test_healthz_names_every_capability(monkeypatch, tmp_path):
    import sqlite3 as _sqlite3
    from fastapi.testclient import TestClient

    from diligence import peers as peers_mod, xml990 as xml_mod
    import app as app_mod

    monkeypatch.setattr(xml_mod, "XML_DB", tmp_path / "absent-xml.sqlite3")
    monkeypatch.setattr(peers_mod, "DB_PATH", tmp_path / "absent-peers.sqlite3")
    monkeypatch.setattr(app_mod.irs_status, "DB_PATH", tmp_path / "absent-irs.sqlite3")
    monkeypatch.delenv("GRANTSIGHT_NEWS", raising=False)

    body = TestClient(app_mod.app).get("/healthz").json()
    assert set(body["capabilities"]) == {
        "exemption_status", "current_financials", "peer_percentiles", "press_mentions"}
    assert body["degraded"] == [
        "exemption_status", "current_financials", "peer_percentiles"]
    # Every degraded capability names the command that fixes it.
    for key in body["degraded"]:
        assert body["capabilities"][key]["fix"]


def _xml_index(tmp_path):
    import sqlite3 as _sqlite3
    db = tmp_path / "xml.sqlite3"
    conn = _sqlite3.connect(db)
    conn.execute("CREATE TABLE filings (ein TEXT, tax_year INTEGER, "
                 "object_id TEXT, form_type TEXT)")  # pre-batch_id schema
    conn.executemany("INSERT INTO filings VALUES (?,?,?,?)",
                     [(f"9910000{i:02d}", 2025, f"obj{i}", "990") for i in range(7)])
    conn.commit(); conn.close()
    return db


def test_healthz_reports_live_only_when_a_fetch_succeeds(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from diligence import peers as peers_mod, xml990 as xml_mod
    import app as app_mod

    monkeypatch.setattr(xml_mod, "XML_DB", _xml_index(tmp_path))
    monkeypatch.setattr(peers_mod, "DB_PATH", tmp_path / "absent.sqlite3")
    monkeypatch.setattr(app_mod.irs_status, "DB_PATH", tmp_path / "absent2.sqlite3")
    monkeypatch.setattr(xml_mod, "load_for", lambda ein: _xml())

    body = TestClient(app_mod.app).get("/healthz").json()
    financials = body["capabilities"]["current_financials"]
    assert financials["live"] is True
    assert "7 filings indexed" in financials["detail"]
    assert "fetched FY2023" in financials["detail"]
    assert "current_financials" not in body["degraded"]


def test_healthz_catches_an_index_whose_source_is_dead(tmp_path, monkeypatch):
    """The exact failure that hid for several rounds: 2.5M filings indexed,
    object ids resolving, and every fetch 404ing. Row count alone reported it
    as healthy."""
    from fastapi.testclient import TestClient

    from diligence import peers as peers_mod, xml990 as xml_mod
    import app as app_mod

    monkeypatch.setattr(xml_mod, "XML_DB", _xml_index(tmp_path))
    monkeypatch.setattr(peers_mod, "DB_PATH", tmp_path / "absent.sqlite3")
    monkeypatch.setattr(app_mod.irs_status, "DB_PATH", tmp_path / "absent2.sqlite3")
    monkeypatch.setattr(xml_mod, "load_for", lambda ein: None)
    monkeypatch.setattr(xml_mod, "LAST_FETCH_ERROR", "404 from every source")

    body = TestClient(app_mod.app).get("/healthz").json()
    financials = body["capabilities"]["current_financials"]
    assert financials["live"] is False
    assert "no source served a document" in financials["detail"]
    # The distinction that matters: a dead document source costs current
    # figures, not the ability to say whether the organization filed.
    assert "unaffected" in financials["filing_recency"]
    assert "404 from every source" in financials["detail"]
    assert "current_financials" in body["degraded"]
    assert "--build-index" in financials["fix"]


def test_dead_aws_bucket_is_not_the_default_source():
    """The IRS stopped updating the irs-form-990 S3 bucket at the end of 2021
    and AWS marks it deprecated; defaulting to it 404s every modern filing."""
    from diligence import xml990 as xml_mod
    assert xml_mod.OBJECT_URL_TEMPLATES
    assert not any("s3.amazonaws.com/irs-form-990" in url
                   for url in xml_mod.OBJECT_URL_TEMPLATES)


def test_fetch_failure_is_distinguishable_from_no_filing(tmp_path, monkeypatch):
    from diligence import xml990 as xml_mod
    monkeypatch.setattr(xml_mod, "XML_DB", tmp_path / "absent.sqlite3")
    assert xml_mod.load_for("99-1693387") is None
    assert "not built" in xml_mod.LAST_FETCH_ERROR

    monkeypatch.setattr(xml_mod, "XML_DB", _xml_index(tmp_path))
    assert xml_mod.load_for("99-9999999") is None
    assert "no filing for this EIN" in xml_mod.LAST_FETCH_ERROR


def test_diagnose_reports_each_stage(tmp_path, monkeypatch):
    from diligence import xml990 as xml_mod
    monkeypatch.setattr(xml_mod, "XML_DB", _xml_index(tmp_path))
    monkeypatch.setattr(xml_mod, "XML_CORPUS", tmp_path / "corpus")
    monkeypatch.setattr(xml_mod, "OBJECT_URL_TEMPLATES", ["https://dead.invalid/{object_id}"])

    report = xml_mod.diagnose("99-1000000")
    assert report["index_exists"] is True
    assert report["object_ids"]                    # the index resolved one
    assert report["fetched"] is False              # but nothing served it
    assert "no source would serve the document" in report["verdict"]
    # The return is still reported as on file: a failed download is not an
    # absent filing, and conflating the two was the original bug.
    assert report["latest_indexed_year"] == 2025
    assert "source_probe" in report


def test_healthz_keeps_the_old_keys(monkeypatch, tmp_path):
    """Anything already polling irs_index_built must not break."""
    from fastapi.testclient import TestClient
    import app as app_mod
    monkeypatch.setattr(app_mod.irs_status, "DB_PATH", tmp_path / "absent.sqlite3")
    body = TestClient(app_mod.app).get("/healthz").json()
    assert "irs_index_built" in body and "irs_index_at" in body
    assert body["ok"] is True


# ---------------------------------------------------------------------------
# Filing recency is source-independent. Neither a lagging IRS extract nor a
# dead XML source may make a known filing disappear. This class of bug cost
# several rounds and is worth pinning down from every direction.
# ---------------------------------------------------------------------------

from diligence.propublica import filing_years  # noqa: E402


def test_filing_years_reads_both_propublica_lists():
    record = {
        "filings_with_data": [{"tax_prd_yr": 2023}, {"tax_prd_yr": 2022}],
        "filings_without_data": [{"tax_prd_yr": 2025}, {"tax_prd_yr": 2024}],
    }
    years = filing_years(record)
    assert years["latest_filed"] == 2025            # they have filed through 2025
    assert years["latest_with_figures"] == 2023     # figures only through 2023
    assert years["all"] == [2025, 2024, 2023, 2022]


def test_filing_years_handles_an_empty_second_list():
    record = {"filings_with_data": [{"tax_prd_yr": 2023}], "filings_without_data": []}
    years = filing_years(record)
    assert years["latest_filed"] == years["latest_with_figures"] == 2023


def test_dead_xml_source_cannot_lower_a_known_filing_year(monkeypatch):
    """The precise regression: XML unavailable must not turn a FY2025 filer
    into a FY2023 delinquent."""
    record = json.loads(json.dumps(FIXTURE))
    record["filings_without_data"] = [{"tax_prd_yr": 2025, "tax_prd": 202512,
                                       "formtype": 0, "pdf_url": "x"}]
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: record)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: None)
    monkeypatch.setattr("diligence.xml990.LAST_FETCH_ERROR", "404 from every source")

    result = brief_mod.build("99-1693387", include_profile=False)
    assert result.latest_filing_year == 2025
    assert not any("No return filed recently" in f.title for f in result.flags)
    assert any("figures not yet published" in f.title for f in result.flags)
    # and the reason the figures are stale is stated, not hidden
    assert any("404 from every source" in err for err in result.errors)


def test_xml_raises_but_never_lowers_the_filing_year(monkeypatch):
    record = json.loads(json.dumps(FIXTURE))
    record["filings_without_data"] = [{"tax_prd_yr": 2024, "tax_prd": 202412,
                                       "formtype": 0, "pdf_url": "x"}]
    monkeypatch.setattr(brief_mod, "fetch_organization", lambda ein: record)
    monkeypatch.setattr(brief_mod.irs_status, "check", lambda ein: irs_status.ExemptStatus(
        state=irs_status.CLEAR, in_pub78=True, detail=""))

    raw = (XML_DIR / "sample_990.xml").read_text().replace(
        "<TaxYr>2023</TaxYr>", "<TaxYr>2025</TaxYr>")
    monkeypatch.setattr("diligence.xml990.load_for", lambda ein: xml990.parse(raw))
    result = brief_mod.build("99-1693387", include_profile=False)
    assert result.latest_filing_year == 2025        # XML is newer, so it wins


def test_portfolio_filing_recency_matches_the_brief(monkeypatch):
    record = json.loads(json.dumps(FIXTURE))
    record["filings_without_data"] = [{"tax_prd_yr": 2025, "tax_prd": 202512,
                                       "formtype": 0, "pdf_url": "x"}]
    _portfolio_stubs(monkeypatch, record=record)
    result = portfolio_mod.evaluate_one("99-1693387", today=date(2026, 9, 15))
    assert result.latest_filing_year == 2025
    late = [s for s in result.signals if s.kind == "late"]
    assert all(s.severity == portfolio_mod.CONTEXT for s in late)


def test_index_urls_are_processing_years_not_tax_years():
    """A FY2025 return filed in April 2026 is in the 2026 index. Indexing only
    the tax years of interest misses exactly the newest filings."""
    urls = xml990.default_index_urls(4)
    assert len(urls) == 4
    assert all("index_" in u and u.endswith(".csv") for u in urls)
    years = [int(u.rsplit("index_", 1)[1][:4]) for u in urls]
    assert years == sorted(years, reverse=True)
    assert years[0] >= date.today().year


def test_build_index_accepts_urls(monkeypatch, tmp_path):
    """build_index took only local paths, so passing the documented URLs
    silently indexed nothing."""
    captured = []

    def fake_materialize(source):
        captured.append(source)
        return None

    monkeypatch.setattr(xml990, "DATA_DIR", tmp_path)
    monkeypatch.setattr(xml990, "XML_DB", tmp_path / "xml.sqlite3")
    monkeypatch.setattr(xml990, "_materialize_index_source", fake_materialize)
    xml990.build_index(["https://example.invalid/index_2026.csv"])
    assert any(c.startswith("http") for c in captured)
    # defaults are added, not replaced by the explicit URL
    assert len(captured) > 1


# ---------------------------------------------------------------------------
# /debug. Built after five speculative fixes were shipped without anyone
# looking at the actual API response, and after the terminal diagnostics
# turned out to be unusable by the person who needed them.
# ---------------------------------------------------------------------------

def test_debug_page_traces_every_stage(monkeypatch):
    from fastapi.testclient import TestClient

    from diligence import propublica as pp
    import app as app_mod

    record = json.loads(json.dumps(FIXTURE))
    record["filings_without_data"] = [
        {"tax_prd": 202512, "tax_prd_yr": 2025, "formtype": 0, "pdf_url": "x"}]
    monkeypatch.setattr(pp, "fetch_organization", lambda e, ttl_s=None: record)
    monkeypatch.setattr(app_mod.brief_mod, "fetch_organization", lambda e: record)
    monkeypatch.setattr(app_mod.brief_mod.irs_status, "check",
                        lambda e: irs_status.ExemptStatus(state=irs_status.CLEAR,
                                                          in_pub78=True, detail=""))
    monkeypatch.setattr("diligence.xml990.load_for", lambda e: None)

    body = TestClient(app_mod.app).get("/debug?ein=99-1693387").text
    assert "filings_with_data_years" in body
    assert "filings_without_data_years" in body
    assert "After our normalizer" in body
    assert "Form 990 XML lookup" in body
    assert "What the brief concludes" in body


def test_debug_page_survives_a_failing_source(monkeypatch):
    """A trace that 500s when a source is down is worthless precisely when it
    is needed."""
    from fastapi.testclient import TestClient

    from diligence import propublica as pp
    import app as app_mod

    def boom(*a, **k):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(pp, "fetch_organization", boom)
    monkeypatch.setattr(app_mod.brief_mod, "fetch_organization", boom)
    response = TestClient(app_mod.app).get("/debug?ein=99-1693387")
    assert response.status_code == 200
    assert "FAILED" in response.text
    assert "upstream down" in response.text


def test_debug_without_an_ein_explains_itself():
    from fastapi.testclient import TestClient
    import app as app_mod
    body = TestClient(app_mod.app).get("/debug").text
    assert "/debug?ein=" in body


def test_fetch_organization_can_bypass_the_cache(monkeypatch):
    """A stale cached response looks exactly like a bug in this codebase."""
    calls = []

    from diligence import propublica as pp

    def fake_get_json(url, **kwargs):
        calls.append(kwargs.get("ttl_s", "default"))
        return {"organization": {"ein": 991693387, "name": "X"}}

    monkeypatch.setattr(pp, "get_json", fake_get_json)
    pp.fetch_organization("99-1693387")
    pp.fetch_organization("99-1693387", ttl_s=0)
    assert calls == ["default", 0]


# ---------------------------------------------------------------------------
# The FY2025 bug: a failed document download was being read as "never filed".


def test_indexed_filings_reads_a_pre_sub_date_index(tmp_path, monkeypatch):
    """A volume built before sub_date existed must keep working, not crash."""
    from diligence import xml990 as xml_mod

    monkeypatch.setattr(xml_mod, "XML_DB", _xml_index(tmp_path))
    rows = xml_mod.indexed_filings("99-1000000")
    assert rows and rows[0]["tax_year"] == 2025
    assert rows[0]["sub_date"] is None
    assert xml_mod.latest_indexed_year("99-1000000") == 2025


def test_filing_recency_survives_a_dead_document_source(tmp_path, monkeypatch):
    """The bug, pinned.

    ProPublica's extract stops at FY2023. The IRS index has FY2025. Every
    document source is dead. The organization must still be reported as having
    filed for FY2025 -- and must NOT be flagged as years late.
    """
    from diligence import portfolio as portfolio_mod, xml990 as xml_mod

    monkeypatch.setattr(xml_mod, "XML_DB", _xml_index(tmp_path))
    monkeypatch.setattr(xml_mod, "XML_CORPUS", tmp_path / "corpus")
    monkeypatch.setattr(xml_mod, "OBJECT_URL_TEMPLATES", ["https://dead.invalid/{object_id}"])
    # Every document route refuses, exactly as in production.
    monkeypatch.setattr(xml_mod, "load_years", lambda ein, limit=3: [])

    record = {
        "organization": {"ein": 991000000, "name": "Test Org"},
        "filings_with_data": [
            {"tax_prd_yr": 2023, "totrevenue": 1_000_000, "totfuncexpns": 900_000,
             "formtype": 0},
        ],
        "filings_without_data": [],
    }
    monkeypatch.setattr(portfolio_mod, "fetch_organization", lambda ein: record)
    monkeypatch.setattr(
        portfolio_mod.irs_status, "check",
        lambda ein: portfolio_mod.irs_status.ExemptStatus(),
    )
    result = portfolio_mod.evaluate_one("99-1000000", use_xml=True)
    assert result.latest_filing_year == 2025, (
        "the IRS index says FY2025 was filed; a dead document source must not "
        "erase it"
    )
    late = [s for s in result.signals if s.kind == "late"
            and "No return filed since" in s.headline]
    assert not late, f"flagged late despite a FY2025 return on file: {late}"


def test_batch_id_names_the_archive():
    """XML_BATCH_ID resolves to a real IRS archive URL; SUB_DATE cannot.

    SUB_DATE holds a bare four-digit year in every published index, so any
    attempt to derive a month from it must fail rather than guess.
    """
    from diligence import irszip

    assert irszip.zip_url_for_batch("2026_TEOS_XML_05A") == (
        "https://apps.irs.gov/pub/epostcard/990/xml/2026/2026_TEOS_XML_05A.zip"
    )
    assert irszip.zip_url_for_batch("2025_TEOS_XML_12A").endswith(
        "/2025/2025_TEOS_XML_12A.zip"
    )
    # A bare year -- what SUB_DATE actually contains -- names no archive.
    assert irszip.zip_url_for_batch("2026") is None
    assert irszip.zip_url_for_batch(None) is None
    assert irszip.zip_url_for_batch("") is None


def test_ranged_zip_reader_extracts_one_member(tmp_path, monkeypatch):
    monkeypatch.setenv("GRANTSIGHT_DATA", str(tmp_path / "a"))
    """Parse a real zip through ranged reads, so the format work is verified."""
    import io as _io
    import zipfile as _zipfile

    import httpx

    from diligence import irszip

    payload = (b'<?xml version="1.0"?><Return><ReturnData>'
               b"<TaxYr>2025</TaxYr></ReturnData></Return>")
    buffer = _io.BytesIO()
    with _zipfile.ZipFile(buffer, "w", _zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("filler_public.xml", b"x" * 5000)
        archive.writestr("202641349349313769_public.xml", payload)
    blob = buffer.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        if "05A" not in str(request.url):
            return httpx.Response(404)
        rng = request.headers.get("Range", "")
        if rng.startswith("bytes=-"):
            start = max(0, len(blob) - int(rng.split("-")[1]))
            end = len(blob) - 1
        else:
            start, _, end_s = rng.removeprefix("bytes=").partition("-")
            start, end = int(start), int(end_s)
        chunk = blob[start:end + 1]
        return httpx.Response(
            206, content=chunk,
            headers={"Content-Range": f"bytes {start}-{end}/{len(blob)}"},
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    httpx.Client = fake_client
    try:
        path = irszip.fetch_object("202641349349313769", "2026_TEOS_XML_05A")
        assert b"<TaxYr>2025</TaxYr>" in path.read_bytes()
    finally:
        httpx.Client = real_client


def test_zip_reader_says_so_when_ranges_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("GRANTSIGHT_DATA", str(tmp_path / "b"))
    """A server that ignores Range must produce a readable reason, not silence."""
    import httpx

    from diligence import irszip

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"whole file")
    )
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    httpx.Client = fake_client
    try:
        report = irszip.probe("202641349349313769", "2026_TEOS_XML_05A")
        assert report["usable"] is False
        assert "Range" in report["detail"]
    finally:
        httpx.Client = real_client
