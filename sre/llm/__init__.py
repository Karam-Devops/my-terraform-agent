"""SRE engine LLM helpers.

Two surfaces:

  * :mod:`sre.llm.hypothesis_writer` — rewrites the heuristic
    correlator's templated headlines + reasoning in operator-grade
    prose. One LLM call per triage (not per hypothesis) — sends the
    alert + top-N ranked hypotheses + cited evidence summaries, gets
    back a JSON response with rewritten narrative for each.

  * :mod:`sre.llm.refine` — re-runs ranking + rewriting given fresh
    operator notes. Lets the operator type "I just rolled back the
    deploy and the alert is still firing" and have the agent
    de-emphasize deploy-related hypotheses.

Both share the platform's existing LLM plumbing:
  - ``llm_provider.get_llm_client()`` returns the JSON-mode Vertex AI
    client (temperature=0).
  - ``llm_provider.safe_invoke()`` adds exponential backoff on
    transient 429/503/timeout failures.

The scoring + clustering logic stays in :mod:`sre.correlator` (pure
heuristic, no LLM). The LLM only writes prose. That separation keeps
ranks auditable AND keeps LLM cost bounded to top-N narrative writeups
regardless of how busy the project is.
"""
