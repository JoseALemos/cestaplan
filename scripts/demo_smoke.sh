#!/usr/bin/env bash
# CestaPlan — prueba de humo end-to-end del vertical slice contra la API en marcha.
#
# Requisitos previos (ver README):
#   1. Postgres en marcha y migraciones aplicadas:  make migrate
#   2. Datos demo cargados:                          make seed
#   3. API en marcha:                                make api      (en otra terminal)
#   4. Worker en marcha:                             make worker   (en otra terminal)
#
# Uso:  bash scripts/demo_smoke.sh [API_URL]
# Recorre: registro -> login -> hogar de 2 -> equipamiento -> generar plan
#          (2 desayunos/4 comidas/1 merienda/3 cenas) -> polling -> plan -> lista.
set -euo pipefail
API="${1:-http://127.0.0.1:8000}"
JAR="$(mktemp)"
PY="python3"
j() { $PY -c "import sys,json;print(json.load(sys.stdin)$1)"; }
trap 'rm -f "$JAR"' EXIT

EMAIL="demo_$(date +%s)@example.com"
echo "== 1. Registro ($EMAIL) =="
curl -fsS -X POST "$API/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"Sup3rSecret!\",\"display_name\":\"Demo\"}" \
  -o /dev/null -w "  HTTP %{http_code}\n"

echo "== 2. Login =="
LOGIN=$(curl -fsS -c "$JAR" -X POST "$API/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"Sup3rSecret!\"}")
CSRF=$(echo "$LOGIN" | j "['csrf_token']")
CH=(-b "$JAR" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json')

echo "== 3. Hogar =="
HID=$(curl -fsS "${CH[@]}" -X POST "$API/api/v1/households" -d '{"name":"Casa Demo","currency":"EUR"}' | j "['id']")
echo "  household_id=$HID"

echo "== 4. Dos miembros (uno con alergia dura al gluten) =="
curl -fsS "${CH[@]}" -X POST "$API/api/v1/households/$HID/members" \
  -d '{"display_name":"Persona 1","role":"owner","allergies":[{"allergen_code":"gluten","severity":"anaphylaxis"}]}' \
  -o /dev/null -w "  m1 HTTP %{http_code}\n"
curl -fsS "${CH[@]}" -X POST "$API/api/v1/households/$HID/members" \
  -d '{"display_name":"Persona 2"}' -o /dev/null -w "  m2 HTTP %{http_code}\n"

echo "== 5. Equipamiento =="
curl -fsS "${CH[@]}" -X PUT "$API/api/v1/households/$HID/equipment" \
  -d '{"equipment":[{"equipment_code":"blender"},{"equipment_code":"oven"},{"equipment_code":"stovetop"},{"equipment_code":"toaster"}]}' \
  -o /dev/null -w "  HTTP %{http_code}\n"

echo "== 6. Generar plan (2 desayunos / 4 comidas / 1 merienda / 3 cenas, 80 EUR) =="
REQ='{"household_id":"'$HID'","start_date":"2026-07-22","end_date":"2026-07-28","budget_amount":"80.00","currency":"EUR","requirements":[
 {"meal_type":"breakfast","requested_count":2,"default_servings":2},
 {"meal_type":"lunch","requested_count":4,"default_servings":2},
 {"meal_type":"snack","requested_count":1,"default_servings":2},
 {"meal_type":"dinner","requested_count":3,"default_servings":2}]}'
GEN=$(curl -fsS "${CH[@]}" -X POST "$API/api/v1/plans/generate" -d "$REQ")
RUN=$(echo "$GEN" | j "['optimization_run_id']")
MPID=$(echo "$GEN" | j "['meal_plan_id']")
echo "  202 aceptado. run=$RUN"

echo "== 7. Esperando al worker =="
for _ in $(seq 1 30); do
  ST=$(curl -fsS "${CH[@]}" "$API/api/v1/plans/runs/$RUN")
  S=$(echo "$ST" | j "['status']")
  [[ "$S" == completed || "$S" == failed || "$S" == cancelled ]] && break
  sleep 2
done
echo "  estado final: $S"

echo "== 8. Plan =="
curl -fsS "${CH[@]}" "$API/api/v1/plans/$MPID" | $PY -c "
import sys,json
d=json.load(sys.stdin); meals=d.get('planned_meals',[])
from collections import Counter
print('  comidas:', len(meals), Counter(m.get('meal_type') for m in meals))
print('  cobertura:', d.get('coverage',{}).get('status'), d.get('coverage',{}).get('price_coverage'))
print('  margen presupuesto:', d.get('budget_diff'))
"
echo "== 9. Lista de la compra =="
curl -fsS "${CH[@]}" "$API/api/v1/plans/$MPID/grocery-list" | $PY -c "
import sys,json
d=json.load(sys.stdin); cats=d.get('categories',[])
items=[x for c in cats for x in c.get('items',[])]
print('  categorías:', len(cats), '| líneas:', len(items))
print('  coste conocido:', d.get('known_cost'), '| estimado:', d.get('estimated_cost'), d.get('currency'))
"
echo "== OK =="
