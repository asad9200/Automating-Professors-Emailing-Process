import logging

import pandas as pd
import streamlit as st
from sqlalchemy.engine import Engine

from profreach.db.models import DraftStatus
from profreach.db.outreach_store import OutreachStore
from profreach.db.profile_store import ProfileStore
from profreach.gmail.auth import GmailAuthError
from profreach.gmail.client import Attachment, GmailError
from profreach.services import followup_agent, gmail_agent, outreach_runner
from profreach.tools.cv_parser import MAX_CV_SIZE_BYTES
from profreach.ui.common import CV_SESSION_KEY, apply_app_theme, page_context, session_attachment
from profreach.ui.setup import render_google_connect

logger = logging.getLogger(__name__)


def _gmail_section(settings) -> bool:
    st.subheader("Gmail")
    connected = render_google_connect(settings, key="send_google")
    if not connected:
        st.caption("Do the one-time Google setup on the **Setup** page in the sidebar.")
    return connected


def _cv_section(settings) -> None:
    st.subheader("CV attachment")
    current = session_attachment()
    if current:
        st.caption(f"Attaching {current.filename} from this session (kept in memory only).")
    elif settings.cv_attachment_path:
        st.caption(f"Attaching the file at CV_ATTACHMENT_PATH: {settings.cv_attachment_path}")
    uploaded = st.file_uploader("CV to attach to emails marked 'Attach CV'", type=["pdf", "docx"])
    if uploaded is not None:
        content = uploaded.getvalue()
        if len(content) > MAX_CV_SIZE_BYTES:
            st.error("The CV must be smaller than 10 MB.")
        else:
            st.session_state[CV_SESSION_KEY] = Attachment(filename=uploaded.name, content=content)


def _send_section(settings, engine: Engine, store: OutreachStore) -> None:
    st.subheader("Send approved emails")
    try:
        agent = gmail_agent(settings, engine, session_attachment())
    except (GmailAuthError, GmailError) as error:
        st.error(str(error))
        return
    approved = store.list_drafts(statuses={DraftStatus.APPROVED})
    pending = store.list_drafts(statuses={DraftStatus.DRAFT})
    throttle = agent.throttle_status()
    st.write(
        f"Approved and waiting: **{len(approved)}** · still in draft: {len(pending)} · "
        f"sent in the last 24 hours: {throttle.sent_last_24h}/{throttle.max_per_day}"
    )
    if not throttle.can_send:
        when = (
            f" Next send allowed at {throttle.next_allowed_at:%H:%M:%S} UTC."
            if (throttle.next_allowed_at)
            else ""
        )
        st.info(throttle.reason + when)

    sync_col, send_col = st.columns(2)
    if sync_col.button("Save all open drafts to Gmail"):
        report = agent.sync_drafts(pending + approved)
        st.success(f"Created {len(report.created)}, updated {len(report.updated)} Gmail drafts.")
        for draft_id, reason in report.skipped.items():
            if reason != "already up to date":
                st.warning(f"Draft {draft_id}: {reason}")
    if send_col.button(
        "Send next approved email",
        type="primary",
        disabled=not approved or not throttle.can_send,
    ):
        report = agent.send_approved(limit=1)
        if report.sent:
            st.success("Sent one approved email.")
        for draft_id, reason in report.failed.items():
            st.error(f"Draft {draft_id}: {reason}")
        if report.stopped_reason:
            st.info(report.stopped_reason)

    st.caption(
        "To send every approved email with the daily cap and randomized "
        f"{settings.send_min_gap_seconds // 60}-{settings.send_max_gap_seconds // 60} minute gaps, "
        "run this in a terminal and leave it open:"
    )
    st.code("uv run profreach send", language="powershell")

    waiting = [run for run in store.list_runs() if run.stage == "awaiting_approval"]
    if waiting:
        with st.expander(f"Pipeline runs paused before sending ({len(waiting)})"):
            for run in waiting:
                cols = st.columns([3, 1])
                cols[0].write(f"`{run.thread_id}` · {len(run.professor_ids)} professors")
                if cols[1].button(
                    "Resume: send next",
                    key=f"resume_{run.thread_id}",
                    disabled=not throttle.can_send,
                ):
                    runner = outreach_runner(settings, engine, attachment=session_attachment())
                    runner.resume_send(run.thread_id, limit=1, wait=False)
                    st.rerun()


def _followup_section(settings, engine: Engine) -> None:
    st.subheader("Replies and follow-ups")
    st.caption(
        f"Marks threads with a reply as replied, and drafts one follow-up for emails with no "
        f"reply after {settings.followup_after_days} days. Follow-ups appear on the Review page "
        "and still need your approval."
    )
    if st.button("Check replies and draft follow-ups"):
        try:
            with st.spinner("Checking Gmail threads..."):
                report = followup_agent(settings, engine).run(ProfileStore(engine).get_profile())
        except (GmailAuthError, GmailError) as error:
            st.error(str(error))
            return
        st.success(
            f"{len(report.replied)} replies detected; "
            f"{len(report.followups_drafted)} follow-ups drafted."
        )
        for draft_id, reason in report.errors.items():
            st.warning(f"Draft {draft_id}: {reason}")


def _log_section(store: OutreachStore) -> None:
    logs = store.list_logs()
    if not logs:
        return
    st.subheader("Send log")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "time (UTC)": log.created_at,
                    "draft": log.draft_id,
                    "action": log.action.value,
                    "recipient": log.recipient or "",
                    "message_id": log.gmail_message_id or "",
                    "thread_id": log.gmail_thread_id or "",
                    "detail": log.detail or "",
                }
                for log in logs
            ]
        ),
        hide_index=True,
        width="stretch",
    )


def render_send_page(engine: Engine | None = None) -> None:
    apply_app_theme()
    settings, database_engine = page_context(engine)
    store = OutreachStore(database_engine)
    st.title("Send & follow up")
    connected = _gmail_section(settings)
    _cv_section(settings)
    if connected:
        _send_section(settings, database_engine, store)
        _followup_section(settings, database_engine)
    _log_section(store)
