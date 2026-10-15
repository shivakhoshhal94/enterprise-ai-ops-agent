"""Streamlit dashboard. All live operations go through the existing HTTP API."""

import os
from datetime import datetime

import streamlit as st

from demo.api_client import APIClient, DEFAULT_API_URL, DemoAPIError
from demo.presentation_mode import render_presentation
from demo.presentation import (
    VERIFIED_PROJECT_METRICS, canceled_count, category_values, financial_lines,
    source_cards, tool_names, workflow_card,
)

st.set_page_config(page_title="CommerceOps Hub", page_icon=":material/dashboard:", layout="wide")
st.markdown("""<style>
.stApp{background:#f5f7fb;color:#172b42}
[data-testid="stSidebar"]{background:#102c40;color:#edf5fa}
[data-testid="stSidebar"] *{color:inherit}
.block-container{padding-top:2rem;max-width:1560px}
h1{letter-spacing:-.035em} h2,h3{letter-spacing:-.02em}
[data-testid="stMetric"]{background:white;border:1px solid #e2e9f0;border-radius:12px;padding:17px}
[data-testid="stMetricValue"]{font-size:1.75rem}
.status{background:white;border:1px solid #e2e9f0;border-radius:10px;padding:13px 15px;font-size:14px}
.ready{color:#14795c}.unavailable{color:#b74444}
.eyebrow{color:#197b87;font-size:12px;letter-spacing:.16em;font-weight:700}
.flow{background:white;border:1px solid #e2e9f0;border-radius:12px;padding:18px;text-align:center;font-weight:600}
button[kind="primary"]{background:#176f80;border:0}
</style>""", unsafe_allow_html=True)

EXAMPLES = {
    "Canceled Orders": "How many orders were canceled?",
    "Top Categories": "Which five product categories have the highest total item price?",
    "Payment Policy": "Does three installments mean payment_value should be multiplied by three?",
    "Support Context": "What support issues have been reported for order 03ecec245220b63fd7f68c1737ba99ba?",
    "Financial + Support": "For order 03ecec245220b63fd7f68c1737ba99ba, compare the recorded financial totals with any support issues.",
    "Create Review Proposal": "Create an internal review proposal for order 03ecec245220b63fd7f68c1737ba99ba because the reported support issue should be reviewed.",
}


@st.cache_data(ttl=15, show_spinner=False)
def readiness(base_url):
    return APIClient(base_url).readiness()


def choose_example(question):
    st.session_state.question = question


def knowledge_panel(result):
    knowledge, _ = source_cards(result)
    if not knowledge:
        return
    st.subheader("Company Knowledge Used")
    st.caption("Knowledge Retrieval · The answer was grounded in internal demo documentation, not official Olist policy.")
    for source in knowledge:
        with st.container(border=True):
            st.write(source["title"])
            st.caption("Section: " + source["section"])
            st.caption("Document: " + source["document"])


def support_panel(result):
    _, external = source_cards(result)
    if not external:
        return
    st.subheader("External Support Context")
    st.caption("MCP / Read-only external source · Synthetic support-system data used for demonstration.")
    for source in external:
        with st.container(border=True):
            st.write("System: " + source["system"])
            st.caption("Order: " + source["order_id"])
            st.write("Ticket IDs: " + ", ".join(source["ticket_ids"]))


def review_panel(result):
    proposal, workflow = result.action_proposal, result.workflow
    if not (proposal or workflow):
        return
    with st.container(border=True):
        st.subheader("Internal Review Request")
        record = workflow or proposal
        cols = st.columns([3, 1, 1])
        cols[0].write("Order: " + record.order_id)
        cols[1].write("Priority: " + record.priority.title())
        cols[2].write("Requires Approval")
        st.write(record.reason)
        if workflow:
            card = workflow_card(workflow)
            if card["pending_without_action"]:
                st.warning(card["label"])
                st.write("No business action has been executed yet.")
            else:
                st.info(card["label"])
            st.caption("Workflow ID: " + workflow.workflow_id)
            st.caption("State returned with this answer; it may have changed since. This dashboard cannot approve or reject workflows.")
        else:
            st.info("Proposal requires approval; no workflow status was returned.")


presentation_mode = st.toggle("Presentation Mode", key="presentation_mode", help="Compact rendering of real results; normal mode keeps operational details.")
st.markdown('<div class="eyebrow">COMMERCE OPERATIONS</div>', unsafe_allow_html=True)
st.title("CommerceOps Hub")
st.write("Operations intelligence and controlled workflows for e-commerce teams")
st.caption("Demo environment using historical Olist data and synthetic support context.")

try:
    client = APIClient(os.getenv("DEMO_API_URL", DEFAULT_API_URL))
except DemoAPIError as error:
    st.error(str(error))
    st.stop()

if presentation_mode:
    render_presentation(client, readiness, EXAMPLES)
    st.stop()

with st.sidebar:
    st.title("CommerceOps")
    st.caption("DEMO ENVIRONMENT")
    st.write("API: " + client.base_url)
    st.caption("Dataset: Olist historical data")
    st.caption("External support: synthetic demo data")
    st.divider()
    st.write("Project capabilities")
    for label in ["Business Q&A", "Analytics", "Knowledge Retrieval", "MCP Integration", "Human Approval"]:
        st.write("• " + label)
    st.divider()
    st.caption("Approval controls are disabled. Review requests create pending workflows; execution requires the protected backend approval process.")

st.subheader("System status")
if st.button("Refresh status"):
    readiness.clear()
try:
    status = readiness(client.base_url)
    labels = {"database": "Database", "analytics": "Analytics", "knowledge": "Knowledge", "workflow": "Workflow", "openai_configuration": "OpenAI", "approval_configuration": "Approval", "mcp_configuration": "MCP"}
    for column, (key, label) in zip(st.columns(7), labels.items()):
        available = status.checks.get(key) == "ready"
        color, text = ("ready", "Ready") if available else ("unavailable", "Unavailable")
        column.markdown(f'<div class="status">{label}<br><strong class="{color}">● {text}</strong></div>', unsafe_allow_html=True)
    st.sidebar.success("Backend ready" if status.ready else "Backend responding; services unavailable")
    if not status.ready:
        st.warning("One or more backend services are unavailable. Some questions may not complete.")
except DemoAPIError:
    st.warning("Backend unavailable. Start the FastAPI service at " + client.base_url + ".")
    st.sidebar.warning("Backend unavailable")
st.caption("Provider, approval and support checks verify configuration only; they do not test live provider calls. Status refreshes at most every 15 seconds or on demand.")

st.subheader("Business overview")
st.caption("Verified project dataset · Recorded reconciliation metrics, not live production KPIs.")
for column, (label, value) in zip(st.columns(6), VERIFIED_PROJECT_METRICS.items()):
    column.metric(label, value)

st.divider()
st.subheader("Ask CommerceOps")
st.write("Ask about orders, customers, payments, analytics, company guidance, or support issues — or choose an example below.")
st.caption("Examples fill the question. Review proposals save a real pending workflow and require approval.")
for column, (label, question) in zip(st.columns(6), EXAMPLES.items()):
    column.button(label, on_click=choose_example, args=(question,), width="stretch")
st.text_area("Ask about orders, payments, analytics, policies, or support issues...", key="question", height=95)
if st.button("Ask CommerceOps", type="primary"):
    question = st.session_state.question
    with st.spinner("Checking the appropriate sources…"):
        try:
            response = client.ask(question)
            st.session_state.latest = (question, response, datetime.now().strftime("%H:%M:%S"))
            st.session_state.pop("request_error", None)
        except DemoAPIError as error:
            st.session_state.request_error = str(error)
if "request_error" in st.session_state:
    st.error(st.session_state.request_error)

if "latest" in st.session_state:
    question, result, answered_at = st.session_state.latest
    st.subheader("Latest response")
    st.caption("Received at " + answered_at + " · Last successful request; not a live workflow monitor.")
    st.write(question)
    names = tool_names(result)
    st.caption("Tools used: " + (", ".join(names) if names else "No tool selected"))
    summary_tab, sources_tab = st.tabs(["Answer & business context", "Sources & attribution"])
    with summary_tab:
        count = canceled_count(result)
        if count is not None:
            st.metric("Canceled Orders", f"{count:,}")
            st.caption("Source: Historical PostgreSQL data · Extracted from the returned answer.")
        mixed = {"get_order_financial_summary", "get_support_context"}.issubset(names)
        if mixed:
            left, right = st.columns(2)
            with left:
                st.subheader("Historical Database")
                rows = financial_lines(result)
                if rows:
                    st.table(rows)
                else:
                    st.info("Structured financial values could not be extracted reliably. See the full answer below.")
            with right:
                support_panel(result)
                _, marker, support_text = result.answer.partition("External synthetic support-system observations")
                if marker:
                    with st.expander("Reported support observations", expanded=True):
                        st.markdown(marker + support_text)
            st.info("These sources are independent. A reported support issue is not automatically a verified financial discrepancy.")
            with st.expander("Full returned answer"):
                st.markdown(result.answer)
        else:
            with st.container(border=True):
                st.markdown(result.answer)
        categories = category_values(result)
        if categories:
            st.subheader("Category analytics · item value")
            st.bar_chart(categories, x="Category", y="Item value (BRL)", horizontal=True, sort="-Item value (BRL)", color="#197b87", height=300)
            st.caption("Values extracted from this live API answer. Chart precision is for display; original amounts remain in the returned answer.")
        elif "run_analytical_query" in names:
            st.caption("Analytical answer shown as returned; no chart is drawn when values cannot be extracted reliably.")
        review_panel(result)
    with sources_tab:
        knowledge_panel(result)
        support_panel(result)
        if not result.knowledge_sources and not result.external_sources:
            st.caption("No company knowledge or external support sources were returned.")
    with st.expander("How this answer was produced"):
        st.write("Tools used: " + (", ".join(names) if names else "No tool selected"))
        evidence = []
        if any(name in names for name in ["get_order_status_summary", "get_customer_order_history", "get_order_financial_summary", "run_analytical_query"]):
            evidence.append("Historical database")
        if result.knowledge_sources:
            evidence.append("Company knowledge")
        if result.external_sources:
            evidence.append("MCP external support context")
        st.write("Evidence: " + (", ".join(evidence) or "No retrieved source returned"))
        st.caption("Only public API metadata is shown; no hidden reasoning or provider objects.")
        if st.checkbox("Show public API response JSON"):
            st.json(result.model_dump())

st.divider()
st.subheader("How the system works")
st.markdown('<div class="flow">User → Operations Dashboard → FastAPI → Operations assistant</div>', unsafe_allow_html=True)
descriptions = {
    "Business Data": "Verified order, customer, item and payment information",
    "Analytics": "Flexible read-only analysis",
    "Company Knowledge": "Internal procedures and policy guidance",
    "MCP Support": "External support-system context",
    "Approval Workflow": "Human approval before controlled business actions",
}
for column, (label, description) in zip(st.columns(5), descriptions.items()):
    with column.container(border=True):
        st.write(label)
        st.caption(description)
