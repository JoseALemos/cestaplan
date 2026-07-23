# Golden provider fixtures (spec §N)

Only **golden** fixtures live here — manually reviewed and rights-cleared, preferably
synthetic (`synthetic_structure=true`), and independent of any live API. Raw and unsanitized
samples are git-ignored (`.local/`, `provider-samples/`, `raw-provider-responses/`) and must
never be committed. Promotion is gated by `cestaplan_api.ingestion.providers.fixtures`.
