"""Streamlit UI for the RAG pipeline (the response-generation front-end).

This is the user-facing layer over the existing pipeline modules:
    vector_store.retrieve()  -> top-k chunks for a question (Retrieval stage)
    generate.generate_answer() -> grounded, source-attributed answer (Generation)

Run it with:
    streamlit run app.py

Requires a Groq API key in .env (GROQ_API_KEY=...).
"""

import streamlit as st

import generate as g
import vector_store as vs

EXAMPLE_QUESTIONS = [
    "Which CS professors do students recommend, and why?",
    "What CS electives are best for data science?",
    "Which CS professors do students say to avoid?",
    "How do I manage a heavy CS course load?",
    "Which CS courses do students find the most difficult?",
]


@st.cache_resource(show_spinner="Loading the embedding model and vector store...")
def load_collection():
    """Open the ChromaDB collection once and reuse it across reruns.

    Caching keeps the embedding model loaded in memory instead of reloading it
    on every interaction.
    """
    return vs.get_collection()


@st.cache_resource(show_spinner=False)
def load_client():
    """Build the Groq client once; reused across reruns."""
    return g._get_client()


def set_question(question: str) -> None:
    st.session_state.question = question


st.set_page_config(page_title="The Unofficial Guide - GMU CS", page_icon="🎓")

st.title("🎓 The Unofficial Guide")
st.caption(
    "Ask about CS courses and professors at George Mason University. Answers are "
    "drawn only from real student reviews on r/gmu - if the reviews don't cover "
    "it, the assistant will say so."
)

# --- Sidebar: settings + examples ------------------------------------------
with st.sidebar:
    st.header("Settings")
    top_k = st.slider(
        "Reviews to retrieve (top-k)", min_value=1, max_value=10, value=vs.TOP_K,
        help="How many review chunks are used as context for the answer.")
    show_context = st.checkbox("Show retrieved context", value=False)

    st.markdown("---")
    st.subheader("Try an example")
    for example in EXAMPLE_QUESTIONS:
        st.button(example, key=f"ex::{example}", use_container_width=True,
                  on_click=set_question, args=(example,))

# --- API key check ----------------------------------------------------------
try:
    client = load_client()
except Exception as exc:  # missing / placeholder GROQ_API_KEY
    st.error(str(exc))
    st.stop()

collection = load_collection()

# --- Question input ---------------------------------------------------------
if "question" not in st.session_state:
    st.session_state.question = ""

question = st.text_input(
    "Your question",
    key="question",
    placeholder="e.g. Which professors should I take for operating systems?",
)
ask = st.button("Ask", type="primary")

# --- Answer -----------------------------------------------------------------
if ask or (question and st.session_state.get("_last_asked") != question):
    if not question.strip():
        st.warning("Please enter a question.")
        st.stop()

    st.session_state["_last_asked"] = question
    with st.spinner("Searching reviews and writing an answer..."):
        chunks = vs.retrieve(question, k=top_k, collection=collection)
        result = g.generate_answer(question, chunks=chunks, client=client)

    st.markdown("### Answer")
    if result["sources"]:
        st.write(result["text"])
        st.markdown("**Sources**")
        for source in result["sources"]:
            st.markdown(f"- {source}")
    else:
        # No-info fallback or generation failure: no sources to show.
        st.info(result["answer"])

    if show_context:
        with st.expander(f"Retrieved context ({len(chunks)} chunks)"):
            for i, chunk in enumerate(chunks, start=1):
                st.markdown(
                    f"**{i}. {chunk['source']}**  \n"
                    f"_position {chunk['position']} - similarity {chunk['similarity']}_")
                st.write(chunk["text"])
                st.divider()
