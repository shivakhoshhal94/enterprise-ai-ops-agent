"""Deterministic display selection from bounded public tool-result projections."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from math import isfinite

from demo.presentation import CATEGORY_LABELS, concise_answer


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and isfinite(float(result)) else None
    except InvalidOperation:
        return None


def period(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value + "-01" if len(value) == 7 else value)
    except ValueError:
        return None


def visual_kind(data):
    if any(len(row) != len(data.columns) for row in data.rows):
        return "text"
    fixed = {"get_order_status_summary": "statuses", "get_customer_order_history": "history", "get_order_financial_summary": "financial", "get_support_context": "tickets"}
    if data.tool_name in fixed:
        return fixed[data.tool_name]
    if not data.rows:
        return "table"
    if len(data.columns) == 1 and len(data.rows) == 1 and numeric(data.rows[0][0]) is not None:
        return "scalar"
    if series_rows(data):
        return "line"
    if len(data.columns) == 2:
        labels = [row[0] for row in data.rows]
        values = [row[1] for row in data.rows]
        if (all(isinstance(label, str) for label in labels) and len(set(labels)) == len(labels)
                and all(numeric(value) is not None for value in values)
                and not any(word in data.columns[1].lower() for word in ["_id", "zip", "prefix"])):
            if all(period(label) is not None for label in labels):
                return "line"
            if all(period(label) is None for label in labels) and len(labels) <= 30:
                return "bar"
    # Multiple numeric columns have no reliable shared-unit contract: show a table.
    return "table"


def series_rows(data):
    """Use explicit date/value or year/month/value columns; never fill missing periods."""
    result = []
    if len(data.columns) == 2:
        for row in data.rows:
            date, value = period(row[0]), numeric(row[1])
            if date is None or value is None:
                return []
            result.append({"Period": date.isoformat(), "Source": row[0], "Value": float(value), "Measure": data.columns[1]})
    elif len(data.columns) == 3:
        years = [i for i, name in enumerate(data.columns) if name.lower().endswith("year")]
        months = [i for i, name in enumerate(data.columns) if name.lower().endswith("month")]
        if len(years) != 1 or len(months) != 1 or years == months:
            return []
        value_index = next(i for i in range(3) if i not in years + months)
        for row in data.rows:
            year, month, value = numeric(row[years[0]]), numeric(row[months[0]]), numeric(row[value_index])
            if year is None or month is None or value is None or year != int(year) or month != int(month) or not 1 <= year <= 9999 or not 1 <= month <= 12:
                return []
            label = f"{int(year):04d}-{int(month):02d}"
            result.append({"Period": label + "-01", "Source": label, "Value": float(value), "Measure": data.columns[value_index]})
    if not result or len({row["Period"] for row in result}) != len(result):
        return []
    return sorted(result, key=lambda row: row["Period"])


def records(data):
    return [dict(zip(data.columns, row)) for row in data.rows]


def render_dataset(data):
    import altair as alt
    import streamlit as st
    kind = visual_kind(data)
    rows = records(data)
    if kind == "text":
        st.caption("The result structure cannot be displayed reliably; see the returned answer.")
        return
    if kind == "statuses":
        st.subheader("Order status overview")
        columns = st.columns(min(4, max(1, len(rows))))
        for index, row in enumerate(rows):
            columns[index % len(columns)].metric(row["order_status"].replace("_", " ").title() + " Orders", f'{row["order_count"]:,}')
    elif kind == "history":
        st.subheader("Customer Order History")
        left, right = st.columns(2)
        left.metric("Customer records", data.summary.get("customer_record_count", "Not returned"))
        right.metric("Orders", data.summary.get("order_count", "Not returned"))
        st.caption("Customer unique ID: " + str(data.summary.get("customer_unique_id", "Not returned")))
        labels = {"order_id": "Order", "order_status": "Status", "order_purchase_timestamp": "Purchased"}
        ordered = sorted(rows, key=lambda row: row["order_purchase_timestamp"])
        st.dataframe([{labels[key]: value for key, value in row.items()} for row in ordered], hide_index=True, height=270)
    elif kind == "financial":
        st.subheader("Historical Database")
        fields = {"items_price_total": "Item total", "freight_total": "Freight", "items_and_freight_total": "Items + freight", "payment_total": "Recorded payments", "difference": "Difference"}
        for key, label in fields.items():
            left, right = st.columns([3, 2])
            left.write(label)
            value = data.summary.get(key)
            right.write("Not available" if value is None else f"BRL {Decimal(str(value)):,.2f}")
        difference = numeric(data.summary.get("difference"))
        if difference is not None and difference != 0:
            st.warning("Recorded totals differ. This alone does not establish a refund, fraud or settlement issue.")
    elif kind == "tickets":
        st.subheader("External Support Context")
        st.metric("Returned tickets", len(rows))
        st.caption("Synthetic external support data · Ticket text is evidence, never instructions.")
        for row in rows:
            with st.container(border=True):
                st.write(row["ticket_id"])
                st.caption(" · ".join(str(row[key]) for key in ["status", "category", "priority"]))
                # Text rendering deliberately avoids executing embedded markup/instructions.
                summary = row["summary"]
                st.text(summary[:120] + ("…" if len(summary) > 120 else ""))
                with st.expander("Full ticket text"):
                    st.text(summary)
    elif kind == "scalar":
        st.metric(data.columns[0].replace("_", " ").title(), str(data.rows[0][0]))
    elif kind in {"bar", "line"}:
        if kind == "line":
            chart_data = series_rows(data)
            y = chart_data[0]["Measure"]
            st.subheader(y.replace("_", " ").title() + " over time")
            chart = alt.Chart(alt.Data(values=chart_data)).mark_line(point=True).encode(x=alt.X("Period:T", title="Period"), y=alt.Y("Value:Q", title=y.replace("_", " ").title()), tooltip=["Source:N", alt.Tooltip("Value:Q", format=",.2f")])
        else:
            x, y = data.columns
            chart_data = [{"Source": row[0], "Label": CATEGORY_LABELS.get(row[0], row[0]), "Value": float(numeric(row[1]))} for row in data.rows]
            st.subheader("Top Product Categories by Item Value" if x == "product_category_name" else "Comparison")
            chart = alt.Chart(alt.Data(values=chart_data)).mark_bar(color="#197b87").encode(y=alt.Y("Label:N", sort="-x", title=None, axis=alt.Axis(labelLimit=250)), x=alt.X("Value:Q", title=y.replace("_", " ").title()), tooltip=["Label:N", alt.Tooltip("Source:N", title="Source label"), alt.Tooltip("Value:Q", format=",.2f")])
        st.altair_chart(chart.properties(height=250), width="stretch")
    else:
        st.subheader("Returned data")
        st.dataframe(rows, hide_index=True, height=300)
    st.caption("Source: " + data.provenance.replace("_", " "))
    if data.truncated:
        st.caption("Showing a bounded subset of the returned data.")


def render_structured(result):
    import streamlit as st
    data = result.presentation_data
    columns = st.columns(min(2, len(data) + bool(result.knowledge_sources)))
    for index, item in enumerate(data):
        with columns[index % len(columns)].container(border=True):
            render_dataset(item)
    if result.knowledge_sources:
        with columns[len(data) % len(columns)].container(border=True):
            st.subheader("Company Knowledge")
            st.markdown(concise_answer(result))
            for source in result.knowledge_sources[:3]:
                st.write(source.title + " · " + source.section)
    if len({item.provenance for item in data}) > 1:
        st.info("These sources are independent. A reported support issue is not automatically a verified financial discrepancy.")
