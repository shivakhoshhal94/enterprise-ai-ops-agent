"""Compact rendering of the same live API response, without another model call."""

from datetime import datetime
from html import escape

import altair as alt
import streamlit as st

from demo.api_client import DemoAPIError, AskResponse
from demo.visualizations import render_structured
from demo.presentation import (
    VERIFIED_PROJECT_METRICS, canceled_count, capability_labels, presentation_categories,
    compact_brl, concise_answer, financial_lines, tool_names, workflow_card, chart_rows, choose_visualization,
)


def choose_screen(screen):
    st.session_state.presentation_screen = screen


def choose_question(question):
    st.session_state.question = question


def overview(client, readiness):
    st.subheader("System status")
    try:
        status = readiness(client.base_url)
        labels = {"database": "Database", "analytics": "Analytics", "knowledge": "Knowledge", "workflow": "Workflow", "openai_configuration": "OpenAI", "mcp_configuration": "MCP"}
        for column, (key, label) in zip(st.columns(6), labels.items()):
            available = status.checks.get(key) == "ready"
            color, text = ("ready", "Ready") if available else ("unavailable", "Unavailable")
            column.markdown(f'<div class="status">{label}<br><strong class="{color}">● {text}</strong></div>', unsafe_allow_html=True)
        if not status.ready:
            st.warning("One or more backend services are unavailable.")
        with st.expander("Status details"):
            st.write(status.model_dump())
            st.caption("Provider and support checks verify configuration, not live provider calls. Approval configuration is included in overall readiness.")
    except DemoAPIError:
        st.warning("Backend unavailable. Start the FastAPI service.")
    st.subheader("Business overview")
    st.caption("Verified demo dataset · Recorded snapshot metrics, not live production KPIs.")
    labels = ["Orders", "Order Items", "Payments", "Agent Tools", "Documents", "Chunks"]
    for column, label, value in zip(st.columns(6), labels, VERIFIED_PROJECT_METRICS.values()):
        column.metric(label, value)
    st.info("Ask questions across business data, company guidance, and support context. Controlled actions require human approval.")


def ask(client, examples):
    st.subheader("Ask CommerceOps")
    st.write("Ask about orders, customers, payments, analytics, company guidance, or support issues — or choose an example below.")
    for column, (label, question) in zip(st.columns(6), examples.items()):
        column.button(label, on_click=choose_question, args=(question,), width="stretch")
    st.caption("Examples fill the question. A review proposal saves a real pending workflow; nothing is approved automatically.")
    with st.form("presentation_question"):
        st.text_area("Your question", key="question", height=90)
        submitted = st.form_submit_button("Ask CommerceOps", type="primary")
    if submitted:
        question = st.session_state.question
        with st.spinner("Checking the appropriate sources…"):
            try:
                result = client.ask(question)
                st.session_state.latest = (question, result, datetime.now().strftime("%H:%M:%S"))
                st.session_state.pop("request_error", None)
                choose_screen("result")
                st.rerun()
            except DemoAPIError as error:
                st.session_state.request_error = str(error)
    if "request_error" in st.session_state:
        st.error(st.session_state.request_error)


def review(result):
    record = result.workflow or result.action_proposal
    st.subheader("Internal Review Request")
    with st.container(border=True):
        columns = st.columns([3, 1, 1])
        columns[0].write("Order")
        columns[0].code(record.order_id, language=None)
        columns[1].metric("Priority", record.priority.title())
        columns[2].metric("Requires Approval", "Yes")
        st.write(record.reason)
        if result.workflow:
            state = workflow_card(result.workflow)
            if state["pending_without_action"]:
                st.warning(state["label"])
                st.write("No business action has been executed yet.")
            else:
                st.info("Status: " + state["label"])
        else:
            st.info("Approval required · No workflow status returned.")
    st.caption("Returned workflow state. Approval controls are disabled in this dashboard.")


def financial_support(result):
    left, right = st.columns(2)
    with left.container(border=True):
        st.subheader("Historical Database")
        labels = {"item price total": "Item total", "freight total": "Freight", "items plus freight": "Items + freight", "recorded payment total": "Recorded payments", "recorded payment difference": "Difference"}
        rows = financial_lines(result)
        if rows:
            for row in rows:
                label, value = st.columns([3, 2])
                label.write(labels[row["Financial fact"].lower()])
                value.write(row["Recorded value"])
        else:
            st.caption("See the full response for financial values; no reliable structured extraction is available.")
    with right.container(border=True):
        st.subheader("External Support Context")
        for source in result.external_sources:
            st.write("Support system: " + source.system)
            st.metric("Returned tickets", len(source.ticket_ids))
            st.write(" · ".join(source.ticket_ids))
        st.caption("Synthetic support-system data used for demonstration.")
    st.info("These sources are independent. A reported support issue is not automatically a verified financial discrepancy.")


def knowledge(result):
    st.subheader("Company Knowledge")
    with st.container(border=True):
        st.markdown(concise_answer(result))
    for column, source in zip(st.columns(min(3, len(result.knowledge_sources))), result.knowledge_sources[:3]):
        with column.container(border=True):
            st.write(source.title)
            st.caption("Section: " + source.section)
    st.caption("Internal demo documentation · Not official Olist policy.")


def analytics(result, question):
    rows = chart_rows(result)
    if not rows:
        st.subheader("Read-only Analytics")
        st.markdown(result.answer)
        return
    item_value = "item price" in question.lower() or "item value" in question.lower()
    title = "Top Product Categories by Item Value" if item_value and "highest" in question.lower() else "Category Ranking"
    st.subheader(title)
    data = alt.Data(values=rows)
    chart = alt.Chart(data).mark_bar(color="#197b87", cornerRadiusEnd=4).encode(
        x=alt.X("Item value (BRL):Q", title="Item value (BRL)" if item_value else "Returned value (BRL)", axis=alt.Axis(format="~s")),
        y=alt.Y("Display category:N", sort="-x", title=None, axis=alt.Axis(labelLimit=250)),
        tooltip=[alt.Tooltip("Display category:N"), alt.Tooltip("Category:N", title="Source category"), alt.Tooltip("Item value (BRL):Q", format=",.2f")],
    ).properties(height=220)
    st.altair_chart(chart, width="stretch")
    use_millions = all(row["Item value (BRL)"] >= 100_000 for row in rows)
    if len(rows) <= 5:
        for column, row in zip(st.columns(len(rows)), rows):
            column.metric(row["Display category"], compact_brl(row["Item value (BRL)"], millions=use_millions))
    st.caption("Amounts formatted from this response; exact values are available in the full response.")


def result_screen():
    question, result, _ = st.session_state.latest
    result = AskResponse.model_validate(result.model_dump())  # Preserve older in-memory responses after UI updates.
    st.caption("Question")
    st.write(question)
    names = tool_names(result)
    visual = "workflow" if result.workflow or result.action_proposal else "structured" if result.presentation_data else choose_visualization(result)
    if visual == "workflow":
        review(result)
    elif result.presentation_data:
        render_structured(result)
    elif visual == "financial_support":
        financial_support(result)
    elif visual == "knowledge":
        knowledge(result)
    elif visual == "ranking":
        analytics(result, question)
    elif visual == "financial":
        st.subheader("Financial Data")
        st.table(financial_lines(result))
    elif visual == "support":
        st.subheader("External Support Context")
        for source in result.external_sources:
            with st.container(border=True):
                st.write(source.system)
                st.metric("Returned tickets", len(source.ticket_ids))
                st.write(" · ".join(source.ticket_ids))
        st.caption("Synthetic support-system data used for demonstration.")
    else:
        count = canceled_count(result)
        if count is not None:
            st.metric("Canceled Orders", f"{count:,}")
            st.caption("Historical PostgreSQL data")
        with st.container(border=True):
            answer = concise_answer(result)
            st.markdown(answer if len(answer) <= 700 else answer[:700].rsplit(" ", 1)[0] + "…")
            if len(answer) > 700:
                st.caption("Verbatim excerpt; the complete answer is available below.")
    labels = capability_labels(result)
    if not labels and (result.workflow or result.action_proposal):
        labels = ["Review Proposal"]  # The structured response proves this capability, even without a tool trace.
    st.markdown('<div class="capabilities">Capabilities used: ' + (" · ".join(escape(label) for label in labels) or "No tool selected") + '</div>', unsafe_allow_html=True)
    with st.expander("View full response"):
        st.markdown(result.answer)
    with st.expander("Tools and source attribution"):
        st.write("Exact tool names: " + (", ".join(names) or "No tool selected"))
        st.write("Knowledge sources")
        st.json([source.model_dump() for source in result.knowledge_sources])
        st.write("External sources")
        st.json([source.model_dump() for source in result.external_sources])
        if result.workflow:
            st.write(result.workflow.model_dump())
        if result.presentation_data:
            st.write("Bounded returned tool data")
            st.json([item.model_dump() for item in result.presentation_data])


def render_presentation(client, readiness, examples):
    st.markdown("""<style>
    [data-testid="stSidebar"],[data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stSidebarCollapsedControl"]{display:none}
    .block-container{max-width:1200px;margin:0 auto;padding-top:1.25rem;padding-bottom:1rem}
    .block-container [data-testid="stVerticalBlock"]{gap:.7rem}
    .block-container h1{margin-bottom:.2rem}
    [data-testid="stMetricValue"]{font-size:2rem}
    .capabilities{margin:18px 0;padding:12px 16px;border:1px solid #dce8eb;border-radius:10px;color:#176f80;background:#eef7f7;font-weight:600}
    </style>""", unsafe_allow_html=True)
    overview_button, ask_button, result_button, _ = st.columns([1, 1, 1, 5])
    overview_button.button("Overview", on_click=choose_screen, args=("overview",), width="stretch")
    ask_button.button("Ask a question", on_click=choose_screen, args=("ask",), width="stretch")
    if "latest" in st.session_state:
        result_button.button("Latest result", on_click=choose_screen, args=("result",), width="stretch")
    screen = st.session_state.get("presentation_screen", "overview")
    if screen == "result" and "latest" in st.session_state:
        result_screen()
    elif screen == "ask":
        ask(client, examples)
    else:
        overview(client, readiness)
