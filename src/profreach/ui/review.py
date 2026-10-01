import logging

import streamlit as st
from sqlalchemy.engine import Engine

from profreach.agents.writer_agent import EmailComposition, WriterAgent, WriterError, word_count
from profreach.db.discovery_store import DiscoveryStore
from profreach.db.models import DraftStatus, EmailConfidence, EmailDraft, Professor
from profreach.db.outreach_store import OutreachStore
from profreach.db.profile_store import ProfileStore
from profreach.ui.common import apply_app_theme, page_context, target_selector

logger = logging.getLogger(__name__)
_EDITABLE = {DraftStatus.DRAFT, DraftStatus.APPROVED}


def _regenerate(
    draft: EmailDraft,
    professor: Professor,
    instructions: str,
    store: OutreachStore,
    engine: Engine,
    settings,
) -> None:
    profile = ProfileStore(engine).get_profile()
    target = store.get_target(professor.university_id)
    if profile is None or target is None:
        st.error("Save your profile first.")
        return
    writer = WriterAgent(settings)
    try:
        with st.spinner("Rewriting..."):
            if draft.is_followup:
                parent = store.get_draft(draft.parent_draft_id) if draft.parent_draft_id else None
                if parent is None:
                    st.error("The original email for this follow-up is missing.")
                    return
                subject, body = writer.write_followup(
                    profile, professor, parent.subject, parent.body, 0
                )
                store.update_draft(draft.id, subject=subject, body=body, status=DraftStatus.DRAFT)
                return
            composition = writer.write(
                profile,
                professor,
                store.get_research(professor.id),
                store.list_publications(professor.id),
                target.target_degree,
                draft.start_term,
                instructions=instructions.strip() or None,
                previous=EmailComposition(
                    subject=draft.subject,
                    body=draft.body,
                    cited_paper_titles=draft.cited_paper_titles,
                    match_note=draft.match_note,
                ),
                attach_cv=draft.attach_cv,
                target=target,
                colleagues=[
                    other.name
                    for _, other in store.list_drafts(target_id=target.id)
                    if other.id != professor.id
                ],
            )
    except WriterError as error:
        st.error(str(error))
        return
    except Exception:
        logger.exception("Regeneration failed for draft %s", draft.id)
        st.error("Regeneration failed. The current draft was kept.")
        return
    store.update_draft(
        draft.id,
        subject=composition.subject,
        body=composition.body,
        match_note=composition.match_note,
        cited_paper_titles=composition.cited_paper_titles,
        status=DraftStatus.DRAFT,
    )


def _draft_card(
    draft: EmailDraft,
    professor: Professor,
    store: OutreachStore,
    engine: Engine,
    settings,
) -> None:
    label = "Follow-up" if draft.is_followup else "Email"
    with st.container(border=True):
        st.markdown(
            f"**{label} to {professor.name}** · {professor.title or ''} · "
            f"relevance {professor.relevance_score} · status **{draft.status.value}**"
        )
        if professor.email:
            confidence = professor.email_confidence
            note = (
                " (decoded from obfuscated text; double-check)"
                if (confidence is EmailConfidence.DECODED)
                else ""
            )
            st.caption(f"To: {professor.email}{note} · source: {professor.email_source_url}")
        else:
            st.error("No published email address; this draft cannot be sent.")
        if draft.match_note:
            with st.expander("Notes for you (not sent)", expanded=draft.status.value == "draft"):
                st.markdown(draft.match_note)
        if draft.cited_paper_titles:
            st.caption("Cites: " + " | ".join(draft.cited_paper_titles))
        if draft.last_error:
            st.warning(draft.last_error)

        editable = draft.status in _EDITABLE
        # Versioned keys so regenerated or saved text replaces what the widgets show.
        version = f"{draft.id}_{draft.updated_at.timestamp():.6f}"
        subject = st.text_input(
            "Subject", value=draft.subject, key=f"subject_{version}", disabled=not editable
        )
        body = st.text_area(
            "Body", value=draft.body, height=320, key=f"body_{version}", disabled=not editable
        )
        st.caption(f"{word_count(body)} words")
        attach_cv = st.checkbox(
            "Attach CV",
            value=draft.attach_cv,
            key=f"attach_{version}",
            disabled=not editable,
        )
        if not editable:
            if draft.sent_at:
                st.caption(
                    f"Sent {draft.sent_at:%Y-%m-%d %H:%M} UTC · thread {draft.gmail_thread_id}"
                )
            if draft.status is DraftStatus.REJECTED and st.button(
                "Restore to draft", key=f"restore_{draft.id}"
            ):
                store.update_draft(draft.id, status=DraftStatus.DRAFT)
                st.rerun()
            return

        changed = (subject, body, attach_cv) != (draft.subject, draft.body, draft.attach_cv)
        save_col, approve_col, reject_col = st.columns(3)
        if save_col.button("Save edits", key=f"save_{draft.id}", disabled=not changed):
            # Edited text needs a fresh approval before it can be sent.
            store.update_draft(
                draft.id,
                subject=subject,
                body=body,
                attach_cv=attach_cv,
                status=DraftStatus.DRAFT,
            )
            st.rerun()
        approve_label = (
            "Approved ✓"
            if draft.status is DraftStatus.APPROVED and not changed
            else ("Save and approve" if changed else "Approve")
        )
        if approve_col.button(
            approve_label,
            key=f"approve_{draft.id}",
            type="primary",
            disabled=not professor.email or (draft.status is DraftStatus.APPROVED and not changed),
        ):
            store.update_draft(
                draft.id,
                subject=subject,
                body=body,
                attach_cv=attach_cv,
                status=DraftStatus.APPROVED,
                last_error=None,
            )
            st.rerun()
        if reject_col.button("Reject", key=f"reject_{draft.id}"):
            store.update_draft(draft.id, status=DraftStatus.REJECTED)
            st.rerun()

        with st.expander("Regenerate"):
            instructions = st.text_area(
                "Instructions",
                key=f"instructions_{draft.id}",
                placeholder="e.g. Emphasize my robotics internship; make it shorter.",
            )
            if st.button("Regenerate draft", key=f"regenerate_{draft.id}"):
                _regenerate(draft, professor, instructions, store, engine, settings)
                st.rerun()


def render_review_page(engine: Engine | None = None) -> None:
    apply_app_theme()
    settings, database_engine = page_context(engine)
    store = OutreachStore(database_engine)
    st.title("Review emails")
    st.caption(
        "Edit, regenerate, approve, or reject each email. Only approved emails can be sent, "
        "and editing an approved email returns it to draft."
    )
    target = target_selector(DiscoveryStore(database_engine).list_targets(), "review_target")
    if target is None:
        return
    statuses = st.multiselect(
        "Show",
        list(DraftStatus),
        default=[DraftStatus.DRAFT, DraftStatus.APPROVED],
        format_func=lambda status: status.value,
    )
    drafts = store.list_drafts(target_id=target.id, statuses=set(statuses) or None)
    counts = {
        status: sum(draft.status is status for draft, _ in store.list_drafts(target_id=target.id))
        for status in DraftStatus
    }
    st.write(" · ".join(f"{status.value}: {count}" for status, count in counts.items()))
    if not drafts:
        st.info("No emails match this filter. Write drafts on the Research & write page.")
        return
    for draft, professor in drafts:
        _draft_card(draft, professor, store, database_engine, settings)
