# Visual regression verification

## Method

- Untouched references were rendered through Microsoft Word to PDF and then to full-page PNG at 2× scale.
- Generated samples were produced from the placeholder Master DOCX files through the same Word converter.
- Every output page was inspected at full resolution.
- DOCX ZIP package parts were hash-compared. Only `word/document.xml` changed; media files, relationships, styles, numbering, theme, settings, and embedded artwork remained byte-identical.
- Page size, margins, anchored images, hyperlink fields, and content controls were audited with the document toolchain.

## Reference audit

| Check | ELKOLIND | JASENS |
|---|---:|---:|
| Pages | 1 | 1 |
| Page | A4 portrait, 8.27 × 11.69 in | A4 portrait, 8.27 × 11.69 in |
| Margins (L/R/T/B) | 1.18 / 0.59 / 0.00 / 0.19 in | 1.18 / 1.18 / 0.00 / 0.19 in |
| Floating images | 4 | 2 |
| Word hyperlink fields | 2 | 1 |
| Content controls | none | none |

## Final sample results

| Sample | Pages | Header/banner | Signature/stamp | Indexing logos | Overflow |
|---|---:|---|---|---|---|
| `Sample_ELKOLIND_LoA` | 1 | unchanged | original objects and placement preserved | unchanged | none |
| `Sample_JASENS_LoA` | 1 | unchanged | source contains no separate signature/stamp artwork; Editor-in-Chief block preserved | original image pinned to audited page coordinate | none |

Raster evidence:

- ELKOLIND static header changed-pixel ratio: `0.000000`.
- ELKOLIND static footer changed-pixel ratio: `0.000000`.
- JASENS static header changed-pixel ratio: `0.000000`.
- JASENS indexing-logo colored bounding box: reference `(190, 1503, 814, 1551)`; generated `(190, 1503, 814, 1552)` at `1191 × 1684` pixels (within one raster pixel).

The JASENS source anchored its indexing banner relative to a body paragraph, which allowed it to move when a title used a different number of lines. The one-time Master conversion changed only that existing anchor's vertical reference to the audited page coordinate; the image bytes, relationships, extent, and horizontal placement were not changed or recreated.

## Manual inspection notes

- Both generated LoAs are exactly one page.
- Logos, headers, e-ISSN/p-ISSN, body alignment, fonts, bold/italic emphasis, Editor-in-Chief identity, ELKOLIND signature/stamp/NIP, and indexing artwork are intact.
- Dynamic titles and author lists are complete and not truncated.
- The sample invoice and receipt are one page, use the same journal identity, display `Rp 300.000`, contain the requested metadata, and include verification QR codes without decimal noise.

## Outputs inspected

- `samples/Sample_ELKOLIND_LoA.docx`
- `samples/Sample_ELKOLIND_LoA.pdf`
- `samples/Sample_JASENS_LoA.docx`
- `samples/Sample_JASENS_LoA.pdf`
- `samples/Sample_Invoice.pdf`
- `samples/Sample_Receipt.pdf`
- Full-page PNG evidence under `samples/rendered-final/`
