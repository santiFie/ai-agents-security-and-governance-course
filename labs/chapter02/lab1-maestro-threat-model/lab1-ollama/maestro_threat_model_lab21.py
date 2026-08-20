#!/usr/bin/env python3
"""
Lab Propuesto 2.1: MAESTRO Automatico -- Generador de Threat Models con ADK,
version con modelo local (qwen3.5:9b via Ollama).

Adaptado del "Codigo esqueleto" de ch02-labs.md (lineas 36-164). Es el lab
mas dificil del capitulo: combina `output_schema=MaestroThreatModel`
(Pydantic) CON `tools`, en la generacion mas larga de todo el set (7 capas +
amenazas por capa + amenazas cross-layer + top-5 riesgos), y el libro
parsea el resultado con `MaestroThreatModel.model_validate_json(result_text)`
SIN try/except -- cualquier drift de schema con un modelo 9B local en CPU
(JSON mal formado, texto antes/despues del JSON, un campo faltante) tira
abajo el lab entero con una excepcion no controlada.

===========================================================================
HALLAZGO PRINCIPAL: output_schema + tools con LiteLlm/Ollama
===========================================================================

(a) Test de instanciacion (SIN invocar Runner, no toca Ollama):
    `Agent(output_schema=MaestroThreatModel, tools=[...], model=LiteLlm(...))`
    se instancia sin excepcion en la version instalada de ADK (1.31.1). No
    hay ningun validador que rechace la combinacion output_schema+tools a
    nivel de construccion del objeto -- la restriccion clasica de ADK
    ("If output_schema is set, tools must be empty") ya no existe en esta
    version: fue reemplazada por un mecanismo de compatibilidad mas nuevo.

    Ver `google.adk.flows.llm_flows.basic._build_basic_request` (linea ~53):

        # Only set output_schema if no tools are specified. as of now, model
        # don't support output_schema and tools together. we have a
        # workaround to support both output_schema and tools at the same
        # time. see _output_schema_processor.py for details
        if agent.output_schema:
            if not agent.tools or can_use_output_schema_with_tools(model):
                llm_request.set_output_schema(agent.output_schema)

    Y `google.adk.utils.output_schema_utils.can_use_output_schema_with_tools`:
    para CUALQUIER instancia de `LiteLlm` (sin importar el modelo/proveedor
    underlying) devuelve `True` incondicionalmente, con este comentario:

        # LiteLLM handles tools + response_format compatibility per-provider
        # ...This is strictly more reliable than the SetModelResponseTool
        # prompt-based workaround.

    O sea: para Gemini nativo sin soporte del combo, ADK inyecta un tool
    extra `set_model_response` (ver `_output_schema_processor.py`) que el
    modelo debe llamar en vez de responder texto plano -- un workaround
    "prompt-based". Para CUALQUIER LiteLlm (incluido Ollama), ADK se salta
    ese workaround por completo y delega 100% en litellm/el proveedor.

(b) Lo que hace litellm con response_format+tools para el proveedor
    `ollama_chat` (litellm 1.83.x, `llms/ollama/chat/transformation.py`,
    `map_openai_params`): mapea `response_format` (json_schema) al campo
    NATIVO de Ollama `format` (decoding con grammar/schema-constrained
    output de Ollama) y lo manda en el mismo request JUNTO con `tools`.
    Es real -- Ollama soporta `format` (json schema) + `tools` en el mismo
    POST /api/chat.

(c) EL RIESGO NO VERIFICADO (no invocamos Ollama en esta migracion, asi que
    esto es analisis de codigo, no evidencia empirica): `_build_basic_request`
    es un request processor que corre en CADA turno del flow, no solo en el
    turno final. Con `output_schema` + `tools` seteados, `set_output_schema`
    se llama en TODOS los turnos -- incluidos los turnos intermedios donde
    el modelo deberia estar emitiendo una tool_call (`analyze_layer`,
    `identify_cross_layer_threats`), no una respuesta final schema-conforme.
    Eso significa que Ollama recibe, en el mismo request, `format=<schema
    completo de MaestroThreatModel>` Y `tools=[analyze_layer, ...]`
    simultaneamente en un turno donde se espera que el modelo llame una
    tool. No esta documentado (a nivel de Ollama ni de litellm) que priorice
    tool-calling sobre grammar-constrained decoding en ese caso -- con un
    modelo 9B en CPU, forzar dos restricciones estructurales competidoras al
    mismo tiempo es exactamente el tipo de escenario donde un LLM local
    chico se rompe (tool call nunca emitida, o JSON prematuro/incompleto
    que no cumple el schema porque el modelo todavia no completo el
    razonamiento por capas).

CONCLUSION Y DISENO DE ESTE ARCHIVO: la combinacion nativa NO esta prohibida
por ADK (se instancia y probablemente hasta funcione), pero es
arquitectonicamente fragil para este lab en particular (la generacion mas
larga del capitulo, con 9B en CPU). Por eso este archivo:
  1. Mantiene `maestro_agent` (nativo, output_schema+tools) definido tal
     cual el libro -- PERO `build_threat_model()` ya NO lo intenta primero
     cuando el modelo es `LiteLlm` (ver "FIX APLICADO" mas abajo). Se
     conserva el agente y el codigo del intento nativo (no se borra) porque
     documenta el hallazgo y porque, si algun dia se reutiliza esta funcion
     con un agente Gemini nativo (no-LiteLlm), el intento nativo se sigue
     ejecutando normalmente.
  2. Envuelve el parseo en try/except con extraccion robusta de JSON
     (bloques ```json, texto antes/despues del JSON) en vez del
     `model_validate_json` desnudo del libro.
  3. Si se intenta el nativo (solo si el modelo NO es `LiteLlm`) y falla el
     parseo, cae automaticamente a `maestro_agent_manual`: la ALTERNATIVA
     pedida por la consigna -- ningun `output_schema` nativo, el schema
     esperado se describe como texto plano en el `instruction` (una sola
     fuente de verdad para evitar que el texto se desalinee del modelo
     Pydantic: se genera el bloque de ejemplo a mano pero los campos
     coinciden 1:1 con `MaestroThreatModel`), y el parseo es manual (mismo
     extractor robusto).
  4. Si TAMBIEN falla el fallback manual, el lab NO revienta con una
     excepcion no controlada: devuelve un resultado explicito de fallo con
     diagnostico impreso, y el driver lo reporta como tal.

FIX APLICADO (2026-08-02, tras evidencia empirica de 3 corridas reales
documentadas -- ver el `.md` de este lab y `run_live_financial_trading_agent.log`):
el riesgo (c) de arriba dejo de ser solo analisis de codigo. En la unica
corrida real completa registrada contra este modelo, el intento nativo
alucino un JSON valido sin llamar NINGUNA tool (`_LAYERS_ANALYZED` vacio) --
y ademas gasta varios minutos reales por sistema sin aportar nada, porque
`can_use_output_schema_with_tools(LiteLlm(...))` siempre da `True` (ver
--selftest), asi que el riesgo (d) esta activo en CADA corrida contra
Ollama, no es una posibilidad ocasional. Por eso `build_threat_model()`
ahora saltea el intento nativo directamente cuando `maestro_agent` usa
`LiteLlm` -- yendo derecho al fallback manual, que en la misma corrida real
SI paso por las 7 capas via tool (aunque haya terminado en FALLO CONTROLADO
por un problema de formato distinto, no relacionado con este riesgo). Esto
ahorra ~6 min por sistema sin perder ninguna corrida que hubiera funcionado
de otro modo -- no hay evidencia registrada de que el intento nativo haya
producido alguna vez un resultado confiable con este modelo.

Requiere: google-adk, pydantic, y para los drivers con LLM, Ollama corriendo
con qwen3.5:9b ya descargado (ollama pull qwen3.5:9b).

Modo de verificacion sin LLM (instanciacion de ambos agentes + logica de
extraccion/parseo robusta, con textos canned que simulan salidas buenas y
malas de un modelo 9B -- sin tocar Ollama):
    python3 maestro_threat_model_lab21.py --selftest
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from typing import Dict, List, Optional, Tuple

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
import google.genai.types as types
from pydantic import BaseModel, ValidationError


# ── Estructura del Threat Model (identica al libro) ─────────────────────
class ThreatItem(BaseModel):
    layer: int
    threat_name: str
    description: str
    agentic_factor: str  # non-determinism | autonomy | identity | a2a-comm
    likelihood: str      # low | medium | high
    impact: str           # low | medium | high
    mitigation: str


class LayerAnalysis(BaseModel):
    """Analisis de una capa MAESTRO individual (clave = numero de capa como string)."""
    technology: str
    threats: List[str]
    severity: str


class CrossLayerThreat(BaseModel):
    """Amenaza que encadena dos o mas capas MAESTRO."""
    layers_involved: List[int]
    threat_name: str
    description: str
    severity: str


class MaestroThreatModel(BaseModel):
    system_name: str
    layer_decomposition: Dict[str, LayerAnalysis]  # "1".."7" -> analisis de la capa
    layer_threats: List[ThreatItem]
    cross_layer_threats: List[CrossLayerThreat]
    top_risks: List[ThreatItem]  # top 5 por likelihood * impact


# ── Herramientas (identicas al libro, + evidencia de efecto) ────────────
MAESTRO_LAYERS = {
    1: "Foundation Models", 2: "Data Operations", 3: "Agent Frameworks",
    4: "Deployment Infrastructure", 5: "Evaluation & Observability",
    6: "Security & Compliance", 7: "Agent Ecosystem",
}


# Evidencia de arity, no solo de que la tool se llamo: --selftest no invoca
# al LLM, asi que esto solo se llena en corridas reales contra Ollama -pero
# es la senal mas util para diagnosticar el riesgo (d) documentado en el
# docstring del modulo (output_schema+tools compitiendo en el mismo turno):
# si el modelo corto camino al JSON final sin pasar por las 7 capas, este
# set queda incompleto y print_threat_model_report() lo hace visible.
_LAYERS_ANALYZED: set = set()

# HALLAZGO REAL (corrida en vivo, 2026-07-28): decirle al modelo por prompt
# "no repitas la misma llamada" NO alcanzo -- el agente fallback (manual,
# sin output_schema) quedo en loop infinito repitiendo identify_cross_layer_
# threats con los MISMOS 2 pares de capas una y otra vez, sin converger
# nunca al JSON final (mato el proceso el timeout de 1500s, no el modelo).
# Fix real: la tool misma detecta la repeticion y responde con un mensaje
# que empuja a seguir -en vez de confiar en que el prompt alcance-, mismo
# principio que el resto del curso: no delegarle al LLM una garantia que el
# codigo puede hacer cumplir solo.
_CROSS_LAYER_SEEN: set = set()


def analyze_layer(layer_num: int, system_context: str) -> dict:
    """Resuelve el nombre canonico de una capa MAESTRO (1-7) y devuelve el contexto
    del sistema para que el modelo razone sobre amenazas especificas de esa capa."""
    layer_name = MAESTRO_LAYERS.get(layer_num, "capa desconocida")
    result = {"layer": layer_num, "layer_name": layer_name, "system_context": system_context}
    _LAYERS_ANALYZED.add(layer_num)
    print(f"[tool call ejecutado] analyze_layer({layer_num}) -> layer_name={layer_name!r}")
    return result


def identify_cross_layer_threats(layers_involved: List[int]) -> dict:
    """Marca una combinacion de capas (ej. [1, 3, 6]) como candidata a amenaza
    cross-layer, devolviendo sus nombres para que el modelo describa el encadenamiento.

    Deduplica por codigo (ver nota arriba de _CROSS_LAYER_SEEN): si la misma
    combinacion ya se reporto antes, en vez de repetir el resultado devuelve
    una instruccion explicita de avanzar, para cortar loops donde el modelo
    reinvoca la misma tool indefinidamente en vez de progresar al JSON final.
    """
    key = tuple(sorted(set(layers_involved)))
    names = [MAESTRO_LAYERS.get(l, str(l)) for l in layers_involved]
    if key in _CROSS_LAYER_SEEN:
        print(f"[tool call ejecutado] identify_cross_layer_threats({layers_involved}) -> "
              f"DUPLICADO, ya reportado -- devolviendo nudge para avanzar")
        return {
            "layers_involved": layers_involved,
            "layer_names": names,
            "already_reported": True,
            "instruction": (
                "Esta combinacion de capas YA fue identificada antes en esta sesion. "
                "No la vuelvas a llamar. Si ya cubriste las combinaciones cross-layer "
                "relevantes, DEJA de llamar tools y escribi el JSON final ahora."
            ),
        }
    _CROSS_LAYER_SEEN.add(key)
    result = {"layers_involved": layers_involved, "layer_names": names}
    print(f"[tool call ejecutado] identify_cross_layer_threats({layers_involved}) -> {names}")
    return result


# ── Plantilla textual del schema esperado (fuente unica: campos 1:1 con ──
# MaestroThreatModel; se usa en la instruccion del agente fallback manual, y
# tambien como referencia extra en el agente nativo -- barata de agregar,
# puede ayudar si el grammar-constrained decoding de Ollama no aplica del
# todo en un turno con tools activas). Se escribe a mano (no
# MaestroThreatModel.model_json_schema() crudo) porque el JSON Schema de
# Pydantic trae $defs/$ref/additionalProperties -- ruido que un modelo 9B
# local interpreta peor que un ejemplo concreto.
MAESTRO_JSON_TEMPLATE = """{
  "system_name": "<string>",
  "layer_decomposition": {
    "1": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "2": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "3": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "4": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "5": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "6": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"},
    "7": {"technology": "<string>", "threats": ["<string>", "..."], "severity": "<low|medium|high>"}
  },
  "layer_threats": [
    {"layer": 1, "threat_name": "<string>", "description": "<string>",
     "agentic_factor": "<non-determinism|autonomy|identity|a2a-comm>",
     "likelihood": "<low|medium|high>", "impact": "<low|medium|high>", "mitigation": "<string>"}
  ],
  "cross_layer_threats": [
    {"layers_involved": [1, 3, 6], "threat_name": "<string>", "description": "<string>", "severity": "<low|medium|high>"}
  ],
  "top_risks": [
    {"layer": 1, "threat_name": "<string>", "description": "<string>",
     "agentic_factor": "<non-determinism|autonomy|identity|a2a-comm>",
     "likelihood": "<low|medium|high>", "impact": "<low|medium|high>", "mitigation": "<string>"}
  ]
}"""

PROCESS_INSTRUCTION = """Eres un experto en threat modeling de sistemas agenticos usando el framework MAESTRO.

REGLA OBLIGATORIA, ANTES QUE NADA: para CUALQUIER pedido de threat modeling,
tenes que llamar a analyze_layer UNA VEZ POR CADA UNA de las 7 capas (1 a 7)
ANTES de escribir tu respuesta final, y despues llamar a
identify_cross_layer_threats al menos una vez con las capas que encadenen
una amenaza cross-layer real. Nunca faltan estas llamadas: no importa que
tan simple parezca el sistema descripto, siempre analizas las 7 capas.
Generar un threat model sin haber llamado analyze_layer para las 7 capas es
un error grave -- equivale a inventar un analisis que nunca se hizo.

PROCESO A SEGUIR (6 pasos del modulo 2):
1. DESCOMPONER el sistema en las 7 capas MAESTRO, identificando tecnologias especificas
2. IDENTIFICAR amenazas por capa considerando los 4 factores agenticos:
   - Non-determinism: comportamiento emergente del LLM
   - Autonomy: acciones autonomas sin supervision humana
   - Identity Management: gestion de identidades de agentes
   - Agent-to-Agent Communication: comunicacion entre agentes
3. IDENTIFICAR amenazas cross-layer usando identify_cross_layer_threats -- UNA
   SOLA VEZ por cada par de capas distinto que encadene una amenaza real, no
   repitas la misma llamada con las mismas capas mas de una vez
4. EVALUAR riesgo (likelihood x impact) para cada amenaza
5. PLANIFICAR mitigaciones (preventivas + detectivas + responsivas)
6. RECOMENDAR plan de monitoreo continuo

Usa analyze_layer para consultar el nombre canonico de cada capa antes de describir
sus amenazas."""

NATIVE_INSTRUCTION = (
    PROCESS_INSTRUCTION
    + "\n\nTu respuesta final debe ajustarse EXACTAMENTE al esquema "
    "MaestroThreatModel, con esta forma (referencia, no la copies literal, "
    "completa los valores reales del analisis):\n\n"
    + MAESTRO_JSON_TEMPLATE
)

MANUAL_INSTRUCTION = (
    PROCESS_INSTRUCTION
    + "\n\nDespues de llamar todas las tools necesarias, tu respuesta final "
    "debe ser SOLO un objeto JSON valido (sin bloques ```json, sin texto "
    "antes ni despues, sin comentarios) que cumpla EXACTAMENTE esta forma "
    "(referencia de la estructura -- completa los valores reales de tu "
    "analisis, no copies los placeholders <...> literalmente):\n\n"
    + MAESTRO_JSON_TEMPLATE
    + "\n\nSi no emitis JSON valido y completo, el lab no puede procesar tu "
    "respuesta -- es un error grave."
)


# Config de modelo verificada (Lab 8.1/8.2/11.A/5.A): num_ctx mas alto que
# el default de esos labs (16384 en vez de 8192) porque este es el lab mas
# largo del set -- instruccion + 2 tool schemas + plantilla JSON completa +
# resultados de hasta 8 tool calls (7x analyze_layer + identify_cross_layer)
# + el threat model final (7 capas + N amenazas + cross-layer + top-5) no
# entran comodos en 8192 sin arriesgar el mismo corte a mitad de respuesta
# que motivo num_ctx=8192 en los labs mas chicos. temperature=0.2 +
# reasoning_effort="none": misma razon que el resto (qwen3.5 es un modelo
# "thinking" que sin esto se queda narrando y nunca cierra el JSON final).
def _make_model() -> LiteLlm:
    return LiteLlm(
        model="ollama_chat/qwen3.5:9b",
        num_ctx=16384,
        temperature=0.2,
        reasoning_effort="none",
    )


maestro_agent = Agent(
    name="maestro_threat_modeler",
    model=_make_model(),
    description="Aplica el framework MAESTRO para generar threat models de sistemas agenticos.",
    instruction=NATIVE_INSTRUCTION,
    tools=[analyze_layer, identify_cross_layer_threats],
    output_schema=MaestroThreatModel,
)

# Agente ALTERNATIVO (sin output_schema nativo): mismo rol y mismas tools,
# pero el schema esperado se describe en texto dentro de la instruccion y el
# parseo es 100% manual del lado de Python. Es el fallback automatico si
# `maestro_agent` no produce un JSON parseable -- ver build_threat_model().
maestro_agent_manual = Agent(
    name="maestro_threat_modeler_manual",
    model=_make_model(),
    description="Version fallback de maestro_threat_modeler sin output_schema nativo.",
    instruction=MANUAL_INSTRUCTION,
    tools=[analyze_layer, identify_cross_layer_threats],
)

# FIX APLICADO (ver "FIX APLICADO" en el docstring del modulo): con LiteLlm
# (Ollama), `can_use_output_schema_with_tools` siempre da True, asi que el
# intento nativo (output_schema+tools) SIEMPRE corre en riesgo de alucinar
# sin llamar ninguna tool -- confirmado empiricamente, no es hipotetico. Se
# calcula una sola vez aca (no en cada llamada a build_threat_model) porque
# el tipo de modelo del agente no cambia en tiempo de ejecucion.
_SKIP_NATIVE_ATTEMPT = isinstance(maestro_agent.canonical_model, LiteLlm)


# ── Driver ADK (Runner canonico) ─────────────────────────────────────────
def ask_agent(agent: Agent, prompt: str, app_name: str) -> str:
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(
        app_name=app_name, user_id="student", session_id=str(uuid.uuid4())
    )
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    result_text = ""
    for event in runner.run(user_id="student", session_id=session.id, new_message=content):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    result_text = part.text
    return result_text


# ── Parseo robusto (reemplaza el model_validate_json desnudo del libro) ──
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json_candidate(text: str) -> Optional[str]:
    """Intenta aislar el objeto JSON de una respuesta de modelo que puede
    traer texto extra alrededor (bloque ```json ... ```, o prosa antes/
    despues del JSON). No usa json.loads todavia -- solo recorta el string
    candidato mas prometedor para que el llamador lo valide."""
    text = text.strip()
    if not text:
        return None

    # 1) Bloque de codigo ```json ... ``` o ``` ... ```
    fence_match = _CODE_FENCE_RE.search(text)
    if fence_match:
        candidate = fence_match.group(1).strip()
        if candidate.startswith("{"):
            return candidate

    # 2) Substring balanceado desde el primer '{' hasta su '}' de cierre
    # (cuenta llaves respetando strings entre comillas, para no cortar en
    # una '}' que aparezca dentro de un valor de texto).
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None  # nunca cerro -- JSON truncado (respuesta cortada a mitad)


def try_parse_threat_model(result_text: str) -> Tuple[Optional[MaestroThreatModel], str]:
    """Intenta parsear result_text como MaestroThreatModel con 2 estrategias
    antes de rendirse. Nunca deja escapar ValidationError/JSONDecodeError --
    devuelve (None, motivo) en vez de reventar el proceso del llamador."""
    if not result_text or not result_text.strip():
        return None, "respuesta vacia del modelo"

    # Estrategia 1: el texto ya es JSON valido de punta a punta (caso feliz,
    # el que asume el codigo del libro sin red de seguridad).
    try:
        return MaestroThreatModel.model_validate_json(result_text.strip()), "parseo directo"
    except (ValidationError, json.JSONDecodeError):
        pass

    # Estrategia 2: extraer el JSON de en medio de fences/prosa y reintentar.
    candidate = _extract_json_candidate(result_text)
    if candidate:
        try:
            return MaestroThreatModel.model_validate_json(candidate), "parseo tras extraccion"
        except (ValidationError, json.JSONDecodeError) as e:
            return None, f"JSON extraido pero invalido para el schema: {e}"

    return None, "no se encontro un objeto JSON balanceado en la respuesta"


# ── Driver de alto nivel: nativo -> fallback manual -> fallo controlado ──
def build_threat_model(
    system_description: str,
    ask_fn=ask_agent,
) -> Tuple[Optional[MaestroThreatModel], str, str]:
    """Devuelve (modelo_o_none, modo_usado, texto_crudo_del_ultimo_intento).

    modo_usado in {"native", "manual_fallback", "failed"}. Nunca propaga una
    excepcion de parseo -- a diferencia del codigo del libro
    (MaestroThreatModel.model_validate_json(result_text) sin try/except),
    que tira abajo el proceso entero ante cualquier drift de schema.

    `ask_fn` es inyectable (default: `ask_agent`, que SI invoca Runner/Ollama
    de verdad) precisamente para que --selftest pueda probar esta funcion
    con un doble de prueba que nunca toca la red -- ver _self_test(). Un
    monkeypatch por reimportacion de modulo (`import este_mismo_archivo`)
    NO sirve para interceptar esto: crea un objeto de modulo nuevo con su
    propio namespace global, separado del que ve esta funcion, y el patch
    queda sin efecto -- exactamente el bug que causo una invocacion real a
    Ollama sin querer durante el desarrollo de este lab. La inyeccion por
    parametro es la forma correcta de evitarlo.
    """
    prompt = f"Realizar threat modeling MAESTRO completo para: {system_description}"

    native_text = ""
    if _SKIP_NATIVE_ATTEMPT:
        # FIX APLICADO (ver docstring del modulo, "FIX APLICADO"): con
        # LiteLlm/Ollama, el intento nativo (output_schema+tools) esta
        # confirmado empiricamente como propenso a alucinar sin llamar
        # ninguna tool, y ademas gasta minutos reales por sistema. Se
        # saltea directo al fallback manual, que SI tiene evidencia real de
        # pasar por las 7 capas via tool.
        print("[build_threat_model] agente usa LiteLlm/Ollama -- saltando intento nativo "
              "(output_schema+tools): evidencia empirica confirma que esta combinacion "
              "alucina sin llamar tools con este modelo (ver docstring del modulo). "
              "Yendo directo al fallback manual.")
        native_text = "(intento nativo salteado a proposito -- ver 'FIX APLICADO' en el docstring del modulo)"
    else:
        print("[build_threat_model] intento 1/2: agente nativo (output_schema+tools)...")
        _LAYERS_ANALYZED.clear()
        _CROSS_LAYER_SEEN.clear()
        native_text = ask_fn(maestro_agent, prompt, "maestro-native")
        model, reason = try_parse_threat_model(native_text)
        # HALLAZGO REAL (corrida en vivo, 2026-07-28): con output_schema nativo,
        # qwen3.5:9b devolvio un JSON perfectamente valido y con apariencia
        # razonable SIN llamar a analyze_layer ni una sola vez -- _LAYERS_ANALYZED
        # quedo vacio. El threat model era 100% alucinado (aunque coherente), y
        # como el parseo tuvo exito, la logica anterior (solo `model is not
        # None`) lo daba por bueno y NUNCA intentaba el fallback -- exactamente
        # el patron "evidencia de apariencia, no de efecto" que el resto del
        # curso ensena a evitar, colado en el propio codigo del lab. Fix:
        # tratar el intento nativo como fallido tambien si no se analizaron las
        # 7 capas via tool, sin importar que el JSON haya parseado bien.
        layers_ok = _LAYERS_ANALYZED == set(range(1, 8))
        if model is not None and layers_ok:
            print(f"[build_threat_model] OK con agente nativo ({reason}). "
                  f"Capas analizadas via tool: {sorted(_LAYERS_ANALYZED)} (esperado: 1-7 completo).")
            return model, "native", native_text

        if model is not None and not layers_ok:
            print(f"[build_threat_model] agente nativo produjo JSON valido PERO sin evidencia de "
                  f"analisis real -- capas analizadas via tool: {sorted(_LAYERS_ANALYZED)} "
                  f"(esperado: 1-7 completo). Tratando como fallo (threat model alucinado, no confiable).")
        else:
            print(f"[build_threat_model] agente nativo NO produjo un threat model valido: {reason}")
            print(f"[build_threat_model] Capas analizadas via tool antes de fallar: {sorted(_LAYERS_ANALYZED)}")

    print("[build_threat_model] intento 2/2: agente fallback (schema en texto, parseo manual)...")
    _LAYERS_ANALYZED.clear()
    _CROSS_LAYER_SEEN.clear()
    manual_text = ask_fn(maestro_agent_manual, prompt, "maestro-manual")
    model, reason = try_parse_threat_model(manual_text)
    layers_ok = _LAYERS_ANALYZED == set(range(1, 8))
    if model is not None and layers_ok:
        print(f"[build_threat_model] OK con agente fallback ({reason}). "
              f"Capas analizadas via tool: {sorted(_LAYERS_ANALYZED)} (esperado: 1-7 completo).")
        return model, "manual_fallback", manual_text
    if model is not None and not layers_ok:
        # No queda otro intento -- devolvemos el resultado pero marcado como
        # no verificado, en vez de esconder que el analisis puede estar
        # incompleto o alucinado (mismo principio que arriba: JSON valido no
        # es lo mismo que analisis real).
        print(f"[build_threat_model] agente fallback produjo JSON valido PERO con evidencia "
              f"incompleta -- capas analizadas via tool: {sorted(_LAYERS_ANALYZED)} "
              f"(esperado: 1-7 completo). Devolviendo igual (ultimo intento disponible), "
              f"marcado como no verificado.")
        return model, "manual_fallback_unverified", manual_text

    print(f"[build_threat_model] agente fallback TAMBIEN fallo: {reason}")
    print(f"[build_threat_model] Capas analizadas via tool antes de fallar: {sorted(_LAYERS_ANALYZED)}")
    print("[build_threat_model] FALLO CONTROLADO -- se devuelve diagnostico en vez de excepcion.")
    return None, "failed", f"--- nativo ---\n{native_text}\n\n--- fallback ---\n{manual_text}"


def print_threat_model_report(threat_model: MaestroThreatModel) -> None:
    print(f"Threat model para: {threat_model.system_name}")
    print(f"Amenazas por capa identificadas: {len(threat_model.layer_threats)}")
    print("\nTop riesgos:")
    for t in threat_model.top_risks:
        print(f"  [L{t.layer}] {t.threat_name} -- likelihood={t.likelihood}, impact={t.impact}")
        print(f"    Mitigacion: {t.mitigation}")
    print("\nAmenazas cross-layer:")
    for cl in threat_model.cross_layer_threats:
        print(f"  [capas {cl.layers_involved}] {cl.threat_name} (severity={cl.severity}): {cl.description}")


# ── Sistemas de prueba (los 3 que pide el objetivo del lab) ──────────────
TEST_SYSTEMS = {
    "code_analysis_agent": """
Sistema: Agente de analisis de codigo fuente para seguridad
- Modelo: qwen3.5:9b local via Ollama
- Framework: Google ADK v2
- Herramientas: git_clone(), run_sast_scan(), search_nvd_cve(),
                write_jira_ticket(), notify_slack()
- Datos: accede a repos privados de GitHub Enterprise
- Vector store: Pinecone con documentacion de patrones de vulnerabilidades
- Infraestructura: GKE (Google Kubernetes Engine), us-central1
- Multi-agent: orquestador + 3 subagentes especializados (SAST, SCA, secrets)
- Usuarios: equipo de seguridad (20 personas), integracion CI/CD
""",
    "rag_customer_support": """
Sistema: Agente RAG de soporte al cliente
- Modelo: qwen3.5:9b local via Ollama
- Framework: Google ADK v2
- Herramientas: search_knowledge_base(), create_support_ticket(), escalate_to_human()
- Datos: base de conocimiento de politicas de la empresa (ChromaDB)
- Vector store: ChromaDB local, embeddings all-MiniLM-L6-v2
- Infraestructura: contenedor Docker en Cloud Run
- Multi-agent: agente unico
- Usuarios: publico general via chat web, sin autenticacion
""",
    "financial_trading_agent": """
Sistema: Agente de trading algoritmico
- Modelo: qwen3.5:9b local via Ollama
- Framework: Google ADK v2
- Herramientas: submit_order(), get_market_data(), check_risk_limits()
- Datos: feed de mercado en tiempo real, posiciones internas
- Infraestructura: on-prem, latencia critica
- Multi-agent: agente de estrategia + gateway de riesgo (Z3-verified)
- Usuarios: mesa de trading interna, acceso con credenciales de alto privilegio
""",
}


# ── --selftest: instanciacion + logica de parseo, SIN tocar Ollama ───────
def _self_test() -> None:
    # Guardrail de defensa en profundidad: --selftest NO debe llamar a
    # Runner.run bajo NINGUNA circunstancia (ni por un bug futuro en este
    # archivo). Se parchea Runner.run para que levante si algo lo invoca
    # durante el selftest -- ver el docstring de build_threat_model() para
    # el bug real que motivo este guardrail (un monkeypatch por
    # reimportacion de modulo que no interceptaba nada, y termino
    # disparando una generacion real contra Ollama sin querer).
    from google.adk.runners import Runner as _RunnerClass

    def _forbidden_run(*args, **kwargs):
        raise RuntimeError(
            "GUARDRAIL: Runner.run() fue invocado durante --selftest. "
            "--selftest NUNCA debe tocar Ollama -- esto es un bug, no una "
            "corrida real. Abortando antes de gastar CPU/tiempo real."
        )

    _original_runner_run = _RunnerClass.run
    _RunnerClass.run = _forbidden_run

    try:
        _self_test_body()
    finally:
        _RunnerClass.run = _original_runner_run


def _self_test_body() -> None:
    print("[selftest] --- Instanciacion de agentes (output_schema+tools con LiteLlm) ---")
    assert maestro_agent.output_schema is MaestroThreatModel
    assert len(maestro_agent.tools) == 2
    assert maestro_agent_manual.output_schema is None
    assert len(maestro_agent_manual.tools) == 2
    print("[selftest] OK: ambos agentes se instanciaron sin excepcion.")

    print("\n[selftest] --- _LAYERS_ANALYZED registra arity de analyze_layer (llamada directa) ---")
    _LAYERS_ANALYZED.clear()
    _CROSS_LAYER_SEEN.clear()
    for n in (1, 3, 7):
        analyze_layer(n, "contexto de prueba")
    assert _LAYERS_ANALYZED == {1, 3, 7}, _LAYERS_ANALYZED
    print(f"[selftest] OK: analyze_layer(1/3/7) registro {sorted(_LAYERS_ANALYZED)} en _LAYERS_ANALYZED "
          "(esta es la senal que build_threat_model imprime tras una corrida real contra Ollama para "
          "confirmar si el modelo paso por las 7 capas o corto camino al JSON final).")
    _LAYERS_ANALYZED.clear()
    _CROSS_LAYER_SEEN.clear()

    from google.adk.utils.output_schema_utils import can_use_output_schema_with_tools
    can_combine = can_use_output_schema_with_tools(maestro_agent.canonical_model)
    print(f"[selftest] can_use_output_schema_with_tools(LiteLlm ollama_chat) = {can_combine}")
    assert can_combine is True, (
        "Si esto cambia a False en una version futura de ADK, significa que "
        "ADK empezo a inyectar el workaround SetModelResponseTool para "
        "LiteLlm tambien -- lo cual seria una BUENA noticia (menos riesgo), "
        "pero cambia el analisis documentado en el docstring del modulo."
    )

    print("\n[selftest] --- Parseo robusto: caso feliz (JSON limpio) ---")
    clean_json = json.dumps({
        "system_name": "Sistema de prueba",
        "layer_decomposition": {
            str(i): {"technology": f"tech-{i}", "threats": ["t1"], "severity": "low"}
            for i in range(1, 8)
        },
        "layer_threats": [{
            "layer": 1, "threat_name": "Prompt injection", "description": "desc",
            "agentic_factor": "non-determinism", "likelihood": "high", "impact": "high",
            "mitigation": "input validation",
        }],
        "cross_layer_threats": [{
            "layers_involved": [1, 3], "threat_name": "chained attack",
            "description": "desc", "severity": "medium",
        }],
        "top_risks": [{
            "layer": 1, "threat_name": "Prompt injection", "description": "desc",
            "agentic_factor": "non-determinism", "likelihood": "high", "impact": "high",
            "mitigation": "input validation",
        }],
    })
    model, reason = try_parse_threat_model(clean_json)
    assert model is not None and model.system_name == "Sistema de prueba", reason
    print(f"[selftest] OK: JSON limpio parseado ({reason}).")

    print("\n[selftest] --- Parseo robusto: JSON envuelto en prosa + fences ---")
    wrapped = f"Aca esta el analisis que pediste:\n\n```json\n{clean_json}\n```\n\nEspero que sirva."
    model, reason = try_parse_threat_model(wrapped)
    assert model is not None and model.system_name == "Sistema de prueba", reason
    print(f"[selftest] OK: JSON envuelto en prosa/fences parseado tras extraccion ({reason}).")

    print("\n[selftest] --- Parseo robusto: JSON truncado (respuesta cortada a mitad) ---")
    truncated = clean_json[: len(clean_json) // 2]
    model, reason = try_parse_threat_model(truncated)
    assert model is None, "un JSON truncado NUNCA deberia parsear correctamente"
    print(f"[selftest] OK: fallo detectado sin excepcion, motivo reportado: {reason!r}")

    print("\n[selftest] --- Parseo robusto: respuesta vacia ---")
    model, reason = try_parse_threat_model("")
    assert model is None
    print(f"[selftest] OK: {reason!r}")

    print("\n[selftest] --- build_threat_model degrada sin excepcion cuando AMBOS intentos fallan ---")
    # Doble de prueba inyectado via el parametro ask_fn (NO via monkeypatch
    # de modulo reimportado -- esa tecnica NO intercepta la funcion real,
    # ver el docstring de build_threat_model) -- nunca toca la red/Ollama.
    calls = []

    def _fake_ask_fn(agent, prompt, app_name):
        calls.append(app_name)
        return "esto no es JSON en absoluto"

    model, mode, raw = build_threat_model("sistema de prueba", ask_fn=_fake_ask_fn)
    assert model is None and mode == "failed", (model, mode)
    assert "nativo" in raw and "fallback" in raw
    # Con el FIX APLICADO (ver mas abajo), el intento nativo se saltea
    # siempre para este agente (LiteLlm) -- "calls" ya no incluye
    # "maestro-native": solo se invoca el fallback manual, que es el unico
    # intento real que se hace.
    assert calls == ["maestro-manual"], calls
    print(f"[selftest] OK: build_threat_model devolvio modo={mode!r} sin excepcion, "
          f"probo el fallback manual ({calls}) usando el doble de prueba (0 llamadas reales a Ollama) "
          "-- el intento nativo ya no se prueba aca porque el fix lo saltea siempre para este agente.")

    print("\n[selftest] --- FIX APLICADO: build_threat_model saltea el intento nativo con LiteLlm/Ollama ---")
    # Historia de este test (contexto para quien lo lea despues): la version
    # anterior de este selftest probaba lo opuesto -- que build_threat_model
    # SI llegaba a modo "native" cuando el doble de prueba simulaba un
    # intento nativo exitoso. Eso dejo de ser el comportamiento correcto:
    # tras confirmar empiricamente (ver docstring del modulo, "FIX
    # APLICADO", y run_live_financial_trading_agent.log) que el intento
    # nativo alucina con este modelo casi siempre y ademas gasta minutos
    # reales sin evidencia de funcionar, se cambio build_threat_model para
    # que NUNCA llame al agente nativo cuando maestro_agent usa LiteLlm --
    # este test verifica esa garantia con un doble de prueba que, si el
    # intento nativo se llegara a invocar por error, lo delataria (dejando
    # "maestro-native" en la lista de llamadas).
    assert _SKIP_NATIVE_ATTEMPT is True, (
        "Este test asume que maestro_agent usa LiteLlm (Ollama) -- si esto "
        "cambia, _SKIP_NATIVE_ATTEMPT deberia dar False y el intento nativo "
        "volveria a intentarse primero, lo cual esta bien pero invalida "
        "este assert puntual."
    )
    calls3 = []

    def _fake_ask_fn_would_reveal_native_call(agent, prompt, app_name):
        calls3.append(app_name)
        _LAYERS_ANALYZED.update(range(1, 8))
        return clean_json

    model3, mode3, _ = build_threat_model("sistema de prueba", ask_fn=_fake_ask_fn_would_reveal_native_call)
    assert model3 is not None and mode3 == "manual_fallback", (model3, mode3)
    assert calls3 == ["maestro-manual"], calls3  # si "maestro-native" aparece aca, el fix se rompio
    print(f"[selftest] OK: modo={mode3!r}, solo se invoco {calls3} -- el intento nativo "
          "NUNCA se llamo (confirma que el fix sigue aplicado).")

    print("\n[selftest] OK: Lab 2.1 verificado de punta a punta sin invocar Ollama.")


if __name__ == "__main__":
    print("=" * 70)
    print("Lab 2.1 - MAESTRO Automatico (Threat Model Generator)")
    print("qwen3.5:9b local via Ollama")
    print("=" * 70)

    if "--selftest" in sys.argv:
        _self_test()
        sys.exit(0)

    for name, system_description in TEST_SYSTEMS.items():
        print(f"\n{'#' * 70}\n# Sistema: {name}\n{'#' * 70}")
        threat_model, mode, raw_text = build_threat_model(system_description)
        if threat_model is not None:
            print(f"\n[modo usado: {mode}]")
            print_threat_model_report(threat_model)
        else:
            print(f"\n[FALLO CONTROLADO -- modo: {mode}] No se pudo construir un "
                  f"threat model valido para '{name}'. Diagnostico:\n{raw_text[:1000]}")
