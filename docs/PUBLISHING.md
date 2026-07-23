# CestaPlan — Publicación en GitHub

Guía para publicar CestaPlan como repositorio público en GitHub. Cubre una
**checklist previa** (verificar antes de hacer nada público) y los **pasos exactos**
que ejecuta el mantenedor.

> **El repositorio NO tiene remoto configurado.** El *push* lo realiza el mantenedor a
> mano; el asistente **no** ejecuta `git push` ni ninguna operación de git. Sustituye
> `OWNER` por tu usuario/organización de GitHub en todos los ejemplos.

---

## 1. Checklist previa a la publicación

Marca cada punto antes de crear el repositorio público.

### Secretos y entorno

- [ ] **Auditoría de secretos hecha.** No hay claves, tokens ni contraseñas reales en
      el historial ni en el árbol de trabajo (`OPENAI_API_KEY`, `SESSION_SECRET`,
      `COMMERCIAL_FEED_API_KEY`, credenciales de base de datos…).
- [ ] **`.env` NO está versionado.** `.gitignore` ignora `.env` y `.env.*`; sólo se
      publica **`.env.example`** con valores de ejemplo/placeholder.
- [ ] **`.env.example` no contiene secretos reales**, sólo plantillas.
- [ ] Revisado que no hay volcados de base de datos, catálogos comerciales ni datos de
      usuarios reales en `data/` (sólo demo sintético `is_synthetic=true`).
- [ ] **Subsistema de precios (ingesta) seguro por defecto:** conectores **desactivados
      por defecto** (`SCRAPING_ENABLED`, `PRICE_SYNC_ENABLED` y todos los
      `*_CONNECTOR_ENABLED` en `false`); **sin scraping de fuentes bloqueadas**
      (`permission_required`/`unsupported` no rastrean, habilitarlos por API devuelve
      409); **auditoría de secretos limpia** (redacción de cabeceras/cookies/tokens, sin
      claves de feed reales en el árbol ni en `.env.example`).

### Licencias y comunidad

- [ ] **`LICENSE`** presente (MIT).
- [ ] Ficheros de comunidad presentes: **`CONTRIBUTING.md`**, **`CODE_OF_CONDUCT.md`**,
      **`SECURITY.md`**.
- [ ] Plantillas de GitHub presentes: `.github/ISSUE_TEMPLATE/`,
      `.github/PULL_REQUEST_TEMPLATE.md`, `.github/DISCUSSION_TEMPLATE/`.
- [ ] **`CODEOWNERS`** con el handle real del mantenedor (sustituir el placeholder
      `@OWNER`).
- [ ] **Licencias de datos documentadas.** `docs/DATA_SOURCES.md` explica que el código
      es MIT pero los datos **no** heredan esa licencia; Open Food Facts y Open Prices
      son **ODbL** (atribución + share-alike); los catálogos comerciales son
      `proprietary` y no se redistribuyen.
- [ ] **Documentación de la ingesta de precios presente y enlazada** desde el `README.md`:
      `docs/PRICE_INGESTION.md`, `docs/CONNECTOR_ARCHITECTURE.md`,
      `docs/RETAILER_SOURCE_MATRIX.md`, `docs/SCRAPING_POLICY.md`,
      `docs/DATA_RETENTION.md`, `docs/PRICE_QUALITY.md`, `docs/RAILWAY_PRICE_SYNC.md`,
      `docs/INCIDENT_RESPONSE.md`, `docs/FASE_F_DEPLOYMENT.md`,
      `docs/PRICE_SUBSYSTEM_AUDIT.md` y ADR `docs/adr/0008-price-ingestion-subsystem.md`.
- [ ] **`README.md`** y **`CHANGELOG.md`** actualizados y sin enlaces rotos.

### Integración continua

- [ ] **CI en verde** localmente: `make lint`, `make typecheck`, `make test`
      (~547 tests de backend, incluida la ingesta de precios, + suites JS) pasan.
- [ ] El workflow `.github/workflows/ci.yml` existe y apunta a las ramas correctas.

---

## 2. Pasos de publicación (los ejecuta el mantenedor)

### 2.1 Crear el repositorio en GitHub

Con la CLI de GitHub (recomendado):

```bash
gh repo create OWNER/cestaplan --public \
  --description "PWA open source de planes de comida por tienda y presupuesto" \
  --disable-wiki
```

O créalo desde la web (**New repository**, público, **sin** README/licencia/`.gitignore`
autogenerados: el repositorio ya los trae).

### 2.2 Añadir el remoto y hacer push de `master`

El repositorio usa la rama **`master`**.

```bash
cd /ruta/a/cestaplan
git remote add origin git@github.com:OWNER/cestaplan.git   # o la URL HTTPS
git push -u origin master
```

### 2.3 Configurar temas (topics) y descripción

```bash
gh repo edit OWNER/cestaplan \
  --add-topic meal-planning --add-topic pwa --add-topic nextjs \
  --add-topic fastapi --add-topic python --add-topic typescript \
  --add-topic open-source --add-topic budget --add-topic spain
```

### 2.4 Habilitar Discussions

```bash
gh repo edit OWNER/cestaplan --enable-discussions
```

(Las plantillas de `.github/DISCUSSION_TEMPLATE/` se activan automáticamente.)

### 2.5 Etiquetas, incluida "good first issue"

`good first issue` existe por defecto en GitHub. Crea o revisa el resto:

```bash
gh label create "good first issue" --color 7057ff --description "Buen punto de entrada" --force
gh label create "help wanted"      --color 008672 --description "Se agradece ayuda"       --force
gh label create "documentation"    --color 0075ca --description "Mejoras de documentación" --force
gh label create "data-source"      --color d4c5f9 --description "Adaptadores y fuentes de datos" --force
```

### 2.6 Protección de la rama `master`

Desde **Settings → Branches → Add rule** (o con la API):

- Requiere **pull request** antes de fusionar (al menos 1 revisión).
- Requiere que **pasen los status checks** de CI antes de fusionar.
- (Opcional) Requiere ramas actualizadas y conversaciones resueltas.

Con la API de GitHub:

```bash
gh api -X PUT repos/OWNER/cestaplan/branches/master/protection \
  -H "Accept: application/vnd.github+json" \
  -f "required_status_checks[strict]=true" \
  -f "required_pull_request_reviews[required_approving_review_count]=1" \
  -f "enforce_admins=false" \
  -f "restrictions=null"
```

> Nota: ajusta el nombre del *status check* al job real de `ci.yml` cuando GitHub lo
> haya ejecutado al menos una vez (los checks sólo aparecen tras la primera ejecución).

### 2.7 Activar CI

GitHub Actions se activa solo al detectar `.github/workflows/ci.yml` tras el primer
push. Verifica en la pestaña **Actions** que el workflow **CI** corre y termina en
verde; si no, actívalo en **Settings → Actions → General** (permitir workflows).

---

## 3. Después de publicar

- [ ] Rellenar el badge de CI del `README.md` con el badge real del workflow.
- [ ] Actualizar los enlaces `[Unreleased]` / `[0.2.0]` / `[0.1.0]` del `CHANGELOG.md`
      con la URL real del repositorio.
- [ ] Crear el release `v0.2.0` (`gh release create v0.2.0 --generate-notes`).
- [ ] Añadir la descripción social / imagen del repositorio (opcional).
