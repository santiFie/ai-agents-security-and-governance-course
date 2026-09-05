# Guía de Demo — Tema 3: Identidad y Autenticación (SPIFFE/SPIRE)

> Versión para alumnos. Slides de referencia: `slides/ch03-teoria-slides_condensado.md`.
> Recurso de aprendizaje asociado: [`RECURSOS_APRENDIZAJE/jti.md`](../RECURSOS_APRENDIZAJE/jti.md)
> (el claim `jti` que usa el Lab 3.2 para revocar SVIDs).

## Qué se muestra y por qué

El Tema 3 responde una pregunta que OAuth/SAML no fueron diseñados para
responder: ¿quién es un agente, y qué tiene permitido hacer, cuando no hay un
humano presente para autenticarse? Los 4 labs recorren el arco completo de
identidad → autorización → guardrails → secretos, en este orden:
**3.2 → 3.1 → 3.4 → 3.3**.

- **Lab 3.2 (Workload Identity)** es el punto de partida conceptual: un
  mini-SPIRE que emite SVIDs (JWT RS256 de corta duración) y demuestra las
  tres propiedades que reemplazan a una API key estática — expiración real,
  revocación independiente de la expiración, y un `spiffe_id` verificado
  criptográficamente (no un string de confianza). Sin LLM, sin
  infraestructura: es la base sobre la que se apoya todo lo demás.
- **Lab 3.1 (OPA)** contesta "¿y ahora qué puede hacer ese agente?" — un
  Policy Decision Point (OPA + Rego) evaluado **antes** de cada tool call, no
  después de que el LLM decida. El punto pedagógico central: la autorización
  no depende de que el LLM "se porte bien" — depende de un enforcement point
  que no negocia. El Ejercicio 4 (recargar la política en runtime sin
  redeployar el agente) es el momento más fuerte de la demo.
- **Lab 3.4 (NeMo Guardrails)** es la capa complementaria, no sustituta: OPA
  frena una acción ya decidida por el agente (post-decisión); NeMo Guardrails
  intercepta el mensaje del usuario **antes** de que el LLM razone sobre él
  (pre-decisión), vía una acción Python determinista registrada en un flujo
  Colang. Mismo criterio de bloqueo (DELETE en producción) que Lab 3.1, para
  que la comparación de capas sea directa.
- **Lab 3.3 (Secretless)** cierra con la otra mitad del problema de
  identidad: una vez que el agente tiene su SVID y su autorización, ¿de dónde
  saca sus credenciales hacia terceros? Nunca hardcodeadas — de un vault (GCP
  Secret Manager o, si no hay credenciales o el circuit breaker se activa, un
  vault JSON local) consultado en cada llamada, de forma que rotar/revocar un
  secreto cambia el comportamiento del agente sin tocar código.

Los 4 labs existen en dos variantes con la misma lógica pedagógica:
`-ollama` (modelo local `qwen3.5:9b`, gratis, sin cuota) y `-gcp` (Gemini vía
Vertex AI). Lab 3.2 es la excepción: no tiene LLM en absoluto (JWT /
criptografía pura), así que sus dos carpetas corren el mismo código.

## Secuencia de la demo

### Lab 3.2 — Workload Identity Simulada

**Comando** (mismo código en ambas carpetas):

```bash
python3 labs/ch03-lab2-ollama/workload_identity_lab32.py
# o, si se prefiere la carpeta GCP (código idéntico):
python3 labs/ch03-lab2-gcp/workload_identity_lab32.py
```

**Qué mirar**: `MiniSVIDIssuer` genera un par RSA-2048 y emite un JWT RS256
con `exp` corto y `jti` único. `main()` corre 4 ejercicios en secuencia:
verificación de un SVID recién emitido; un SVID con TTL de 5 segundos que
expira de verdad tras esperar 6 segundos; un SVID revocado por `jti` que se
rechaza aunque no haya expirado (el punto pedagógico más importante:
revocación y expiración son mecanismos independientes); y el armado del
`policy_input` que Lab 3.1 esperaría, a partir del `spiffe_id` extraído de un
SVID **verificado**, no confiado a ciegas. Ver
[`jti.md`](../RECURSOS_APRENDIZAJE/jti.md) para el fundamento completo de ese
claim.

**Para pensar**: ¿por qué el `jti` (JWT ID) importa si ya tenemos `exp`? ¿Qué
ataque cubre la revocación que la expiración sola no cubre? *(Pista:
revocar antes de tiempo — por ejemplo un agente comprometido detectado a
mitad de su TTL de 1 hora — sin depender de esperar a que expire solo.)*

### Lab 3.1 — OPA Policy Engine: Autorización ABAC

**Comando**:

```bash
# Selftest: levanta OPA real vía Docker, corre 5 ejercicios contra el
# PDP con requests HTTP puros -- NO invoca al LLM en ningún momento.
python3 labs/ch03-lab1-ollama/opa_authz_lab31.py --selftest
python3 labs/ch03-lab1-gcp/opa_authz_lab31.py --selftest

# Con el agente real (dispara llamadas al LLM):
python3 labs/ch03-lab1-ollama/opa_authz_lab31.py
python3 labs/ch03-lab1-gcp/opa_authz_lab31.py
```

**Qué mirar**: la política Rego `agent_authz` (default-deny) y la regla
combinada `decision := allow AND NOT deny` — en Rego, `deny` **no**
sobreescribe automáticamente a `allow`, por eso hace falta la regla
combinada explícita. La línea `[OPA check] <method> <path> -> decision=<bool>`
que imprime cada tool call es la evidencia de que la política se evaluó de
verdad, no que "el agente dijo que no".

**Salida esperada**: `get_sales_data` se permite y devuelve datos reales;
`delete_sales_data` en producción se bloquea (`decision=False`), con
`denied_by: OPA` en el resultado estructurado. El Ejercicio 4 (recargar la
política en runtime sin redeployar el agente) es el momento más fuerte:
`delete_sales_data`, que antes fallaba, ahora se ejecuta sin tocar una línea
de Python — el cambio de comportamiento viene enteramente de OPA. El
Ejercicio 5 verifica una política de trading con límite de $10.000 en ambos
sentidos.

**Hallazgos técnicos reales de este lab** (buen material sobre "esto no
está en el libro, aparece al correr el lab de verdad"):
1. El comando de Docker más directo (`docker run -p 8181:8181
   openpolicyagent/opa run --server`, sin `--addr`) deja el contenedor "Up"
   pero inalcanzable desde el host — `Connection reset by peer`, no
   timeout. Causa: sin `--addr` explícito, OPA escucha en el loopback
   *interno* del contenedor, no accesible vía el port-mapping de Docker.
2. `decision := allow AND NOT deny` no alcanza con neutralizar `deny` —
   hace falta además una regla `allow` explícita para el nuevo caso, porque
   `allow` por defecto es `false`. Buen ejemplo de que "deny nunca se asume
   implícito, pero tampoco allow".
3. Si `delete_sales_data` dejara propagar el `PermissionError` de OPA sin
   capturarlo, rompería el flujo del agente con una excepción no
   controlada, en vez de reportar limpiamente "DENIED" — mismo principio de
   "fallo controlado, no excepción sin manejar" que se aplica en otras
   capas del curso.

**Para pensar**: si el LLM decidiera fabricar en su respuesta final "listo,
borré los datos" sin haber llamado a la tool, ¿cómo lo detectaríamos?
*(Pista: el print `[tool call ejecutado]` nunca aparecería para
`delete_sales_data` — la evidencia de efecto está en los logs de la tool,
no en el texto del agente.)*

### Lab 3.4 — NeMo Guardrails: filtrado pre-decisión

**Comando** (requiere un venv con Python 3.12 — ver el README del lab):

```bash
./labs/ch03-lab4-ollama/venv_nemo/bin/python3 labs/ch03-lab4-ollama/nemo_guardrails_lab34.py --parte-b
./labs/ch03-lab4-gcp/venv_nemo/bin/python3 labs/ch03-lab4-gcp/nemo_guardrails_lab34.py
```

**Qué mirar**: el flujo Colang `check dangerous tool request` ejecuta una
acción Python determinista (`check_dangerous_tool_request`, matching de
substrings) **antes** de que el mensaje llegue a cualquier LLM. Contrastar
con Lab 3.1: ahí OPA frena una acción que el LLM ya decidió tomar; acá el
mensaje ni siquiera llega al LLM si es peligroso.

**Salida esperada**: en la Parte A (determinista, con `FakeListLLM`, sin
red), un mensaje benigno pasa y devuelve la respuesta fija del modelo de
prueba; un mensaje peligroso ("Elimina los datos de ventas de producción")
se bloquea con el texto de rechazo del Colang, sin que el LLM llegue a
participar. La Parte B repite el mismo bloqueo contra el modelo real.

**Hallazgo técnico real**: no existe un engine `ollama` nativo en
`nemoguardrails` — la solución es usar `engine: openai` +
`parameters.base_url` apuntando al endpoint OpenAI-compatible que Ollama
expone en `/v1`. Para Vertex AI, el engine correcto es `google_vertexai`
(no `google_genai`, que resolvería contra AI Studio).

**Para pensar**: ¿por qué NeMo Guardrails no reemplaza a OPA, ni viceversa?
*(Pista: son distintas capas — uno filtra el mensaje de entrada antes de
que el LLM razone, el otro autoriza una acción ya decidida; un atacante que
evada el filtrado de texto igual choca con OPA al intentar la tool call, y
viceversa un mensaje benigno que el LLM malinterprete en una tool call
peligrosa igual choca con OPA aunque haya pasado el rail.)*

### Lab 3.3 — GCP IAM Secretless

**Comando**:

```bash
python3 labs/ch03-lab3-ollama/secretless_lab33.py --selftest
python3 labs/ch03-lab3-gcp/secretless_lab33.py --selftest

# Con el agente real:
python3 labs/ch03-lab3-ollama/secretless_lab33.py
python3 labs/ch03-lab3-gcp/secretless_lab33.py
```

**Qué mirar**: `SecretlessConfig` intenta GCP Secret Manager primero; si no
hay credenciales, cae a un vault JSON local. Comparar en el código
`HARDCODED_API_KEY` (el anti-patrón, comentado como "NUNCA hacer esto")
contra `query_external_api_secure`, que llama a `get_secret()` en cada
invocación.

**Salida esperada**: si las credenciales de GCP no están disponibles o
fallan, un circuit breaker acota el intento a unos segundos y cae al vault
local, sirviendo un secreto dummy. Rotar y revocar secretos localmente
(`rotate_secret`, `disable_version`) cambia de inmediato el valor que
devuelve `get_secret()`, sin reiniciar el agente.

**Hallazgos técnicos reales** (dos capas, buen ejemplo de depuración en
cascada):
1. **El colgado no respeta `timeout=`.** Con credenciales vencidas, la
   reautenticación de la librería de Google ocurre *antes* del RPC —
   pasarle un `timeout` a la llamada de Secret Manager no sirve de nada,
   porque el colgado está un nivel más arriba. El fix acota el intento
   *completo* desde afuera con un hilo daemon.
2. **Un `ThreadPoolExecutor` no alcanza** para este mismo problema: la
   lógica de negocio funciona, pero el *proceso* queda colgado al final
   igual, porque el executor espera a que todos sus hilos terminen antes
   de salir. Un hilo `daemon=True` crudo no tiene ese problema.
3. Sin una regla imperativa explícita en la instrucción del agente ("tenés
   que llamar a la herramienta ANTES de responder"), el modelo puede
   fabricar una respuesta sin haber llamado a la tool — un mandato
   descriptivo ("después de llamar a la herramienta, respondé...") no es
   lo mismo que uno imperativo.

**Para pensar**: ¿qué pasaría si el circuit breaker no existiera y las
credenciales de GCP estuvieran vencidas? *(Pista: cada llamada a
`get_secret()` se colgaría indefinidamente esperando el refresh de
credenciales — el agente completo se congelaría, no solo esa tool call.)*

## GCP vs Ollama

- **Lab 3.2**: sin diferencia real — código idéntico, sin LLM.
- **Lab 3.1**: misma lógica y misma política Rego en ambas variantes; solo
  cambia de dónde viene la respuesta del modelo.
- **Lab 3.3**: misma lógica central (vault + circuit breaker). La versión
  GCP agrega un campo `source` en el resultado (`secret-manager` /
  `local-vault` / `local-vault-fallback`) para que el fallback sea visible
  en el reporte del agente, en vez de un fallback silencioso.
- **Lab 3.4**: acá sí hay una diferencia estructural, no solo de modelo — el
  engine cambia (`openai` + `base_url` para Ollama vs. `google_vertexai`
  para GCP) porque no existe un engine nativo de Ollama en
  `nemoguardrails`, y Vertex AI no expone un endpoint OpenAI-compatible
  simple.

## Preguntas frecuentes

1. **"¿Por qué no alcanza con que el LLM tenga instrucciones claras de no
   borrar datos en producción?"** — Porque las instrucciones son texto que
   el modelo puede ignorar, malinterpretar, o que un prompt injection puede
   sobreescribir. OPA y el input rail de NeMo son mecanismos que evalúan la
   acción/mensaje de forma determinista, fuera del control del LLM.
2. **"¿SPIFFE/SPIRE reemplaza a OAuth?"** — No, resuelve un problema
   distinto: OAuth/SAML asumen un humano presente para autenticarse una vez
   y confiar por un rato largo; SPIFFE emite identidad de *workload* (no de
   usuario), de vida corta, verificada por atestación de la plataforma,
   pensada para que un proceso pruebe quién es sin secretos pre-colocados.
3. **"Si OPA es el enforcement point, ¿qué pasa si OPA mismo se cae?"** —
   En el diseño de este lab, `check_opa()` no captura una eventual
   excepción de conexión HTTP hacia OPA (solo captura el `PermissionError`
   de una decisión ya recibida) — si OPA no responde, la tool lanzaría una
   excepción de conexión sin capturar. Es un punto real para discutir:
   default-deny en política sí, pero ¿qué política de fail-open/fail-closed
   debería aplicar si el propio PDP es inalcanzable?
4. **"¿Por qué el vault local usa un archivo temporal?"** — Es a propósito
   simple e inseguro (es un *fallback* de desarrollo, no la solución real)
   — el punto pedagógico es que el código de la tool nunca cambia entre
   usar Secret Manager real o el vault dummy; la seguridad real vendría de
   usar Secret Manager (o Vault/KMS) en producción, con IAM restringiendo
   el acceso al secreto en sí.
5. **"¿Por qué NeMo Guardrails y OPA no se combinaron en un solo lab?"** —
   Pedagógicamente es más claro separarlos: uno enseña autorización
   post-decisión (Lab 3.1), el otro filtrado pre-decisión (Lab 3.4). En un
   sistema real, ambos correrían juntos — son complementarios, no
   alternativos.
