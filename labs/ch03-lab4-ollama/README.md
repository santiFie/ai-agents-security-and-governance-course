# Lab 3.4 — NeMo Guardrails: Política Conversacional Declarativa

## Qué es este lab

Prueba el mismo criterio de bloqueo que la política Rego de Lab 3.1 (ninguna
acción DELETE contra datos de producción), pero implementado como *input
rail* declarativo (NVIDIA NeMo Guardrails) en vez de sidecar HTTP de OPA. El
punto pedagógico es de capas, no de sustitución: OPA evalúa una acción ya
decidida por el agente (autorización *post*-decisión — el LLM ya eligió
invocar la tool, y ahí se lo frena); NeMo Guardrails intercepta el mensaje
del usuario *antes* de que el LLM siquiera razone sobre él (filtrado
*pre*-decisión). Un sistema robusto usa ambas.

## Las piezas que corren

- **Parte A — `FakeListLLM`**: un LLM de prueba determinista de
  `langchain_community`, que siempre devuelve una respuesta fija de una
  lista predefinida. No hace red, no tiene API key, no depende de ningún
  modelo real — sirve para verificar el *mecanismo* del rail (¿bloquea o no
  bloquea?) sin el ruido ni la latencia de un LLM de verdad.
- **`check_dangerous_tool_request`**: la acción Python determinista
  (`@action`, sin LLM) que decide si un mensaje es peligroso — mismo
  criterio por substring que la política Rego de Lab 3.1 (`delete`,
  `eliminar`, `drop table`, etc.).
- **El input rail** (Colang): `define flow check dangerous tool request`
  ejecuta la acción; si devuelve `True`, corta el flujo (`stop`) y responde
  con el mensaje de rechazo — el mensaje del usuario nunca llega al LLM
  principal.
- **Parte B — Ollama local**: el mismo rail, apuntando a `qwen3.5:9b` vía el
  cliente OpenAI-compatible integrado de `nemoguardrails` 0.23
  (`engine: openai` + `parameters.base_url: http://localhost:11434/v1`) en
  vez de un engine contra un modelo en la nube.

## El flujo

1. El usuario manda un mensaje.
2. El input rail lo intercepta y llama a `check_dangerous_tool_request`.
3. Si el resultado es `True` (mensaje peligroso), el rail corta el flujo y
   devuelve el mensaje de rechazo — sin que el mensaje llegue nunca al LLM
   principal (ni `FakeListLLM` en la Parte A, ni `qwen3.5:9b` en la Parte
   B).
4. Si el resultado es `False` (mensaje benigno), el mensaje sigue su curso
   normal hacia el LLM principal, que genera la respuesta.

## Por qué importa la verificación

La acción `check_dangerous_tool_request` imprime
`[input rail ejecutado] check_dangerous_tool_request(text=...) -> <bool>`
en cada invocación — eso confirma que el mecanismo se ejecutó de verdad, no
que "la respuesta sonó como un rechazo". Con `FakeListLLM`, además, hay una
garantía estructural extra: como el LLM de prueba SIEMPRE devuelve la misma
respuesta fija ("Aquí tenés los datos de ventas..."), si esa respuesta
apareciera para el mensaje peligroso sería prueba inequívoca de que el rail
no bloqueó — no hay forma de que un LLM "alucine" un bloqueo que nunca pasó,
porque el LLM ni siquiera decide el contenido de la respuesta de rechazo (
viene fijo del Colang). Ojo con los desajustes de substring: si el texto de
prueba usa "Elimina" (imperativo) pero la lista de marcadores solo tiene
"eliminar" (infinitivo), el rail no bloquea — no porque el mecanismo falle,
sino por un desajuste de string.
Recordatorio de que las reglas basadas en keywords son frágiles ante
variaciones morfológicas.

## Resultado esperado

Parte A: el mensaje benigno pasa y devuelve la respuesta fija de
`FakeListLLM`; el mensaje peligroso es bloqueado con el texto "Solicitud
bloqueada por la política de seguridad...". Parte B (si se ejecutara con
`--parte-b`): el mismo bloqueo, pero contra `qwen3.5:9b` real — el rail
debería interceptar antes de que el mensaje llegue al modelo local, igual
que con `FakeListLLM`.

## Requisitos de infraestructura

`nemoguardrails==0.23.0` requiere Python `>=3.10,<3.14`. Si el Python del
sistema es más nuevo, hay que crear un venv con Python 3.12 (por ejemplo con
`uv python install 3.12 && uv venv --python 3.12 venv_nemo`).

## Hallazgos técnicos

No existe un engine `ollama` nativo en `nemoguardrails` — la solución es
usar el patrón que la propia documentación describe para vLLM/TGI/
llama.cpp/OpenRouter: `engine: openai` + `parameters.base_url`, el cliente
HTTP integrado (sin LangChain) que habla el protocolo OpenAI-compatible que
Ollama expone en `/v1` desde 2024:

```yaml
models:
  - type: main
    engine: openai
    model: qwen3.5:9b
    parameters:
      base_url: http://localhost:11434/v1
      api_key: ollama   # placeholder no vacío; Ollama no valida la key
rails:
  input:
    flows:
      - check dangerous tool request
```

Es más simple que apuntar a un modelo en la nube vía LangChain, porque no
requiere el framework LangChain en absoluto para esta parte — solo lo
necesita la Parte A, donde `FakeListLLM` es explícitamente un objeto
LangChain que se pasa como `llm=`.

Por defecto el script solo valida el `RailsConfig` por sintaxis; correr con
`--parte-b` dispara la llamada real contra Ollama.

**Nota de diseño**: existe también la alternativa `engine: litellm` con
`ollama_chat/qwen3.5:9b` (mencionada en `ch03-labs.md`) documentada como
comentario en el código, pero no implementada — requiere instalar
`litellm` en este venv y fijar `NEMOGUARDRAILS_LLM_FRAMEWORK=langchain`
para el mismo resultado, más dependencias para nada adicional.

## Backend Gemini (opcional) — Parte C

Mismo rail (Colang + `check_dangerous_tool_request`), corriendo contra
Gemini real en Vertex AI en vez de Ollama. A diferencia de la
Parte B, acá NO se resuelve el modelo por `engine:` en la YAML — el `engine`
de nemoguardrails solo conoce providers de `langchain_community` (no
partner packages como `langchain-google-vertexai`), así que `build_rails_gemini()`
instancia `ChatVertexAI` directamente y lo pasa como `llm=` a `build_rails()`
(mismo mecanismo que ya usa `FakeListLLM` en la Parte A).

Requiere ADC, un proyecto de GCP con Vertex AI habilitado, y el paquete
extra `langchain-google-vertexai` (no viene con `nemoguardrails`):

```
gcloud auth application-default login
uv pip install --python venv_nemo/bin/python3 langchain-google-vertexai
export GCP_PROJECT_ID=<tu-proyecto-gcp>
export GCP_LOCATION=us-central1
./venv_nemo/bin/python3 nemo_guardrails_lab34.py --parte-c
```

Con este backend, tanto el mensaje peligroso (bloqueado por el rail antes de
llegar al modelo, igual que en la Parte A/B) como un mensaje benigno
(round-trip real de generación) deberían funcionar. Nota:
`langchain-google-vertexai` emite un `DeprecationWarning` sugiriendo migrar
a `langchain-google-genai` (`ChatGoogleGenerativeAI`) — es solo un warning,
no afecta el resultado.
