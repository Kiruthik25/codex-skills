# Code Map

## Backend

app/main.py
- FastAPI entry point
- HTTP endpoints
- SSE streaming endpoints

app/agents/graph.py
- LangGraph workflow construction
- Node registration
- Conditional routing

app/agents/planner.py
- Query classification
- Retrieval planning

app/agents/retriever.py
- Document retrieval
- Reranking

app/agents/responder.py
- Final response generation
- Citation formatting

## Services

app/services/qdrant.py
- Vector database operations

app/services/reranker.py
- Document scoring and ranking

## Frontend

frontend/app.py
- Streamlit interface
- Streaming response rendering
- Citation display
