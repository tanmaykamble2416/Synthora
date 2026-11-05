"""structure_repair: every repair needs PDF-line evidence; without it nothing changes.

Each case builds GROBID-shaped TEI and the PDF's page text (as PyMuPDF extracts it, one
printed line per text line). The shapes come from a real GROBID 0.9.1 export of an IEEE-style
paper whose headings use the same font as the body text.
"""

import re
from collections import Counter

import pytest

from app.services.structure_repair import PdfLineIndex, cut_block, repair_structure
from app.services.tei_parser import TeiParagraph, TeiSentence, parse_tei

NS = 'xmlns="http://www.tei-c.org/ns/1.0"'


def tei(*divs: str, figures: str = "") -> bytes:
    body = "".join(f"<div {NS}>{d}</div>" for d in divs)
    return f"<TEI {NS}><text><body>{body}{figures}</body></text></TEI>".encode()


def p(text: str, page: int = 1, y: float = 100.0) -> str:
    return f'<p coords="{page},72,{y},400,10"><s coords="{page},72,{y},400,10">{text}</s></p>'


def repaired(tei_bytes: bytes, pages: list[str], **kwargs):
    document = parse_tei(tei_bytes, page_count=len(pages))
    return document, repair_structure(document.sections, pages, figures=document.figures, **kwargs)


def outline(result) -> list[tuple]:
    return [(s.number, s.heading, s.level, s.section_type) for s in result.sections]


def words(text: str | None) -> list[str]:
    return re.findall(r"[^\W_]+", (text or "").lower())


def all_words(sections, figures=()) -> Counter:
    counter: Counter = Counter()
    for s in sections:
        counter.update(words(s.number))
        counter.update(words(s.heading))
        for block in s.paragraphs:
            counter.update(words(block.text))
    for f in figures:
        counter.update(words(f.text))
    return counter


# --- evidence index --------------------------------------------------------------------------


def test_pdf_heading_lines_are_found_on_one_or_two_lines():
    index = PdfLineIndex(["Intro text.\nII.\nRELATED WORK OR LITERATURE STUDIES\nThe authors of [1]...", "VI. Results And Discussion\nBody."])
    assert [(h.page, h.number, h.title) for h in index.headings] == [
        (1, "II.", "RELATED WORK OR LITERATURE STUDIES"), (2, "VI.", "Results And Discussion"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "1. Load the data",            # Arabic numbering: list items, never used as evidence
        "A. Smith, B. Jones, C. Lee",  # an author list, not a heading
        "B. This is a whole sentence that ends with a full stop.",
        "TABLE III. K-Means Cluster Profiles",  # a caption, not a heading
        "IV. " + "word " * 20,         # too long for a heading
    ],
)
def test_lines_that_are_not_headings_are_never_evidence(line):
    assert PdfLineIndex([line]).headings == []


# --- 1. number-only headings -----------------------------------------------------------------


def test_number_only_roman_heading_gets_its_printed_title():
    pages = ["II.\nRELATED WORK OR LITERATURE STUDIES\nThe authors of the article by Desai et al. [1] developed a model."]
    doc, result = repaired(tei("<head>II.</head>" + p("RELATED WORK OR LITERATURE STUDIES The authors of the article by Desai et al. [1] developed a model.")), pages)
    section = result.sections[0]
    assert (section.number, section.heading, section.section_type) == ("II.", "RELATED WORK OR LITERATURE STUDIES", "related_work")
    assert section.paragraphs[0].text == "The authors of the article by Desai et al. [1] developed a model."
    assert result.report.titles_recovered == 1
    assert all_words(result.sections) == all_words(doc.sections)  # moved, not added or lost


def test_title_case_heading_is_recovered_too():
    pages = ["VIII.\nConclusions\nThis paper presented a pipeline."]
    _, result = repaired(tei("<head>VIII.</head>" + p("Conclusions This paper presented a pipeline.")), pages)
    assert outline(result) == [("VIII.", "Conclusions", 1, "conclusion")]


def test_number_only_heading_without_pdf_evidence_is_left_alone():
    pages = ["II.\nSomething else entirely\nRELATED WORK OR LITERATURE STUDIES The authors of [1]."]
    _, result = repaired(tei("<head>II.</head>" + p("RELATED WORK OR LITERATURE STUDIES The authors of [1].")), pages)
    section = result.sections[0]
    assert (section.number, section.heading) == ("II.", None)  # nothing invented
    assert section.paragraphs[0].text.startswith("RELATED WORK OR LITERATURE STUDIES")


# --- 2. captions merged into headings ---------------------------------------------------------


def test_caption_printed_before_a_heading_moves_to_the_previous_section():
    pages = ["A. Transaction Categorization Performance\nFirst body.\nTABLE I. Transaction Categorization Coverage by Pipeline Stage\nB. Behavioral Feature Distribution\nSecond body."]
    doc, result = repaired(tei(
        "<head>A. Transaction Categorization Performance</head>" + p("First body."),
        "<head>TABLE I. Transaction Categorization Coverage by Pipeline Stage B. Behavioral Feature Distribution</head>" + p("Second body."),
    ), pages)
    first, second = result.sections
    assert (second.number, second.heading, second.level) == ("B.", "Behavioral Feature Distribution", 2)
    assert first.paragraphs[-1].kind == "caption"
    assert first.paragraphs[-1].text == "TABLE I. Transaction Categorization Coverage by Pipeline Stage"
    assert all_words(result.sections) == all_words(doc.sections)


def test_caption_printed_after_a_heading_starts_the_section():
    pages = ["VII.\nPROPOSED SYSTEM ARCHITECTURE AND METHODOLOGY\nFig 1. System Architecture for Statement Analytics\nThe engine has two stages."]
    _, result = repaired(tei("<head>PROPOSED SYSTEM ARCHITECTURE AND METHODOLOGY Fig 1. System Architecture for Statement Analytics</head>" + p("The engine has two stages.")), pages)
    section = result.sections[0]
    assert (section.number, section.heading, section.section_type) == ("VII.", "PROPOSED SYSTEM ARCHITECTURE AND METHODOLOGY", "methodology")
    assert [(b.kind, b.text) for b in section.paragraphs] == [
        ("caption", "Fig 1. System Architecture for Statement Analytics"), ("text", "The engine has two stages."),
    ]


def test_caption_split_needs_both_parts_printed():
    pages = ["B. Behavioral Feature Distribution\nSecond body."]  # the caption line is not in the PDF
    _, result = repaired(tei("<head>TABLE I. Coverage by Stage B. Behavioral Feature Distribution</head>" + p("Second body.")), pages)
    assert result.sections[0].heading == "TABLE I. Coverage by Stage B. Behavioral Feature Distribution"


# --- 3. equations taken for headings -----------------------------------------------------------


def test_equation_heading_becomes_a_formula_of_the_previous_section():
    pages = ["D. Feature Engineering Module\nThe ratio is defined below.\nDTI = Total EMI Outflows / Monthly Salary Income(1)\nThe Savings Rate measures retention."]
    doc, result = repaired(tei(
        "<head>D. Feature Engineering Module</head>" + p("The ratio is defined below."),
        "<head>DTI = Total EMI Outflows / Monthly Salary Income(1)</head>" + p("The Savings Rate measures retention."),
    ), pages)
    assert len(result.sections) == 1
    assert [(b.kind, b.text) for b in result.sections[0].paragraphs] == [
        ("text", "The ratio is defined below."),
        ("formula", "DTI = Total EMI Outflows / Monthly Salary Income(1)"),
        ("text", "The Savings Rate measures retention."),
    ]
    assert result.report.equations_demoted == 1
    assert all_words(result.sections) == all_words(doc.sections)


def test_equation_heading_not_printed_as_a_line_is_kept():
    _, result = repaired(tei("<head>Intro</head>" + p("Text."), "<head>x = y + z (2)</head>" + p("More.")), ["Intro\nText.\nMore."])
    assert [s.heading for s in result.sections] == ["Intro", "x = y + z (2)"]


# --- 4. numbers GROBID dropped -----------------------------------------------------------------


def test_missing_number_is_recovered_and_its_stray_fragment_removed():
    pages = ["II.\nRELATED WORK\nBody one ends here. IV.\nIV.\nPROBLEM DOMAIN\nBody two."]
    doc, result = repaired(tei(
        "<head>II. RELATED WORK</head>" + p("Body one ends here. IV."),
        "<head>PROBLEM DOMAIN</head>" + p("Body two."),
    ), pages)
    first, second = result.sections
    assert (second.number, second.heading) == ("IV.", "PROBLEM DOMAIN")
    assert first.paragraphs[-1].text == "Body one ends here."  # "IV." moved into section_number
    assert result.report.number_fragments_removed == 1
    assert all_words(result.sections) == all_words(doc.sections)


def test_lone_number_paragraph_is_removed_when_it_moves_to_the_next_heading():
    pages = ["Intro\nBody.\nV.\nPROBLEM DEFINITION\nMore."]
    _, result = repaired(tei("<head>Intro</head>" + p("Body.") + p("V."), "<head>PROBLEM DEFINITION</head>" + p("More.")), pages)
    assert [b.text for b in result.sections[0].paragraphs] == ["Body."]
    assert result.sections[1].number == "V."


def test_number_is_recovered_from_a_one_line_heading():
    _, result = repaired(tei("<head>INTRODUCTION</head>" + p("Body.")), ["I. INTRODUCTION\nBody."])
    assert outline(result) == [("I.", "INTRODUCTION", 1, "introduction")]


def test_number_is_not_invented_without_a_printed_line():
    _, result = repaired(tei("<head>INTRODUCTION</head>" + p("Body.")), ["INTRODUCTION\nBody."])
    assert result.sections[0].number is None


# --- 5. headings trapped inside paragraphs -----------------------------------------------------


def test_heading_trapped_at_the_end_of_a_paragraph_starts_a_new_section():
    pages = ["E. Customer Segmentation Module\nClusters are mapped. VI. Results And Discussion\nVI. Results And Discussion\nThis section presents the outcomes."]
    doc, result = repaired(tei(
        "<head>E. Customer Segmentation Module</head>"
        + p("Clusters are mapped. VI. Results And Discussion")
        + p("This section presents the outcomes."),
    ), pages)
    assert outline(result) == [("E.", "Customer Segmentation Module", 2, "other"), ("VI.", "Results And Discussion", 1, "results")]
    assert [b.text for b in result.sections[0].paragraphs] == ["Clusters are mapped."]
    assert [b.text for b in result.sections[1].paragraphs] == ["This section presents the outcomes."]
    assert result.report.trapped_headings_recovered == 1
    assert all_words(result.sections) == all_words(doc.sections)


def test_consecutive_trapped_headings_build_the_outline():
    pages = ["Prior work text.\nIII. MOTIVATION\nA. Real-World Problem Perception\nThe core motivation is cost.\nB. Inspiration from Related Works\nThe foundation."]
    doc, result = repaired(tei(
        "<head>II. RELATED WORK</head>"
        + p("Prior work text. III. MOTIVATION A. Real-World Problem Perception")
        + p("The core motivation is cost. B. Inspiration from Related Works The foundation."),
    ), ["II. RELATED WORK\n" + pages[0]])
    assert outline(result) == [
        ("II.", "RELATED WORK", 1, "related_work"),
        ("III.", "MOTIVATION", 1, "other"),  # "motivation" alone no longer forces "introduction"
        ("A.", "Real-World Problem Perception", 2, "other"),
        ("B.", "Inspiration from Related Works", 2, "related_work"),
    ]
    assert result.sections[1].paragraphs == []  # heading-only parent, kept in the outline
    assert [b.text for b in result.sections[2].paragraphs] == ["The core motivation is cost."]
    assert [b.text for b in result.sections[3].paragraphs] == ["The foundation."]
    assert all_words(result.sections) == all_words(doc.sections)


def test_trapped_heading_on_another_page_is_not_used():
    pages = ["Body. VI. Results And Discussion text continues.", "VI. Results And Discussion\nLater."]
    _, result = repaired(tei("<head>Intro</head>" + p("Body. VI. Results And Discussion text continues.", page=1)), pages)
    assert len(result.sections) == 1


def test_phrase_that_is_not_a_printed_heading_line_is_not_split():
    pages = ["As argued in VI. Results And Discussion of prior work, the method works."]
    _, result = repaired(tei("<head>Intro</head>" + p(pages[0])), pages)
    assert len(result.sections) == 1


def test_numbered_list_items_inside_paragraphs_are_not_headings():
    pages = ["Steps\n1. Load the data\n2. Train the model"]
    _, result = repaired(tei("<head>Steps</head>" + p("1. Load the data 2. Train the model")), pages)
    assert outline(result) == [(None, "Steps", 1, "other")]


# --- 6. table text ------------------------------------------------------------------------------


TABLE = [[(50.0, 400.0, 560.0, 700.0)]]  # one detected table region on page 1


def test_sentences_inside_a_table_region_become_a_table_block():
    paragraph = (
        '<p><s coords="1,60,410,100,8;1,300,410,100,8">Paper Title Data Source Output</s>'
        '<s coords="1,60,430,100,8">Desai et al. Transactions Accuracy</s>'
        '<s coords="1,72,100,400,10">The core motivation follows the table.</s></p>'
    )
    doc, result = repaired(tei("<head>Related</head>" + paragraph), ["Related"], table_regions=TABLE)
    blocks = result.sections[0].paragraphs
    assert [(b.kind, b.text) for b in blocks] == [
        ("table", "Paper Title Data Source Output Desai et al. Transactions Accuracy"),
        ("text", "The core motivation follows the table."),
    ]
    assert all_words(result.sections) == all_words(doc.sections)


def test_mostly_outside_a_table_stays_text():
    paragraph = '<p><s coords="1,60,390,100,20">Half above the table region.</s></p>'  # 50% inside
    _, result = repaired(tei("<head>X</head>" + paragraph), ["X"], table_regions=TABLE)
    assert result.sections[0].paragraphs[0].kind == "text"


def test_no_table_regions_changes_nothing():
    _, result = repaired(tei("<head>X</head>" + p("Text.", y=450)), ["X"], table_regions=[[]])
    assert result.sections[0].paragraphs[0].kind == "text"


def test_table_region_on_another_page_does_not_count():
    _, result = repaired(tei("<head>X</head>" + p("Text.", page=2, y=450)), ["X", "Y"], table_regions=[[(50, 400, 560, 700)], []])
    assert result.sections[0].paragraphs[0].kind == "text"


# --- 7. figures and GROBID tables -----------------------------------------------------------------


def test_figure_caption_is_inserted_at_its_position():
    figures = (
        f'<figure {NS} coords="1,72,300,400,10"><head>Fig. 2 .</head><figDesc>Fig. 2. Ingestion Pipeline</figDesc></figure>'
    )
    _, result = repaired(tei("<head>B. Ingestion</head>" + p("Before.", y=100) + p("After.", y=500), figures=figures), ["B. Ingestion"])
    assert [(b.kind, b.text) for b in result.sections[0].paragraphs] == [
        ("text", "Before."), ("caption", "Fig. 2. Ingestion Pipeline"), ("text", "After."),
    ]


def test_grobid_table_is_kept_with_its_cells():
    figures = (
        f'<figure {NS} type="table" coords="1,72,300,400,100"><head>TABLE I</head><figDesc>TABLE I. Coverage</figDesc>'
        "<table><row><cell>Stage</cell><cell>Share</cell></row><row><cell>Rules</cell><cell>78.8%</cell></row></table></figure>"
    )
    _, result = repaired(tei("<head>Results</head>" + p("Text.", y=100), figures=figures), ["Results"])
    table = result.sections[0].paragraphs[-1]
    assert (table.kind, table.text) == ("table", "TABLE I. Coverage\nStage | Share\nRules | 78.8%")


def test_figure_without_coordinates_goes_to_the_end_of_the_body():
    figures = f'<figure {NS}><figDesc>Fig. 9. Overview</figDesc></figure>'
    _, result = repaired(tei("<head>A</head>" + p("One."), "<head>B</head>" + p("Two."), figures=figures), ["A\nB"])
    assert result.sections[-1].paragraphs[-1].text == "Fig. 9. Overview"


def test_printed_caption_paragraph_is_marked_as_caption():
    _, result = repaired(tei("<head>C. Pipeline</head>" + p("Body.") + p("Fig. 3. Two-Stage Pipeline")), ["C. Pipeline\nBody.\nFig. 3. Two-Stage Pipeline"])
    assert [b.kind for b in result.sections[0].paragraphs] == ["text", "caption"]


# --- surgery helper -------------------------------------------------------------------------------


def test_cut_block_keeps_sentences_and_pages():
    block = TeiParagraph(
        text="One two. Three four. Five six.",
        sentences=(TeiSentence("One two.", 1, 1), TeiSentence("Three four.", 1, 2), TeiSentence("Five six.", 2, 2)),
        page_start=1, page_end=2,
    )
    before, after = cut_block(block, len("One two. Three"), len("One two. Three four."))
    assert (before.text, before.page_start, before.page_end) == ("One two. Three", 1, 2)
    assert (after.text, after.page_start, after.page_end) == ("Five six.", 2, 2)
    assert cut_block(block, 0, len(block.text)) == (None, None)


def test_abstract_is_never_repaired():
    doc = parse_tei(
        f'<TEI {NS}><teiHeader><profileDesc><abstract><div {NS}><p coords="1,72,100,400,10">II. RELATED WORK inside the abstract.</p></div></abstract></profileDesc></teiHeader>'
        f'<text><body><div {NS}><head>Intro</head>{p("Body.")}</div></body></text></TEI>'.encode(),
        page_count=1,
    )
    result = repair_structure(doc.sections, ["II. RELATED WORK\nBody."])
    assert result.sections[0].paragraphs[0].text == "II. RELATED WORK inside the abstract."
