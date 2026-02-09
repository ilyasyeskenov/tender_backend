# Tender Backend Service

Standalone Python FastAPI service that exposes the multi-agent tender checking workflow.

## Quick Start

1. **Clone this repository:**
```bash
git clone <your-repo-url>
cd tender_backend
```

2. **Install Python dependencies:**
```bash
pip install -r requirements.txt
```

3. **Set environment variables:**

Create a `.env` file in the root of this repo:

```env
SUPABASE_URL=your_supabase_url
SUPABASE_ANON_KEY=your_supabase_anon_key
OPENAI_API_KEY=your_openai_api_key
AI_PROVIDER=openai
OPENAI_MODEL=gpt-4o
EMBEDDING_MODEL=text-embedding-3-large
```

4. **Start the FastAPI server:**

```bash
python server.py

# Or using uvicorn directly
uvicorn server:app --reload --port 8001
```

The server will start on `http://localhost:8001`

## Railway Deployment

See [RAILWAY_DEPLOYMENT.md](./RAILWAY_DEPLOYMENT.md) for detailed Railway deployment instructions.

**Quick Railway setup:**
1. Create new Railway project
2. Connect this GitHub repo
3. Set environment variables in Railway dashboard
4. Railway will auto-detect `Procfile` and deploy
5. Copy the public URL and set `TENDER_BACKEND_URL` in your Next.js app

## API Endpoints

### POST `/tender-check`

Run tender checking workflow.

**Request Body:**
```json
{
  "tender_text": "optional full tender document text",
  "requirements": [
    {
      "id": "REQ-1",
      "requirement_text": "The system must...",
      "context": "optional context",
      "category": "Technical"
    }
  ],
  "project_id": "uuid-of-reference-project",
  "guidelines_project_id": "uuid-of-guidelines-project (optional)",
  "top_k": 8
}
```

**Either `tender_text` OR `requirements` must be provided:**
- If `tender_text`: Full workflow runs (breakdown → retrieval → check → orchestrate)
- If `requirements`: Skips breakdown, uses provided requirements directly

**Response:**
```json
{
  "final_report": {
    "overall_status": "COMPLIANT" | "NON_COMPLIANT" | "CONDITIONALLY_COMPLIANT",
    "compliance_score": 0.0-1.0,
    "summary": "...",
    "critical_issues": [...],
    "omission_summary": {...},
    "contradiction_summary": {...},
    "recommendations": [...],
    "risk_assessment": "..."
  },
  "requirements": [...],
  "omission_results": [...],
  "contradiction_results": [...]
}
```

### GET `/health`

Health check endpoint.

## Integration with Next.js

The Next.js app calls `/api/tender-check` which proxies to this Python service.

Make sure `TENDER_BACKEND_URL` in your `.env.local` points to where this service is running.
