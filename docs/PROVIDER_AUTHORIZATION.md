# Autorización y derechos de las fuentes de precios

Cómo CestaPlan registra y presenta los **derechos de uso** de cada fuente de precios, manteniendo
la autorización legal **separada** de la disponibilidad técnica, la calidad, la cobertura, la
aptitud para costear y la activación productiva del planificador.

> **Confidencialidad.** Las autorizaciones de las cadenas se instrumentan mediante **acuerdos
> privados**. Este repositorio **nunca** contiene contratos, claves de API, nombres de firmantes,
> correos, números de expediente ni ningún documento confidencial. Solo se registran hechos
> *públicos-seguros* (que el uso está autorizado, la base de licencia, un texto público y el
> alcance de permisos). Las referencias contractuales viven exclusivamente en campos **internos**,
> visibles solo para administradores, y se rellenan fuera de banda.

## 1. Contexto

El titular del proyecto declara que dispone de licencia válida de las APIs empleadas y de
autorización expresa de **DIA, Alcampo, Carrefour, Lidl, Aldi, Deza y Mercadona** para almacenar,
tratar, visualizar y usar comercialmente sus datos dentro de CestaPlan. Antes, todas las fuentes se
presentaban como *licencia desconocida / derechos en revisión*; ahora su uso autorizado queda
correctamente registrado.

## 2. Modelo de derechos

Los derechos se guardan en `ProviderActivation` (`models/ingestion.py`), en columnas
**independientes** de la activación de producción:

| Campo | Uso | Visibilidad |
|---|---|---|
| `data_rights_status` | vocabulario: `commercial_use_allowed`, `odbl`, `own_synthetic`, … | pública |
| `authorization_status` | `unknown` / `pending` / `verified` / `rejected` | pública |
| `license_basis` | `private_commercial_agreement` / `odbl` / `own_synthetic` | pública |
| `license_display_name` | p. ej. «Licencia comercial privada» | pública |
| `rights_display_name` | p. ej. «Uso autorizado» | pública |
| `rights_scope` (JSONB) | permisos explícitos (ver abajo) | pública |
| `attribution_text_public` | atribución a mostrar (nullable) | pública |
| `valid_from` / `valid_until` | vigencia (nullable) | pública |
| `authorization_verified_at` / `_by` | sello de verificación | interna |
| `internal_evidence_reference` | referencia privada al acuerdo/evidencia | **interna — nunca se expone** |
| `legal_notes_internal` | notas legales internas | **interna — nunca se expone** |

### `rights_scope`

```json
{
  "api_access": true, "storage": true, "processing": true, "display": true,
  "commercial_use": true, "derived_results": true,
  "raw_redistribution": false, "attribution_required": null
}
```

- `raw_redistribution` queda **`false`** salvo autorización diferenciada explícita.
- `attribution_required = null` significa **«gestionado por acuerdo privado»**, no «no hace falta
  atribución». No se afirma que la atribución sea innecesaria.

## 3. Separación de ejes

La autorización legal **no** desbloquea producción. Se mantienen independientes:
`transport_enabled`, `capture_enabled`, `normalization_enabled`, `staging_enabled`,
`shadow_enabled`, `production_enabled`, `production_approved`, `production_eligibility`,
`costing_eligibility`.

- **Derechos aprobados** → visualización, almacenamiento y actualización permitidos.
- **Planificador** → usa una fuente solo cuando además supera mapper, calidad, cobertura, envase,
  frescura y **aprobación humana** (`production_enabled` **y** `production_approved`).
- Una fuente autorizada pero incompleta se muestra **autorizada + experimental**, nunca
  «jurídicamente bloqueada». El *badge* técnico y la etiqueta de derechos son ejes distintos.

## 4. Registro canónico

`ingestion/providers/rights.py` declara **una sola vez** los hechos por fuente (base de licencia,
display, scope, proveedor técnico, `official_api`, texto público). Es la fuente de verdad para el
endpoint y para el bootstrap.

- Proveedor técnico (**Parse.bot** / **Apify**) ≠ titular de los datos (la cadena). Un
  intermediario **nunca** se presenta como API oficial (`official_api = false`); se expone
  `authorized_source = true` para reflejar la autorización real.
- **Open Prices**: ODbL, `official_api = true`, atribución requerida.
- **MercaEjemplo (demo)**: datos sintéticos propios; se avisa de que no son precios reales.

## 5. Endpoint

`GET /api/v1/price-providers` devuelve un modelo tipado (`schemas/catalog.py`) con los derechos, el
proveedor técnico, `official_api`, `authorized_source`, `rights_scope`, los textos públicos, y —en
ejes separados— transporte, mapper, calidad, cobertura, costeo y producción. **Nunca** expone los
campos internos.

## 6. Bootstrap de derechos

```bash
# Producción: SIEMPRE dry-run primero y revisar el diff saneado.
python -m cestaplan_api.tools.bootstrap_source_rights --dry-run --all
python -m cestaplan_api.tools.bootstrap_source_rights --apply --all
python -m cestaplan_api.tools.bootstrap_source_rights --apply --provider parsebot-dia
```

Garantías: ejecutable sobre una DB recién migrada; **no** siembra productos ni precios; **no** hace
llamadas externas; **no** activa producción ni costeo; idempotente; **rellena** solo valores
indecisos (`unknown` / `under_review` / sin valor) y **nunca** sobrescribe un cambio administrativo;
**nunca** escribe secretos (las columnas internas no se tocan y no se imprimen).

Esto también cubre la **migración de datos** (§11): los `ProviderActivation` existentes reciben los
derechos correctos sin alterar `production_enabled`, `production_approved`, `costing_eligibility`,
`mapper_status` ni `data_quality_status` (ninguno de esos valores se derivaba del antiguo bloqueo
jurídico, por lo que no cambian).

## 7. Frontend

- Público (`/onboarding/tienda`): cada cadena muestra su eje de **derechos** («Uso autorizado ·
  Licencia comercial privada · vía Parse.bot») separado del *badge* técnico y de la aptitud para
  costear, con chips independientes: Autorizado · Operativo · Costeable · Aprobado para el
  planificador.
- Admin (`/admin/fuentes`): se eliminaron los textos «Licencia no especificada» y «Sin texto de
  atribución declarado»; si un dato falta, se muestra copy neutro honesto, nunca una afirmación
  falsa de derechos.

## 8. Alcance pendiente (seguimiento)

La preservación completa de campos crudos de catálogo en el modelo normalizado
(`services/provider_sync.py` descarta hoy barcode/imagen/promoción/precio unitario…), el JSONB
`provider_specific_metadata` y los endpoints de navegación de catálogo (productos, promociones,
histórico, disponibilidad, con filtros y paginación) se abordan como trabajo **posterior**: es un
cambio grande, sin datos reales aún en producción, y merece su propia revisión. Los puntos de
descarte exactos están documentados en la auditoría de esta rama.
