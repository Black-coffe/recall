"""Co-pilot — the live call co-pilot (Phase 19).

A local dispatcher model listens to the dialogue in real time (topics / RAG /
contradictions); Claude joins as a verifier only on escalations
(local-first cascade). Full plan — docs/COPILOT_ROADMAP.md.

Step 1: session skeleton (config resolver for the mode x importance matrix + CopilotService
for persistence into copilot_sessions). No analysis yet — settings are only recorded.
"""
