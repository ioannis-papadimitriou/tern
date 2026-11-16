# MIME goldens (plan 04 step 3a, staged)

Hand-written expected outputs for `tern.index.mime`, derived from the fixture input bytes, never
from parser output (A9). One file per fixture key, `<key>.json`. The two norm-pair copies are
`norm-pair.<variant>.json`. Raw input: `tests/fixtures/synthetic_mailbox/` at MANIFEST
`messages[key].copies[]` (mbox locator / Maildir file). Frozen until step 3g moves them
byte-identical to `tests/golden/mime/`. A failing case is recorded, not edited.

## Fields
| field | type | meaning |
|---|---|---|
| `subject` | str \| null | Subject, unfolded and RFC 2047-decoded, with leading and trailing whitespace stripped. `null` if the field is absent. |
| `from_addr` | str \| null | addr-spec of the first From mailbox, lower-cased. `null` if From is absent or no addr-spec can be parsed (no guessing); an addr-spec with an empty local part or domain (e.g. an unterminated `<`) counts as unparseable. See rule 8. |
| `main_text` | str | The author's own text from the chosen text part, after the split and normalization below. `""` if none. |
| `quoted_text` | str | Quoted lines from the same part, verbatim, markers kept. `""` if none. |
| `parse_status` | `ok` \| `partial` \| `failed` | See below. |
| `attachments` | list of `{filename, mime, size}` | Metadata only, in MIME order: `filename` (decoded, or null), `mime` (lower-cased type/subtype), `size` (decoded payload bytes). |

## Rules
1. **Input.** Use the content_hash-normalized bytes: CRLF→LF, and in the body `^>(>*From )`→`\1`
   (one level of mbox quoting undone). After this, a `>From ` line counts as text, not as a quote.
2. **Text part choice.** For a single text/plain part, use it. For multipart/alternative, use
   text/plain when present and `html_to_text(text/html)` only when it is absent. For multipart/mixed,
   use the non-attachment text parts in order, joined by one blank line. Decode with the declared
   charset after decoding the CTE (7bit/8bit/QP with soft breaks/base64).
3. **Charset.** If the label is iso-8859-1 or us-ascii and the bytes are valid non-ASCII UTF-8, decode
   as UTF-8 and mark `partial`. An unknown or undecodable charset is decoded with U+FFFD replacement and
   marked `partial`.
4. **Quote split (per line).** A quote line starts with `>`. An attribution line (`On … wrote:`, or
   Greek `… έγραψε:`) that is followed, skipping blank lines, by a quote line also counts as a quote line.
   A blank line between two quote lines belongs to `quoted_text`. Every other line belongs to
   `main_text`. From a line that is exactly `-- ` to the end of the part is the signature, which goes
   in neither field.
5. **Normalization (both text fields).** LF line endings. Strip trailing spaces and tabs from each line
   (after delimiter detection). Remove zero-width (U+200B–U+200F, U+2060–U+2064, U+FEFF) and bidi
   (U+202A–U+202E, U+2066–U+2069) characters; the same removal applies to `subject`. Collapse runs of
   blank lines to one blank line. Drop leading and trailing blank lines, so there is no final newline.
   No Unicode normalization (NFC/NFD) and no whitespace collapsing inside a line.
6. **Status.** `ok`: no defect. `partial`: headers were parsed but there was a body or charset
   defect, a mislabel, or truncation at the 1 MiB cap. `failed`: no header block could be parsed
   (the parser never raises). Structural MIME damage gives `partial`.
7. **Broken multipart.** When the declared boundary never appears, the multipart has no parts.
   Its body is not used as text (no fallback) and yields no attachments. Text that failed to decode
   from invalid base64 is dropped, not guessed.
8. **`from_addr` vs content_hash.** `from_addr` is the stored, display/search field and is `null` for an
   unparseable From. `content_hash` (decisions #5) keeps hashing the `parseaddr` string unchanged, so
   for an unparseable From (`malformed`: `From: "Broken Sender <broken@example.test`) the two differ
   on purpose: the stdlib yields `('', 'Broken Sender <broken@example.test')` and an Address with
   `domain=''`, which mime.py must reject explicitly (an inline `test_mime` case covers that rule).

## Status of this file
Not a golden: `test_mime_golden.py` reads `GOLDEN_DIR/*.json` only. It moves with the goldens in
step 3g. Rules 2, 4, 5, 7 and 8 (plain over HTML, the quote/attribution/signature split, the
normalization, no text from a broken multipart, the empty-domain rejection) are provisional choices
of step 3a until they are recorded in the plan's Decisions or decisions #30.
