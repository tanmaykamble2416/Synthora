# TEI fixtures

Synthetic GROBID output used by the parser, chunker and integration tests. The prose is
generated filler, not text from a real paper.

The layout follows what GROBID 0.9.x returns from `POST /api/processFulltextDocument`
with `segmentSentences=1`, `includeRawCitations=1` and
`teiCoordinates=p,head,s,biblStruct` (checked against GROBID's `TEIFormatter.java`):
flat `<div>`s in `<body>`, `<head n="3.1">`, `<p>`/`<s>` with
`coords="page,x,y,w,h;..."` (1-based pages), `<listBibl>/<biblStruct>` with
`<note type="raw_reference">` in `<back>`.

| File | Purpose |
|---|---|
| `full_paper.tei.xml` | 5-page paper: header metadata, abstract, numbered sections and a subsection, list, figure (caption kept since the Day 6 quality pass), formula (kept), footnote (not section text), acknowledgements, 5 references (one empty, one with invalid values) |
| `no_coordinates.tei.xml` | Body paragraphs without `coords` and an empty header |
| `header_only.tei.xml` | Header and abstract but no body paragraphs (must be rejected) |

Real GROBID output is exercised by `tests/integration/test_grobid_live.py`
(opt-in: `RUN_GROBID_TESTS=1` with the GROBID container running).
