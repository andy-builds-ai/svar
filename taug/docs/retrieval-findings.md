# Retrieval findings

A hit should be a passage, not a page: the answering model in build step 3
gets what retrieve returns. Getting there took five configurations, all
measured on 17.09.2026 against the demo corpus (37 Markdown documents,
540 to 1,014 words each). Word counts are whitespace-separated words.

## What was measured

| Configuration | Stored in the index | What retrieve returned |
|---|---|---|
| Fixed-size, 800 tokens | not inspected | Whole documents, e.g. `support-process.md` with all 976 words in one hit. A request for 10 hits returned 6. |
| Fixed-size, 300 tokens | 249 chunks of 2 to 246 words | For 28 of 37 documents the whole document, once per document; for the other 9 the chunk. A request for 10 hits returned 7, each from a different document. |
| Hierarchical, parents 500 / children 200 tokens, overlap 30 | 328 vectors | For `release-notes-4.1.md`, one of the 9 documents without the effect, parent chunks of 260 and 280 words, as documented. For the same 28 documents as above, still the whole document: 67 of 91 hits in a sample of 12 queries. |
| Fixed-size, 300 tokens, documents uploaded as sections | 304 vectors from 147 section files | 185 hits over 12 queries: 18 to 440 words, median 165. No hit was a whole document, none exceeded 450 words. |
| Fixed-size, 300 tokens, whole documents, foundation-model parser (Amazon Nova Lite), as a second data source | 249 chunks; every chunk of the same 28 documents carries `parentText`. The chunk texts match the default parser's: a table chunk of `support-process.md` is character for character the same in both data sources. | `support-process.md` whole (976 words) in 4 of 4 queries; `release-notes-4.1.md` a chunk of 198 words. Requests for 10 hits over 4 queries returned 3 to 7, 14 of 17 of them whole documents. |

Reading the vectors directly (`s3vectors list-vectors`) showed where the
whole documents come from. Every chunk of the 28 affected documents carries
the complete document text in `AMAZON_BEDROCK_METADATA.parentText`, and
retrieve returns that text in `content.text` instead of the chunk. The 9
unaffected documents have no `parentText`.

The 28 affected documents are exactly the 28 that contain a Markdown table;
the 9 unaffected ones contain none. That is an observed correlation across
37 of 37 documents, not a documented cause. Length does not explain it:
`faq.md` (908 words, no table) was unaffected, `api-resources-bookings-and-time.md`
(633 words, tables) was affected.

## What the documentation says, and does not

- The chunking guide ([kb-chunking](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-chunking.html))
  describes replacing matched chunks with parent chunks only for
  hierarchical chunking. For fixed-size chunking it describes no such step.
- The [Retrieve API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_Retrieve.html)
  has one text field per hit, `content.text`. `parentText` is not mentioned
  there, nor anywhere else found in the documentation.
- The same guide advises against hierarchical chunking on S3 Vectors
  because of metadata size limits above 8,000 tokens combined — far above
  the sizes tried here, and not the effect observed.
- A question on AWS re:Post
  ([Retrieve returns the full parent document instead of the matched chunk](https://repost.aws/questions/QUjFWDQ_XRRNCAU5WefBWTPA/))
  describes the same behaviour on S3 Vectors with Markdown sources. The
  suggested fix, declaring fixed-size chunking explicitly, did not change it
  according to the person asking. No cause is given there either. A later
  answer there suggests setting the data source's parser explicitly to a
  foundation model (`parsingConfiguration` with `BEDROCK_FOUNDATION_MODEL`).
  Measured as the fifth row above, it changed nothing: same chunks, same
  `parentText`, same whole documents.
- The [parsing options](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-advanced-parsing.html)
  page describes the foundation-model and Data Automation parsers for `.pdf`,
  `.jpeg` and `.png` files; `.md` is listed only under the default parser. The
  [ParsingConfiguration](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_ParsingConfiguration.html)
  reference adds that Bedrock falls back to the default parser when the chosen
  one fails on a file. Identical chunk texts fit both readings: Markdown files
  still go through the default parser.

## What was chosen

Chunking stays fixed-size at 300 tokens. The fill script splits every
Markdown document before the upload, so that whatever Bedrock returns is at
most a section:

1. cut at every `## ` heading outside code fences;
2. join sections under 80 words to the one before (a short intro to the one
   after);
3. cut sections over 450 words again at `### `;
4. start every section with the document title, and pieces of step 3 also
   with their `## ` heading, so a hit read alone still says where it is from.

The repository keeps whole documents; only the bucket holds sections, named
`<document>--<nn>-<heading>.md`, each with a copy of the document's sidecar.
This does not depend on why Bedrock returns whole files: if it does, the
file is a section.

## Measured after the change

- `support-process.md` (976 words, tables): 7 section files, 16 vectors;
  hits of 97 to 182 words.
- `release-notes-4.1.md` (540 words, no table): 2 section files, 5
  vectors; hits of 43, 70, 198 and 210 words.
- Sections containing a table came back whole in every one of the 81 hits
  where they appeared; no such section is longer than 450 words.
- 81 of the 185 hits were under 150 words: sections that are short in
  themselves, and chunk pieces of sections without tables. Short hits are
  complete but carry little context; the two levers are the 80-word joining
  threshold and the chunk size.
- A request for 10 hits returned 10.
- The workspace filter held in every query: no hit from the other company.

What is still not known is the rule Bedrock follows. If it changes, the
section split keeps hits bounded either way.
