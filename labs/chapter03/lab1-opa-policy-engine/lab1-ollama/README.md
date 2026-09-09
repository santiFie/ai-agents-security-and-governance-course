# Lab 3.1 — OPA Policy Engine: Autorización ABAC para Agentes

## Qué es este lab

Prueba si la autorización de un agente depende de un enforcement point real
(un Policy Decision Point externo que puede decir que no) o solo de que el
LLM "se porte bien". Es la contraparte agéntica del patrón OWASP de
Broken Access Control / Excessive Agency: cada tool del agente consulta a
OPA (Open Policy Agent) con una política Rego ABAC antes de ejecutar la
acción, no después.

## Las piezas que corren

- **OPA (Open Policy Agent)**: servidor de políticas standalone, corriendo en un
  contenedor Docker (`openpolicyagent/opa run --server --addr :8181`),
  expuesto en `localhost:8181`. Evalúa la política Rego `agent_authz` y
  responde `allow`/`deny`/`decision` vía API REST.
- **Política Rego (`agent_authz`)**: default-deny, con reglas ABAC por
  `spiffe_id`, bloqueo incondicional de `DELETE` en rutas `/production/*`
  (con precedencia explícita vía la regla combinada `decision := allow AND
  NOT deny` — en Rego, `deny` no sobreescribe `allow` automáticamente), y
  una regla de horario laboral para el rol `report-writer`.
- **`check_opa()`**: helper de enforcement que se llama de forma inline
  desde cada tool, antes de ejecutar la acción real. Si OPA deniega, lanza
  `PermissionError` y la tool nunca llega a "hacer" nada.
- **`financial_analyzer_opa`**: el agente, con modelo qwen3.5:9b local vía
  Ollama (`LiteLlm(model="ollama_chat/qwen3.5:9b", num_ctx=8192,
  temperature=0.2, reasoning_effort="none")`). Instrucción: comportarse como
  analista financiero, reportar el resultado real de la tool (permitido o
  denegado) sin narrar el razonamiento. Tools: `get_sales_data` (lectura,
  casi siempre permitida) y `delete_sales_data` (borrado, casi siempre
  denegada por política).

## El flujo

1. Se levanta OPA vía Docker y se carga la política Rego (`PUT
   /v1/policies/agent_authz`).
2. El agente recibe un pedido en lenguaje natural del usuario.
3. El LLM decide qué tool invocar (`get_sales_data` o `delete_sales_data`).
4. La tool, ANTES de hacer cualquier otra cosa, llama a `check_opa()`, que
   arma el `input` (subject/resource/action) y consulta `POST
   /v1/data/agent_authz/decision`.
5. Si OPA deniega, la tool lanza `PermissionError` — la "acción" nunca se
   ejecuta, sin importar qué haya decidido el LLM.
6. Se puede modificar la política en runtime (recargarla en OPA) y verificar
   que el comportamiento cambia sin tocar ni redeployar el agente.

## Por qué importa la verificación

El enforcement point es OPA, no el LLM — por eso la evidencia de efecto acá
no es "el agente dijo que no" sino que la tool nunca completa su lógica: se
imprime `[OPA check] <method> <path> -> decision=<bool>` en cada consulta y
`[tool call ejecutado] ...` solo si la tool llegó a ejecutar su lógica real
(nunca aparece para `delete_sales_data` cuando OPA deniega, porque la
excepción corta la ejecución antes de esa línea). Si solo se mirara la
respuesta en texto del agente, un LLM podría fabricar una respuesta de
"denegado" sin haber consultado a OPA en absoluto — el print del `decision`
real es lo que confirma que la política se evaluó de verdad.

## Resultado esperado

Con OPA cargado con la política original: `get_sales_data` responde con
datos reales (`decision=True`); `delete_sales_data` lanza `PermissionError`
(`decision=False`). Tras cargar una política modificada en runtime que
agrega una regla `allow` explícita para DELETE en producción (ver hallazgo
abajo), la misma tool que antes fallaba ahora se ejecuta — sin haber tocado
una línea de `financial_analyzer_opa`. La política de límite de trading
($10,000) permite órdenes de $5,000 y deniega órdenes de $15,000.

## Requisitos de infraestructura

Docker, para levantar OPA como servidor local (`openpolicyagent/opa run
--server --addr :8181`, expuesto en el puerto 8181). Si Docker no está
disponible, `_start_opa_container()` lo detecta y omite esa parte sin romper
el resto del script — el `Agent(...)` igual se instancia y se verifica por
sintaxis.

## Hallazgos técnicos

1. **El comando de Docker más directo no alcanza.** `docker run -p
   8181:8181 openpolicyagent/opa run --server` (sin `--addr`) deja el
   contenedor "Up" sin errores, pero cualquier request desde el host recibe
   `Connection reset by peer` — no timeout, no connection refused. Causa:
   sin `--addr` explícito, OPA escucha en `localhost:8181` *dentro* del
   namespace de red del contenedor, que el port-mapping de Docker no puede
   alcanzar. Fix: `opa run --server --addr :8181`.
2. **`decision := allow AND NOT deny` requiere que `allow` matchee de
   verdad, no alcanza con neutralizar `deny`.** Para permitir una acción que
   antes estaba bloqueada, no basta con anular la regla `deny` — hace falta
   además una regla `allow` explícita, porque `allow` por defecto es
   `false`. Es el mismo invariante ("deny nunca se asume implícito, pero
   tampoco allow") que motivó el patrón `decision` en primer lugar.
3. Si `delete_sales_data` dejara propagar el `PermissionError` de
   `check_opa` sin capturarlo, rompería el flujo del agente con una
   excepción no controlada en vez de reportar limpiamente "DENIED". El fix:
   capturar el error y devolver `{"deleted": False, "denied_by": "OPA",
   "reason": ...}` — la política se sigue aplicando de verdad, solo que de
   forma controlada.

## Backend Gemini (opcional)

El agente puede correr contra Gemini en Vertex AI en vez de Ollama local,
sin tocar la política OPA/Rego ni las tools — solo cambia `_build_model()`.
Requiere ADC y un proyecto de GCP con Vertex AI habilitado:

```
gcloud auth application-default login
export LAB_LLM_BACKEND=gemini
export GCP_PROJECT_ID=<tu-proyecto-gcp>
export GCP_LOCATION=us-central1
python3 opa_authz_lab31.py --selftest   # o sin --selftest para el agente real
```

Con este backend, el comportamiento esperado es el mismo que con Ollama:
`get_sales_data` permitido con datos reales, `delete_sales_data` en
producción bloqueado por OPA. Sin `LAB_LLM_BACKEND` (o con cualquier otro
valor), el comportamiento es el original (Ollama, sin cambios).
