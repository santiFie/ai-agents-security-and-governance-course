# Lab 3.3 — GCP IAM para Agentes: Secretless Architecture

## Qué es este lab

Prueba el patrón "secretless" (nunca hardcodear credenciales en el código;
obtenerlas siempre de una fuente externa en runtime) contra la categoría de
amenaza de credenciales estáticas filtradas/hardcodeadas. Compara
explícitamente un anti-patrón (`HARDCODED_API_KEY` en el código fuente)
contra un agente que obtiene su credencial de un vault en cada llamada, con
Secret Manager de GCP como backend real opcional y un vault JSON local como
fallback — de forma que rotar o revocar un secreto cambia el comportamiento
del agente sin tocar una línea de código.

## Las piezas que corren

- **`SecretlessConfig`**: gestiona el acceso a secretos. Intenta construir
  un cliente de GCP Secret Manager; si no hay librería o credenciales, cae a
  un vault JSON local (`/tmp/local_secrets_vault_lab33.json`) con versiones
  y un índice de "versión activa", simulando rotación y revocación sin
  depender de GCP.
- **Circuit breaker de Secret Manager**: protege contra el caso en que las
  credenciales de GCP (ADC) estén vencidas o no disponibles. El cliente se
  construye sin error, pero la primera llamada real (`access_secret_version`)
  intenta refrescar el token y puede colgarse indefinidamente — ni siquiera
  un `timeout=5` explícito en el RPC lo evita, porque el colgado ocurre en el
  paso de refresh de `google-auth`, antes del RPC. El breaker acota el
  intento completo desde afuera con un hilo daemon + `join(timeout)`: si no
  vuelve a tiempo, abre el circuito y el resto de la sesión usa el vault
  local sin reintentar GCP.
- **`secretless_agent`**: el agente, con modelo qwen3.5:9b local vía Ollama
  (misma config que Lab 3.1: `num_ctx=8192`, `temperature=0.2`,
  `reasoning_effort="none"`). Tool: `query_external_api_secure`, que llama a
  `config.get_secret()` en cada invocación — nunca lee una credencial
  cacheada en una variable global fija.

## El flujo

1. `config.get_secret("external-api-key")` intenta GCP; si falla o el
   circuito está abierto, usa el vault local (creando un secreto dummy la
   primera vez).
2. El agente usa la tool, que llama a `get_secret()` en runtime — nunca hay
   una API key hardcodeada en el código del agente.
3. `config.rotate_secret(...)` agrega una nueva versión al vault y la marca
   activa — la siguiente llamada a `get_secret()` la usa automáticamente,
   sin reiniciar el agente.
4. `config.disable_version(...)` revoca una versión específica — si era la
   activa, cae a la versión habilitada más reciente.

## Por qué importa la verificación

La evidencia de efecto es el valor real del secreto usado en cada llamada
(`key_used` en el resultado de la tool, truncado a 4 caracteres + `****`
para no loguear el secreto completo) — no la afirmación en texto del
agente. Si el agente dijera "usé la credencial rotada" sin que
`query_external_api_secure` realmente haya llamado a `get_secret()` después
de la rotación, el `key_used` real revelaría la discrepancia. También
importa medir el tiempo: el selftest hace un `assert elapsed <
_GCP_TIMEOUT_SECONDS + 2` explícito — sin el circuit breaker, esa aserción
fallaría (o el proceso ni siquiera llegaría a esa línea, colgado
indefinidamente contra GCP).

## Resultado esperado

Si las credenciales de GCP no están disponibles o están vencidas, el
circuit breaker corta el intento a los pocos segundos (el timeout
configurado), imprime que el circuito se abrió, y devuelve el secreto dummy
del vault local. Las llamadas siguientes son instantáneas (circuito ya
abierto, ni lo intenta con GCP). Rotar y revocar secretos localmente cambia
el valor devuelto por `get_secret()` de inmediato. Con credenciales de GCP
vigentes, el comportamiento es idéntico al original: usa Secret Manager de
forma transparente, sin activar nunca el breaker.

## Requisitos de infraestructura

Ninguno obligatorio. GCP Secret Manager es opcional; el lab es 100%
ejecutable con el vault JSON local.

## Hallazgos técnicos

1. **El colgado no respeta `timeout=` en el RPC.** Con credenciales
   vencidas, la reautenticación de `google-auth` ocurre *antes* del RPC en
   sí, y ese paso no está cubierto por el parámetro `timeout` del request.
2. **`ThreadPoolExecutor` no es suficiente para acotar el colgado.** Una
   implementación con `concurrent.futures.ThreadPoolExecutor` +
   `future.result(timeout=...)` deja que la lógica de negocio funcione bien
   (circuito se abre, vault local responde), pero el *proceso completo*
   queda colgado al final igual, porque `ThreadPoolExecutor` registra un
   hook de `atexit` que espera a que todos sus hilos terminen antes de
   dejar salir al intérprete, y el hilo bloqueado contra GCP nunca termina.
   Fix: reemplazar por un `threading.Thread(daemon=True)` crudo — un hilo
   daemon no bloquea la salida del proceso.
3. Sin una regla imperativa explícita en la instrucción del agente ("tenés
   que llamar a la herramienta ANTES de responder"), el modelo puede
   fabricar una respuesta sin haber llamado a la tool — un mandato
   descriptivo ("después de llamar a la herramienta, respondé...") no es lo
   mismo que uno imperativo.

## Backend Gemini (opcional)

El agente puede correr contra Gemini en Vertex AI en vez de Ollama local
(mismo cambio que Lab 3.1: solo `_build_model()`, el patrón secretless no
cambia). Requiere ADC y un proyecto de GCP con Vertex AI habilitado:

```
gcloud auth application-default login
export LAB_LLM_BACKEND=gemini
export GCP_PROJECT_ID=<tu-proyecto-gcp>
export GCP_LOCATION=us-central1
python3 secretless_lab33.py --selftest   # o sin --selftest para el agente real
```

**Nota importante**: `GCP_PROJECT_ID` alimenta DOS cosas distintas en este
lab — el modelo Gemini (Vertex AI) y el backend de `SecretlessConfig`
(Secret Manager). Si el secreto `external-api-key` no existe todavía en ese
proyecto, el circuit breaker se activa por `NotFound` en vez de por
timeout, pero el resultado final es el mismo: cae al vault local sin
bloquear el resto del lab.
