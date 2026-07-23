# Versioned provider schemas (spec §L)

Each fetched provider schema is stored here as `<provider>/vN/{schema.json,meta.json}` by
`python -m cestaplan_api.tools.fetch_provider_schema`. Only **public** schemas (e.g. Open
Prices OpenAPI) live here — never secrets, never raw provider catalogues. Drift is graded and
an incompatible change is written as a NEW version marked `breaking`/`review_required`, never
silently overwriting the prior one.
