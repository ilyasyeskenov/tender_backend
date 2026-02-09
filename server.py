"""FastAPI server for tender checking workflow."""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import os

from tender_checker.workflow import TenderCheckWorkflow, TenderCheckState
from clients.ai_client import AIClient
from clients.supabase_client import SupabaseClient

app = FastAPI(title="Tender Checker API", version="1.0.0")

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


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/tender-check")
def tender_check(request: TenderCheckRequest) -> Dict[str, Any]:
    """
    Run tender checking workflow.
    
    Either tender_text OR requirements must be provided:
    - If tender_text: Full workflow (breakdown → retrieval → check → orchestrate)
    - If requirements: Skip breakdown, use provided requirements directly
    """
    try:
        # Initialize clients
        ai_client = AIClient()
        supabase_client = SupabaseClient()
        
        # Initialize workflow
        workflow = TenderCheckWorkflow(
            ai_client=ai_client,
            supabase_client=supabase_client,
        )
        
        # Determine guidelines project (default to same as project_id)
        guidelines_id = request.guidelines_project_id or request.project_id
        
        # Case 1: tender_text provided - use full workflow
        if request.tender_text:
            final_state = workflow.run(
                tender_text=request.tender_text,
                project_id=request.project_id,
                guidelines_project_id=guidelines_id,
                top_k=request.top_k,
                use_pageindex_chat=False,  # RAG mode only
            )
            return final_state
        
        # Case 2: requirements provided directly - skip breakdown
        if request.requirements and len(request.requirements) > 0:
            # Normalize requirement IDs
            normalized_reqs = []
            for i, req in enumerate(request.requirements, 1):
                normalized_reqs.append({
                    "id": req.id or f"REQ-{i}",
                    "requirement_text": req.requirement_text,
                    "context": req.context or "",
                    "category": req.category or "General",
                })
            
            # Create initial state with requirements already set
            initial_state: TenderCheckState = {
                "tender_text": "",  # Not used when requirements are provided
                "tender_summary": f"Tender submission with {len(normalized_reqs)} requirements",
                "requirements": normalized_reqs,
                "retrieval_results": [],
                "omission_results": [],
                "contradiction_results": [],
                "final_report": {},
                "project_id": request.project_id,
                "guidelines_project_id": guidelines_id,
                "top_k": request.top_k,
                "error": "",
                "use_pageindex_chat": False,
                "reference_doc_id": "",
                "guidelines_doc_id": "",
            }
            
            # Skip breakdown node, manually invoke: retrieval → check → orchestrate
            # Access the private methods directly (they're instance methods)
            state_after_retrieval = workflow._retrieval_node(initial_state)
            state_after_check = workflow._check_node(state_after_retrieval)
            final_state = workflow._orchestrate_node(state_after_check)
            
            # Merge all states to ensure we have complete data
            final_state.update({
                "requirements": normalized_reqs,
                "tender_summary": initial_state["tender_summary"],
                "project_id": request.project_id,
                "guidelines_project_id": guidelines_id,
            })
            
            return final_state
        
        # Neither provided
        raise HTTPException(
            status_code=400,
            detail="Either tender_text or requirements must be provided"
        )
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import traceback
        error_detail = f"{str(e)}\n{traceback.format_exc()}"
        raise HTTPException(status_code=500, detail=error_detail)


if __name__ == "__main__":
    import uvicorn
    # Railway uses PORT, local dev can use TENDER_BACKEND_PORT
    port = int(os.getenv("PORT", os.getenv("TENDER_BACKEND_PORT", "8001")))
    uvicorn.run(app, host="0.0.0.0", port=port)
