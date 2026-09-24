"""enum_check_constraints: CHECK a nivel de BD para todas las columnas enum (D1)

Las columnas enum usaban ``Enum(native_enum=False)`` SIN CHECK real en BD (solo validación del
ORM): cualquier SQL crudo podía escribir un valor arbitrario. Este cambio añade un CHECK
``col IN (...)`` a cada una de las 44 columnas enum. Scan previo de producción: 0 valores fuera de
enum, así que ADD CONSTRAINT no falla. Aditivo y reversible.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-24 19:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None

# (tabla, columna, valores permitidos) — generado por introspección del modelo.
_ENUM_COLUMNS: list[tuple[str, str, list[str]]] = [
    ('allergy', 'severity', ['intolerance', 'allergy', 'anaphylaxis']),
    ('connector_state', 'status', ['active', 'degraded', 'disabled', 'unsupported', 'permission_required', 'temporarily_blocked', 'parser_broken', 'source_unavailable', 'partial_only']),
    ('coverage_snapshot', 'status', ['complete', 'high', 'partial', 'insufficient', 'stale', 'none']),
    ('crawl_job', 'status', ['queued', 'locked', 'completed', 'failed', 'dead_letter', 'cancelled']),
    ('crawl_run', 'run_type', ['discovery', 'catalog', 'prices', 'offers', 'health']),
    ('crawl_run', 'status', ['queued', 'running', 'completed', 'failed', 'cancelled']),
    ('data_import', 'format', ['csv', 'json']),
    ('data_import', 'source_type', ['official', 'authorized_partner', 'community_connector', 'open_dataset', 'admin_import', 'manual_entry', 'user_receipt', 'estimated', 'demo']),
    ('data_import', 'status', ['pending', 'validating', 'dry_run', 'committed', 'rolled_back', 'failed']),
    ('data_source', 'legal_status', ['unknown', 'public', 'authorized', 'permission_required', 'prohibited']),
    ('data_source', 'source_type', ['official', 'authorized_partner', 'community_connector', 'open_dataset', 'admin_import', 'manual_entry', 'user_receipt', 'estimated', 'demo']),
    ('food_preference', 'sentiment', ['like', 'dislike', 'avoid']),
    ('food_preference', 'subject_type', ['ingredient', 'cuisine', 'tag']),
    ('generation_job', 'status', ['queued', 'collecting_data', 'generating_candidates', 'validating', 'optimizing', 'completed', 'failed', 'cancelled']),
    ('grocery_list', 'coverage_status', ['complete', 'high', 'partial', 'insufficient', 'stale', 'none']),
    ('grocery_list_item', 'price_status', ['known', 'estimated', 'missing', 'stale']),
    ('history_remediation_change', 'status', ['planned', 'applied', 'restored', 'failed']),
    ('history_remediation_run', 'execution_mode', ['verify_only', 'simulate', 'apply', 'restore']),
    ('history_remediation_run', 'restore_status', ['none', 'restored', 'restore_failed', 'manual_review_required']),
    ('history_remediation_run', 'status', ['pending', 'verified', 'simulated', 'applied', 'failed', 'restored', 'rolled_back']),
    ('household_invitation', 'role', ['editor', 'viewer']),
    ('household_invitation', 'status', ['pending', 'accepted', 'revoked', 'expired']),
    ('household_member', 'role', ['owner', 'editor', 'viewer']),
    ('ingredient_product_mapping', 'verification_status', ['unverified', 'machine_verified', 'human_verified', 'disputed']),
    ('meal_plan', 'status', ['draft', 'generating', 'ready', 'failed', 'archived']),
    ('meal_requirement', 'meal_type', ['breakfast', 'lunch', 'snack', 'dinner']),
    ('optimization_run', 'status', ['queued', 'collecting_data', 'generating_candidates', 'validating', 'optimizing', 'completed', 'failed', 'cancelled']),
    ('planned_meal', 'meal_type', ['breakfast', 'lunch', 'snack', 'dinner']),
    ('planned_meal', 'status', ['planned', 'accepted', 'rejected', 'cooked', 'regenerating']),
    ('price_anomaly', 'severity', ['low', 'medium', 'high', 'critical']),
    ('price_anomaly', 'status', ['open', 'quarantined', 'approved', 'rejected']),
    ('price_observation', 'price_scope', ['exact_store', 'delivery_zone', 'postal_code', 'municipality', 'province', 'region', 'national', 'unknown']),
    ('price_observation', 'price_type', ['regular', 'promotional', 'loyalty', 'manual', 'receipt', 'estimated', 'unknown']),
    ('price_observation', 'verification_status', ['unverified', 'machine_verified', 'human_verified', 'disputed']),
    ('price_observation_occurrence', 'verification_status', ['unverified', 'machine_verified', 'human_verified', 'disputed']),
    ('product_nutrition', 'source_type', ['official', 'authorized_partner', 'community_connector', 'open_dataset', 'admin_import', 'manual_entry', 'user_receipt', 'estimated', 'demo']),
    ('product_price', 'availability', ['in_stock', 'out_of_stock', 'unknown']),
    ('product_price', 'source_type', ['official', 'authorized_partner', 'community_connector', 'open_dataset', 'admin_import', 'manual_entry', 'user_receipt', 'estimated', 'demo']),
    ('product_price', 'verification_status', ['unverified', 'machine_verified', 'human_verified', 'disputed']),
    ('promotion_rule', 'type', ['percentage', 'fixed', 'nxm', 'second_unit', 'min_quantity', 'pack']),
    ('recipe', 'origin', ['seed', 'ai_generated', 'user', 'imported']),
    ('recipe_feedback', 'sentiment', ['like', 'reject', 'no_show']),
    ('store_resolution', 'scope', ['exact_store', 'delivery_zone', 'postal_code', 'municipality', 'province', 'region', 'national', 'unknown']),
    ('user', 'status', ['active', 'suspended', 'anonymized']),
]


def _cname(table: str, column: str) -> str:
    return f"ck_{table}_{column}"


def upgrade() -> None:
    for table, column, values in _ENUM_COLUMNS:
        allowed = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
        op.create_check_constraint(
            _cname(table, column), table, f'"{column}" IN ({allowed})'
        )


def downgrade() -> None:
    for table, column, _values in _ENUM_COLUMNS:
        op.drop_constraint(_cname(table, column), table, type_="check")
