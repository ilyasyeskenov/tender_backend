"""FastAPI server for tender checking workflow."""
import threading
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import os

from tender_checker.workflow import TenderCheckWorkflow, TenderCheckState
from clients.ai_client import AIClient
from clients.supabase_client import SupabaseClient

app = FastAPI(title="Tender Checker API", version="1.0.0")

# In-memory job store for async tender-check (avoids request timeouts on Railway)
_job_store: Dict[str, Dict[str, Any]] = {}
_job_store_lock = threading.Lock()

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"

# CORS middleware to allow Next.js frontend to call this
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict this to your Next.js domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RequirementInput(BaseModel):
    """Single requirement input."""
    id: Optional[str] = None
    requirement_text: str
    context: Optional[str] = None
    category: Optional[str] = None


class TenderCheckRequest(BaseModel):
    """Request model for tender checking."""
    tender_text: Optional[str] = None  # Full tender document text (triggers breakdown)
    requirements: Optional[List[RequirementInput]] = None  # Pre-extracted requirements
    project_id: str  # Reference documents project (for omission checks)
    guidelines_project_id: Optional[str] = None  # Guidelines project (for contradiction checks)
    top_k: int = 8  # Number of chunks per requirement


def _run_tender_check_job(job_id: str, payload: Dict[str, Any]) -> None:
    """Run the tender-check workflow in a background thread and update job state."""
    with _job_store_lock:
        if job_id not in _job_store:
            return
        _job_store[job_id]["status"] = JOB_STATUS_RUNNING

    try:
        ai_client = AIClient()
        supabase_client = SupabaseClient()
        workflow = TenderCheckWorkflow(
            ai_client=ai_client,
            supabase_client=supabase_client,
        )
        guidelines_id = payload.get("guidelines_project_id") or payload.get("project_id")

        if payload.get("tender_text"):
            final_state = workflow.run(
                tender_text=payload["tender_text"],
                project_id=payload["project_id"],
                guidelines_project_id=guidelines_id,
                top_k=payload.get("top_k", 8),
                use_pageindex_chat=False,
            )
            with _job_store_lock:
                if job_id in _job_store:
                    _job_store[job_id]["status"] = JOB_STATUS_COMPLETED
                    _job_store[job_id]["result"] = final_state
            return

        requirements = payload.get("requirements") or []
        if not requirements:
            with _job_store_lock:
                if job_id in _job_store:
                    _job_store[job_id]["status"] = JOB_STATUS_FAILED
                    _job_store[job_id]["error"] = "Either tender_text or requirements must be provided"
            return

        normalized_reqs = []
        for i, req in enumerate(requirements, 1):
            r = req if isinstance(req, dict) else req
            normalized_reqs.append({
                "id": r.get("id") or f"REQ-{i}",
                "requirement_text": r.get("requirement_text", ""),
                "context": r.get("context") or "",
                "category": r.get("category") or "General",
            })

        initial_state: TenderCheckState = {
            "tender_text": "",
            "tender_summary": f"Tender submission with {len(normalized_reqs)} requirements",
            "requirements": normalized_reqs,
            "retrieval_results": [],
            "omission_results": [],
            "contradiction_results": [],
            "final_report": {},
            "project_id": payload["project_id"],
            "guidelines_project_id": guidelines_id,
            "top_k": payload.get("top_k", 8),
            "error": "",
            "use_pageindex_chat": False,
            "reference_doc_id": "",
            "guidelines_doc_id": "",
        }
        state_after_retrieval = workflow._retrieval_node(initial_state)
        state_after_check = workflow._check_node(state_after_retrieval)
        final_state = workflow._orchestrate_node(state_after_check)
        final_state.update({
            "requirements": normalized_reqs,
            "tender_summary": initial_state["tender_summary"],
            "project_id": payload["project_id"],
            "guidelines_project_id": guidelines_id,
        })

        with _job_store_lock:
            if job_id in _job_store:
                _job_store[job_id]["status"] = JOB_STATUS_COMPLETED
                _job_store[job_id]["result"] = final_state
    except Exception as e:
        import traceback
        err = f"{str(e)}\n{traceback.format_exc()}"
        with _job_store_lock:
            if job_id in _job_store:
                _job_store[job_id]["status"] = JOB_STATUS_FAILED
                _job_store[job_id]["error"] = err


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/tender-check", status_code=202)
def tender_check(request: TenderCheckRequest) -> Dict[str, Any]:
    """
    Start tender checking workflow as a background job (avoids timeouts on Railway).
    Returns 202 with job_id. Poll GET /jobs/{job_id} for status and result.
    Either tender_text OR requirements must be provided.
    """
    if not request.tender_text and not (request.requirements and len(request.requirements) > 0):
        raise HTTPException(
            status_code=400,
            detail="Either tender_text or requirements must be provided"
        )
    job_id = str(uuid.uuid4())
    payload = request.model_dump()
    with _job_store_lock:
        _job_store[job_id] = {
            "status": JOB_STATUS_PENDING,
            "result": None,
            "error": None,
        }
    thread = threading.Thread(target=_run_tender_check_job, args=(job_id, payload))
    thread.daemon = True
    thread.start()
    return {
        "job_id": job_id,
        "status": JOB_STATUS_PENDING,
        "message": "Tender check started. Poll GET /jobs/{job_id} for result.",
        "status_url": f"/jobs/{job_id}",
    }


@app.get("/jobs/{job_id}")
def get_job_status(job_id: str) -> Dict[str, Any]:
    """
    Poll for tender-check job status and result.
    - status: pending | running | completed | failed
    - When completed: result contains the full workflow output.
    - When failed: error contains the error message.
    """
    with _job_store_lock:
        job = _job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    out = {
        "job_id": job_id,
        "status": job["status"],
    }
    if job["status"] == JOB_STATUS_COMPLETED and job.get("result") is not None:
        out["result"] = job["result"]
    if job["status"] == JOB_STATUS_FAILED and job.get("error"):
        out["error"] = job["error"]
    return out


if __name__ == "__main__":
    import uvicorn
    # Railway uses PORT, local dev can use TENDER_BACKEND_PORT
    port = int(os.getenv("PORT", os.getenv("TENDER_BACKEND_PORT", "8001")))
    uvicorn.run(app, host="0.0.0.0", port=port)
