# Emailering Development Guide

## Architecture

Keep each integration behind a small module boundary so providers can be replaced and tests can mock network or model calls.

- `src/profreach/agents/`: profile parsing, university resolution, faculty discovery, research enrichment (`research_agent.py`), email writing with validation (`writer_agent.py`), Gmail drafts/sending with throttling (`gmail_agent.py`), and reply detection/follow-ups (`followup_agent.py`). Keep structured outputs in Pydantic models and log the start and result of each agent step.
- `src/profreach/tools/`: CV parsing, swappable web search, polite official-domain page fetching, and publication clients (`scholarly.py`: OpenAlex, Semantic Scholar, optional SerpAPI).
- `src/profreach/graph/outreach.py`: LangGraph pipeline `research -> write -> gmail_drafts -> send`, compiled with a checkpointer and `interrupt_before=["send"]`; `send` loops back to itself while drafts remain open.
- `src/profreach/gmail/`: OAuth desktop flow (`auth.py`) and a thin Gmail API wrapper (`client.py`).
- `src/profreach/db/`: SQLModel tables, SQLite engine setup (which also adds new nullable columns to existing tables), and stores. `outreach_store.py` covers research cache, publications, drafts, send log, and pipeline runs.
- `src/profreach/llm.py`: the single place that constructs the chat model. `LLM_PROVIDER=groq` (default, free tier, `openai/gpt-oss-120b`) or `anthropic`. Structured outputs use `method="json_schema"`. Current Claude models reject `temperature`, so do not pass sampling parameters.
- Web search: `make_search_provider` picks Tavily when `TAVILY_API_KEY` is set, otherwise free DuckDuckGo (`ddgs`).
- `src/profreach/services.py`: wires real providers into agents and the runner; `cli.py` exposes long-running commands.
- `src/profreach/ui/`: Streamlit pages for profile, discovery, research & write, review, and send & follow up.
- `tests/`: isolated unit tests; `tests/fakes.py` has an in-memory Gmail client. Tests must not require live credentials or network access.

Uploaded CV content is processed in memory; a CV to attach is held in Streamlit session memory or read from `CV_ATTACHMENT_PATH`. Do not log CV contents, email bodies, API keys, OAuth credentials, or access tokens. Never infer missing CV facts.

## Safety invariants

- Only drafts with status `approved` are sent; `GmailAgent` re-reads status immediately before sending. Editing an approved draft in the UI returns it to `draft`.
- Sending is throttled by `SEND_MAX_PER_DAY` over a rolling 24 hours and a randomized gap recorded as `SendLog.next_send_after`.
- Research attaches papers only from an author verified by name plus university affiliation (Semantic Scholar may instead be confirmed by at least two shared paper titles with the verified OpenAlex author).
- The writer must cite listed papers by exact title; `validate_email` rejects unknown citations, flattery, placeholders, wrong length, and missing degree/start term.

## Commands

```powershell
uv sync
uv run streamlit run src/profreach/ui/app.py
uv run profreach --help
uv run pytest
uv run ruff check .
uv run ruff format .
```

## Conventions

- Support Python 3.11 and newer; manage dependencies with uv.
- Use Pydantic for external or LLM-provided data and SQLModel for persisted records.
- Keep provider calls injectable so unit tests can use fakes.
- Restrict faculty data to HTTPS pages on the resolved official domain; respect `robots.txt`, identify the crawler, and pace requests. The one exception is a public Google Sheet that an official page itself loads (`tools/embedded_sources.py`); profile links from it are followed only on the official domain.
- When a site refuses plain HTTP (202 challenge, 401, 403), `OfficialPageFetcher` may open the page and its `robots.txt` in a regular Chromium window (`load_in_browser`). Never add stealth, fingerprint spoofing, or challenge solving; if a real browser is still refused, report the page as blocked.
- Never guess email addresses. Keep email source URLs and distinguish page-found, decoded, and missing values.
- Read secrets from environment or `.env`; commit only `.env.example` placeholders.
- Add focused tests for parsing, validation, persistence, and failure behavior when changing those areas.
- Handle per-document and per-record failures without crashing an entire multi-item workflow.
