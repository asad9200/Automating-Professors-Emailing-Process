# Emailering

Emailering automates academic outreach: it turns your CV into a profile, finds professors on official university faculty pages, researches their recent papers, drafts a personalized email for each one, saves the drafts to Gmail, and sends only the emails you approve, with rate limiting. It also detects replies and drafts follow-ups.

Nothing is ever sent without your explicit approval in the app. Every default service is free: Groq's free tier for the language model, DuckDuckGo for web search, OpenAlex and Semantic Scholar for publications, and the Gmail API. Groq's free tier has per-minute and per-day request limits; if you hit one, wait and rerun, and completed work is kept.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- `GROQ_API_KEY`: CV extraction and email writing on Groq's free tier (default model `openai/gpt-oss-120b`; get a key at <https://console.groq.com/keys>). To use Claude instead, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.
- Web search is free by default (DuckDuckGo, no key). Set `TAVILY_API_KEY` to use Tavily instead.
- Optional: `SEMANTIC_SCHOLAR_API_KEY` (higher rate limits), `OPENALEX_API_KEY` / `OPENALEX_MAILTO`, `SERPAPI_API_KEY` (adds Google Scholar results)
- A Google account and a Google Cloud OAuth client for Gmail (see below)

## Setup

```powershell
uv sync
Copy-Item .env.example .env
```

Install the browser used for pages that refuse plain requests:

```powershell
uv run playwright install chromium
```

Fill in the keys in `.env`. Never commit `.env`, `credentials.json`, `token.json`, or the `.db` files; all are git-ignored.

## Easiest setup: the Setup page

Start the app (see below) and open **Setup**, the first page in the sidebar. You don't need to edit files:

- **AI writing:** select **Get a free Groq key**, sign in to Groq (Google sign-in works), create a key, paste it, and select **Test and save key**.
- **Gmail:** follow the linked one-time steps, drop the downloaded client file onto the page, then select **Continue with Google** and **Allow**. After that, signing in is one click.
- **Optional keys** (Semantic Scholar, SerpAPI, Tavily) can be added there too; everything works without them.

Keys and tokens are saved only on your computer (`.env`, `credentials.json`, `token.json`). The manual steps below describe the same Google setup in detail.

## Google Cloud OAuth and Gmail API setup

1. Open the [Google Cloud Console](https://console.cloud.google.com/), select the project picker at the top, and choose **New Project**. Name it (for example, `emailering`) and create it.
2. With that project selected, open **APIs & Services > Library**, search for **Gmail API**, open it, and select **Enable**.
3. Open **Google Auth Platform > Branding** (older consoles: **APIs & Services > OAuth consent screen**) and select **Get started**. Enter an app name and your support email, choose audience **External**, enter a developer contact email, and finish.
4. Open **Google Auth Platform > Audience**. While the app is in **Testing**, add your own Gmail address under **Test users**. Only listed test users can sign in.
5. Open **Google Auth Platform > Data access > Add or remove scopes** and add:
   - `https://www.googleapis.com/auth/gmail.compose` (create, update, and send drafts)
   - `https://www.googleapis.com/auth/gmail.readonly` (read threads to detect replies)
6. Open **Google Auth Platform > Clients > Create client** (older consoles: **APIs & Services > Credentials > Create credentials > OAuth client ID**). Choose application type **Desktop app**, name it, and create it.
7. Download the client JSON and save it in the project root as `credentials.json` (or set `GMAIL_CREDENTIALS_PATH`).
8. Connect Gmail by selecting **Connect Gmail** on the **Send & follow up** page, or by running `uv run profreach connect-gmail`. A browser window opens for Google consent. Because the app is unverified and in testing, Google shows a warning; choose **Continue** for your own account. The token is saved locally to `token.json` (`GMAIL_TOKEN_PATH`).

Refresh tokens for apps in **Testing** status expire after 7 days. When that happens, connect again. To revoke access, select **Disconnect** in the app and remove the app at <https://myaccount.google.com/permissions>.

## Run the app

```powershell
uv run streamlit run src/profreach/ui/app.py
```

The pages follow the workflow:

1. **Profile**: upload a PDF/DOCX CV and choose **Extract profile from CV**, or enter details by hand. The page lists any missing fields; the writer uses only facts you provide. The CV is processed in memory; only the structured profile is stored.
2. **Discover professors**: enter a university, location, department, field, research interests, and degree. Emailering resolves the official domain, crawls the faculty directory (pagination and profile pages, `robots.txt`, polite delays), scores relevance 0-100 with a reason, and records every email with its source URL and confidence (`found_on_page`, `decoded`, or `missing`). Addresses are never guessed. Select professors and optionally export a CSV. If a university's firewall refuses the crawler, Emailering opens that page in a regular Chromium window, as you would, and still obeys the site's real `robots.txt`. It does not hide the automation or solve challenges. If the site still refuses a real browser, **Import saved page** is the last resort. When a page builds its list with JavaScript from a public Google Sheet (as University of Alberta Computing Science does), Emailering reads Google's public CSV export of that sheet, then follows each person's official profile page for title and email.
3. **Research & write**: enter your start term (for example, `Fall 2027`) and run the pipeline. For each selected professor, it:
   - Looks up OpenAlex and Semantic Scholar authors. It keeps an author only when both the name and the university affiliation match, so papers by someone with the same name are not attached.
   - Adds optional Google Scholar results through SerpAPI.
   - Reads the lab or profile page for statements about openings.
   - Caches everything for `RESEARCH_CACHE_DAYS`.
   - Drafts a 150-250 word email and subject line citing one or two of the professor's actual papers by exact title, plus a private "why this match" note. Drafts that cite unknown papers, flatter, include placeholders, or omit the degree or start term are sent back to the model to fix.
4. **Review emails**: edit, regenerate with instructions, approve, or reject each email. Editing an approved email returns it to draft, so the final text is always the approved text.
5. **Send & follow up**:
   - Connect Gmail, optionally upload the CV to attach (kept in memory for the session), and save drafts to Gmail.
   - Send approved emails one at a time from the page. To send them all, run `uv run profreach send` in a terminal and leave it open. It sends at most `SEND_MAX_PER_DAY` (default 20) per rolling 24 hours, with a random `SEND_MIN_GAP_SECONDS`-`SEND_MAX_GAP_SECONDS` gap (default 2-5 minutes).
   - Gmail message and thread IDs are recorded in the send log.
   - **Check replies and draft follow-ups** marks answered threads as replied and drafts one short follow-up for emails with no reply after `FOLLOWUP_AFTER_DAYS` (default 10). Follow-ups go to the Review page and are sent in the same thread only after approval.

### Command line

```powershell
uv run profreach connect-gmail          # OAuth consent
uv run profreach runs                   # list pipeline runs and their stage
uv run profreach send [--limit N]       # send all approved emails with throttling
uv run profreach send --thread-id ID    # resume a paused pipeline run instead
uv run profreach followups              # detect replies, draft follow-ups
```

## How the pipeline resumes

The pipeline is a LangGraph `StateGraph` (`research -> write -> gmail_drafts -> send`) compiled with a SQLite checkpointer (`CHECKPOINT_DB_PATH`) and `interrupt_before=["send"]`. Every run pauses before sending. Your approvals change draft status in the database, and resuming sends approved drafts only, then pauses again while any drafts remain open. Status fields on research records and drafts (`draft`, `approved`, `sent`, `replied`, `rejected`) let a rerun skip finished work: cached research is reused, and existing drafts are never overwritten. One failed professor, page, or provider call is logged and skipped without stopping the run.

## Test and lint

```powershell
uv run pytest
uv run ruff check .
uv run ruff format .
```

The tests use in-memory SQLite, saved HTML fixtures, mocked HTTP transports, and fake model and Gmail clients. They never call Groq, Anthropic, Tavily, DuckDuckGo, OpenAlex, Semantic Scholar, SerpAPI, Gmail, or university websites. See [PHASE2_ACCURACY.md](PHASE2_ACCURACY.md) for the live faculty-directory audit.

## Project structure

- `src/profreach/agents/`: profile, university, discovery, research, writer, Gmail, and follow-up agents
- `src/profreach/tools/`: CV parsing, web search, official-page fetching, and publication APIs
- `src/profreach/graph/`: the LangGraph outreach pipeline and checkpointer
- `src/profreach/gmail/`: OAuth desktop flow and the Gmail API wrapper
- `src/profreach/db/`: SQLModel tables and stores
- `src/profreach/ui/`: Streamlit pages
- `src/profreach/cli.py`: terminal commands
- `tests/`: unit tests and fixtures
