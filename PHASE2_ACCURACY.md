# Phase 2 Directory Audit

Audit date: 2026-09-25. This is a bounded live extraction check, not a census or exhaustive precision/recall evaluation.

## Live crawl coverage

The crawler ran with a 20-page visit budget and 0.2-second request spacing. The directory URLs were verified as university-controlled before fetching. Results include professors parsed from directory pages plus any profile pages reached within the budget.

| University | Official faculty directory | Pages visited | Records extracted | Page errors |
| --- | --- | ---: | ---: | ---: |
| UC Berkeley | <https://www2.eecs.berkeley.edu/Faculty/Lists/CS/faculty.html> | 20 | 128 | 0 |
| Carnegie Mellon University | <https://csd.cmu.edu/people/faculty> | 20 | 118 | 0 |
| Stanford University | <https://www.cs.stanford.edu/people/faculty> | 20 | 95 | 0 |

The cap is a safety limit: it does not imply all records at each university were fetched. Berkeley's static CS list yielded 128 records from its directory page; CMU and Stanford use pagination, which the crawler followed. JavaScript rendering was disabled for these measurements because Stanford exposes a regular `Load More People` pagination link in its HTML.

## Manually Checked Sample

The first five listed faculty from each official CS directory were compared with that directory and corresponding official profile pages. Name and faculty-rank extraction matched 15/15. Published email values and missing-email classifications matched 15/15 in the checked pages: 11 addresses were published and extracted, while four Stanford profiles did not publish an email. The crawler followed Jonathan Aldrich's CMU profile to find an address omitted from the directory row. These figures describe only this purposive sample, not every record in the crawl.

| University | Checked names | Email result |
| --- | --- | --- |
| UC Berkeley | Pieter Abbeel; Ahmed Alaa; Krste Asanović; Babak Ayazifar; Ruzena Bajcsy | 5/5 published addresses matched their official entries |
| Carnegie Mellon | Umut Acar; Anil Ada; Leman Akoglu; Jonathan Aldrich; George Amvrosiadis | 5/5 addresses matched after following the profile link for Jonathan Aldrich |
| Stanford | Sara Achour; Ehsan Adeli; Maneesh Agrawala; Alex Aiken; Nima Anari | Ehsan Adeli's address matched; the other four official profile contact sections did not publish an email and remained missing |

## Limits

- `TAVILY_API_KEY` was not configured, so live search-based university resolution was not run. The UniversityAgent was tested with mocked results, including an off-domain rejection case. The three crawls used their manually verified official directory URLs directly.
- Relevance scores are deterministic keyword-overlap scores, not semantic judgments. A score of zero means the page lacked overlapping explicit research terms, not that the professor is irrelevant.
- Faculty directories differ in how they expose titles, research areas, and contact data. The selection table is a human review checkpoint; verify source links and missing fields before using the records.