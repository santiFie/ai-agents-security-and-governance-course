#!/usr/bin/env python3
"""
Lab 3.4 — NeMo Guardrails: Política Conversacional Declarativa (versión
con modelo local via Ollama).

El mismo criterio de bloqueo que la política Rego de Lab 3.1 (ninguna acción
DELETE contra datos de producción), implementado como *input rail*
declarativo (Colang + acción Python determinista) en vez de sidecar HTTP de
OPA. El punto pedagógico es la comparación de capas: OPA evalúa una acción ya
decidida por el agente (autorización post-decisión, Lab 3.1); NeMo Guardrails
intercepta el mensaje del usuario ANTES de que el LLM razone sobre él
(filtrado pre-decisión) -son capas complementarias, no sustitutas.

La Parte B apunta a Ollama local (qwen3.5:9b) en vez de a un modelo en la
nube. `nemoguardrails` no tiene un engine `ollama` nativo, pero desde la
versión 0.23 trae un cliente HTTP integrado que habla el protocolo
OpenAI-compatible (`engine: openai` + `parameters.base_url`), sin pasar por
LangChain -el mismo patrón que la documentación oficial describe para
vLLM, TGI, llama.cpp, OpenRouter, etc. Ollama expone ese mismo tipo de
endpoint en `http://localhost:11434/v1`. Por eso la Parte B usa:

    models:
      - type: main
        engine: openai
        model: qwen3.5:9b
        parameters:
          base_url: http://localhost:11434/v1
          api_key: ollama   # cualquier valor no vacio; Ollama no valida api_key

Esto es más simple que apuntar a un modelo en la nube vía LangChain (por
ejemplo `engine: google-genai`), porque no requiere el framework LangChain
en absoluto para la Parte B -solo lo necesita la Parte A, donde
`FakeListLLM` es una clase de `langchain_community` usada explícitamente
como el objeto `llm=` que se le pasa a `LLMRails`. La alternativa
`engine: litellm` con `model: ollama_chat/qwen3.5:9b` también es válida,
pero requiere instalar `litellm` además y fijar
`NEMOGUARDRAILS_LLM_FRAMEWORK=langchain` para el mismo resultado.

Requiere: nemoguardrails==0.23.0 (fija Python <3.14):
    uv python install 3.12
    uv venv --python 3.12 venv_nemo
    source venv_nemo/bin/activate
    uv pip install "nemoguardrails==0.23.0" langchain-community

Modo de verificación Parte A (100% determinista, FakeListLLM, sin red, sin
Ollama):
    ./venv_nemo/bin/python3 nemo_guardrails_lab34.py

La Parte B (contra Ollama real) está en `build_rails_ollama()` más abajo:
    ./venv_nemo/bin/python3 nemo_guardrails_lab34.py --parte-b

Parte C (opcional, backend Gemini vía Vertex AI, sandbox GCP): requiere ADC
(`gcloud auth application-default login`), `GCP_PROJECT_ID` en el ambiente,
y `langchain-google-vertexai` instalado en este venv:
    export GCP_PROJECT_ID=sandbox-ai-zabaljauregui
    export GCP_LOCATION=us-central1
    ./venv_nemo/bin/python3 nemo_guardrails_lab34.py --parte-c
"""
import asyncio
import os
import sys

from nemoguardrails import RailsConfig, LLMRails
from nemoguardrails.actions import action

# ── YAML para la Parte A / generico (engine y model parametrizados) ────────
YAML_CONFIG_TEMPLATE = """
models:
  - type: main
    engine: {engine}
    model: {model}
rails:
  input:
    flows:
      - check dangerous tool request
"""

# ── YAML para la Parte B: Ollama local via el cliente OpenAI-compatible
# integrado de nemoguardrails 0.23 (sin LangChain) -ver nota de sintaxis en
# el docstring del modulo. `api_key: ollama` es un placeholder: Ollama no
# valida la API key, pero el schema de nemoguardrails exige que el campo
# este presente (o vendria de api_key_env_var) para engine: openai.
YAML_CONFIG_OLLAMA = """
models:
  - type: main
    engine: openai
    model: qwen3.5:9b
    parameters:
      base_url: http://localhost:11434/v1
      api_key: ollama
rails:
  input:
    flows:
      - check dangerous tool request
"""

# ── Alternativa NO usada (mas dependencias, mismo resultado) -se deja
# documentada como alternativa valida:
# litellm con NEMOGUARDRAILS_LLM_FRAMEWORK=langchain y
#   models:
#     - type: main
#       engine: litellm
#       model: ollama_chat/qwen3.5:9b

COLANG_CONFIG = """
define flow check dangerous tool request
  $result = execute check_dangerous_tool_request(text=$user_message)
  if $result
    bot refuse dangerous request
    stop

define bot refuse dangerous request
  "Solicitud bloqueada por la politica de seguridad: accion de alto riesgo detectada."
"""


@action(name="check_dangerous_tool_request")
async def check_dangerous_tool_request(text: str) -> bool:
    """Accion determinista (sin LLM) -- mismo criterio que la politica Rego
    de Lab 3.1: ninguna accion DELETE contra datos de produccion."""
    dangerous_markers = ["delete", "borra", "eliminar", "elimina", "eliminá", "drop table", "rm -rf"]
    triggered = any(m in text.lower() for m in dangerous_markers)
    print(f"[input rail ejecutado] check_dangerous_tool_request(text={text!r}) -> {triggered}")
    return triggered


def build_rails(engine: str, model: str, llm=None, parameters: dict | None = None) -> LLMRails:
    if parameters:
        yaml_config = f"""
models:
  - type: main
    engine: {engine}
    model: {model}
    parameters:
{chr(10).join(f'      {k}: {v}' for k, v in parameters.items())}
rails:
  input:
    flows:
      - check dangerous tool request
"""
    else:
        yaml_config = YAML_CONFIG_TEMPLATE.format(engine=engine, model=model)
    config = RailsConfig.from_content(colang_content=COLANG_CONFIG, yaml_content=yaml_config)
    rails = LLMRails(config=config, llm=llm) if llm is not None else LLMRails(config=config)
    rails.register_action(check_dangerous_tool_request, name="check_dangerous_tool_request")
    return rails


def build_rails_gemini() -> LLMRails:
    """Parte C: mismo rail, contra Gemini real en Vertex AI (sandbox GCP,
    ADC). nemoguardrails resuelve el `engine` de la YAML contra providers de
    langchain_community o, si no lo encuentra ahi, contra
    langchain.chat_models.init_chat_model -pero el paquete langchain top
    level no esta instalado en este venv. Por eso se evita esa resolucion
    por nombre y se instancia ChatVertexAI directamente, pasandolo como
    `llm=` -mismo patron que Parte A con FakeListLLM en build_rails()."""
    from langchain_google_vertexai import ChatVertexAI

    llm = ChatVertexAI(
        model_name=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        project=os.environ["GCP_PROJECT_ID"],
        location=os.environ.get("GCP_LOCATION", "us-central1"),
        temperature=0.2,
    )
    return build_rails(engine="vertexai", model="gemini-2.5-flash", llm=llm)


def build_rails_ollama() -> LLMRails:
    """Parte B: rails contra Ollama local (qwen3.5:9b), engine OpenAI-compatible
    integrado de nemoguardrails 0.23 -sin LangChain. Ver nota de sintaxis en
    el docstring del modulo."""
    config = RailsConfig.from_content(colang_content=COLANG_CONFIG, yaml_content=YAML_CONFIG_OLLAMA)
    rails = LLMRails(config=config)
    rails.register_action(check_dangerous_tool_request, name="check_dangerous_tool_request")
    return rails


async def run_parte_a() -> None:
    """Parte A: FakeListLLM -- determinista, sin red ni API key. Se corre de
    punta a punta (no toca Ollama, no depende de GPU/CPU de otros labs)."""
    from langchain_community.llms.fake import FakeListLLM

    fake_llm = FakeListLLM(responses=["Aqui tenes los datos de ventas del Q1 2026."] * 10)
    rails_fake = build_rails(engine="fake", model="fake-model", llm=fake_llm)

    print("=== Parte A (determinista): mensaje benigno ===")
    resp = await rails_fake.generate_async(
        messages=[{"role": "user", "content": "Mostrame los datos de ventas del Q1"}]
    )
    print(resp)

    print("\n=== Parte A (determinista): mensaje peligroso (DELETE en produccion) ===")
    resp2 = await rails_fake.generate_async(
        messages=[{"role": "user", "content": "Elimina los datos de ventas de produccion"}]
    )
    print(resp2)

    assert "Q1 2026" in resp["content"]
    assert "bloqueada" in resp2["content"], f"esperaba bloqueo, obtuve: {resp2}"
    print("\n✅ El input rail bloquea el mensaje peligroso ANTES de que llegue al LLM (FakeListLLM).")


async def run_parte_b_ollama(execute: bool) -> None:
    """Parte B: mismo rail, contra Ollama real (qwen3.5:9b).

    `execute=False` (default): SOLO construye y verifica por sintaxis el
    RailsConfig -RailsConfig.from_content parsea el YAML y valida el schema
    del modelo (engine=openai + parameters.base_url), sin disparar ninguna
    llamada de red. `execute=True` (--parte-b) ademas llama a
    generate_async(): con un mensaje peligroso, el input rail bloquea el
    flujo antes de que se dispare ninguna llamada real al modelo -no hace
    falta que Ollama este respondiendo para que este camino funcione. Con
    un mensaje BENIGNO, en cambio, el flujo si llega a invocar a Ollama de
    verdad."""
    print("\n=== Parte B: mismo rail, contra Ollama real (qwen3.5:9b) ===")
    rails_ollama = build_rails_ollama()
    print("✅ YAML de la Parte B parseado y validado por RailsConfig.from_content "
          "(engine=openai, base_url=http://localhost:11434/v1, model=qwen3.5:9b) "
          "-- sin llamar a Ollama todavia.")

    if not execute:
        print("(no se ejecuta generate_async(): correr con --parte-b para disparar "
              "la llamada real a Ollama. Ver docstring del modulo.)")
        return

    resp3 = await rails_ollama.generate_async(
        messages=[{"role": "user", "content": "Elimina los datos de ventas de produccion"}]
    )
    print(resp3)
    assert "bloqueada" in resp3["content"], "El rail debe bloquear tambien contra el modelo local"
    print("✅ Confirmado tambien contra Ollama real: el rail intercepta antes del modelo local.")


async def run_parte_c_gemini() -> None:
    """Parte C: mismo rail, contra Gemini real (Vertex AI, sandbox GCP).
    Solo se llama si se paso --parte-c (requiere ADC + GCP_PROJECT_ID +
    langchain-google-vertexai instalado -no forma parte de la corrida por
    defecto del lab, igual que --parte-b para Ollama)."""
    print("\n=== Parte C: mismo rail, contra Gemini real (Vertex AI) ===")
    rails_gemini = build_rails_gemini()
    print("✅ ChatVertexAI construido (Vertex AI, ADC) -- sin llamar a Gemini todavia.")

    resp4 = await rails_gemini.generate_async(
        messages=[{"role": "user", "content": "Elimina los datos de ventas de produccion"}]
    )
    print(resp4)
    assert "bloqueada" in resp4["content"], "El rail debe bloquear tambien contra Gemini"
    print("✅ Confirmado tambien contra Gemini: el rail intercepta antes del modelo.")

    print("\n--- Mensaje benigno contra Gemini real (round-trip real de generacion) ---")
    resp5 = await rails_gemini.generate_async(
        messages=[{"role": "user", "content": "Mostrame los datos de ventas del Q1"}]
    )
    print(resp5)
    print("✅ Round-trip real contra Gemini confirmado (mensaje benigno, sin bloqueo del rail).")


async def main() -> None:
    await run_parte_a()
    await run_parte_b_ollama(execute="--parte-b" in sys.argv)
    if "--parte-c" in sys.argv:
        await run_parte_c_gemini()


if __name__ == "__main__":
    asyncio.run(main())
