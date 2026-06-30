"""Forge LangGraph Workflow — wires runtime components into a state machine.

This package contains:
- deps: RuntimeDeps container for all component references
- nodes/: Node function factories for each workflow step
- routing: Conditional edge routing functions
- graph: Graph builder that assembles the full LangGraph StateGraph
- bootstrap: Startup sequence (discovery → registry → health → mode → ready)
- app: FastAPI application entry point with lifespan management
"""
