import html
import requests
import streamlit as st
from pypdf import PdfReader
from io import BytesIO

st.set_page_config(page_title="Tessera Agentic RAG", layout="wide")

st.markdown("""
<style>
.stApp {
  background-color: #0b0f14;
  color: #e6edf3;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}

[data-testid="stSidebar"] {
  background-color: #11161c;
  border-right: 1px solid #252c34;
}

[data-testid="stSidebar"] * {
  color: #e6edf3;
}

[data-testid="stSidebar"] input,
[data-testid="stSidebar"] select,
[data-testid="stSidebar"] textarea {
  background-color: #0b0f14;
  border: 1px solid #2a333d;
  color: #e6edf3;
}

.stButton > button {
  background: #4f8cff;
  color: #0b0f14;
  font-weight: 600;
  border-radius: 10px;
  border: none;
}

.stButton > button:hover {
  background: #6a9eff;
}

.app-header {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 0 8px 0;
  border-bottom: 1px solid #252c34;
  margin-bottom: 18px;
}

.logo-mark {
  width: 44px;
  height: 44px;
  background: #4f8cff;
  color: #0b0f14;
  font-weight: 700;
  font-size: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 12px;
}

.wordmark {
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: #f2f5f9;
  line-height: 1.1;
}

.tagline {
  font-size: 14px;
  color: #8b98a9;
}

.chat-row {
  display: flex;
  margin: 12px 0;
}

.user-row {
  justify-content: flex-end;
}

.assistant-row {
  justify-content: flex-start;
}

.chat-bubble {
  max-width: 78%;
  padding: 14px 16px;
  border-radius: 16px;
  border: 1px solid #252c34;
  background: #11161c;
}

.user-bubble {
  background: #1a2635;
  border-color: #2f4a6d;
}

.assistant-bubble {
  background: #11161c;
}

.answer-card {
  background: #0f141a;
  border: 1px solid #252c34;
  border-radius: 12px;
  padding: 16px 18px;
  margin-bottom: 12px;
  color: #e6edf3;
  font-size: 15px;
  line-height: 1.5;
  white-space: pre-wrap;
}

.metrics-card {
  background: #0f141a;
  border: 1px solid #252c34;
  border-radius: 12px;
  padding: 12px 14px;
  margin-bottom: 12px;
}

.metrics-row {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
  align-items: center;
}

.metric-item {
  min-width: 100px;
}

.metric-label {
  font-size: 11px;
  font-weight: 600;
  color: #8b98a9;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 4px;
}

.metric-value {
  font-size: 16px;
  font-weight: 600;
  color: #e6edf3;
  font-variant-numeric: tabular-nums;
}

.source-chip-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.source-chip {
  padding: 4px 8px;
  border-radius: 8px;
  background: #1a2129;
  border: 1px solid #2a333d;
  font-size: 12px;
  color: #b7c0cc;
}

.route-badge {
  display: inline-block;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  color: #ffffff;
  background: #3a3f47;
  border: 1px solid #4a5568;
}

.route-badge.vector {
  background: #1f6feb;
  border-color: #4f8cff;
}

.route-badge.query {
  background: #9c6ade;
  border-color: #b392f0;
}

.route-badge.agentic {
  background: #087ea4;
  border-color: #22b8cf;
}

.route-badge.hybrid {
  background: #0f766e;
  border-color: #2dd4bf;
}

.route-badge.default {
  background: #3a3f47;
  border-color: #4a5568;
}

.empty-state {
  color: #8b98a9;
  padding: 28px;
  text-align: center;
  border: 1px dashed #2a333d;
  border-radius: 12px;
  margin: 10px 0;
}

.health-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  font-size: 14px;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}

.dot.online {
  background: #2dd4bf;
  box-shadow: 0 0 8px rgba(45, 212, 191, 0.5);
}

.dot.offline {
  background: #ef4444;
  box-shadow: 0 0 8px rgba(239, 68, 68, 0.5);
}

.dot.unknown {
  background: #8b98a9;
}
</style>
""", unsafe_allow_html=True)

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "health" not in st.session_state:
    st.session_state.health = None
if "health_detail" not in st.session_state:
    st.session_state.health_detail = ""
if "retriever_type" not in st.session_state:
    st.session_state.retriever_type = "auto"
if "retriever_top_k" not in st.session_state:
    st.session_state.retriever_top_k = 5


def _extract_detail(resp):
    try:
        detail = resp.json().get("detail")
        if isinstance(detail, list):
            return "; ".join(str(d) for d in detail)
        return str(detail if detail else resp.text[:300])
    except Exception:
        return resp.text[:300] or "No response body"


def _extract_pdf_text(pdf_bytes):
    try:
        pdf = PdfReader(BytesIO(pdf_bytes))
        text = ""
        for page in pdf.pages:
            text += page.extract_text()
        return text if text.strip() else None
    except Exception:
        return None


def health_check(api_base):
    try:
        resp = requests.get(f"{api_base}/health", timeout=5)
        if resp.status_code == 200 and resp.json().get("status") == "ok":
            return True, None
        return False, f"HTTP {resp.status_code}: {_extract_detail(resp)}"
    except requests.exceptions.Timeout:
        return False, "Request timed out after 5 seconds."
    except requests.exceptions.ConnectionError:
        return False, "Connection failed: could not reach the API."
    except requests.exceptions.RequestException as exc:
        return False, f"Request failed: {exc}"


def upload_file(api_base, token, file_obj):
    url = f"{api_base}/upload"
    headers = {"Authorization": f"Bearer {token}"}

    file_content = file_obj.getvalue()
    file_name = file_obj.name

    if file_name.lower().endswith('.pdf'):
        pdf_text = _extract_pdf_text(file_content)
        if pdf_text is None:
            return None, "Failed to extract text from PDF"
        file_content = pdf_text.encode('utf-8')
        file_name = file_name.rsplit('.', 1)[0] + '.txt'

    try:
        resp = requests.post(
            url,
            headers=headers,
            files={"file": (file_name, file_content, "text/plain")},
            timeout=120,
        )
    except requests.exceptions.Timeout:
        return None, "Request timed out after 120 seconds."
    except requests.exceptions.ConnectionError:
        return None, "Connection failed: could not reach the API."
    except requests.exceptions.RequestException as exc:
        return None, f"Request failed: {exc}"

    if resp.status_code != 200:
        detail = _extract_detail(resp)
        if resp.status_code == 401:
            return None, "Unauthorized: check your tenant token"
        return None, f"HTTP {resp.status_code}: {detail}"

    try:
        return resp.json(), None
    except ValueError:
        return None, "Response is not valid JSON."


def ask_question(api_base, token, question, top_k, force_route=None):
    url = f"{api_base}/ask"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"question": question, "top_k": top_k}
    if force_route and force_route != "auto":
        payload["force_route"] = force_route
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
    except requests.exceptions.Timeout:
        return None, "Request timed out after 60 seconds."
    except requests.exceptions.ConnectionError:
        return None, "Connection failed: could not reach the API."
    except requests.exceptions.RequestException as exc:
        return None, f"Request failed: {exc}"

    if resp.status_code != 200:
        detail = _extract_detail(resp)
        if resp.status_code == 401:
            return None, "Unauthorized: check your tenant token"
        return None, f"HTTP {resp.status_code}: {detail}"

    try:
        return resp.json(), None
    except ValueError:
        return None, "Response is not valid JSON."


def route_badge(route):
    route_lower = (route or "unknown").lower()
    cls = "route-badge"
    if "vector" in route_lower:
        cls += " vector"
    elif "query" in route_lower or "sql" in route_lower:
        cls += " query"
    elif "agentic" in route_lower or "tool" in route_lower:
        cls += " agentic"
    elif "hybrid" in route_lower:
        cls += " hybrid"
    else:
        cls += " default"
    return f'<span class="{cls}">{html.escape(route or "unknown")}</span>'


def render_chat_entry(entry, show_trace=False):
    question = entry.get("question") or ""
    answer = entry.get("answer") or ""
    user_html = (
        '<div class="chat-row user-row">'
        f'<div class="chat-bubble user-bubble">{html.escape(question)}</div>'
        '</div>'
    )

    sources = entry.get("sources") or []
    sources_html = ""
    if sources:
        chips = "".join(
            f'<span class="source-chip">{html.escape(str(src))}</span>'
            for src in sources
        )
        sources_html = f'<div class="source-chip-list">{chips}</div>'

    tokens = entry.get("tokens") or {}
    total_tokens = tokens.get("total")
    total_tokens_str = str(total_tokens) if total_tokens is not None else "N/A"
    prompt_tokens_str = str(tokens.get("prompt", "N/A"))
    completion_tokens_str = str(tokens.get("completion", "N/A"))

    cost = entry.get("estimated_cost_usd")
    cost_str = f"${cost:.6f}" if isinstance(cost, (int, float)) else "N/A"

    latency = entry.get("latency_ms")
    latency_str = f"{latency:.2f} ms" if isinstance(latency, (int, float)) else "N/A"

    route = entry.get("route") or "unknown"
    metric_items = (
        '<div class="metric-item"><div class="metric-label">Route</div>'
        f'<div class="metric-value">{route_badge(route)}</div></div>'
        '<div class="metric-item"><div class="metric-label">Latency</div>'
        f'<div class="metric-value">{latency_str}</div></div>'
        '<div class="metric-item"><div class="metric-label">Tokens (P/C/T)</div>'
        f'<div class="metric-value">{prompt_tokens_str}/{completion_tokens_str}/{total_tokens_str}</div></div>'
        '<div class="metric-item"><div class="metric-label">Cost</div>'
        f'<div class="metric-value">{cost_str}</div></div>'
    )

    trace_html = ""
    if show_trace and entry.get("trace"):
        trace_items = entry.get("trace", [])
        trace_text = "\n".join(f"• {item}" for item in trace_items)
        trace_html = f'<div style="font-size: 12px; color: #8b98a9; margin-top: 8px; border-top: 1px solid #252c34; padding-top: 8px; white-space: pre-wrap;">{html.escape(trace_text)}</div>'

    assistant_html = (
        '<div class="chat-row assistant-row">'
        '<div class="chat-bubble assistant-bubble">'
        f'<div class="answer-card">{html.escape(answer)}</div>'
        '<div class="metrics-card"><div class="metrics-row">'
        f'{metric_items}'
        '</div></div>'
        f'{sources_html}'
        f'{trace_html}'
        '</div></div>'
    )

    return user_html + assistant_html


with st.sidebar:
    st.markdown("### Connection")
    api_base = st.text_input("API base URL", value="http://localhost:8000")
    token_option = st.selectbox("Tenant token", ["dev-token-a", "dev-token-b", "custom"])
    if token_option == "custom":
        token = st.text_input("Custom token", type="password")
    else:
        token = token_option

    if st.button("Check health"):
        ok, err = health_check(api_base)
        st.session_state.health = "online" if ok else "offline"
        st.session_state.health_detail = err if err else ""

    if st.session_state.health == "online":
        st.markdown(
            '<div class="health-indicator"><span class="dot online"></span>Online</div>',
            unsafe_allow_html=True,
        )
    elif st.session_state.health == "offline":
        detail = st.session_state.health_detail
        if detail:
            st.markdown(
                f'<div class="health-indicator"><span class="dot offline"></span>Offline</div>',
                unsafe_allow_html=True,
            )
            st.caption(detail)
        else:
            st.markdown(
                '<div class="health-indicator"><span class="dot offline"></span>Offline</div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            '<div class="health-indicator"><span class="dot unknown"></span>Not checked</div>',
            unsafe_allow_html=True,
        )

    st.markdown("### Retrieval Configuration")
    retriever_type = st.selectbox(
        "Retriever Type",
        ["vector", "direct"],
        help="vector: Search local documents | direct: Generate without retrieval"
    )

    st.markdown("### Search Options")
    enable_web_search = st.checkbox("Enable web search", value=False, help="When enabled, system can search live internet if needed")

    st.markdown("### Generation Settings")
    retriever_top_k = st.slider("Top-K (documents)", min_value=1, max_value=20, value=5, help="Number of documents to retrieve")

    st.markdown("### Advanced Settings")
    enable_self_check = st.checkbox("Self-checking (auto-refinement)", value=True, help="Automatic quality improvement with retries")
    show_trace = st.checkbox("Show execution trace", value=False, help="View step-by-step processing details")

    with st.expander("📋 System Info", expanded=False):
        st.markdown("""
        **Models:**
        - Embeddings: text-embedding-3-small
        - Generation: deepseek-flash
        - Reranking: ms-marco-MiniLM-L-6-v2

        **Retrieval Methods:**
        - Vector: Local semantic search + BM25
        - Web: Live internet search (Tavily)
        - Direct: No retrieval

        **Pricing:**
        - DeepSeek: $0.07 per 1M tokens
        - OpenAI Embedding: $0.02 per 1M tokens
        """)

st.markdown(
    '<div class="app-header">'
    '<div class="logo-mark">T</div>'
    '<div>'
    '<div class="wordmark">Tessera</div>'
    '<div class="tagline">Multi-tenant agentic RAG console.</div>'
    '</div>'
    '</div>',
    unsafe_allow_html=True,
)

tab_ask, tab_upload = st.tabs(["Ask", "Upload"])

with tab_ask:
    web_indicator = "🌐 Web search enabled" if enable_web_search else "📄 Local only"
    st.info(f"🔍 Retriever: **{retriever_type}** | {web_indicator} | 📊 Top-K: **{retriever_top_k}**")

    if st.session_state.chat_history:
        history_html = "".join(
            render_chat_entry(entry, show_trace=show_trace) for entry in st.session_state.chat_history
        )
        st.markdown(history_html, unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="empty-state">No questions yet. Ask a question below.</div>',
            unsafe_allow_html=True,
        )

    question = st.chat_input("Ask a question about your documents")
    if question:
        with st.spinner("Asking..."):
            if retriever_type == "direct":
                force_route = "direct"
            elif enable_web_search:
                force_route = "web"
            else:
                force_route = "vector"
            result, err = ask_question(api_base, token, question.strip(), retriever_top_k, force_route=force_route)
        if err:
            if "401" in err or "Unauthorized" in err:
                st.error("Unauthorized: check your tenant token")
            else:
                st.error(err)
        else:
            entry = {
                "question": question.strip(),
                "answer": result.get("answer", ""),
                "route": result.get("route", ""),
                "sources": result.get("sources", []),
                "latency_ms": result.get("latency_ms"),
                "tokens": result.get("tokens", {}),
                "estimated_cost_usd": result.get("estimated_cost_usd"),
                "tenant": result.get("tenant"),
                "trace": result.get("trace", []),
            }
            st.session_state.chat_history.append(entry)
            st.rerun()

with tab_upload:
    uploaded_file = st.file_uploader("Choose a .md, .txt, or .pdf file", type=["md", "txt", "pdf"])

    if st.button("Upload and re-index"):
        if uploaded_file is None:
            st.warning("Please choose a .md, .txt, or .pdf file first.")
        else:
            with st.spinner("Uploading and indexing..."):
                result, err = upload_file(api_base, token, uploaded_file)
            if err:
                if "401" in err or "Unauthorized" in err:
                    st.error("Unauthorized: check your tenant token")
                else:
                    st.error(err)
            else:
                filename = result.get("filename", "unknown")
                docs = result.get("docs", 0)
                chunks = result.get("chunks", 0)
                tenant = result.get("tenant", "unknown")

                st.success(
                    f"Uploaded file: {filename}. Docs: {docs}. Chunks: {chunks}. Tenant: {tenant}."
                )

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Docs", docs)
                col2.metric("Chunks", chunks)
                col3.metric("Tenant", tenant)
                col4.metric("Filename", filename)