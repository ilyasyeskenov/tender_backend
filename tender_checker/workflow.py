"""LangGraph workflow for tender checking multi-agent system."""
import asyncio
import threading
from typing import TypedDict, List, Dict, Any, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from langgraph.graph import StateGraph, END

from clients.ai_client import AIClient
from clients.supabase_client import SupabaseClient
from agents.breakdown_agent import BreakdownAgent
from agents.omission_checker_agent import OmissionCheckerAgent
from agents.contradiction_checker_agent import ContradictionCheckerAgent
from agents.orchestrator_agent import OrchestratorAgent

try:
    from config.config import (
        WORKFLOW_OPENAI_CONCURRENCY as _CFG_OPENAI_CONCURRENCY,
        WORKFLOW_RETRIEVAL_WORKERS as _CFG_RETRIEVAL_WORKERS,
        WORKFLOW_RETRIEVAL_BATCH_SIZE as _CFG_RETRIEVAL_BATCH_SIZE,
        WORKFLOW_PIPELINE_WORKERS as _CFG_PIPELINE_WORKERS,
    )
except ImportError:
    _CFG_OPENAI_CONCURRENCY = 4
    _CFG_RETRIEVAL_WORKERS = 6
    _CFG_RETRIEVAL_BATCH_SIZE = 10
    _CFG_PIPELINE_WORKERS = 4

# Batching and rate-limit defaults (Railway 1GB/2vCPU friendly)
RETRIEVAL_BATCH_SIZE = _CFG_RETRIEVAL_BATCH_SIZE
RETRIEVAL_MAX_WORKERS = _CFG_RETRIEVAL_WORKERS
OPENAI_CONCURRENCY = _CFG_OPENAI_CONCURRENCY
PIPELINE_WORKERS = _CFG_PIPELINE_WORKERS

# PageIndex Chat prompts for omission/contradiction (answer-over-doc)
PAGEINDEX_OMISSION_QUESTION = (
    "Does this document state or imply the following requirement? "
    "Quote the relevant parts. Answer: fulfilled / partially / not fulfilled.\n\nRequirement: {requirement_text}"
)
PAGEINDEX_CONTRADICTION_QUESTION = (
    "Does this guideline document contradict or conflict with the following requirement? "
    "Quote any conflicting parts. Answer: no contradiction / minor / moderate / critical contradiction.\n\nRequirement: {requirement_text}"
)


def normalize_requirement_ids(requirements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Normalize requirement IDs to ensure consistent format (REQ-1, REQ-2, etc.).
    
    Args:
        requirements: List of requirement dicts
        
    Returns:
        List of requirements with normalized IDs
    """
    normalized = []
    for i, req in enumerate(requirements, 1):
        normalized_req = req.copy()
        normalized_req["id"] = f"REQ-{i}"
        normalized.append(normalized_req)
    return normalized


def normalize_final_report(final_report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize final report schema to ensure all expected keys exist with defaults.
    
    Args:
        final_report: Raw orchestrator output
        
    Returns:
        Normalized report with all expected keys
    """
    return {
        "overall_status": final_report.get("overall_status", "UNKNOWN"),
        "compliance_score": final_report.get("compliance_score", 0.0),
        "summary": final_report.get("summary", "No summary available"),
        "critical_issues": final_report.get("critical_issues", []),
        "omission_summary": final_report.get("omission_summary", {
            "total_requirements": 0,
            "fulfilled": 0,
            "partially_fulfilled": 0,
            "not_fulfilled": 0,
            "missing_requirements": []
        }),
        "contradiction_summary": final_report.get("contradiction_summary", {
            "total_checked": 0,
            "critical_contradictions": 0,
            "moderate_contradictions": 0,
            "minor_contradictions": 0,
            "contradictions": []
        }),
        "recommendations": final_report.get("recommendations", []),
        "risk_assessment": final_report.get("risk_assessment", "Unable to assess risk")
    }


class TenderCheckState(TypedDict):
    """State for the tender checking workflow."""
    tender_text: str
    tender_summary: str
    requirements: List[Dict[str, Any]]
    retrieval_results: List[Dict[str, Any]]
    omission_results: List[Dict[str, Any]]
    contradiction_results: List[Dict[str, Any]]
    final_report: Dict[str, Any]
    project_id: str
    guidelines_project_id: str
    top_k: int
    error: str
    # PageIndex Chat (Option B): when True, retrieval uses PageIndex Chat API instead of Supabase RAG
    use_pageindex_chat: bool
    reference_doc_id: str
    guidelines_doc_id: str
    
class TenderCheckWorkflow:
    """LangGraph workflow for tender checking with fan-out retrieval and single fan-in to orchestrate."""

    def __init__(
        self,
        ai_client: AIClient,
        supabase_client: SupabaseClient,
        breakdown_prompt: str = None,
        omission_prompt: str = None,
        contradiction_prompt: str = None,
        orchestrator_prompt: str = None,
        retrieval_batch_size: int = RETRIEVAL_BATCH_SIZE,
        retrieval_max_workers: int = RETRIEVAL_MAX_WORKERS,
        openai_concurrency: int = OPENAI_CONCURRENCY,
        pipeline_workers: int = PIPELINE_WORKERS,
        progress_callback: Optional[Callable[[str, int, int, Dict[str, Any]], None]] = None,
        pageindex_client: Optional[Any] = None,
    ):
        self.ai_client = ai_client
        self.supabase_client = supabase_client
        self.pageindex_client = pageindex_client
        self.retrieval_batch_size = retrieval_batch_size
        self.retrieval_max_workers = retrieval_max_workers
        self.openai_semaphore = threading.Semaphore(openai_concurrency)
        self._openai_concurrency = openai_concurrency
        self.pipeline_workers = pipeline_workers
        self.progress_callback = progress_callback

        # Initialize agents with custom prompts
        self.breakdown_agent = BreakdownAgent(ai_client, breakdown_prompt)
        self.omission_checker = OmissionCheckerAgent(ai_client, supabase_client, omission_prompt)
        self.contradiction_checker = ContradictionCheckerAgent(ai_client, supabase_client, contradiction_prompt)
        self.orchestrator = OrchestratorAgent(ai_client, orchestrator_prompt)

        # Build workflow
        self.workflow = self._build_workflow()

    def _build_workflow(self) -> StateGraph:
        """Build the LangGraph workflow: breakdown → retrieve_and_check (overlapped) → orchestrate."""
        workflow = StateGraph(TenderCheckState)

        workflow.add_node("breakdown", self._breakdown_node)
        workflow.add_node("retrieve_and_check", self._retrieve_and_check_node)
        workflow.add_node("orchestrate", self._orchestrate_node)

        workflow.set_entry_point("breakdown")
        workflow.add_edge("breakdown", "retrieve_and_check")
        workflow.add_edge("retrieve_and_check", "orchestrate")
        workflow.add_edge("orchestrate", END)

        return workflow.compile()
    
    def _breakdown_node(self, state: TenderCheckState) -> Dict[str, Any]:
        """Break down tender into requirements."""
        try:
            if self.progress_callback:
                self.progress_callback("breakdown", 1, 4, {"step": "Breaking down tender into requirements..."})
            
            result = self.breakdown_agent.breakdown_tender(state["tender_text"])
            requirements = result.get("requirements", [])
            
            # Normalize requirement IDs
            requirements = normalize_requirement_ids(requirements)
            
            tender_summary = f"Tender document with {len(requirements)} requirements extracted."
            
            if self.progress_callback:
                self.progress_callback("breakdown", 1, 4, {
                    "step": "Breakdown complete",
                    "requirements_count": len(requirements)
                })
            
            return {
                "requirements": requirements,
                "tender_summary": tender_summary,
                "error": ""
            }
        except Exception as e:
            if self.progress_callback:
                self.progress_callback("breakdown", 1, 4, {"step": "Breakdown error", "error": str(e)})
            return {
                "requirements": [],
                "tender_summary": "",
                "error": f"Breakdown error: {str(e)}"
            }

    def _retrieve_one(
        self,
        requirement: Dict[str, Any],
        project_id: str,
        guidelines_project_id: str,
        top_k: int,
    ) -> Dict[str, Any]:
        """Retrieve omission and contradiction chunks for one requirement (for parallel execution)."""
        req_text = requirement.get("requirement_text", "")
        omission_chunks = []
        contradiction_chunks = []
        try:
            omission_chunks = self.supabase_client.get_chunks_by_hybrid_search(
                project_id=project_id,
                query=req_text,
                match_count=top_k
            )
        except Exception:
            pass
        try:
            contradiction_chunks = self.supabase_client.get_chunks_by_hybrid_search(
                project_id=guidelines_project_id,
                query=req_text,
                match_count=top_k
            )
        except Exception:
            pass
        return {
            "requirement": requirement,
            "omission_chunks": omission_chunks,
            "contradiction_chunks": contradiction_chunks,
        }

    def _retrieve_one_pageindex(
        self,
        requirement: Dict[str, Any],
        reference_doc_id: str,
        guidelines_doc_id: str,
    ) -> Dict[str, Any]:
        """Retrieve omission and contradiction evidence via PageIndex Legacy Retrieval API (submit + poll → chunks)."""
        req_text = requirement.get("requirement_text", "")
        omission_q = PAGEINDEX_OMISSION_QUESTION.format(requirement_text=req_text)
        contradiction_q = PAGEINDEX_CONTRADICTION_QUESTION.format(requirement_text=req_text)
        omission_chunks = []
        contradiction_chunks = []
        try:
            omission_chunks = self.pageindex_client.retrieve(
                doc_id=reference_doc_id,
                query=omission_q,
                file_name="PageIndex Reference",
            )
        except Exception:
            omission_chunks = []
        try:
            con_doc_id = guidelines_doc_id or reference_doc_id
            contradiction_chunks = self.pageindex_client.retrieve(
                doc_id=con_doc_id,
                query=contradiction_q,
                file_name="PageIndex Guidelines",
            )
        except Exception:
            contradiction_chunks = []
        return {
            "requirement": requirement,
            "omission_chunks": omission_chunks,
            "contradiction_chunks": contradiction_chunks,
        }

    def _process_one_requirement(
        self,
        idx: int,
        requirement: Dict[str, Any],
        state: TenderCheckState,
    ) -> tuple:
        """Retrieve then check one requirement (for pipeline overlap). Returns (retrieval_result, omission_result, contradiction_result)."""
        use_pageindex = state.get("use_pageindex_chat", False) and self.pageindex_client
        reference_doc_id = (state.get("reference_doc_id") or "").strip()
        guidelines_doc_id = (state.get("guidelines_doc_id") or "").strip()
        project_id = state["project_id"]
        guidelines_id = state.get("guidelines_project_id", project_id)
        top_k = state.get("top_k", 8)

        if use_pageindex and reference_doc_id:
            retrieval_result = self._retrieve_one_pageindex(
                requirement, reference_doc_id, guidelines_doc_id
            )
        else:
            retrieval_result = self._retrieve_one(
                requirement, project_id, guidelines_id, top_k
            )

        req = retrieval_result["requirement"]
        om_chunks = retrieval_result.get("omission_chunks", [])
        con_chunks = retrieval_result.get("contradiction_chunks", [])
        req_id = req.get("id", "UNKNOWN")

        omission_result = contradiction_result = None
        try:
            omission_result = self.omission_checker.check_requirement_with_chunks(
                requirement=req,
                chunks=om_chunks,
                openai_semaphore=self.openai_semaphore,
            )
        except Exception as e:
            omission_result = {
                "requirement_id": req_id,
                "status": "ERROR",
                "confidence": 0.0,
                "justification": str(e),
                "citations": [],
                "missing_elements": [],
            }
        try:
            contradiction_result = self.contradiction_checker.check_requirement_with_chunks(
                requirement=req,
                chunks=con_chunks,
                openai_semaphore=self.openai_semaphore,
            )
        except Exception as e:
            contradiction_result = {
                "requirement_id": req_id,
                "has_contradiction": False,
                "severity": "ERROR",
                "contradiction_details": str(e),
                "reference_guideline": "",
                "tender_statement": "",
                "citations": [],
                "recommendation": "",
            }

        return (retrieval_result, omission_result, contradiction_result)

    def _retrieve_and_check_node(self, state: TenderCheckState) -> Dict[str, Any]:
        """Single node: retrieve then check per requirement (overlaps retrieval and check). Uses async I/O when available."""
        requirements = state.get("requirements", [])
        if not requirements:
            return {"retrieval_results": [], "omission_results": [], "contradiction_results": []}

        if self.ai_client.async_client is not None:
            return asyncio.run(self._retrieve_and_check_async(state))

        n = len(requirements)
        workers = min(self.pipeline_workers, n)
        retrieval_results = [None] * n
        omission_results = [None] * n
        contradiction_results = [None] * n
        completed = [0]
        lock = threading.Lock()

        if self.progress_callback:
            self.progress_callback("retrieve_and_check", 2, 3, {
                "step": "Retrieving and checking requirements",
                "total_requirements": n,
                "completed": 0,
            })

        def process_one(idx: int, req: Dict[str, Any]) -> None:
            try:
                r, o, c = self._process_one_requirement(idx, req, state)
                retrieval_results[idx] = r
                omission_results[idx] = o
                contradiction_results[idx] = c
            except Exception:
                retrieval_results[idx] = {
                    "requirement": req,
                    "omission_chunks": [],
                    "contradiction_chunks": [],
                }
                omission_results[idx] = {
                    "requirement_id": req.get("id", "UNKNOWN"),
                    "status": "ERROR",
                    "confidence": 0.0,
                    "justification": "Pipeline error",
                    "citations": [],
                    "missing_elements": [],
                }
                contradiction_results[idx] = {
                    "requirement_id": req.get("id", "UNKNOWN"),
                    "has_contradiction": False,
                    "severity": "ERROR",
                    "contradiction_details": "Pipeline error",
                    "reference_guideline": "",
                    "tender_statement": "",
                    "citations": [],
                    "recommendation": "",
                }
            with lock:
                completed[0] += 1
                if self.progress_callback:
                    self.progress_callback("retrieve_and_check", 2, 3, {
                        "step": "Retrieving and checking",
                        "total_requirements": n,
                        "completed": completed[0],
                    })

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(process_one, i, req) for i, req in enumerate(requirements)]
            for f in futures:
                f.result()

        if self.progress_callback:
            self.progress_callback("retrieve_and_check", 2, 3, {
                "step": "Retrieve and check complete",
                "total_requirements": n,
                "completed": n,
            })

        return {
            "retrieval_results": retrieval_results,
            "omission_results": omission_results,
            "contradiction_results": contradiction_results,
        }

    async def _retrieve_and_check_async(self, state: TenderCheckState) -> Dict[str, Any]:
        """Async path: overlap retrieval (in executor) and check (async LLM) with semaphore-limited concurrency."""
        requirements = state.get("requirements", [])
        n = len(requirements)
        retrieval_results = [None] * n
        omission_results = [None] * n
        contradiction_results = [None] * n
        loop = asyncio.get_event_loop()
        sem_workers = asyncio.Semaphore(self.pipeline_workers)
        sem_llm = asyncio.Semaphore(self._openai_concurrency)
        ac = self.ai_client.async_client
        model = self.ai_client.model

        def _retrieve_one_requirement(req: Dict[str, Any], st: TenderCheckState) -> Dict[str, Any]:
            use_pageindex = st.get("use_pageindex_chat", False) and self.pageindex_client
            ref_doc = (st.get("reference_doc_id") or "").strip()
            guide_doc = (st.get("guidelines_doc_id") or "").strip()
            proj_id = st["project_id"]
            guide_id = st.get("guidelines_project_id", proj_id)
            top_k = st.get("top_k", 8)
            if use_pageindex and ref_doc:
                return self._retrieve_one_pageindex(req, ref_doc, guide_doc)
            return self._retrieve_one(req, proj_id, guide_id, top_k)

        async def process_one_async(idx: int, req: Dict[str, Any]) -> None:
            async with sem_workers:
                retrieval = await loop.run_in_executor(None, lambda: _retrieve_one_requirement(req, state))
                retrieval_results[idx] = retrieval
                r = retrieval["requirement"]
                om_chunks = retrieval.get("omission_chunks", [])
                con_chunks = retrieval.get("contradiction_chunks", [])
                req_id = r.get("id", "UNKNOWN")

                async with sem_llm:
                    o = await self.omission_checker.check_requirement_with_chunks_async(
                        r, om_chunks, ac, model
                    )
                async with sem_llm:
                    c = await self.contradiction_checker.check_requirement_with_chunks_async(
                        r, con_chunks, ac, model
                    )
                omission_results[idx] = o
                contradiction_results[idx] = c

                if self.progress_callback:
                    done = sum(1 for x in omission_results if x is not None)
                    self.progress_callback("retrieve_and_check", 2, 3, {
                        "step": "Retrieving and checking",
                        "total_requirements": n,
                        "completed": done,
                    })

        if self.progress_callback:
            self.progress_callback("retrieve_and_check", 2, 3, {
                "step": "Retrieving and checking requirements",
                "total_requirements": n,
                "completed": 0,
            })

        await asyncio.gather(*[process_one_async(i, req) for i, req in enumerate(requirements)])

        if self.progress_callback:
            self.progress_callback("retrieve_and_check", 2, 3, {
                "step": "Retrieve and check complete",
                "total_requirements": n,
                "completed": n,
            })

        return {
            "retrieval_results": retrieval_results,
            "omission_results": omission_results,
            "contradiction_results": contradiction_results,
        }

    def _retrieval_node(self, state: TenderCheckState) -> Dict[str, Any]:
        """Parallel retrieval: either Supabase RAG (chunks) or PageIndex Legacy Retrieval (chunks)."""
        requirements = state.get("requirements", [])
        if not requirements:
            return {"retrieval_results": []}

        use_pageindex = state.get("use_pageindex_chat", False) and self.pageindex_client
        reference_doc_id = (state.get("reference_doc_id") or "").strip()
        guidelines_doc_id = (state.get("guidelines_doc_id") or "").strip()

        if self.progress_callback:
            step_label = "PageIndex Retrieval" if use_pageindex else "Retrieving chunks"
            self.progress_callback("retrieval", 2, 4, {
                "step": step_label,
                "total_requirements": len(requirements),
                "completed": 0
            })

        if use_pageindex and reference_doc_id:
            # PageIndex Legacy Retrieval path: submit + poll per requirement per doc → chunks
            batch_size = min(self.retrieval_batch_size, len(requirements))
            max_workers = min(self.retrieval_max_workers, len(requirements))
            results = [None] * len(requirements)
            completed_count = 0
            for start in range(0, len(requirements), batch_size):
                batch = requirements[start : start + batch_size]
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_idx = {
                        executor.submit(
                            self._retrieve_one_pageindex,
                            req,
                            reference_doc_id,
                            guidelines_doc_id,
                        ): start + i
                        for i, req in enumerate(batch)
                    }
                    for future in as_completed(future_to_idx):
                        idx = future_to_idx[future]
                        try:
                            results[idx] = future.result()
                            completed_count += 1
                            if self.progress_callback:
                                self.progress_callback("retrieval", 2, 4, {
                                    "step": "PageIndex Retrieval",
                                    "total_requirements": len(requirements),
                                    "completed": completed_count
                                })
                        except Exception:
                            results[idx] = {
                                "requirement": requirements[idx],
                                "omission_chunks": [],
                                "contradiction_chunks": [],
                            }
                            completed_count += 1
            if self.progress_callback:
                self.progress_callback("retrieval", 2, 4, {
                    "step": "PageIndex Retrieval complete",
                    "total_requirements": len(requirements),
                    "completed": completed_count
                })
            return {"retrieval_results": results}

        # Supabase RAG path (existing)
        project_id = state["project_id"]
        guidelines_id = state.get("guidelines_project_id", project_id)
        top_k = state.get("top_k", 8)
        batch_size = min(self.retrieval_batch_size, len(requirements))
        max_workers = min(self.retrieval_max_workers, len(requirements))

        results = [None] * len(requirements)
        completed_count = 0
        
        for start in range(0, len(requirements), batch_size):
            batch = requirements[start : start + batch_size]
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_idx = {
                    executor.submit(
                        self._retrieve_one,
                        req,
                        project_id,
                        guidelines_id,
                        top_k,
                    ): start + i
                    for i, req in enumerate(batch)
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        results[idx] = future.result()
                        completed_count += 1
                        if self.progress_callback:
                            self.progress_callback("retrieval", 2, 4, {
                                "step": f"Retrieving chunks",
                                "total_requirements": len(requirements),
                                "completed": completed_count
                            })
                    except Exception as e:
                        results[idx] = {
                            "requirement": requirements[idx],
                            "omission_chunks": [],
                            "contradiction_chunks": [],
                        }
                        completed_count += 1
        
        if self.progress_callback:
            self.progress_callback("retrieval", 2, 4, {
                "step": "Retrieval complete",
                "total_requirements": len(requirements),
                "completed": completed_count
            })
        
        return {"retrieval_results": results}

    def _check_node(self, state: TenderCheckState) -> Dict[str, Any]:
        """Run omission and contradiction LLM per requirement using shared retrieval; single node, one orchestrator call."""
        retrieval_results = state.get("retrieval_results", [])
        if not retrieval_results:
            return {"omission_results": [], "contradiction_results": []}

        if self.progress_callback:
            self.progress_callback("check", 3, 4, {
                "step": "Starting requirement checks",
                "total_requirements": len(retrieval_results),
                "completed": 0
            })

        omission_results = [None] * len(retrieval_results)
        contradiction_results = [None] * len(retrieval_results)
        completed_count = 0

        def process_one(idx: int, item: Dict[str, Any]) -> None:
            nonlocal completed_count
            req = item["requirement"]
            om_chunks = item.get("omission_chunks", [])
            con_chunks = item.get("contradiction_chunks", [])
            req_id = req.get("id", "UNKNOWN")
            try:
                omission_results[idx] = self.omission_checker.check_requirement_with_chunks(
                    requirement=req,
                    chunks=om_chunks,
                    openai_semaphore=self.openai_semaphore,
                )
            except Exception as e:
                omission_results[idx] = {
                    "requirement_id": req_id,
                    "status": "ERROR",
                    "confidence": 0.0,
                    "justification": str(e),
                    "citations": [],
                    "missing_elements": [],
                }
            try:
                contradiction_results[idx] = self.contradiction_checker.check_requirement_with_chunks(
                    requirement=req,
                    chunks=con_chunks,
                    openai_semaphore=self.openai_semaphore,
                )
            except Exception as e:
                contradiction_results[idx] = {
                    "requirement_id": req_id,
                    "has_contradiction": False,
                    "severity": "ERROR",
                    "contradiction_details": str(e),
                    "reference_guideline": "",
                    "tender_statement": "",
                    "citations": [],
                    "recommendation": "",
                }
            
            completed_count += 1
            if self.progress_callback:
                self.progress_callback("check", 3, 4, {
                    "step": f"Checking requirements",
                    "total_requirements": len(retrieval_results),
                    "completed": completed_count,
                    "current_requirement": req_id
                })

        with ThreadPoolExecutor(max_workers=min(20, len(retrieval_results))) as executor:
            futures = [
                executor.submit(process_one, i, item)
                for i, item in enumerate(retrieval_results)
            ]
            for f in futures:
                f.result()

        if self.progress_callback:
            self.progress_callback("check", 3, 4, {
                "step": "Check complete",
                "total_requirements": len(retrieval_results),
                "completed": completed_count
            })

        return {
            "omission_results": omission_results,
            "contradiction_results": contradiction_results,
        }

    def _orchestrate_node(self, state: TenderCheckState) -> Dict[str, Any]:
        """Synthesize all results (single call after check node)."""
        try:
            if self.progress_callback:
                self.progress_callback("orchestrate", 4, 4, {
                    "step": "Synthesizing final report..."
                })
            
            omission_results = state.get("omission_results", [])
            contradiction_results = state.get("contradiction_results", [])
            errors = []
            if state.get("error"):
                errors.append(state["error"])

            final_report = self.orchestrator.synthesize_results(
                tender_summary=state.get("tender_summary", ""),
                omission_results=omission_results or [],
                contradiction_results=contradiction_results or [],
            )
            
            # Normalize final report schema
            final_report = normalize_final_report(final_report)
            
            error_msg = "; ".join(errors) if errors else ""
            
            if self.progress_callback:
                self.progress_callback("orchestrate", 4, 4, {
                    "step": "Orchestration complete",
                    "overall_status": final_report.get("overall_status", "UNKNOWN")
                })
            
            return {"final_report": final_report, "error": error_msg}
        except Exception as e:
            if self.progress_callback:
                self.progress_callback("orchestrate", 4, 4, {
                    "step": "Orchestration error",
                    "error": str(e)
                })
            return {
                "final_report": normalize_final_report({
                    "overall_status": "ERROR",
                    "summary": f"Error: {str(e)}"
                }),
                "error": f"Orchestration error: {str(e)}"
            }
    
    def run(
        self,
        tender_text: str,
        project_id: str,
        guidelines_project_id: str = None,
        top_k: int = 8,
        use_pageindex_chat: bool = False,
        reference_doc_id: str = "",
        guidelines_doc_id: str = "",
    ) -> Dict[str, Any]:
        """
        Run the complete tender checking workflow.
        
        Args:
            tender_text: Full text of tender submission
            project_id: Project ID for reference documents (omission) — used when use_pageindex_chat is False
            guidelines_project_id: Project ID for guidelines (contradiction) — used when use_pageindex_chat is False
            top_k: Chunks per requirement (RAG mode only)
            use_pageindex_chat: If True, use PageIndex Chat API instead of Supabase RAG
            reference_doc_id: PageIndex doc_id for reference document (omission)
            guidelines_doc_id: PageIndex doc_id for guidelines (contradiction); if empty, uses reference_doc_id
            
        Returns:
            Final state with all results
        """
        initial_state: TenderCheckState = {
            "tender_text": tender_text,
            "tender_summary": "",
            "requirements": [],
            "retrieval_results": [],
            "omission_results": [],
            "contradiction_results": [],
            "final_report": {},
            "project_id": project_id,
            "guidelines_project_id": guidelines_project_id or project_id,
            "top_k": top_k,
            "error": "",
            "use_pageindex_chat": use_pageindex_chat,
            "reference_doc_id": reference_doc_id or "",
            "guidelines_doc_id": guidelines_doc_id or "",
        }
        
        # Run workflow
        final_state = self.workflow.invoke(initial_state)
        return final_state

